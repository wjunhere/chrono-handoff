"""Entry point for the bundled conductor (`chrono-core`).

PyInstaller freezes a *script*, not a console-script entry point, and this
package uses relative imports -- so ``chrono_handoff/cli.py`` cannot be the
target directly (running it as a script fails on ``from .conductor import ...``).
This file is the whole reason it exists.
"""

from chrono_handoff.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
