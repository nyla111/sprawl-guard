# sprawl-guard

Stops Claude Code from cluttering your workspace with `train_v2.py`, `train_resnet.py`,
`paper_final.tex`, `IMPLEMENTATION_SUMMARY.md`, `utils2.py` and throwaway `debug_*.py` scripts.

Rules in CLAUDE.md fade in long sessions and after compaction. sprawl-guard uses Claude Code
**hooks**, so the checks run on every new file, however deep the session is. The checks run
locally and cost no tokens; Claude only sees a short message when a rule fires.

## How it decides

Every rule has a mode:

| Mode | What happens |
|---|---|
| `deny` | Blocked. Claude fixes it itself: merges into the original, renames, or uses the scratchpad. |
| `confirm` | Blocked, and Claude must ask you with options. If you choose "create anyway", the retry gets a one-time permission prompt. |
| `ask` | A normal Claude Code permission prompt. |
| `warn` | Allowed. Claude gets a one-line note. |
| `off` | Rule disabled. |

**If you named the file in your prompt** (or asked for "a new version", "a separate file",
"keep the original"...), rules that would `deny` become `confirm`: you get asked instead of
Claude silently doing something else. Backups are the exception and are always denied.

**Unattended mode.** Say it in your prompt ("work autonomously", "I'll be away", "overnight",
"don't ask me", "until done", or the tag `#unattended`), or set the environment variable
`SPRAWL_GUARD_UNATTENDED=1`. While on, `confirm` and `ask` never interrupt: the file is allowed
and logged to `~/.claude/sprawl-guard.log`, and the end-of-turn message lists it. `deny` rules
still apply because Claude can fix those without you. It switches off with your next normal prompt.

## Rules

| Rule | Example | Default |
|---|---|---|
| `versioned_files` | `train_v2.py` when `train.py` exists | `deny`: merge into `train.py`, remove superseded parts |
| `backup_files` | `train_backup.py`, `paper (1).tex` | `deny`, even if named |
| `versioned_no_base` | `resnet_v2.py` with no `resnet.py` | `warn` (often a real model name) |
| `similar_files` | new file 60%+ identical to an existing one | `deny`: add a config entry or argument instead |
| `sibling_names` | `train_resnet.py` next to `train.py`, different content | `warn` |
| `report_files` | `SUMMARY.md`, `final_report.txt`, `ChangesSummary.md` | `confirm` |
| `other_docs` | `design.md` | `warn` |
| `oneoff_scripts` | `debug_*`, `tmp_*`, `scratch*`, `quick_test*` | `deny`: use the session scratchpad |
| `long_names` | over 30 characters or more than 3 `_`/`-`/`.` separators | `deny`: rename shorter |
| `generic_names` | `utils2`, `misc`, `helper_new`, `main2`, dates in names | `deny` |
| `file_budget` | more than 10 new files in one session | `ask` |

**Git checkpoint.** Before Claude merges into an original file, sprawl-guard makes a local
commit of just that file (`sprawl-guard: checkpoint train.py before in-place edit`), so the old
version stays recoverable. It never pushes, never commits other staged files, and skips
during a merge or rebase. Turn off with `"git_checkpoint": false`.

**Always allowed:** editing existing files; standard repo files (README, LICENSE, CHANGELOG,
CLAUDE.md, AGENTS.md, requirements.txt, ...); anything outside the project, in the scratchpad,
or in `.git`, `.claude`, `node_modules`, `venv`, `build` and similar folders.

**Context.** No per-prompt reminder. The rules are restated only after compaction
(about 40 tokens) and inside each block message. Set `"reminder": "always"` to restate them
on every prompt, or `"off"`.

**End of turn.** You see a list of files created that turn (including ones made through Bash)
and anything allowed in unattended mode. This is shown to you, not added to Claude's context.

## Install

Requires Python 3.8+ and nothing else. Same steps on Windows and Linux.

```bash
python install.py                          # all your projects
python install.py --project /path/to/repo  # one project only
```

Then **restart Claude Code** (`/exit`, then `claude`) and run `/hooks` to check the four hooks.

The installer keeps your existing settings, writes a `.sprawl-guard.bak` backup first, and
records the absolute path of the Python you ran it with. Re-running it is safe and replaces
the old entries. The hooks point to this folder: if you move it, run the installer again.

## Configure

Copy `sprawl-guard.example.json` to `~/.claude/sprawl-guard.json` (all projects) or
`<project>/.claude/sprawl-guard.json` (one project, overrides the global file), and keep only
the keys you want to change. `allow_paths` takes globs that are always allowed, e.g.
`["docs/**"]`. Word lists (`report_keywords`, `version_suffixes`, `backup_suffixes`,
`oneoff_prefixes`, `generic_patterns`, `request_keywords`, `unattended_keywords`, ...) can be
replaced the same way; see the top of `sprawl_guard.py`.

## Turn it off

- One session: start Claude Code with `SPRAWL_GUARD=off`.
- One project: `{"enabled": false}` in `<project>/.claude/sprawl-guard.json`.
- Completely: `python install.py --uninstall` (add `--project PATH` if installed per project).

## Test

```bash
python -m unittest test_sprawl_guard -v
```

Set `SPRAWL_GUARD_DEBUG=1` to print each decision to the hook debug log.

## Known limits (v0.2)

- Files created through Bash (`cat > x.md`) are only reported at the end of the turn, not blocked.
- Detection is name- and content-based heuristics. Unattended and "new version" detection use
  keyword lists; adjust them in the config if they misfire.
- Whether the one-time `ask` prompt appears in auto mode depends on Claude Code's permission
  handling; if it is auto-approved, the retry simply goes through.
