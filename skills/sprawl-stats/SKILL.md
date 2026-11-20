---
name: sprawl-stats
description: Show what sprawl-guard caught in this project: warnings and blocks per rule, blocked files created anyway, prose findings.
argument-hint: "[--days N] [--all]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"*)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"*)
---
<!-- installed by sprawl-guard -->

# /stats

!`python3 "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py" stats $ARGUMENTS || python "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py" stats $ARGUMENTS`

Tell the user in a few lines what stands out above: which rules fire most, any blocked files that
were created anyway, and any rule that looks like a false positive (many warnings on files they
clearly wanted). Suggest a config change only if the numbers support it. Do not edit config files.
