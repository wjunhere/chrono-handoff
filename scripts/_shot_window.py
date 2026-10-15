"""Capture a native window to a PNG, so a UI claim can be *looked at*.

Written while chasing a layout bug that every assertion in the suite passed
through: the rail's grid column grew to its own min-content width, every sibling
inherited it, and the card cropped the GO button and all five metric values.  In
text that read as "no overflow"; in a picture it was obvious in one second.  A
tool that turns "the numbers look fine" into "here is what it looks like" is
worth keeping.

Three things here are learned rather than chosen:

1. **Physical pixels, deliberately, per thread.**  A DPI-unaware process gets
   every rect virtualised (a 690x930 window reports as 460x620 at 150%), so
   BitBlt then copies a *shifted* region -- the first attempt screenshotted the
   terminal behind the widget.  ``SetThreadDpiAwarenessContext`` fixes that for
   this thread only, at any point in the process's life.  ``SetProcessDPIAware``
   would also work but is irreversible and would silently change the coordinate
   space of any suite that imports this module.
2. **Not PrintWindow.**  It is the usual tool for occluded windows, but it
   renders WebView2 content blank -- which is exactly the content we want.
   Grabbing the screen region means the window must be visible, which is fine
   for a widget that is always-on-top.
3. **Restore first.**  A minimised window reports the Win32 sentinel rect
   (-21333,-21333 at 158x26), so capturing without restoring returns a picture
   of the taskbar.

Output defaults under ``logs/`` (gitignored) on purpose: a screenshot is a local
artefact and must not end up in a commit by accident.

    python scripts/_shot_window.py chrono-handoff
    python scripts/_shot_window.py chrono-handoff -o logs/shots/dense-rail.png
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_u = ctypes.windll.user32
_g = ctypes.windll.gdi32
_k = ctypes.windll.kernel32

#: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, as a pseudo-HANDLE.
PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

_QUERY_LIMITED_INFORMATION = 0x1000
_SRCCOPY = 0x00CC0020
_DIB_RGB_COLORS = 0
_BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _process_name(pid: int) -> str:
    handle = _k.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(len(buf))
        if _k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        _k.CloseHandle(handle)


def windows_of(exe_name: str) -> list[tuple[int, int]]:
    """Visible top-level windows whose process image is ``exe_name``.

    Matched by PROCESS, never by window title: a browser tab showing the panel
    titles itself "chrono-handoff - <browser>", and capturing that instead of the
    widget would be a confusing wrong answer rather than an error.
    """
    found: list[tuple[int, int]] = []
    # Accept either "chrono-handoff" or "chrono-handoff.exe"; the image name always
    # carries the extension, and requiring it is a papercut, not a safety feature.
    target = Path(exe_name).stem.lower()

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not _u.IsWindowVisible(hwnd):
            return True
        pid = ctypes.wintypes.DWORD()
        _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if Path(_process_name(pid.value)).stem.lower() == target:
            found.append((int(hwnd), int(pid.value)))
        return True

    _u.EnumWindows(cb, 0)
    return found


def restore(hwnd: int) -> None:
    """Bring a minimised window back so its rect is real rather than the sentinel."""
    if _u.IsIconic(hwnd):
        _u.ShowWindow(hwnd, 9)          # SW_RESTORE
        time.sleep(0.9)


def rect(hwnd: int) -> tuple[int, int, int, int]:
    r = ctypes.wintypes.RECT()
    if not _u.GetWindowRect(hwnd, ctypes.byref(r)):
        raise OSError(f"GetWindowRect failed for hwnd {hwnd}")
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _write_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    """Minimal PNG writer: BGRA rows in, RGB PNG out.

    Hand-rolled because the alternatives are a Pillow dependency (this repo has
    none, and the tool is not worth one) or shelling out to something else.  PNG
    is zlib plus five chunks; that is small enough to be honest about.
    """
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                                   # filter: none
        row = bgra[y * stride:(y + 1) * stride]         # BGRA
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]                           # R <- offset 2
        rgb[1::3] = row[1::4]                           # G <- offset 1
        rgb[2::3] = row[0::4]                           # B <- offset 0
        raw.extend(rgb)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)   # 8-bit RGB
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def capture(hwnd: int, path: Path) -> tuple[int, int]:
    """Copy the window's screen region into ``path``.  Returns (width, height)."""
    # Physical pixels for THIS thread only, so the rect and the BitBlt agree and
    # no other code in the process is affected.
    previous = _u.SetThreadDpiAwarenessContext(PER_MONITOR_AWARE_V2)
    if previous is None:
        raise OSError("SetThreadDpiAwarenessContext failed; cannot trust coordinates")
    try:
        x, y, w, h = rect(hwnd)
        if w <= 0 or h <= 0:
            raise OSError(f"window rect is empty: {w}x{h} at ({x},{y})")

        screen_dc = _u.GetDC(0)
        mem_dc = _g.CreateCompatibleDC(screen_dc)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h            # negative: top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB
        bits = ctypes.c_void_p()
        bitmap = _g.CreateDIBSection(
            mem_dc, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            raise OSError("CreateDIBSection failed")
        old = _g.SelectObject(mem_dc, bitmap)
        try:
            if not _g.BitBlt(mem_dc, 0, 0, w, h, screen_dc, x, y, _SRCCOPY):
                raise OSError("BitBlt failed")
            buf = ctypes.string_at(bits, w * h * 4)
            _write_png(path, w, h, buf)
        finally:
            _g.SelectObject(mem_dc, old)
            _g.DeleteObject(bitmap)
            _g.DeleteDC(mem_dc)
            _u.ReleaseDC(0, screen_dc)
        return w, h
    finally:
        _u.SetThreadDpiAwarenessContext(previous)


def capture_process(exe_name: str, path: Path, restore_first: bool = True) -> Path:
    """Capture the (first) visible window of a process, restoring it if minimised."""
    wins = windows_of(exe_name)
    if not wins:
        raise SystemExit(f"no visible window found for process {exe_name!r}")
    hwnd, pid = wins[0]
    if restore_first:
        restore(hwnd)
    w, h = capture(hwnd, path)
    print(f"captured {exe_name} pid={pid} hwnd={hwnd} {w}x{h} -> {path}")
    return path


USAGE = "usage: python scripts/_shot_window.py <exe-name> [-o out.png]"


def main(argv: list[str]) -> int:
    args = list(argv)
    out: Path | None = None
    if "-o" in args:
        i = args.index("-o")
        if i + 1 >= len(args):
            print(USAGE)
            return 2
        out = Path(args[i + 1])
        del args[i:i + 2]
    if not args:
        print(USAGE)
        return 2
    exe = args[0]
    if out is None:
        out = ROOT / "logs" / "shots" / (Path(exe).stem + ".png")
    capture_process(exe, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
