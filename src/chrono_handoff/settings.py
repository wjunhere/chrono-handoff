"""Settings: one small JSON file, shared by the panel and the native window.

Kept deliberately tiny and human-editable.  Two rules encoded here:

* **Defaults must be safe.**  A missing or corrupt file must never stop a cue
  from firing -- the whole point of the component is that it works when the
  moment arrives, so a parse error falls back to defaults rather than raising.
* **The UI and the conductor must agree on the numbers.**  The panel reads
  these to pre-fill its controls; the conductor reads them as the default plan.
  If they disagreed, the settings screen would be a lie.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .home import chrono_home

#: The smallest window the shells will allow, in logical pixels.  Below this the
#: countdown stops being readable at arm's length, which is where it has to be
#: read.
#:
#: It lives here because three places have to agree on it and previously did not:
#: the pywebview host was given a literal 380 while its own constant, the resize
#: clamp and the Tauri shell all said 360, so the two shells disagreed about how
#: short the window could get -- and this file's own clamp floored at 380x240,
#: numbers matching no window at all.  A settings file could therefore claim a
#: size the window would refuse to take.
MIN_LOGIC_W, MIN_LOGIC_H = 320, 360


def default_settings_path() -> Path:
    return chrono_home() / "settings.json"


@dataclass
class Settings:
    # ---- cue timing -------------------------------------------------------
    lead_s: float = 3.0            #: countdown length before the GO beat
    ioi_ms: float = 500.0          #: beat interval. ~500ms is where people entrain most easily
    tail_beats: int = 6            #: beats after GO. Keeps the pulse so the human can entrain
    default_target_in_s: float = 6.0
    duration_s: float = 30.0       #: hard budget

    # ---- audio ------------------------------------------------------------
    volume: float = 0.6            #: 0..1
    accent_hz: float = 1568.0      #: GO tone
    tick_hz: float = 880.0         #: ordinary beat
    accent_ms: float = 90.0
    tick_ms: float = 35.0
    audio_enabled: bool = True
    count_in_voice: bool = False   #: reserved; spoken count-in is not implemented

    # ---- window -----------------------------------------------------------
    always_on_top: bool = True
    opacity: float = 0.96          #: 0.4..1.0
    width: int = 460
    height: int = 620
    x: int | None = None
    y: int | None = None
    frameless: bool = True         #: custom title bar, so it can be a small widget
    #: Windows 11 floating chrome: rounded window, acrylic/mica backdrop, themed
    #: border.  Falls back to a solid window automatically where unsupported.
    glass: bool = True
    #: Let the desktop show through the window.  Required for the acrylic
    #: backdrop to be visible at all; without it the card is painted over an
    #: opaque surface and "floating" is only an illusion.
    transparent: bool = True
    remember_position: bool = True

    # ---- interaction ------------------------------------------------------
    hotkey: str = "space"          #: space | enter | f8 ...
    confirm_dismiss: bool = False
    theme: str = "dark"            #: dark | light | amber (high contrast for dim rooms)
    #: Retired.  Kept as a tolerated field so an existing settings.json still
    #: loads; the panel no longer offers zoom because the layout is now
    #: responsive (clamp/vmin) and the window resizes for real -- a manual zoom
    #: slider on top of that was a workaround for not having resizing.
    scale: float = 1.0

    # ---- calibration (off by default; see docs/component.md §6) -----------
    apply_learned_offset: bool = False

    extra: dict[str, Any] = field(default_factory=dict)

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Settings":
        p = Path(path) if path else default_settings_path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # A corrupt settings file must not be able to break a cue.
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known}
        try:
            return cls(**kwargs)
        except Exception:
            return cls()

    def save(self, path: Path | str | None = None) -> Path:
        p = Path(path) if path else default_settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return p

    def update(self, patch: dict[str, Any], path: Path | str | None = None) -> "Settings":
        """Apply a partial update, ignoring unknown or mistyped values.

        Type coercion is intentional: the values arrive from a web form, where
        everything is a string or a float, and silently accepting a string where
        a number belongs would produce a cue that runs at ``500`` seconds
        instead of 500 ms.
        """
        for key, value in (patch or {}).items():
            if key not in self.__dataclass_fields__:
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, int) and current is not None:
                    value = int(float(value))
                elif isinstance(current, str):
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(self, key, value)
        self._clamp()
        return self

    def _clamp(self) -> None:
        self.lead_s = min(30.0, max(0.0, self.lead_s))
        self.ioi_ms = min(2000.0, max(100.0, self.ioi_ms))
        self.tail_beats = int(min(32, max(0, self.tail_beats)))
        self.default_target_in_s = min(600.0, max(0.5, self.default_target_in_s))
        self.duration_s = min(3600.0, max(1.0, self.duration_s))
        self.volume = min(1.0, max(0.0, self.volume))
        self.opacity = min(1.0, max(0.35, self.opacity))
        self.scale = min(2.5, max(0.6, self.scale))
        self.width = int(min(1600, max(MIN_LOGIC_W, self.width)))
        self.height = int(min(1400, max(MIN_LOGIC_H, self.height)))
        if self.theme not in ("dark", "light", "amber"):
            self.theme = "dark"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_settings(path: Path | str | None = None) -> Settings:
    return Settings.load(path)
