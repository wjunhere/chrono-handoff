"""Give the native window a real Windows 11 "floating card" chrome.

Everything here is opt-in and degrades silently.  The window must never be
invisible or unusable because a DWM attribute was refused -- so each call is
guarded, and `apply_floating()` reports what actually took effect.

Measured on this machine (Windows 11 build 26100) via _probe_dwm.py:

    DWMWA_WINDOW_CORNER_PREFERENCE (33)   set/get both S_OK, round<->donotround works
    DWMWA_SYSTEMBACKDROP_TYPE      (38)   none/mica/acrylic/tabbed all S_OK
    DWMWA_USE_IMMERSIVE_DARK_MODE  (20)   S_OK
    DWMWA_BORDER_COLOR             (34)   S_OK
    WS_EX_LAYERED + SetLayeredWindowAttributes   S_OK (whole-window alpha works)

Two details that are easy to get wrong and cost real debugging time:

* **ctypes argument types.** ``DWMWA_*`` attribute ids must be passed as
  ``c_uint``; ``c_int`` is ``c_long`` on Windows and ctypes raises
  ``ArgumentError: 'c_long' object cannot be interpreted as an integer``.
* **The corner preference needs the frame to be non-client-less.**  With
  ``frameless=True`` the OS frame is gone entirely, so there is nothing for DWM
  to round -- the shape has to come from the page instead (transparent window +
  an inner rounded surface).  Hence two supported looks, see apply_floating().
"""

from __future__ import annotations

import sys
from typing import Any

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_SYSTEMBACKDROP_TYPE = 38

DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MICA = 2
DWMSBT_ACRYLIC = 3
DWMSBT_TABBED = 4

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

#: A separator in Win32's COLORREF order (0x00BBGGRR).
BORDER_RGB = {"dark": 0x00362A24, "light": 0x00D6DEE8, "amber": 0x00140F3A}


def _hwnd(window: Any) -> int:
    import ctypes

    return int(ctypes.windll.user32.FindWindowW(None, window.title))


def _dwm_set(hwnd: int, attr: int, value: int) -> bool:
    """Set one DWM attribute.  True on S_OK.

    ``c_uint`` is required for both the attribute id and the value: on Windows
    ``c_int`` aliases ``c_long`` and ctypes rejects it for this signature.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        v = ctypes.c_uint(value)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(attr), ctypes.byref(v), ctypes.sizeof(v)
        )
        return hr == 0
    except Exception:
        return False


def _dwm_get(hwnd: int, attr: int) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        v = ctypes.c_uint(0xFFFFFFFF)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), ctypes.c_uint(attr), ctypes.byref(v), ctypes.sizeof(v)
        )
        return v.value if hr == 0 else None
    except Exception:
        return None


def set_window_alpha(window: Any, alpha: float) -> bool:
    """Whole-window alpha via WS_EX_LAYERED (0..1).

    Note this fades the window *and its children* uniformly, including text --
    which is why the panel's own opacity is expressed with ``rgba()`` on the
    backgrounds instead, and this is only used as a coarse fallback.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = _hwnd(window)
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (ex & WS_EX_LAYERED):
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        a = int(max(0, min(255, round(alpha * 255))))
        return bool(u.SetLayeredWindowAttributes(hwnd, 0, a, LWA_ALPHA))
    except Exception:
        return False


def apply_floating(window: Any, *, theme: str = "dark", opaque: bool = False) -> dict[str, Any]:
    """Apply the Windows 11 floating-card chrome.

    ``opaque=True`` asks for a solid surface (no acrylic), which is the right
    answer when the user wants a predictable background or is compositing over
    something busy.  Icons/labels keep their own contrast either way.

    Returns what actually applied, for logs and tests -- never raises.
    """
    if sys.platform != "win32":
        return {"supported": False}
    hwnd = _hwnd(window)
    if not hwnd:
        return {"supported": False, "error": "window handle not found"}

    result: dict[str, Any] = {"supported": True, "hwnd": hwnd}
    result["dark_mode"] = _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 0 if theme == "light" else 1)
    result["rounded"] = _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
    result["corner_readback"] = _dwm_get(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE)
    result["border"] = _dwm_set(hwnd, DWMWA_BORDER_COLOR, BORDER_RGB.get(theme, BORDER_RGB["dark"]))

    want = DWMSBT_NONE if opaque else (DWMSBT_MICA if theme == "light" else DWMSBT_ACRYLIC)
    result["backdrop_set"] = _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, want)
    result["backdrop_readback"] = _dwm_get(hwnd, DWMWA_SYSTEMBACKDROP_TYPE)
    result["backdrop_wanted"] = want
    return result
