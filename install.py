#!/usr/bin/env python3
"""Install or remove sprawl-guard hooks in a Claude Code settings file.

  python install.py                      # all your projects   (~/.claude/settings.json)
  python install.py --project PATH       # one project only    (PATH/.claude/settings.local.json)
  python install.py --uninstall [...]    # remove sprawl-guard hooks again
  python install.py --build-plugin       # maintainers: regenerate hooks/hooks.json and skills/

Also installs the /tidy and /sprawl commands as skills (~/.claude/skills/<name>/SKILL.md, or
PATH/.claude/skills/<name>/SKILL.md with --project). An existing skill of the same name that
sprawl-guard did not create is left alone.

Installing as a Claude Code plugin instead (/plugin install) needs none of this; do not use both.

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
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", "pre-write", 20),
    ("PostToolUse", "Write|Edit|MultiEdit", "post-write", 10),
    ("UserPromptSubmit", None, "prompt", 10),
    ("SessionStart", None, "session-start", 15),
    ("SubagentStart", None, "subagent-start", 10),
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


SKILL_MARKER = "<!-- installed by sprawl-guard -->"

SKILL_HEAD = """---
name: {name}
description: {description}
argument-hint: "{hint}"
disable-model-invocation: true
allowed-tools:
{allowed}
---
{marker}

"""

TIDY_BODY = """# /tidy

Scan results from sprawl-guard:

!`{invoke}`

Help the user clean this up. They are present and decide every change.

1. If the scan found nothing, say so in one line and stop.
2. Give a short overview: how many groups of each kind, nothing more.
3. Go through the groups in the order listed. For each group, read the files and compare them
   (a diff is fine), then say in two or three lines what differs and what you propose.
   Ask the user with your multiple-choice question tool if you have one (up to 4 groups per call),
   putting your recommendation first:
   - Versioned copies and backups: merge into the original (recommended when one exists:
     keep the original name, bring over what the copies add, drop what they supersede);
     make a copy the main version under the original's name; keep both; skip.
     With no unversioned original, propose which version becomes the main file and its plain name.
   - Near-copies: fold the differences into one file as a config entry, argument or option;
     keep both; skip.
   - One-off scripts: delete; keep; skip.
   - Report files: delete; fold the parts worth keeping into README or existing docs; keep; skip.
4. Before changing any file in a group:
   - In a git repo: commit a local checkpoint of only that group's files
     (`git add -- <files>` then `git commit -m "sprawl-guard: checkpoint before tidy" -- <files>`).
     Never push. Skip the checkpoint if a merge or rebase is in progress, and tell the user.
   - Not a git repo: do not delete anything. Move removed files into the session scratchpad
     (or a temporary folder outside the project) so they stay recoverable, and say where.
5. After merging or removing a file, search the project for its name (imports, configs, scripts,
   README) and update those references to the file that stays.
6. Never create new files while tidying (no _merged copies, no report files), and never touch
   protected paths or files the scan did not list.
7. When done, run the project's tests if it has any. Then reply in chat with a short list of what
   changed and which checkpoint commit holds the old versions. No summary file.
"""

SPRAWL_BODY = """# /sprawl

!`{invoke}`

Tell the user the result above in one or two lines. Do not edit config files yourself.
Profiles: relaxed (most rules only warn), default, strict (tighter rules, file names in the
turn summary, strict prose check), off (sprawl-guard does nothing in this project).
"""

SKILLS = {
    "tidy": {
        "description": "Find existing file sprawl in this project (_v2/_final copies, backups, near-copies, "
                       "one-off scripts, report files) and help merge or remove it, asking before every change.",
        "hint": "[folder]", "cli": "tidy", "body": TIDY_BODY,
    },
    "sprawl": {
        "description": "Show or set the sprawl-guard profile for this project (relaxed, default, strict, off); "
                       "add --global for all projects.",
        "hint": "[relaxed|default|strict|off] [--global]", "cli": "profile", "body": SPRAWL_BODY,
    },
}


def q(p: str) -> str:
    return f'"{p}"' if " " in p else p


def render_skill(name: str, plugin: bool = False) -> str:
    spec = SKILLS[name]
    if plugin:
        # Claude Code fills in ${CLAUDE_PLUGIN_ROOT}; python3 first, python where only that exists (Windows)
        script = '"${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"'
        runners = [f"python3 {script}", f"python {script}"]
        invoke = " || ".join(f"{r} {spec['cli']} $ARGUMENTS" for r in runners)
    else:
        runners = [f"{q(Path(sys.executable).as_posix())} {q(SCRIPT.as_posix())}"]
        invoke = f"{runners[0]} {spec['cli']} $ARGUMENTS"
    allowed = "\n".join(f"  - Bash({r}*)" for r in runners)
    head = SKILL_HEAD.format(name=name, description=spec["description"], hint=spec["hint"],
                             allowed=allowed, marker=SKILL_MARKER)
    return head + spec["body"].format(invoke=invoke)


def skill_dir(project: str | None, name: str) -> Path:
    base = Path(project).expanduser().resolve() / ".claude" if project else Path.home() / ".claude"
    return base / "skills" / name


def install_skills(project: str | None) -> list[str]:
    notes = []
    for name in SKILLS:
        target = skill_dir(project, name) / "SKILL.md"
        if target.exists() and SKILL_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
            notes.append(f"Skipped /{name}: {target} already exists and was not made by sprawl-guard.")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_skill(name), encoding="utf-8")
        notes.append(f"Installed /{name} command: {target}")
    return notes


def remove_skills(project: str | None) -> list[str]:
    notes = []
    for name in SKILLS:
        target = skill_dir(project, name) / "SKILL.md"
        if not target.exists() or SKILL_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
            continue
        target.unlink()
        try:
            target.parent.rmdir()
        except OSError:
            pass
        notes.append(f"Removed /{name} command: {target}")
    return notes


def plugin_hooks() -> dict:
    """hooks/hooks.json for the plugin install: shell form, python3 with a python fallback."""
    hooks: dict = {}
    for event, matcher, arg, timeout in HOOKS:
        script = '"${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"'
        group = {"hooks": [{"type": "command", "timeout": timeout,
                            "command": f"python3 {script} {arg} || python {script} {arg}"}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return {"hooks": hooks}


def plugin_files() -> dict[Path, str]:
    files = {HERE / "hooks" / "hooks.json": json.dumps(plugin_hooks(), indent=2) + "\n"}
    for name in SKILLS:
        files[HERE / "skills" / name / "SKILL.md"] = render_skill(name, plugin=True)
    return files


def build_plugin() -> int:
    for path, text in plugin_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path.relative_to(HERE).as_posix()}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", metavar="PATH", help="install for one project only")
    ap.add_argument("--uninstall", action="store_true", help="remove sprawl-guard hooks")
    ap.add_argument("--build-plugin", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.build_plugin:
        return build_plugin()

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
    for note in (remove_skills(args.project) if args.uninstall else install_skills(args.project)):
        print(note)
    if not args.uninstall:
        print(f"Python: {sys.executable}")
        print("Restart Claude Code (hook settings are read at session start), then run /hooks to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
