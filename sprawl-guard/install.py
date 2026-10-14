#!/usr/bin/env python3
"""Install or remove sprawl-guard hooks in a Claude Code settings file.

  python install.py                      # all your projects   (~/.claude/settings.json)
  python install.py --project PATH       # one project only    (PATH/.claude/settings.local.json)
  python install.py --uninstall [...]    # remove sprawl-guard hooks again

Uses the absolute path of the Python running this script, so it works the same on
Windows ("python") and Linux ("python3"). Existing settings are kept, and a backup
is written next to the file before any change.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "sprawl_guard.py"
MARKER = "sprawl_guard.py"

# (event, matcher or None, script argument, timeout seconds)
HOOKS = [
    ("PreToolUse", "Write", "pre-write", 20),
    ("UserPromptSubmit", None, "prompt", 10),
    ("SessionStart", None, "session-start", 15),
    ("Stop", None, "stop", 15),
]


def is_ours(group: dict) -> bool:
    for h in group.get("hooks", []):
        if any(MARKER in str(a) for a in [h.get("command", "")] + list(h.get("args", []))):
            return True
    return False


def strip_ours(settings: dict) -> None:
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        kept = [g for g in hooks[event] if not is_ours(g)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def add_ours(settings: dict) -> None:
    hooks = settings.setdefault("hooks", {})
    for event, matcher, arg, timeout in HOOKS:
        group = {"hooks": [{
            "type": "command",
            "command": sys.executable,
            "args": [str(SCRIPT), arg],
            "timeout": timeout,
        }]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", metavar="PATH", help="install for one project only")
    ap.add_argument("--uninstall", action="store_true", help="remove sprawl-guard hooks")
    args = ap.parse_args()

    if args.project:
        target = Path(args.project).expanduser().resolve() / ".claude" / "settings.local.json"
    else:
        target = Path.home() / ".claude" / "settings.json"

    settings: dict = {}
    if target.exists():
        try:
            settings = json.loads(target.read_text(encoding="utf-8") or "{}")
        except ValueError:
            print(f"Could not parse {target}; fix or move it, then run again.")
            return 1
        backup = target.with_name(target.name + ".sprawl-guard.bak")
        shutil.copy2(target, backup)
        print(f"Backup written: {backup}")

    strip_ours(settings)  # makes re-running install idempotent
    if not args.uninstall:
        add_ours(settings)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    action = "Removed from" if args.uninstall else "Installed into"
    print(f"{action} {target}")
    if not args.uninstall:
        print(f"Python: {sys.executable}")
        print("Restart Claude Code (hook settings are read at session start), then run /hooks to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
