"""Entry point: `rewii` opens the app; `rewii <command>` runs a CLI command."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ("--drive",):
        from .cli import main as cli_main
        return cli_main(argv)
    from pathlib import Path
    from .tui.app import run
    drive = Path(argv[1]) if len(argv) >= 2 and argv[0] == "--drive" else None
    run(drive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
