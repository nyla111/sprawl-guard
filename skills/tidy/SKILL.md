---
name: tidy
description: Find existing file sprawl in this project (_v2/_final copies, backups, near-copies, one-off scripts, report files) and help merge or remove it, asking before every change.
argument-hint: "[folder]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"*)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"*)
---
<!-- installed by sprawl-guard -->

# /tidy

Scan results from sprawl-guard:

!`python3 "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py" tidy $ARGUMENTS || python "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py" tidy $ARGUMENTS`

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
