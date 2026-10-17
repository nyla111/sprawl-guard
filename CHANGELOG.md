# Changelog

## v0.3.0 - 2026-10-17

**Re-run the installer after updating** (`python3 install.py`, plus `--project PATH` if you
installed per project), then restart Claude Code. The write hook now also watches Claude's Edit
tools, which protected paths need.

Changed:
- The end-of-turn check is now `turn_check` (was `stop_check`, which still works). New default
  mode `summary` prints one line with a count and no file names; `list` adds the names.
- The end-of-turn scan skips hidden folders (`.gemini`, `.antigravity-server`, `.cache`, ...),
  the outputs folder, and environment or tracking folders (`miniconda3`, `wandb`, `mlruns`, ...).
  This fixes the "4578 new files" message when Claude Code runs in a home folder.
- `force` mode switches itself off while unattended mode is on.

Added:
- Orphan check: the turn summary counts new code files that nothing in the project references.
- Outputs rule (`outputs_files`, warn): logs, checkpoints and results files are steered to
  `outputs/<run>/`. No rules apply inside that folder.
- Root-folder rule (`root_files`, off by default): flags new code files at the project root when
  code folders already exist.
- Protected paths (`protected_paths`): folders Claude may never create, edit or overwrite files in.

## v0.2.0 - 2026-10-14

- Versioned copies are merged quietly unless you named the file, in which case Claude asks you
  with options. Backups always denied, with a local git checkpoint of the original.
- Run-sprawl detection (near-copies of existing files), short and generic name rules, per-session
  file budget.
- Unattended mode detected from your prompt; reminders only after compaction.

## v0.1.0 - 2026-10-14

- First version: blocks versioned copies, report files and one-off scripts; end-of-turn file list.
