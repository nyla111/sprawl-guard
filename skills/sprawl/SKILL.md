---
name: sprawl
description: Show or set the sprawl-guard profile for this project (relaxed, default, strict, off); add --global for all projects.
argument-hint: "[relaxed|default|strict|off] [--global]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"*)
  - Bash(python "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py"*)
---
<!-- installed by sprawl-guard -->

# /sprawl

!`python3 "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py" profile $ARGUMENTS || python "${CLAUDE_PLUGIN_ROOT}/sprawl_guard.py" profile $ARGUMENTS`

Tell the user the result above in one or two lines. Do not edit config files yourself.
Profiles: relaxed (most rules only warn), default, strict (tighter rules, file names in the
turn summary, strict prose check), off (sprawl-guard does nothing in this project).
