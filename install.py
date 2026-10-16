#!/usr/bin/env python3
"""Install the /kitty-dashboard Cursor command for this clone.

Run it with the Python you want the board to use:

    python install.py              # install or refresh the command
    python install.py --uninstall  # remove it
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "kitty_dashboard" / "kitty_dashboard.py"
TEMPLATE = HERE / "cursor-command" / "kitty-dashboard.md"
COMMAND_NAME = "kitty-dashboard.md"


def render(template: str) -> str:
    python = Path(sys.executable).resolve()
    if os.name == "nt":
        run, shell = f'& "{python}" "{SCRIPT}"', "powershell"
    else:
        run, shell = f'"{python}" "{SCRIPT}"', "bash"
    return template.replace("{{RUN}}", run).replace("{{SCRIPT}}", str(SCRIPT)).replace("{{SHELL}}", shell)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commands-dir", type=Path, default=Path.home() / ".cursor" / "commands",
                        help="Cursor commands folder (default: ~/.cursor/commands)")
    parser.add_argument("--uninstall", action="store_true", help="Remove the command instead of installing it")
    args = parser.parse_args(argv)

    target = args.commands_dir.expanduser() / COMMAND_NAME

    if args.uninstall:
        if target.exists():
            target.unlink()
            print(f"Removed {target}")
        else:
            print(f"Nothing to remove at {target}")
        return 0

    if sys.version_info < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        print(f"Kitty Board needs Python {need}+; this is {sys.version.split()[0]} ({sys.executable}).", file=sys.stderr)
        return 1
    for path in (SCRIPT, SCRIPT.with_name("board.html"), TEMPLATE):
        if not path.is_file():
            print(f"Missing {path}; is this a complete clone?", file=sys.stderr)
            return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"Installed /kitty-dashboard -> {target}")
    print(f"  board script: {SCRIPT}")
    print(f"  python:       {Path(sys.executable).resolve()}")
    print("Restart Cursor or open a new chat if the command doesn't show up straight away.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
