"""Which layout each per-user JSON file is in.

The point of this module is one behaviour: **a version of the program must never
overwrite a state file written by a newer version.**  Before it existed,
``settings.json`` was loaded by filtering to the fields this code knows about and
saving the result -- so a downgrade (exactly what the updater's "keep the last
installer" path exists for) would silently delete every field the older version
did not recognise, and ``calibration.json`` -- real measurements of a person's
timing, the one thing here that cannot be recomputed -- had no protection at all.

Three states, and only three:

===========  ==========================================================
older        migrate forward, keeping the original next to it
same         use it
newer        use what is readable, **refuse to write**, and say why
===========  ==========================================================

The refusal is deliberately not a crash.  ``settings.py`` states the rule for
this component: a corrupt or unusable settings file must never be able to stop a
cue from firing, because the whole point is that it works when the moment
arrives.  So an unreadable-layout file degrades the *numbers* (defaults) and
blocks the *write* (no data loss), which is the one combination that neither
destroys the user's data nor silently lies about the state.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: The layout the current code writes.
#:
#: Bump this only when a file's *meaning* changes: a field renamed, a unit
#: changed, a bool that became a number.  Adding a field with a safe default does
#: not need a bump, because the loaders already ignore what they do not know and
#: default what is missing.
SCHEMA = 1

#: ``{from: payload -> payload}``, applied in order until the payload is current.
#:
#: Empty, and honestly so: no payload change has shipped yet.  The first one goes
#: here as the key ``1`` (schema 1 -> schema 2), and ``adopt`` will keep the
#: original before using it.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


class SchemaMismatch(RuntimeError):
    """Raised by a saver that was told not to write over a newer file."""


@dataclass(frozen=True)
class Adoption:
    """What `adopt` decided about one file on disk."""

    payload: dict[str, Any]
    declared: int
    #: The payload was transformed on the way in.
    migrated: bool
    #: Written by a newer version: read what you can, never write it back.
    #: Do not write this file back.  Two ways to earn it: it came from a newer
    #: version, or it is older but this build has no migration for it.  The name
    #: is the invariant, and `reason` says which case it is.
    refuse_write: bool
    #: One line for `doctor` and the panel.  Empty when there is nothing to say.
    reason: str
    #: Where the pre-migration original was kept, if one was written.
    backup: Path | None = None


def declared_schema(raw: dict[str, Any]) -> int:
    """Which schema a file claims.

    No marker means the current schema, not "unknown": the marker was added after
    the layout was already fixed, so a file without one means exactly what schema
    1 means.  Reading it as 0 would invent a migration for every file that exists.

    ``calibration.json`` used to declare a ``version`` instead -- written on every
    save, read by nobody, a number that looked like versioning and did nothing.
    It is honoured here so a file from that era keeps saying what it meant.
    """
    if "schema" in raw:
        return int(raw["schema"])
    if "version" in raw:
        return int(raw["version"])
    return SCHEMA


def backup(path: Path, declared: int) -> Path | None:
    """Keep the pre-migration file as ``<name>.v<declared>.bak``.

    Written once, never overwritten: the original of the *earliest* migration is
    the one worth having, and a later migration must not replace it with an
    already-half-transformed file.
    """
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.v{declared}.bak")
    if target.exists():
        return target
    shutil.copy2(path, target)
    return target


def adopt(raw: dict[str, Any], *, path: Path, name: str) -> Adoption:
    """Decide what to do with one parsed state file.

    Two side effects worth stating, because both land on disk:

    * when a payload actually has to be migrated, the file is copied aside
      *here*, before the migrated payload is returned -- a backup that only
      happened when the caller remembers to save would be missing in exactly the
      case that matters, a migration that goes wrong on the way in;
    * the migrated payload is *returned*, not written.  The file converges on the
      next save, and until then a restart migrates it again (whose steps are
      expected to be idempotent).
    """
    declared = original = declared_schema(raw)

    if declared > SCHEMA:
        return Adoption(
            payload=raw,
            declared=declared,
            migrated=False,
            refuse_write=True,
            reason=(
                f"{name} was written by a newer chrono-handoff (schema {declared}, "
                f"this build writes {SCHEMA}); it will not be overwritten"
            ),
        )

    payload = dict(raw)
    kept: Path | None = None
    while declared < SCHEMA:
        step = MIGRATIONS.get(declared)
        if step is None:
            # Refusing to guess is the whole point: writing half-migrated state
            # is how a schema change turns into lost measurements.
            return Adoption(
                payload=raw,
                declared=declared,
                migrated=False,
                refuse_write=True,
                reason=(
                    f"{name} is schema {declared} and this build has no migration "
                    f"from {declared} to {declared + 1}; it will not be overwritten"
                ),
            )
        if kept is None:
            kept = backup(path, declared)
        payload = step(payload)
        declared += 1

    if kept is not None:
        payload["schema"] = SCHEMA
        return Adoption(
            payload=payload,
            declared=original,
            migrated=True,
            refuse_write=False,
            reason=f"{name} migrated to schema {SCHEMA}; the original is at {kept.name}",
            backup=kept,
        )

    return Adoption(payload=payload, declared=original, migrated=False,
                    refuse_write=False, reason="")


def envelope(**data: Any) -> dict[str, Any]:
    """A payload plus the schema marker, ready to be written.

    The marker is added on the way out rather than kept in the data, so a client
    posting a settings patch cannot set it and the UI never sees it as a setting.
    Keys are taken as keywords so both callers read the same way:
    ``envelope(**asdict(settings))`` and ``envelope(items={...})``.
    """
    return {**data, "schema": SCHEMA}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a state file the way every saver here does: tmp + atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
