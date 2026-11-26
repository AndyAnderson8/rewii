"""Entry point: `rewii` opens the app; `rewii <command>` runs a CLI command."""
from __future__ import annotations

import sys
from pathlib import Path


def _app_drive(argv: list[str]) -> tuple[bool, Path | None, str]:
    """Is this the "open the app" form (no arguments, ``--drive PATH`` or
    ``--drive=PATH``)? Returns ``(is_app, drive, error)``."""
    if not argv:
        return True, None, ""
    first = argv[0]
    if first == "--drive":
        if len(argv) == 1:
            return True, None, "--drive needs a path (the Wii drive's mount point / drive letter)"
        value, rest = argv[1], argv[2:]
    elif first.startswith("--drive="):
        value, rest = first[len("--drive="):], argv[1:]
    else:
        return False, None, ""
    if rest:
        return True, None, ("to run a command on a drive put --drive after it, e.g. "
                            f"'rewii {rest[0]} ... --drive {value}'")
    if not value:
        return True, None, "--drive needs a path (the Wii drive's mount point / drive letter)"
    p = Path(value).expanduser()
    if not p.is_dir():
        return True, None, f"the drive {p} isn't there (is it plugged in and mounted?)"
    return True, p, ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    is_app, drive, err = _app_drive(argv)
    if not is_app:
        from .cli import main as cli_main
        return cli_main(argv)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    from .tui.app import run
    run(drive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
