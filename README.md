# sprawl-guard

Claude Code hooks that keep your project free of `train_v2.py`, `paper_final.tex`,
`IMPLEMENTATION_SUMMARY.md`, `utils2.py`, stray `results.csv` files and throwaway `debug_*.py`
scripts.

Rules in CLAUDE.md fade in long sessions and after compaction. Hooks run on every file write,
however deep the session is. The checks run locally, cost no tokens, and make no network calls.

## What it does

- Checks every new file before it is written, and blocks or redirects versioned copies, backups,
  near-duplicates, one-off scripts, report files and badly named files.
- Gives Claude the workspace rules and a list of existing files at every session start, and the
  same rules to subagents.
- Counts the files created, edited and deleted at the end of each turn, including changes made
  through shell commands.
- Checks the docs Claude writes for wording that reads as machine-written (em dashes,
  "not X, but Y" frames, stock phrases) and asks it to rewrite those spots.
- `/tidy` cleans up sprawl that already exists, with your approval for every change.

## Install

Requires Python 3.8+ and git. Install on every machine where you run Claude Code.

**Plugin** (recommended). In Claude Code:

```
/plugin install sprawl-guard --marketplace nyla111/sprawl-guard
```

On Claude Code before 2.1.275, run `/plugin marketplace add nyla111/sprawl-guard`, then
`/plugin install sprawl-guard@sprawl-guard`. Restart Claude Code afterwards. On Windows the
plugin needs Git Bash; without it, use the manual install.


**Manual.** Clone to a permanent folder and run the installer:

```bash
git clone https://github.com/nyla111/sprawl-guard.git ~/tools/sprawl-guard
cd ~/tools/sprawl-guard
python3 install.py                          # all projects
python3 install.py --project /path/to/repo  # one project
```

Use `python` on Windows. The installer keeps your existing settings and writes a backup first.
Restart Claude Code and run `/hooks` to confirm.

Use one install method, not both.

## Commands

| Command | Plugin name | What it does |
|---|---|---|
| `/tidy [folder]` | `/sprawl-guard:tidy` | Lists existing sprawl and merges or removes it with you, one group at a time, after a local git checkpoint |
| `/sprawl [profile] [--global]` | `/sprawl-guard:sprawl` | Shows or sets the profile: `relaxed`, `default`, `strict` or `off` |

From a terminal: `python3 sprawl_guard.py tidy`, `profile` or `prose FILE...`. The `prose`
command exits with 1 when it finds something, so it works in CI.

## Rules for new files

Each rule has a mode: `deny` (blocked; Claude fixes it itself), `confirm` (blocked; Claude asks
you), `ask` (permission prompt), `warn` (allowed with a note) or `off`.

| Rule | Example | Default |
|---|---|---|
| `versioned_files` | `train_v2.py` next to `train.py` | `deny`: merge into `train.py` |
| `backup_files` | `train_backup.py`, `paper (1).tex` | `deny` |
| `versioned_no_base` | `resnet_v2.py` with no `resnet.py` | `warn` |
| `similar_files` | new file 60%+ identical to an existing one | `deny`: use a config entry or argument |
| `sibling_names` | `train_resnet.py` next to `train.py` | `warn` |
| `report_files` | `SUMMARY.md`, `final_report.txt` | `confirm` |
| `other_docs` | `design.md` | `warn` |
| `oneoff_scripts` | `debug_*`, `tmp_*`, `scratch*` | `deny`: use the session scratchpad |
| `long_names` | over 30 characters or 3 separators | `deny` |
| `generic_names` | `utils2`, `misc`, dates in names | `deny` |
| `outputs_files` | `model.pt`, `train.log` outside `outputs/` | `warn`: save under `outputs/<run>/` |
| `root_files` | new code at the root when `src/` exists | `off` |
| `file_budget` | more than 10 new files in a session | `ask` |

If you name the file in your prompt or ask for "a new version" or "a separate file", `deny`
becomes `confirm`, so you decide. Backups and protected paths stay denied.

Editing existing files outside protected paths is always allowed, as are standard repo files (README, LICENSE,
CHANGELOG, CLAUDE.md, ...) and anything in the scratchpad, `.git`, `node_modules`, `venv` and
similar folders. Nothing is checked inside the outputs folder.

## Configuration

Put the keys you want to change in `~/.claude/sprawl-guard.json` (all projects) or
`<project>/.claude/sprawl-guard.json` (one project, takes precedence).
[`sprawl-guard.example.json`](sprawl-guard.example.json) lists the main options; all of them,
including the word lists, are at the top of `sprawl_guard.py`.

| Key | Default | Purpose |
|---|---|---|
| `profile` | `default` | `relaxed` turns most denies into warnings; `strict` tightens rules and the prose check. Your own keys override it |
| `protected_paths` | `[]` | Never created, edited or overwritten, e.g. `["data/raw/**", "results/final"]` |
| `outputs_dir` | `outputs` | Where logs, checkpoints and results belong |
| `allow_paths` | `[]` | Globs that are always allowed, e.g. `["docs/**"]` |
| `git_checkpoint` | `true` | Commit the original locally before Claude merges into it (never pushes) |
| `turn_check` | `summary` | End-of-turn message: `summary`, `list` (with file names), `force` (Claude must clean up first) or `off` |
| `reminder` | `session` | When Claude gets the rules: every session start, only after `compact`, `always`, or `off` |
| `file_index` | `true` | List existing files at session start (folder counts above 300 files) |
| `prose` | `warn` | Prose check: `warn`, `strict` or `off` |
| `prose_allow` | `[]` | Words never flagged, e.g. `["seamless"]` |

**Unattended runs.** Write "work autonomously", "overnight" or `#unattended` in your prompt, or
set `SPRAWL_GUARD_UNATTENDED=1`. `confirm` and `ask` then allow the file and log it to
`~/.claude/sprawl-guard.log` instead of waiting for you. `deny` still applies.

**Prose check.** Scans only the text Claude just wrote into `.md`, `.rst` or `.txt` files.
Code, quotes, links and front matter are skipped, and field terms such as "robust" are not
flagged. It removes common surface patterns; it does not make text undetectable.

## Turn it off

- One session: start Claude Code with `SPRAWL_GUARD=off`.
- One project: `/sprawl off`.
- Uninstall: `/plugin uninstall sprawl-guard`, or `python3 install.py --uninstall`.

## Limits

- Files created through shell commands are counted at the end of the turn, not blocked.
  Protected paths cover Claude's file tools only.
- Detection uses name, content and keyword heuristics. Adjust the word lists if they misfire.
- The prose check is a fixed pattern list and does not check chat replies.

## Development

Run the tests with `python3 -m unittest test_sprawl_guard`. The plugin files in `hooks/` and
`skills/` are generated with `python3 install.py --build-plugin`, and a test fails if they are
out of date. Set `SPRAWL_GUARD_DEBUG=1` to log each decision. See [CHANGELOG.md](CHANGELOG.md)
for release notes, including when to re-run the installer.

## License

[MIT](LICENSE)
