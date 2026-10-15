//! chrono-handoff desktop shell (Tauri 2).
//!
//! This process owns the **window**; it does not own the clock.  A Python
//! conductor holds the timing engine (sub-millisecond beats, sample-accurate
//! audio) and serves the panel page over HTTP.  The shell points a frameless,
//! transparent, always-on-top WebView2 window at that page.  The two talk
//! through exactly two channels:
//!
//! * **HTTP** -- the page's own `/api/*` calls, answered by the conductor.  All
//!   *data* (settings, health, calibration, cue control) travels this way.
//! * **the commands below** -- window chrome.  Minimising, closing, dragging and
//!   resizing act on the OS window, which the conductor does not own and no HTTP
//!   route can reach.
//!
//! The contract is `docs/shell-contract.md`; the page discovers this host
//! through `SHELL_BACKENDS` in `statics/panel.html`.  Keeping the shell this
//! thin is the point: swapping the conductor's language must not touch it, and
//! swapping this shell must not touch the conductor.
//!
//! Deliberately absent: `set_opacity` does nothing native (a transparent window
//! has no opacity to set -- the page owns `--win-alpha`), and `set_on_top`
//! persists nothing (persisting is data, so it goes over HTTP).

use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::webview::PageLoadEvent;
use tauri::{LogicalPosition, LogicalSize, PhysicalPosition, Position, Size, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

///: Panel port served by `chrono daemon` (see webui.DEFAULT_PANEL_PORT).
const DEFAULT_PANEL_PORT: u16 = 8790;
///: IPC port served by `chrono daemon` (see mcp_server.DEFAULT_IPC_PORT).
const DEFAULT_IPC_PORT: u16 = 8791;
const DEFAULT_PANEL_HOST: &str = "127.0.0.1";
///: The window must not appear before the page can answer, or the operator sees
///: a blank card and cannot tell it from a broken one.
const CONDUCTOR_WAIT_S: u64 = 20;

#[derive(Debug, Serialize)]
struct Ack {
    ok: bool,
}

fn ack() -> Ack {
    Ack { ok: true }
}

/// Trace every seam call to stdout.
///
/// The failure mode that matters is a seam that **says nothing and does
/// nothing**: the page still renders, so a broken host looks like a working
/// one until a cue is missed.  One line per call makes the difference between
/// "the page never reached Rust" and "it reached Rust and Rust refused"
/// visible without a debugger.
fn trace(cmd: &str) {
    println!("[shell] {cmd}");
    use std::io::Write;
    let _ = std::io::stdout().flush();
}

/// Baseline captured at `begin_drag` / `begin_resize`.
///
/// The page sends the **total** delta since pointerdown, never increments; the
/// baseline is what makes that contract work.  Increments were a real bug once:
/// each call overwrote the last, so a 120 px drag grew the window by 6 px.
#[derive(Default)]
struct Geometry {
    drag_origin: Mutex<Option<(f64, f64)>>,
    resize_base: Mutex<Option<ResizeBase>>,
}

struct ResizeBase {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    edge: String,
}

/// Logical (CSS-pixel) geometry: the page measures in CSS px and Tauri converts
/// to physical using the window's own scale factor, so no manual DPI maths is
/// needed here -- unlike the pywebview path, which had to call GetDpiForWindow.
fn logical_rect(window: &WebviewWindow) -> Result<(f64, f64, f64, f64), String> {
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let pos: PhysicalPosition<i32> = window.outer_position().map_err(|e| e.to_string())?;
    let size = window.outer_size().map_err(|e| e.to_string())?;
    let lp = pos.to_logical::<f64>(scale);
    let ls = size.to_logical::<f64>(scale);
    Ok((lp.x, lp.y, ls.width, ls.height))
}

fn min_size() -> (f64, f64) {
    // Mirrors the pywebview host's floor; below this the countdown stops being
    // readable at arm's length, which is where it has to be read.
    //
    // The pywebview host was passing a literal 380 here while its own constant
    // -- and this one, and the resize clamp -- said 360, so the two shells
    // disagreed about how short the window could get.  One number now.
    (320.0, 360.0)
}

/// Default window size in logical pixels, matching the Python `Settings`
/// defaults.  Duplicated rather than fetched because the window must be able to
/// open even when the conductor refuses to answer.
const DEFAULT_SIZE: (f64, f64) = (460.0, 620.0);

/// A monitor's work area, in logical pixels.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Monitor {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
}

/// Where the window was last time.  Any field may be absent: older settings
/// files predate positions, and a first run has nothing saved at all.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
struct SavedGeometry {
    x: Option<f64>,
    y: Option<f64>,
    w: Option<f64>,
    h: Option<f64>,
}

impl SavedGeometry {
    fn from_settings(v: &serde_json::Value) -> Self {
        let num = |k: &str| v.get(k).and_then(|x| x.as_f64()).filter(|f| f.is_finite());
        Self { x: num("x"), y: num("y"), w: num("width"), h: num("height") }
    }

    /// The size to open at, floored at the minimum and never zero.
    fn size(self) -> (f64, f64) {
        let (min_w, min_h) = min_size();
        (
            self.w.unwrap_or(DEFAULT_SIZE.0).max(min_w),
            self.h.unwrap_or(DEFAULT_SIZE.1).max(min_h),
        )
    }

    /// The origin to open at, or `None` to let the OS place the window.
    fn origin(self, monitors: &[Monitor], primary: Monitor, size: (f64, f64)) -> Option<(f64, f64)> {
        Some(visible_origin(self.x?, self.y?, monitors, primary, size))
    }
}

/// Pull a remembered origin back onto a monitor if it would open off-screen.
///
/// A remembered position is a liability: unplug the external monitor, change
/// the resolution, change the DPI scale, and the saved coordinates land outside
/// the desktop.  Then the widget starts *invisible* and there is nothing on
/// screen to drag back -- the user has to hand-edit JSON to recover it.  This
/// was measured on the pywebview side (a stale y=2047 on a 1067-tall desktop),
/// and the same file is what feeds this shell, so the same guard is needed.
///
/// The window is kept if its origin is within ``MARGIN`` of some monitor --
/// enough of it left to grab -- and centred on the primary otherwise.
fn visible_origin(
    x: f64,
    y: f64,
    monitors: &[Monitor],
    primary: Monitor,
    (w, h): (f64, f64),
) -> (f64, f64) {
    const MARGIN: f64 = 80.0;
    for m in monitors {
        if x > m.x - MARGIN && x < m.x + m.w - MARGIN
            && y > m.y - MARGIN && y < m.y + m.h - MARGIN
        {
            return (x, y);
        }
    }
    (
        primary.x + ((primary.w - w) / 2.0).max(0.0),
        primary.y + ((primary.h - h) / 2.0).max(0.0),
    )
}

/// Physical pixels -> logical, the unit settings.json stores.
///
/// Mixed units here would be a bug that only shows up on a scaled display:
/// save physical, restore logical, and the window grows by the DPI factor every
/// single time it is reopened.  This machine runs at 1.5x.
fn to_logical(physical: f64, scale: f64) -> f64 {
    if scale <= 0.0 { physical } else { physical / scale }
}

/// The last geometry the window had while it was in a *normal* state.
///
/// Only normal states are remembered, and that distinction is the whole point.
/// Minimise a window on Windows and the OS reports it at a sentinel position
/// (``-32000,-32000``; measured here as ``-21333,-21333``) with a shrunken size.
/// Persist that and the user's chosen position is gone -- and "minimise, then
/// close from the taskbar" is an ordinary thing to do, not an edge case.  The
/// first version of this saved exactly that.
///
/// Kept as a running value rather than read at close because by close time the
/// window may already be minimised, i.e. the honest number is the one from
/// before.
static LAST_GOOD: std::sync::Mutex<Option<SavedGeometry>> = std::sync::Mutex::new(None);

/// Whether the window is in a state whose geometry is worth remembering.
fn is_normal(window: &tauri::WebviewWindow) -> bool {
    !window.is_minimized().unwrap_or(false) && !window.is_maximized().unwrap_or(false)
}

/// Remember the current geometry if the window is in a normal state.
///
/// Called from the Moved/Resized handler.  A drag emits these continuously, so
/// this is cheap on purpose: it writes to memory, never to disk or HTTP.
fn remember_geometry(window: &tauri::WebviewWindow) {
    if !is_normal(window) {
        return;
    }
    if let Some(geo) = current_geometry(window)
        && let Ok(mut slot) = LAST_GOOD.lock()
    {
        *slot = Some(geo);
    }
}

/// The window's geometry in the unit settings.json uses.
fn current_geometry(window: &tauri::WebviewWindow) -> Option<SavedGeometry> {
    let scale = window.scale_factor().ok()?;
    let pos = window.outer_position().ok()?;
    let size = window.inner_size().ok()?;
    Some(SavedGeometry {
        x: Some(to_logical(f64::from(pos.x), scale).round()),
        y: Some(to_logical(f64::from(pos.y), scale).round()),
        w: Some(to_logical(f64::from(size.width), scale).round()),
        h: Some(to_logical(f64::from(size.height), scale).round()),
    })
}

/// The panel's settings object inside an `/api/settings` response.
///
/// The API is asymmetric, and that asymmetry cost a debugging session: **GET**
/// answers ``{"settings": {..}, "path": ..}`` while **POST** takes a flat patch.
/// This reader was written against the flat shape, found no ``width`` at the top
/// level, and quietly opened at the defaults -- no error, no warning, just a
/// window that forgot where it was.  So a missing ``settings`` object is
/// reported rather than defaulted through.
fn settings_object(v: &serde_json::Value) -> Option<&serde_json::Value> {
    match v.get("settings") {
        Some(s) if s.is_object() => Some(s),
        _ => {
            let keys = v.as_object().map(|o| o.keys().cloned().collect::<Vec<_>>());
            println!("[shell] WARNING: /api/settings has no 'settings' object (keys: {keys:?})");
            None
        }
    }
}

/// The origin/size the window should open at, read from the conductor.
///
/// Read over HTTP rather than from the file: the panel process is the single
/// writer of settings.json (see docs/shell-contract.md), and two writers of one
/// small JSON file is how a settings screen starts lying.
fn saved_geometry(api_base: &str) -> SavedGeometry {
    match http_get_json(&format!("{api_base}/api/settings")) {
        Ok(v) => settings_object(&v).map_or_else(SavedGeometry::default, SavedGeometry::from_settings),
        Err(e) => {
            // Not fatal: opening at the default size is perfectly usable.
            println!("[shell] WARNING: no saved geometry ({e}); using defaults");
            SavedGeometry::default()
        }
    }
}

/// An HTTP client with a deadline.  `ureq`'s default has no overall timeout, and
/// talking to a socket that accepts but never replies would hang the shell on
/// its own startup path.
fn http_agent(timeout: Duration) -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .build()
        .into()
}

fn http_get_json_within(url: &str, timeout: Duration) -> Result<serde_json::Value, String> {
    let mut res = http_agent(timeout)
        .get(url)
        .call()
        .map_err(|e| format!("GET {url}: {e}"))?;
    res.body_mut()
        .read_json::<serde_json::Value>()
        .map_err(|e| format!("GET {url}: bad JSON: {e}"))
}

fn http_get_json(url: &str) -> Result<serde_json::Value, String> {
    http_get_json_within(url, Duration::from_secs(5))
}

fn http_post_json(url: &str, body: &serde_json::Value) -> Result<(), String> {
    ureq::post(url)
        .send_json(body)
        .map_err(|e| format!("POST {url}: {e}"))?;
    Ok(())
}

/// Persist the window's geometry through the conductor, the way the page does.
///
/// Gated on ``remember_position``: the setting has to actually mean something,
/// and turning it off should stop the window from reappearing where it was.
/// That flag is re-read here rather than cached from startup, because a user who
/// just switched it off expects the *next* close to honour it.
fn save_geometry(window: &tauri::WebviewWindow, api_base: &str) {
    // Prefer the live rect, but only when it is a state worth keeping; a window
    // closed while minimised must not overwrite where the user actually put it.
    let geo = if is_normal(window) {
        current_geometry(window)
    } else {
        LAST_GOOD.lock().ok().and_then(|g| *g)
    };
    let Some(geo) = geo else {
        println!(
            "[shell] geometry not saved: the window was never in a normal state \
             (minimised/maximised its whole life)"
        );
        return;
    };
    let remember = match http_get_json(&format!("{api_base}/api/settings")) {
        Ok(v) => settings_object(&v)
            .and_then(|s| s.get("remember_position"))
            .and_then(|b| b.as_bool())
            .unwrap_or(true),
        Err(e) => {
            println!("[shell] geometry not saved: {e}");
            return;
        }
    };
    if !remember {
        println!("[shell] geometry not saved (remember_position is off)");
        return;
    }
    let patch = serde_json::json!({
        "x": geo.x, "y": geo.y, "width": geo.w, "height": geo.h,
    });
    match http_post_json(&format!("{api_base}/api/settings"), &patch) {
        Ok(()) => println!(
            "[shell] geometry saved: {}x{} at ({}, {})",
            geo.w.unwrap_or(0.0), geo.h.unwrap_or(0.0),
            geo.x.unwrap_or(0.0), geo.y.unwrap_or(0.0),
        ),
        Err(e) => println!("[shell] WARNING: geometry not saved: {e}"),
    }
}

/// Geometry of a resize under the page's absolute-delta contract.
///
/// Pure on purpose: this is the only arithmetic in the shell that can be wrong
/// without looking wrong, and driving it through a real window needs synthetic
/// mouse input -- which this machine drops often enough to make the result
/// meaningless.  Extracted so `cargo test` can decide, and so the `update_resize`
/// command is a thin wrapper with nothing left to get wrong.
///
/// ``dx``/``dy`` are the TOTAL pointer offset since pointerdown, not increments:
/// increments were a real bug once (each call overwrote the baseline, so a
/// 120 px drag grew the window by 6 px).
fn resized_rect(
    edge: &str,
    base: (f64, f64, f64, f64),
    dx: f64,
    dy: f64,
    (min_w, min_h): (f64, f64),
) -> (f64, f64, f64, f64) {
    let (x0, y0, w0, h0) = base;
    let (mut x, mut y, mut w, mut h) = (x0, y0, w0, h0);

    if edge.contains('e') {
        w = (w0 + dx).max(min_w);
    }
    if edge.contains('s') {
        h = (h0 + dy).max(min_h);
    }
    if edge.contains('w') {
        w = (w0 - dx).max(min_w);
        x = x0 + (w0 - w); // dragging the left edge moves the origin
    }
    if edge.contains('n') {
        h = (h0 - dy).max(min_h);
        y = y0 + (h0 - h);
    }
    (x, y, w, h)
}

// ---------------------------------------------------------------- window chrome

#[tauri::command]
fn minimize(window: WebviewWindow) -> Result<Ack, String> {
    trace("minimize");
    window.minimize().map_err(|e| e.to_string())?;
    Ok(ack())
}

/// A log sink the page can write to.
///
/// The page has no console anyone can read: its `console.log` goes to a WebView
/// debug channel that is invisible in a release build, so a script that throws
/// leaves the operator looking at a wrong-looking panel with **no explanation
/// anywhere** -- no window error, no log line, nothing.  This gives the page a
/// way to say what happened in the same log as everything else.
///
/// It is a sink and nothing more: it takes a string, prints it, and returns.
/// No filesystem, no shell, no window control.  Prefixing with `[page]` keeps it
/// separable from the shell's own trace lines.
#[tauri::command]
fn page_note(text: String) -> Ack {
    println!("[page] {text}");
    ack()
}

#[tauri::command]
fn close(window: WebviewWindow) -> Result<Ack, String> {
    trace("close");
    window.close().map_err(|e| e.to_string())?;
    Ok(ack())
}

#[tauri::command]
fn toggle_maximize(window: WebviewWindow) -> Result<serde_json::Value, String> {
    trace("toggle_maximize");
    // a toggle that guesses wrong is worse than none.
    let maximized = window.is_maximized().map_err(|e| e.to_string())?;
    if maximized {
        window.unmaximize().map_err(|e| e.to_string())?;
    } else {
        window.maximize().map_err(|e| e.to_string())?;
    }
    Ok(serde_json::json!({ "ok": true, "maximized": !maximized }))
}

#[tauri::command]
fn set_on_top(window: WebviewWindow, value: bool) -> Result<serde_json::Value, String> {
    trace("set_on_top");
    window.set_always_on_top(value).map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "on_top": value }))
}

#[tauri::command]
fn set_opacity(window: WebviewWindow, value: f64) -> Result<serde_json::Value, String> {
    trace("set_opacity");
    // Nothing native to do: the card's alpha is a CSS variable the *page* owns
    // (`--win-alpha`), and the window itself is fully transparent so the DWM
    // backdrop shows through the rounded corners.  Present in the contract so
    // the page has one place to call; the pywebview host used it to push the
    // variable through evaluate_js.
    let _ = window;
    Ok(serde_json::json!({ "opacity": value }))
}

#[tauri::command]
fn apply_theme(window: WebviewWindow, theme: String) -> Result<serde_json::Value, String> {
    trace(&format!("apply_theme {theme}"));
    // The DWM backdrop is per-window OS state, not CSS: flipping to light mode
    // has to change the material and the title-bar theme too, or a light card
    // ends up framed in dark chrome.
    let dark = theme != "light";
    window
        .set_theme(Some(if dark { tauri::Theme::Dark } else { tauri::Theme::Light }))
        .map_err(|e| e.to_string())?;

    #[cfg(target_os = "windows")]
    {
        // Acrylic only reads as the floating card when the window is
        // transparent; a failure here is cosmetic, so it must not kill the cue.
        let tint = match theme.as_str() {
            "light" => (242, 243, 246, 180),
            "amber" => (20, 14, 4, 200),
            _ => (11, 13, 17, 200),
        };
        let _ = window_vibrancy::apply_acrylic(&window, Some(tint));
    }
    Ok(serde_json::json!({ "ok": true, "theme": theme }))
}

// ------------------------------------------------------------------- drag

#[tauri::command]
fn begin_drag(window: WebviewWindow, state: tauri::State<Geometry>) -> Result<serde_json::Value, String> {
    trace("begin_drag");
    let (x, y, _, _) = logical_rect(&window)?;
    *state.drag_origin.lock().unwrap() = Some((x, y));
    Ok(serde_json::json!({ "ok": true, "x": x, "y": y }))
}

#[tauri::command]
fn update_drag(window: WebviewWindow, state: tauri::State<Geometry>, dx: f64, dy: f64) -> Result<serde_json::Value, String> {
    trace("update_drag");
    let origin = *state.drag_origin.lock().unwrap();
    let Some((ox, oy)) = origin else {
        return Err("no drag in progress".into());
    };
    let x = ox + dx;
    let y = oy + dy;
    window
        .set_position(Position::Logical(LogicalPosition::new(x, y)))
        .map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "ok": true, "x": x, "y": y }))
}

#[tauri::command]
fn end_drag(state: tauri::State<Geometry>) -> Result<Ack, String> {
    trace("end_drag");
    *state.drag_origin.lock().unwrap() = None;
    Ok(ack())
}

// ----------------------------------------------------------------- resize

#[tauri::command]
fn begin_resize(window: WebviewWindow, state: tauri::State<Geometry>, edge: String) -> Result<serde_json::Value, String> {
    trace("begin_resize");
    if !matches!(edge.as_str(), "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw") {
        return Err(format!("bad edge {edge:?}"));
    }
    let (x, y, w, h) = logical_rect(&window)?;
    *state.resize_base.lock().unwrap() = Some(ResizeBase { x, y, w, h, edge: edge.clone() });
    Ok(serde_json::json!({ "ok": true, "edge": edge, "x": x, "y": y, "w": w, "h": h }))
}

#[tauri::command]
fn update_resize(window: WebviewWindow, state: tauri::State<Geometry>, dx: f64, dy: f64) -> Result<serde_json::Value, String> {
    trace("update_resize");
    let base = state.resize_base.lock().unwrap();
    let Some(base) = base.as_ref() else {
        return Err("no resize in progress".into());
    };
    let (x, y, w, h) = resized_rect(
        &base.edge,
        (base.x, base.y, base.w, base.h),
        dx,
        dy,
        min_size(),
    );

    window
        .set_size(Size::Logical(LogicalSize::new(w, h)))
        .map_err(|e| e.to_string())?;
    window
        .set_position(Position::Logical(LogicalPosition::new(x, y)))
        .map_err(|e| e.to_string())?;
    Ok(serde_json::json!({ "ok": true, "x": x, "y": y, "w": w, "h": h }))
}

#[tauri::command]
fn end_resize(state: tauri::State<Geometry>) -> Result<Ack, String> {
    trace("end_resize");
    *state.resize_base.lock().unwrap() = None;
    Ok(ack())
}

// -------------------------------------------------------------------- setup

fn conductor_host_port() -> (String, u16) {
    let host = std::env::var("CHRONO_PANEL_HOST").unwrap_or_else(|_| DEFAULT_PANEL_HOST.to_string());
    let port = std::env::var("CHRONO_PANEL_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(DEFAULT_PANEL_PORT);
    (host, port)
}

fn ipc_port() -> u16 {
    std::env::var("CHRONO_IPC_PORT")
        .ok()
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(DEFAULT_IPC_PORT)
}

/// What will start a conductor, and by which rule it was chosen.
struct ConductorCmd {
    program: std::path::PathBuf,
    args: Vec<String>,
    /// Which rule matched: `CHRONO_SIDECAR` / `bundled` / `CHRONO_PYTHON`.
    ///
    /// Carried out of the resolver so the value written to `shell.json` is the
    /// rule that actually fired rather than a re-derivation of it -- the two
    /// would eventually disagree, and this is the field a user reads to answer
    /// "which clock is this window talking to".
    source: &'static str,
}

/// The command that starts a conductor, or `None` if this build has none.
///
/// The resolution order is explicit and logged -- never a silent substitution,
/// because "which conductor am I talking to" is precisely the question a user
/// cannot answer by looking at the window:
///
/// 1. ``CHRONO_SIDECAR``  -- an explicit path (tests, unusual layouts)
/// 2. ``chrono-core``     -- beside this executable; the bundled release shape
/// 3. ``CHRONO_PYTHON``   -- a development checkout, running the module
///
/// There is deliberately no fallback to whatever ``python`` happens to be on
/// PATH.  A conductor on an unexpected interpreter is worse than no conductor:
/// it is a second, differently configured clock, and the window cannot tell you.
fn conductor_command(host: &str, ipc: u16, panel: u16) -> Option<ConductorCmd> {
    let with_flags = |mut prefix: Vec<String>| {
        prefix.extend([
            "--host".to_string(), host.to_string(),
            "--port".to_string(), ipc.to_string(),
            "--panel-port".to_string(), panel.to_string(),
        ]);
        prefix
    };

    if let Ok(explicit) = std::env::var("CHRONO_SIDECAR") {
        let p = std::path::PathBuf::from(explicit);
        println!("[shell] conductor launcher: CHRONO_SIDECAR -> {}", p.display());
        return Some(ConductorCmd {
            program: p,
            args: with_flags(vec!["daemon".to_string()]),
            source: "CHRONO_SIDECAR",
        });
    }

    let here = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let bundled = here.join(if cfg!(windows) { "chrono-core.exe" } else { "chrono-core" });
    if bundled.is_file() {
        println!("[shell] conductor launcher: bundled {}", bundled.display());
        return Some(ConductorCmd {
            program: bundled,
            args: with_flags(vec!["daemon".to_string()]),
            source: "bundled",
        });
    }

    if let Ok(python) = std::env::var("CHRONO_PYTHON") {
        println!("[shell] conductor launcher: CHRONO_PYTHON -> {python}");
        return Some(ConductorCmd {
            program: std::path::PathBuf::from(python),
            args: with_flags(vec!["-m".to_string(), "chrono_handoff.daemon".to_string()]),
            source: "CHRONO_PYTHON",
        });
    }

    None
}

/// Where the spawned conductor's output goes.
///
/// Deliberately **not** inherited from this shell.  A conductor holding our
/// stdout can block on a full pipe or take EPIPE when the window closes, and a
/// conductor that can block on a write is a conductor that can miss a beat --
/// which is the one failure the split topology exists to prevent.  Its own file
/// keeps the output inspectable without that coupling.
/// The per-user state directory.
///
/// Mirrors ``chrono_handoff.home.chrono_home()``.  If the two ever disagreed,
/// the shell would write its log and its record somewhere `chrono doctor`
/// never looks, and the symptom would be "there is nothing there" rather than
/// an error.
fn chrono_home_dir() -> std::path::PathBuf {
    match std::env::var("CHRONO_HOME") {
        Ok(dir) => std::path::PathBuf::from(dir),
        Err(_) => {
            let home = std::env::var("USERPROFILE")
                .or_else(|_| std::env::var("HOME"))
                .unwrap_or_else(|_| ".".to_string());
            std::path::PathBuf::from(home).join(".chrono-handoff")
        }
    }
}

fn conductor_log_path() -> std::path::PathBuf {
    chrono_home_dir().join("conductor.log")
}

/// Tell `chrono doctor` (and anything else looking) that this shell is up.
///
/// The conductor cannot see the window -- the shell is just another HTTP client
/// to it -- so "a Tauri window is on screen" is knowledge only the shell has.
/// Leaving it on disk is the same trick as `daemon.json`.
///
/// Failing to write this is **not** fatal: the window works, the cue fires, and
/// only a diagnostic is lost.  But it is loud, because a shell that cannot say
/// what it is is exactly the situation this record exists to prevent.
fn write_shell_record(launcher: &str, program: &str, conductor_pid: u32, api_base: &str) {
    let dir = chrono_home_dir();
    if let Err(e) = std::fs::create_dir_all(&dir) {
        println!("[shell] WARNING: cannot create {} ({e}); no shell record", dir.display());
        return;
    }
    let started = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let record = serde_json::json!({
        "backend": "tauri",
        "version": env!("CARGO_PKG_VERSION"),
        "pid": std::process::id(),
        "exe": std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default(),
        "api_base": api_base,
        "started_wall": started,
        "conductor_launcher": launcher,
        "conductor_program": program,
        "conductor_pid": conductor_pid,
    });
    let path = dir.join("shell.json");
    match serde_json::to_string_pretty(&record) {
        Ok(text) => match std::fs::write(&path, text) {
            Ok(()) => println!("[shell] shell record -> {}", path.display()),
            Err(e) => println!("[shell] WARNING: cannot write {} ({e})", path.display()),
        },
        Err(e) => println!("[shell] WARNING: cannot serialise shell record ({e})"),
    }
}

/// Remove our record on a clean exit, leaving it alone if it is not ours.
///
/// A record left behind by a *killed* shell is handled on the reading side
/// (`ShellRecord.alive`), because nothing on this side runs after SIGKILL.
fn clear_shell_record() {
    let path = chrono_home_dir().join("shell.json");
    let ours = std::fs::read_to_string(&path)
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v.get("pid").and_then(|p| p.as_u64()))
        .is_some_and(|pid| pid == u64::from(std::process::id()));
    if ours {
        let _ = std::fs::remove_file(&path);
    }
}

/// Start a conductor as a **detached** process.
///
/// Detached is the whole point, not a detail: the split topology exists so a
/// closed window cannot kill an in-flight cue (see docs/component.md), and that
/// property dies the moment the conductor is this shell's child in the ordinary
/// sense -- a job object would reap it with us.  On Windows that means its own
/// console, which the cue channel wants anyway: keystroke capture needs one
/// (``chrono doctor`` reports "console: NO" without it).
fn spawn_conductor(
    prog: &std::path::Path,
    args: &[String],
    attach_only: bool,
) -> Result<u32, String> {
    if attach_only {
        return Err(format!(
            "no conductor is running, and --attach-only was given (would have run: {} {})",
            prog.display(),
            args.join(" ")
        ));
    }
    println!("[shell] starting conductor: {} {}", prog.display(), args.join(" "));
    let mut cmd = std::process::Command::new(prog);
    cmd.args(args);

    let log_path = conductor_log_path();
    if let Some(dir) = log_path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    match std::fs::OpenOptions::new().create(true).append(true).open(&log_path) {
        Ok(f) => {
            let f2 = f.try_clone().map_err(|e| format!("conductor log: {e}"))?;
            cmd.stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::from(f))
                .stderr(std::process::Stdio::from(f2));
            println!("[shell] conductor output -> {}", log_path.display());
        }
        Err(e) => {
            // Not fatal: a conductor with nowhere to log still keeps time.  But
            // say so, because "why is there no output" is otherwise unanswerable.
            println!("[shell] WARNING: cannot open {} ({e}); conductor output discarded", log_path.display());
            cmd.stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
        }
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_CONSOLE: u32 = 0x0000_0010;
        cmd.creation_flags(CREATE_NEW_CONSOLE);
    }
    match cmd.spawn() {
        Ok(child) => {
            println!("[shell] conductor pid = {}", child.id());
            Ok(child.id())
        }
        Err(e) => Err(format!("could not start conductor {}: {e}", prog.display())),
    }
}

/// Where the conductor's HTTP API lives.
///
/// The page is bundled, so its own origin is this shell's and a relative
/// `/api/*` would be asked of the bundle.  The page reads this base out of
/// `__CHRONO_API_BASE__` (see `apiURL()` in statics/panel.html) and the
/// conductor answers it cross-origin via the closed allowlist in webui.py.
fn api_base() -> String {
    let (host, port) = conductor_host_port();
    format!("http://{host}:{port}")
}

/// Is a panel answering right now?  Single probe, no waiting.
/// Whether a panel that will actually serve this window is at this address.
///
/// Asks `/api/health` rather than merely opening a TCP connection.  A connect
/// answers "is something listening", which is not the question: the shell uses
/// this to decide whether to *attach*, and attaching to a port that accepts but
/// never replies opens a window whose page cannot reach the API, with nothing in
/// the UI to say why.
///
/// When this returns false the shell tries to start a conductor instead; if the
/// port really is taken by something else, that start fails to bind and
/// `wait_for_panel` turns it into a loud, logged startup error -- the right
/// outcome for "something else owns this port".
///
/// Note, so nobody re-derives a wrong story from it: this was written while
/// chasing an intermittent test failure, on the theory that a killed PyInstaller
/// onefile conductor leaves its parent holding the socket.  That theory was
/// **tested and is false** -- killing the child releases the port immediately and
/// both processes exit (see the note in docs/desktop-shell.md).  The real cause
/// of that flake was in the test, not here.  The check is kept because asking the
/// right question is still better than asking a cheap one.
fn panel_answers(host: &str, port: u16) -> bool {
    let url = format!("http://{host}:{port}/api/health");
    // Short: this sits on the startup path and a half-dead port never answers.
    http_get_json_within(&url, Duration::from_millis(700)).is_ok()
}

/// Wait for the conductor's panel to answer before building the window.
///
/// Fail fast and loudly instead of showing a card that cannot count down: the
/// operator must never have to guess whether the widget is broken or early.
fn wait_for_panel(host: &str, port: u16) -> bool {
    let deadline = std::time::Instant::now() + Duration::from_secs(CONDUCTOR_WAIT_S);
    while std::time::Instant::now() < deadline {
        if panel_answers(host, port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

/// Env-gated self-check for the e2e harness (`CHRONO_SHELL_SELFTEST=1`).
///
/// Synthetic mouse input on Windows is unreliable enough that `e2e_resize.py`
/// carries a fallback for exactly this, and on this machine it usually delivers
/// nothing at all -- so without an input-free path the drag/resize/minimise
/// commands would have **no** automated coverage, which is the shape of gap that
/// lets a broken grip ship.
///
/// It drives the commands through the *page's own* `shellCall`, so the chain is
/// real: page JS -> invoke -> ACL -> Rust -> the OS window.  Only the synthetic
/// mouse is skipped.  Off by default and inert in any normal run.
fn run_selftest(window: WebviewWindow) {
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(2200)); // let the page's script settle

        let before = logical_rect(&window).ok();
        let _ = window.eval(
            "shellCall('begin_drag'); shellCall('update_drag', 120, 90); shellCall('end_drag');",
        );
        std::thread::sleep(Duration::from_millis(800));
        if let (Some(b), Ok(a)) = (before, logical_rect(&window)) {
            // The page sends a TOTAL delta, so the result must be the delta
            // itself -- not a multiple of it.
            let ok = ((a.0 - b.0) - 120.0).abs() <= 2.0 && ((a.1 - b.1) - 90.0).abs() <= 2.0;
            println!(
                "[shell] selftest drag ok={ok} from=({:.0},{:.0}) to=({:.0},{:.0})",
                b.0, b.1, a.0, a.1
            );
        }

        let before = logical_rect(&window).ok();
        let _ = window.eval(
            "shellCall('begin_resize','se'); shellCall('update_resize', 80, 60); shellCall('end_resize');",
        );
        std::thread::sleep(Duration::from_millis(800));
        if let (Some(b), Ok(a)) = (before, logical_rect(&window)) {
            let ok = (a.2 - b.2 - 80.0).abs() <= 2.0 && (a.3 - b.3 - 60.0).abs() <= 2.0;
            println!(
                "[shell] selftest resize ok={ok} from=({:.0}x{:.0}) to=({:.0}x{:.0})",
                b.2, b.3, a.2, a.3
            );
        }

        let _ = window.eval("shellCall('minimize')");
        std::thread::sleep(Duration::from_millis(700));
        println!("[shell] selftest minimize ok={}", window.is_minimized().unwrap_or(false));
        let _ = window.unminimize();
        std::thread::sleep(Duration::from_millis(500));

        let _ = window.eval("shellCall('toggle_maximize')");
        std::thread::sleep(Duration::from_millis(800));
        println!("[shell] selftest maximize ok={}", window.is_maximized().unwrap_or(false));
        let _ = window.eval("shellCall('toggle_maximize')");
    });
}

/// Drive the **page's own** pointer handlers, without synthetic mouse input.
///
/// `run_selftest` calls `shellCall` directly, which covers the commands but
/// skips everything between a press and a command: the grip's delta maths, the
/// title-bar drag, the GO button's POST.  Those live in panel.html, and on this
/// machine they could only ever be reported as SKIP -- `mouse_event` drops
/// `pointermove`, so a synthetic drag arrives with no motion.
///
/// This calls `__chronoPageTest` (defined in panel.html), which constructs real
/// DOM events and dispatches them at the real elements.  Same listeners, same
/// deltas, same outgoing calls; only the OS input queue is out of the picture.
///
/// The outcome is reported through the window itself -- geometry, minimise,
/// maximise -- so a missing or broken hook shows up as a failed measurement
/// rather than a silent no-op.
fn run_page_selftest(window: WebviewWindow) {
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(2200)); // let the page's script settle

        let before = logical_rect(&window).ok();
        let _ = window.eval("__chronoPageTest.dragGrip('se', 80, 60)");
        std::thread::sleep(Duration::from_millis(900));
        if let (Some(b), Ok(a)) = (before, logical_rect(&window)) {
            let ok = (a.2 - b.2 - 80.0).abs() <= 2.0 && (a.3 - b.3 - 60.0).abs() <= 2.0;
            println!(
                "[page] grip drag ok={ok} from=({:.0}x{:.0}) to=({:.0}x{:.0})",
                b.2, b.3, a.2, a.3
            );
        }

        let before = logical_rect(&window).ok();
        let _ = window.eval("__chronoPageTest.dragTitle(60, 40)");
        std::thread::sleep(Duration::from_millis(900));
        if let (Some(b), Ok(a)) = (before, logical_rect(&window)) {
            let ok = ((a.0 - b.0) - 60.0).abs() <= 3.0 && ((a.1 - b.1) - 40.0).abs() <= 3.0;
            println!(
                "[page] title drag ok={ok} from=({:.0},{:.0}) to=({:.0},{:.0})",
                b.0, b.1, a.0, a.1
            );
        }

        let _ = window.eval("__chronoPageTest.click('tb-min')");
        std::thread::sleep(Duration::from_millis(700));
        println!("[page] min button ok={}", window.is_minimized().unwrap_or(false));
        let _ = window.unminimize();
        std::thread::sleep(Duration::from_millis(500));

        let _ = window.eval("__chronoPageTest.click('tb-max')");
        std::thread::sleep(Duration::from_millis(800));
        println!("[page] max button ok={}", window.is_maximized().unwrap_or(false));
        let _ = window.eval("__chronoPageTest.click('tb-max')");
    });
}

/// Ask the page to press GO once it has an armed cue, and report the verdict.
///
/// Split from `run_page_selftest` on purpose: the pointer steps move and resize
/// the window, and the phase that arms a cue also asserts the window's size.  A
/// self-check that rearranges the window under another test's measurement makes
/// both results untrustworthy.
fn run_page_press_selftest(window: WebviewWindow) {
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(2200));
        println!("[page] waiting for an armed cue to press GO");
        // 120 s: the caller may have to arm a cue after this line runs.
        let _ = window.eval(
            "__chronoPageTest.pressWhenArmed(120000).then(r => console.log('GO', JSON.stringify(r)))",
        );
    });
}

/// Report a reason the app cannot start.
///
/// In a release build this process has no console (`windows_subsystem =
/// "windows"`), so `println!` goes nowhere and `setup` returning `Err` would be
/// silent: the window just never appears and the user has nothing to go on.
/// So the reason goes to two places that outlive us -- the conductor log and a
/// message box.
fn report_fatal(msg: &str) {
    println!("[shell] FATAL: {msg}");
    let path = conductor_log_path();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        use std::io::Write;
        let _ = writeln!(f, "[shell] FATAL: {msg}");
    }

    #[cfg(windows)]
    {
        use std::iter::once;
        use std::os::windows::ffi::OsStrExt;
        let wide = |s: &str| -> Vec<u16> { std::ffi::OsStr::new(s).encode_wide().chain(once(0)).collect() };
        const MB_ICONERROR: u32 = 0x0000_0010;
        const MB_SETFOREGROUND: u32 = 0x0001_0000;
        unsafe extern "system" {
            fn MessageBoxW(
                hwnd: *mut core::ffi::c_void,
                text: *const u16,
                caption: *const u16,
                utype: u32,
            ) -> i32;
        }
        let text = wide(&format!("chrono-handoff could not start:\n\n{msg}\n\nlog: {}", path.display()));
        let caption = wide("chrono-handoff");
        unsafe {
            MessageBoxW(
                core::ptr::null_mut(),
                text.as_ptr(),
                caption.as_ptr(),
                MB_ICONERROR | MB_SETFOREGROUND,
            );
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Geometry::default())
        .invoke_handler(tauri::generate_handler![
            minimize,
            page_note,
            close,
            toggle_maximize,
            set_on_top,
            set_opacity,
            apply_theme,
            begin_drag,
            update_drag,
            end_drag,
            begin_resize,
            update_resize,
            end_resize,
        ])
        .setup(|app| {
            let (host, port) = conductor_host_port();

            // Attach if one is already running, otherwise start one.  The two
            // outcomes are logged because from inside the window they look
            // identical, and only one of them means the operator now owns a
            // process they did not ask for.
            // Which rule got us a conductor, and the pid, so the record can
            // say so.  "attached" is a distinct answer from every launcher
            // rule: the operator did not start this conductor, so the process
            // outliving the window is not something they asked for.
            let (launcher, launcher_program, conductor_pid) = if panel_answers(&host, port) {
                println!("[shell] attached to the conductor already at {host}:{port}");
                ("attached".to_string(), String::new(), 0u32)
            } else {
                let attach_only = std::env::args().any(|a| a == "--attach-only")
                    || std::env::var("CHRONO_ATTACH_ONLY").as_deref() == Ok("1");
                let cmd = match conductor_command(&host, ipc_port(), port) {
                    Some(cmd) => cmd,
                    None => {
                        let msg = format!(
                            "no conductor at {host}:{port}, and this build has no sidecar to \
                             start one.\n  Set CHRONO_SIDECAR (or CHRONO_PYTHON for a source \
                             checkout), or start one yourself: chrono daemon --background"
                        );
                        report_fatal(&msg);
                        return Err(msg.into());
                    }
                };
                let pid = match spawn_conductor(&cmd.program, &cmd.args, attach_only) {
                    Ok(pid) => pid,
                    Err(msg) => {
                        report_fatal(&msg);
                        return Err(msg.into());
                    }
                };
                if !wait_for_panel(&host, port) {
                    let msg = format!(
                        "the conductor at {host}:{port} was started but did not answer within \
                         {CONDUCTOR_WAIT_S}s -- see {}",
                        conductor_log_path().display()
                    );
                    report_fatal(&msg);
                    return Err(msg.into());
                }
                (cmd.source.to_string(), cmd.program.display().to_string(), pid)
            };
            // The page is bundled, so this is an asset path, not a URL.  The
            // static directory is the Python package's `statics/`, which also
            // holds the fonts -- that is what keeps /fonts/* same-origin.
            let base = api_base();
            println!("[shell] api base = {base}");
            // Written before the window is built: `chrono doctor` run while the
            // page is still loading should already see this shell.
            write_shell_record(&launcher, &launcher_program, conductor_pid, &base);
            let boot = format!(
                "window.__CHRONO_API_BASE__ = {base:?}; window.__CHRONO_SHELL__ = \"desktop\";"
            );

            // -- where the window opens -----------------------------------
            //
            // Read before the window exists, because the geometry has to be
            // handed to the builder.  Off-screen positions are corrected here
            // rather than at save time: the monitor that was unplugged is gone
            // by then, and that is exactly when the saved number turns harmful.
            let saved = saved_geometry(&base);
            let size = saved.size();
            let monitors: Vec<Monitor> = app
                .available_monitors()
                .unwrap_or_default()
                .iter()
                .map(|m| {
                    let s = m.scale_factor();
                    let p = m.position();
                    let sz = m.size();
                    Monitor {
                        x: to_logical(f64::from(p.x), s),
                        y: to_logical(f64::from(p.y), s),
                        w: to_logical(f64::from(sz.width), s),
                        h: to_logical(f64::from(sz.height), s),
                    }
                })
                .collect();
            let fallback = Monitor { x: 0.0, y: 0.0, w: size.0, h: size.1 };
            let primary = app.primary_monitor().ok().flatten().map_or(fallback, |m| {
                let s = m.scale_factor();
                let p = m.position();
                let sz = m.size();
                Monitor {
                    x: to_logical(f64::from(p.x), s),
                    y: to_logical(f64::from(p.y), s),
                    w: to_logical(f64::from(sz.width), s),
                    h: to_logical(f64::from(sz.height), s),
                }
            });
            let origin = saved.origin(&monitors, primary, size);
            match origin {
                Some((x, y)) => println!(
                    "[shell] opening at {}x{}, position ({x}, {y})",
                    size.0, size.1
                ),
                None => println!("[shell] opening at {}x{}, OS-chosen position", size.0, size.1),
            }

            let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("panel.html".into()))
                .title("chrono-handoff")
                .inner_size(size.0, size.1)
                .min_inner_size(min_size().0, min_size().1)
                // Frameless + transparent so the page paints its own rounded
                // card and the acrylic backdrop shows through the corners.
                .decorations(false)
                .transparent(true)
                .shadow(false)
                .always_on_top(true)
                .resizable(true)
                // Built hidden and shown on page load: revealing a blank card
                // first would look identical to a broken widget, and this window
                // is always-on-top, so it cannot be dismissed by looking away.
                .visible(false)
                // Must run before the page's own script: apiURL() and the SHELL
                // check read these on first use.
                .initialization_script(&boot)
                .on_page_load(|window, payload| {
                    if let PageLoadEvent::Finished = payload.event() {
                        let _ = window.show();
                        if std::env::var("CHRONO_SHELL_SELFTEST").as_deref() == Ok("1") {
                            run_selftest(window.clone());
                        }
                        // Separate switch: this one drives the page's pointer
                        // handlers, the other drives the commands.  Both off by
                        // default and inert in any normal run.
                        match std::env::var("CHRONO_PAGE_SELFTEST").as_deref() {
                            Ok("pointer") => run_page_selftest(window.clone()),
                            Ok("press") => run_page_press_selftest(window.clone()),
                            _ => {}
                        }
                    }
                });
            if let Some((x, y)) = origin {
                builder = builder.position(x, y);
            }
            let window = builder.build()?;

            // Persist on close, before the window is gone.  Deliberately not on
            // every Moved/Resized event: a drag emits them continuously, and a
            // settings write per pointer-move is a lot of HTTP for a value only
            // the next launch reads.  Those events still feed `LAST_GOOD`, so a
            // window closed while minimised keeps the position from before.
            let closer = window.clone();
            let closer_base = base.clone();
            let tracker = window.clone();
            window.on_window_event(move |event| match event {
                WindowEvent::Moved(_) | WindowEvent::Resized(_) => remember_geometry(&tracker),
                WindowEvent::CloseRequested { .. } => save_geometry(&closer, &closer_base),
                _ => {}
            });

            let _ = window;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running chrono-handoff shell")
        .run(|_handle, event| {
            // A clean exit removes our `shell.json`, so `chrono doctor` does not
            // report a stale record for a window that closed properly.  A shell
            // that is killed leaves the file; the reader catches that case.
            if let tauri::RunEvent::Exit = event {
                clear_shell_record();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{min_size, resized_rect, to_logical, visible_origin, Monitor, SavedGeometry};

    const PRIMARY: Monitor = Monitor { x: 0.0, y: 0.0, w: 1920.0, h: 1067.0 };

    const BASE: (f64, f64, f64, f64) = (100.0, 200.0, 460.0, 620.0);

    fn r(edge: &str, dx: f64, dy: f64) -> (f64, f64, f64, f64) {
        resized_rect(edge, BASE, dx, dy, min_size())
    }

    #[test]
    fn east_and_south_edges_grow_without_moving_the_origin() {
        assert_eq!(r("se", 110.0, 70.0), (100.0, 200.0, 570.0, 690.0));
        assert_eq!(r("e", 40.0, 999.0), (100.0, 200.0, 500.0, 620.0));
        assert_eq!(r("s", 999.0, 40.0), (100.0, 200.0, 460.0, 660.0));
    }

    #[test]
    fn west_and_north_edges_move_the_origin_by_exactly_what_the_size_lost() {
        // Dragging the left edge right shrinks the window and moves the origin
        // right by the same amount, so the *right* edge stays put -- dragging a
        // left edge must not slide the whole widget.
        assert_eq!(r("w", 60.0, 0.0), (160.0, 200.0, 400.0, 620.0));
        assert_eq!(r("n", 0.0, 80.0), (100.0, 280.0, 460.0, 540.0));
        assert_eq!(r("nw", 60.0, 80.0), (160.0, 280.0, 400.0, 540.0));
    }

    #[test]
    fn min_size_clamps_and_the_origin_stops_following() {
        let (x, _y, w, _h) = r("w", 1000.0, 0.0);
        assert_eq!(w, 320.0, "clamped to the floor");
        // Clamped width means the origin moved by exactly (w0 - min), no further.
        assert_eq!(x, 100.0 + (460.0 - 320.0));

        let (_x, _y, w2, h2) = r("se", -1000.0, -1000.0);
        assert_eq!((w2, h2), (320.0, 360.0));
    }

    #[test]
    fn deltas_are_totals_not_increments() {
        // The page sends the offset since pointerdown.  Re-applying the same
        // total must be idempotent; if these accumulated, a slow drag would run
        // away and the old "120 px drag grew the window 6 px" bug would return
        // in the opposite direction.
        assert_eq!(r("se", 50.0, 50.0), r("se", 50.0, 50.0));
        let seq = [10.0, 25.0, 50.0, 90.0];
        // The last total is the result -- never the sum of all of them.
        assert_eq!(r("e", seq[seq.len() - 1], 0.0).2, 460.0 + 90.0);
    }

    #[test]
    fn a_remembered_origin_on_a_live_monitor_is_kept() {
        let got = visible_origin(300.0, 200.0, &[PRIMARY], PRIMARY, (460.0, 620.0));
        assert_eq!(got, (300.0, 200.0));
    }

    #[test]
    fn an_origin_on_a_second_monitor_is_kept() {
        // The regression this guards: "only the primary monitor counts" would
        // drag a window the user parks on a second display back to the first
        // every single launch.
        let second = Monitor { x: 1920.0, y: 0.0, w: 1920.0, h: 1067.0 };
        let got = visible_origin(2200.0, 300.0, &[PRIMARY, second], PRIMARY, (460.0, 620.0));
        assert_eq!(got, (2200.0, 300.0));
    }

    #[test]
    fn an_offscreen_origin_is_recentred_on_the_primary() {
        // Measured on this machine via the pywebview host: a saved y=2047 on a
        // 1067-tall desktop.  The window opened invisible and the only way back
        // was editing JSON by hand.
        let got = visible_origin(100.0, 2047.0, &[PRIMARY], PRIMARY, (460.0, 620.0));
        let (want_x, want_y) = (
            ((1920.0 - 460.0) / 2.0),
            ((1067.0 - 620.0) / 2.0),
        );
        assert_eq!(got, (want_x, want_y));
    }

    #[test]
    fn a_negative_origin_with_no_monitor_there_is_recentred() {
        // Monitor unplugged and it sat to the left of the primary: x = -1920 is
        // now nowhere at all.
        let got = visible_origin(-1920.0, 100.0, &[PRIMARY], PRIMARY, (460.0, 620.0));
        assert_eq!(got, (((1920.0 - 460.0) / 2.0), ((1067.0 - 620.0) / 2.0)));
    }

    #[test]
    fn no_saved_position_means_the_os_places_the_window() {
        let saved = SavedGeometry::default();
        assert_eq!(saved.origin(&[PRIMARY], PRIMARY, saved.size()), None);
        // ...but the size still has to be usable, not zero.
        assert_eq!(saved.size(), (460.0, 620.0));
    }

    #[test]
    fn a_saved_size_below_the_minimum_is_raised_to_it() {
        let v = serde_json::json!({"width": 10, "height": 10});
        assert_eq!(SavedGeometry::from_settings(&v).size(), min_size());
    }

    #[test]
    fn settings_with_nulls_read_as_absent_not_as_zero() {
        // A fresh settings.json serialises x/y as null.  Parsing those as 0.0
        // would place every first run in the top-left corner -- and would also
        // make "never positioned" indistinguishable from "positioned at 0,0".
        let v = serde_json::json!({"x": null, "y": null, "width": 500, "height": 700});
        let saved = SavedGeometry::from_settings(&v);
        assert_eq!((saved.x, saved.y), (None, None));
        assert_eq!(saved.size(), (500.0, 700.0));
    }

    #[test]
    fn geometry_is_stored_in_logical_pixels() {
        // Save physical, restore logical, and the window grows by the DPI
        // factor on every reopen.  This machine is 1.5x: 690 physical is 460.
        assert_eq!(to_logical(690.0, 1.5), 460.0);
        assert_eq!(to_logical(930.0, 1.5), 620.0);
        assert_eq!(to_logical(460.0, 1.0), 460.0);
        // A bogus scale factor must not poison the value.
        assert_eq!(to_logical(460.0, 0.0), 460.0);
    }
}
