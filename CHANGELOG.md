# Changelog

## v0.5.0 - 2026-11-08

**Manual installs: re-run the installer after updating** (`python3 install.py`, plus
`--project PATH` if you installed per project) to get the two new hooks and the `/sprawl`
command, then restart Claude Code. Or switch to the plugin install; use one, not both.

Added:
- Plugin install: `/plugin install sprawl-guard --marketplace nyla111/sprawl-guard`.
- Session rules at every session start (startup, resume, `/clear`, compaction), not only after
  compaction. New `reminder` default `session`; the old behavior is `compact`.
- Subagents get the same rules when they start (`subagent_rules`).
- File index: at session start Claude gets a compact list of existing files, one line per
  folder, so it edits them instead of adding new ones (`file_index`).
- Profiles: `relaxed`, `default`, `strict` and `off` set several rules at once. Set one with
  `/sprawl strict` or `python3 sprawl_guard.py profile strict`; your own keys still win.
- Prose check (`prose`, default `warn`): text Claude writes into `.md`, `.rst` and `.txt` files
  is scanned for em dashes, "not X, but Y" frames, stock phrases, emoji and similar, and Claude
  is asked to rewrite those spots. Also on the command line: `python3 sprawl_guard.py prose FILE`.
- MIT LICENSE.

Changed:
- Hook events: six instead of four (`post-write` and `subagent-start` are new).
- The session-start context now also carries a one-line style note when the prose check is on.

## v0.4.0 - 2026-10-22

**Re-run the installer after updating** (`python3 install.py`, plus `--project PATH` if you
installed per project) to get the `/tidy` command, then restart Claude Code.

Added:
- `/tidy` command: scans the project for existing sprawl (versioned copies, backups,
  near-copies, one-off scripts, report files) and has Claude merge or remove it with you,
  group by group, with a git checkpoint first. The scan alone: `python3 sprawl_guard.py tidy`.

Changed:
- The turn summary counts files created, edited and deleted, not only created:
  `Files this turn: 3 created, 2 edited, 1 deleted`. `list` mode names each kind.
- The `[sprawl-guard]` tag is gone from the turn summary; Claude Code already puts its own
  label (`Stop says:`) in front of hook messages, and that label cannot be changed by a hook.
- The snapshot is taken when you send a prompt, so changes you make between turns are not
  counted, and it is kept in its own file next to the session state.

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
