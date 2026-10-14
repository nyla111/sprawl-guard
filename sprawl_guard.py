#!/usr/bin/env python3
"""sprawl-guard: stop Claude Code from cluttering your workspace.

One script, four hook events (pass the event as the first argument):
  pre-write      PreToolUse on Write   - judges every NEW file before it is created
  prompt         UserPromptSubmit      - records the prompt (named files, unattended mode); silent by default
  session-start  SessionStart          - snapshots the workspace; re-adds the rules after compaction
  stop           Stop                  - lists files created this turn and anything allowed while unattended

Rule modes:
  deny     blocked; Claude fixes it itself (merge into the original, rename, use the scratchpad)
  confirm  blocked; Claude must ask you with options. A retry of the same file gets a
           one-time permission prompt. Allowed and logged when unattended.
  ask      Claude Code permission prompt. Allowed and logged when unattended.
  warn     allowed; Claude gets a short note
  off      rule disabled

Standard library only, works on Windows and Linux, fails open on any internal error.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

VERSION = "0.2.0"
TAG = "sprawl-guard"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "enabled": True,
    # --- rule modes: deny | confirm | ask | warn | off ---
    "versioned_files": "deny",    # train_v2.py next to train.py -> Claude merges into train.py
    "backup_files": "deny",       # train_backup.py, paper (1).tex -> always denied, git checkpoint instead
    "versioned_no_base": "warn",  # resnet_v2.py with no resnet.py (often a real name)
    "similar_files": "deny",      # new file mostly identical to an existing one -> config entry/argument instead
    "sibling_names": "warn",      # train_resnet.py next to train.py with different content
    "report_files": "confirm",    # SUMMARY.md, final_report.txt
    "other_docs": "warn",         # any other new .md/.txt that is not a standard repo doc
    "oneoff_scripts": "deny",     # debug_x.py, tmp_test.py -> session scratchpad
    "long_names": "deny",         # names over max_name_length or max_name_separators
    "generic_names": "deny",      # utils2.py, misc.py, names containing dates
    # --- thresholds ---
    "file_budget": 10,            # new files per session before each further one needs approval (0 = off)
    "file_budget_mode": "ask",
    "similarity_threshold": 0.6,
    "similarity_min_lines": 10,
    "max_name_length": 30,
    "max_name_separators": 3,
    # --- behaviour ---
    "git_checkpoint": True,       # local commit of the original before Claude edits it in place (never pushes)
    "stop_check": "list",         # off | list | force
    "reminder": "compact",        # compact (after compaction) | always (every prompt) | off
    # --- word lists ---
    "doc_extensions": [".md", ".markdown", ".txt", ".rst", ".adoc", ".org"],
    "report_keywords": [
        "summary", "report", "notes", "findings", "progress", "status", "recap",
        "changes", "analysis", "overview", "walkthrough", "implementation",
        "fixes", "results", "review", "handoff", "explanation", "documentation",
    ],
    "version_suffixes": [
        "final", "new", "updated", "revised", "fixed", "improved", "enhanced",
        "alt", "latest", "tmp", "draft", "refactored", "modified",
    ],
    "backup_suffixes": ["backup", "bak", "orig", "old", "copy", "save", "saved"],
    "oneoff_prefixes": [
        "tmp", "temp", "scratch", "debug", "adhoc", "sandbox", "playground", "try",
    ],
    "oneoff_patterns": [r"^quick[ _\-.]?(test|check|fix|look|run|try|debug)"],
    "generic_patterns": [
        r"^(misc|stuff|things|untitled|foo|bar|baz|asdf|new|newfile|new_file|file\d*|script\d*|code\d*|test\d+|output\d*)$",
        r"^(utils?|helpers?|common|tools?|lib|functions?|main|module)[ _\-.]?(\d+|new|v\d+)$",
        r"(19|20)\d{2}[ _\-.]?(0[1-9]|1[0-2])[ _\-.]?(0[1-9]|[12]\d|3[01])",
    ],
    "request_keywords": [
        "new version", "another version", "separate file", "separate copy", "separate script",
        "keep the old", "keep the original", "keep both", "keep the current",
        "don't overwrite", "do not overwrite", "without overwriting", "without changing",
        "copy of", "duplicate",
    ],
    "unattended_keywords": [
        "unattended", "i'll be away", "i will be away", "while i'm away", "while i am away",
        "while i'm gone", "while i am gone", "autonomously", "don't ask me", "do not ask me",
        "without asking me", "no need to ask", "overnight", "until it's done", "until it is done",
        "until done", "until finished", "i'm going to sleep", "going offline",
    ],
    "allowed_names": [
        "readme", "license", "changelog", "contributing", "code_of_conduct",
        "security", "claude", "agents", "notice", "authors", "requirements",
    ],
    "ignore_dirs": [
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        "env", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build",
        ".idea", ".vscode", ".ipynb_checkpoints", ".claude",
    ],
    "allow_paths": [],            # globs always allowed, relative with forward slashes, e.g. "docs/**"
}

SEVERITY = {"off": 0, "warn": 1, "ask": 2, "confirm": 3, "deny": 4}

REMINDER = (
    "Workspace rules: edit existing files instead of creating copies or variants (_v2, per-run scripts); "
    "put run variations in configs or arguments; summaries go in the chat reply; "
    "throwaway scripts go in the session scratchpad."
)


def guard_home() -> Path:
    return Path(os.environ.get("SPRAWL_GUARD_HOME") or (Path.home() / ".claude"))


def load_config(project_dir: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    for path in (guard_home() / "sprawl-guard.json", project_dir / ".claude" / "sprawl-guard.json"):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except (OSError, ValueError):
            continue
    if cfg.get("reminder") is True:     # v0.1 config compatibility
        cfg["reminder"] = "compact"
    elif cfg.get("reminder") is False:
        cfg["reminder"] = "off"
    return cfg


# ---------------------------------------------------------------------------
# I/O, paths, state
# ---------------------------------------------------------------------------

def read_input() -> dict:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def emit(obj: dict) -> None:
    # ensure_ascii keeps output safe on Windows consoles with non-UTF-8 code pages
    sys.stdout.write(json.dumps(obj, ensure_ascii=True))
    sys.stdout.flush()


def debug(msg: str) -> None:
    if os.environ.get("SPRAWL_GUARD_DEBUG"):
        sys.stderr.write(f"[{TAG}] {msg}\n")


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def project_root(data: dict) -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    return Path(root).resolve()


def norm(p: Path) -> str:
    """Comparable path string: case-insensitive on Windows, forward slashes."""
    return os.path.normcase(str(p)).replace("\\", "/")


def is_within(child: Path, parent: Path) -> bool:
    c, p = norm(child), norm(parent).rstrip("/")
    return c == p or c.startswith(p + "/")


def rel_of(target: Path, root: Path) -> str:
    try:
        return os.path.relpath(target, root).replace("\\", "/")
    except ValueError:  # different drive on Windows
        return str(target).replace("\\", "/")


def state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:120] or "nosession"
    d = Path(tempfile.gettempdir()) / TAG
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.json"


def load_state(session_id: str) -> dict:
    try:
        with open(state_path(session_id), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(session_id: str, st: dict) -> None:
    try:
        p = state_path(session_id)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, p)
    except OSError:
        pass


def log_event(st: dict, root: Path, text: str) -> None:
    st.setdefault("turn_log", []).append(text)
    try:
        home = guard_home()
        home.mkdir(parents=True, exist_ok=True)
        with open(home / "sprawl-guard.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{root}\t{text}\n")
    except OSError:
        pass


def normalize_prompt(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'").lower()
    return re.sub(r"\s+", " ", text)[:20000]


# ---------------------------------------------------------------------------
# File-name and content analysis
# ---------------------------------------------------------------------------

def tokens(stem: str) -> list[str]:
    # split on separators and camelCase: "ImplementationSummary_v2" -> implementation, summary, v2
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def strip_version(stem: str, suffixes: list[str]) -> tuple[str, list[str]]:
    """Remove trailing version/backup suffixes repeatedly. Returns (base, removed suffixes)."""
    words = "|".join(re.escape(s) for s in sorted(suffixes, key=len, reverse=True))
    pattern = re.compile(
        rf"^(.+?)(?:[ _\-.](v\d+|ver\d+|version\d+|{words})|\s*(\(\d+\))|\s+-\s+(copy))$",
        re.IGNORECASE,
    )
    base, removed = stem, []
    while True:
        m = pattern.match(base)
        if not m or not m.group(1).strip(" _-."):
            return base, removed
        suffix = next(g for g in m.groups()[1:] if g)
        removed.append("copy" if suffix.startswith("(") else suffix.lower())
        base = m.group(1).rstrip(" _-.")


def dir_entries(target: Path) -> list[Path]:
    try:
        return sorted(e for e in target.parent.iterdir() if e.is_file() and not e.name.startswith("."))
    except OSError:
        return []


def find_base_sibling(target: Path, base: str, suffixes: list[str]) -> Path | None:
    ext = target.suffix.lower()
    for entry in dir_entries(target):
        if norm(entry) == norm(target) or entry.suffix.lower() != ext:
            continue
        sib_base, _ = strip_version(entry.stem, suffixes)
        if sib_base.lower() == base.lower():
            return entry
    return None


def find_name_sibling(target: Path) -> Path | None:
    """train_resnet.py next to train.py, or run_exp3.py next to run_exp2.py."""
    stem, ext = target.stem.lower(), target.suffix.lower()
    trimmed = re.sub(r"\d+$", "", stem).rstrip(" _-.")
    for entry in dir_entries(target):
        if norm(entry) == norm(target) or entry.suffix.lower() != ext:
            continue
        es = entry.stem.lower()
        if len(stem) > len(es) and stem.startswith(es) and stem[len(es)] in " _-.":
            return entry
        if trimmed and es != stem and trimmed == re.sub(r"\d+$", "", es).rstrip(" _-."):
            return entry
    return None


def most_similar(target: Path, content: str, cfg: dict) -> tuple[Path, float] | None:
    def lines(text: str) -> list[str]:
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    new = lines(content)
    min_lines = int(cfg.get("similarity_min_lines", 10))
    if len(new) < min_lines:
        return None
    thr = float(cfg.get("similarity_threshold", 0.6))
    ext = target.suffix.lower()
    best, best_r, checked = None, 0.0, 0
    for entry in dir_entries(target):
        if norm(entry) == norm(target) or entry.suffix.lower() != ext:
            continue
        try:
            if entry.stat().st_size > 300_000:
                continue
            old = lines(entry.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        checked += 1
        if checked > 200:
            break
        if len(old) < min_lines:
            continue
        sm = difflib.SequenceMatcher(None, new, old, autojunk=False)
        if sm.real_quick_ratio() < thr or sm.quick_ratio() < thr:
            continue
        r = sm.ratio()
        if r >= thr and r > best_r:
            best, best_r = entry, r
    return (best, best_r) if best else None


def requested(target: Path, prompt: str, cfg: dict) -> tuple[bool, bool]:
    """(file is named in the user's prompt, prompt asks for a separate/new version)."""
    if not prompt:
        return False, False
    name, stem = target.name.lower(), target.stem.lower()
    named = name in prompt
    if not named and len(stem) >= 4:
        named = re.search(rf"(?<![a-z0-9_]){re.escape(stem)}(?![a-z0-9_])", prompt) is not None
    asked = any(k in prompt for k in cfg["request_keywords"])
    return named, asked


def judge_new_file(target: Path, rel: str, cfg: dict, scratchpad: str | None, content: str) -> list[dict]:
    """All rule findings for a new file. Each: mode, kind, issue, fix, alt, soften, ref."""
    for pattern in cfg.get("allow_paths", []):
        if fnmatch.fnmatch(rel, pattern):
            return []

    findings: list[dict] = []

    def add(mode, kind, issue, fix, alt=None, soften=True, ref=None):
        if SEVERITY.get(mode, 0) > 0:
            findings.append({"mode": mode, "kind": kind, "issue": issue, "fix": fix,
                             "alt": alt, "soften": soften, "ref": ref})

    stem, ext = target.stem, target.suffix.lower()
    toks = tokens(stem)
    allowed = {n.lower() for n in cfg["allowed_names"]}
    suffixes = list(cfg["version_suffixes"]) + list(cfg["backup_suffixes"])
    backups = {s.lower() for s in cfg["backup_suffixes"]}
    is_doc = ext in cfg["doc_extensions"]
    standard = stem.lower() in allowed
    hidden = target.name.startswith(".")
    where = f"the session scratchpad ({scratchpad})" if scratchpad else "a temporary directory outside the project"

    # 1. one-off scripts
    if (toks and toks[0] in cfg["oneoff_prefixes"]) or any(
            re.search(p, stem, re.IGNORECASE) for p in cfg.get("oneoff_patterns", [])):
        add(cfg["oneoff_scripts"], "oneoff",
            f"'{rel}' looks like a one-off script.",
            f"Put throwaway scripts in {where} so they do not clutter the project; "
            f"if it belongs in the codebase, add it to an existing file.",
            alt="put it in the session scratchpad")

    # 2. versioned copies and backups
    base, removed = strip_version(stem, suffixes)
    sibling = find_base_sibling(target, base, suffixes) if removed else None
    if removed and sibling is not None:
        if any(s in backups for s in removed):
            add(cfg["backup_files"], "backup",
                f"'{rel}' is a backup copy of '{sibling.name}'.",
                f"Backups are not kept as files in the project; edit '{sibling.name}' directly.",
                soften=False, ref=sibling)
        else:
            add(cfg["versioned_files"], "versioned",
                f"'{rel}' is a versioned copy of existing file '{sibling.name}'.",
                f"Edit '{sibling.name}' in place instead: merge the new content into it and remove the parts "
                f"it supersedes, so the file stays one current version. Do not ask the user about this; just do it.",
                alt=f"edit '{sibling.name}' in place", ref=sibling)
    elif removed:
        add(cfg["versioned_no_base"], "versioned_no_base",
            f"'{rel}' has a version-style name.",
            "If it replaces an existing file, edit that file in place instead.")

    # 3. docs and reports
    if is_doc and not standard and base.lower() not in allowed:
        if any(t in cfg["report_keywords"] for t in toks):
            add(cfg["report_files"], "report",
                f"'{rel}' is a new report or summary document.",
                "Summaries go in the chat reply unless the user asked for a file.",
                alt="put it in the chat reply instead of a file")
        else:
            add(cfg["other_docs"], "doc",
                f"'{rel}' is a new document.",
                "Prefer updating an existing doc such as README unless the user asked for a new one.")

    # 4. run sprawl: near-copies and name variants of existing files
    if not is_doc and sibling is None:
        sim = most_similar(target, content, cfg)
        if sim:
            ref, ratio = sim
            add(cfg["similar_files"], "similar",
                f"'{rel}' is about {round(ratio * 100)}% identical to existing '{ref.name}'.",
                f"Extend '{ref.name}' instead of creating a near-copy: add the differences as a config entry, "
                f"command-line argument or option so one file covers both runs.",
                alt=f"add the differences to '{ref.name}' as a config entry or argument", ref=ref)
        else:
            ref = find_name_sibling(target)
            if ref is not None:
                add(cfg["sibling_names"], "sibling",
                    f"'{rel}' looks like a variant of existing '{ref.name}'.",
                    f"If it is another run or setting of the same thing, add it to '{ref.name}' "
                    f"as a config entry or argument instead.")

    # 5. naming
    if not hidden and not standard:
        max_len = int(cfg.get("max_name_length", 30))
        max_sep = int(cfg.get("max_name_separators", 3))
        seps = len(re.findall(r"[ _\-.]", stem))
        if len(stem) > max_len or seps > max_sep:
            add(cfg["long_names"], "long",
                f"'{rel}' has a long file name ({len(stem)} characters, {seps} separators).",
                f"Use a shorter name, at most {max_len} characters and {max_sep} separators, "
                f"that still says what the file is.",
                alt="use a shorter name")
        if any(re.search(p, stem.lower()) for p in cfg.get("generic_patterns", [])):
            add(cfg["generic_names"], "generic",
                f"'{rel}' has a generic or dated file name.",
                "Use a short descriptive name; dates and run IDs belong in configs, logs or git history.",
                alt="use a descriptive name")

    return findings


# ---------------------------------------------------------------------------
# Git checkpoint (local commit only, never push)
# ---------------------------------------------------------------------------

def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=8)


def git_checkpoint(root: Path, path: Path) -> str | None:
    """Commit the current version of `path` locally. Returns 'committed', 'clean' or None."""
    try:
        r = git(root, "rev-parse", "--git-dir")
        if r.returncode != 0:
            return None
        gitdir = Path(r.stdout.decode("utf-8", "replace").strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        busy = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply")
        if any((gitdir / n).exists() for n in busy):
            return None  # never commit in the middle of a merge or rebase
        rel = os.path.relpath(path, root)
        s = git(root, "status", "--porcelain", "--", rel)
        if s.returncode != 0:
            return None
        status = s.stdout.decode("utf-8", "replace")
        if not status.strip():
            tracked = git(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0
            return "clean" if tracked else None  # ignored files are left alone
        if git(root, "add", "--", rel).returncode != 0:
            return None
        msg = f"sprawl-guard: checkpoint {rel.replace(os.sep, '/')} before in-place edit"
        c = git(root, "-c", "commit.gpgsign=false", "commit", "--no-verify", "-q", "-m", msg, "--", rel)
        if c.returncode == 0:
            return "committed"
        if status.startswith("??"):
            git(root, "rm", "--cached", "-q", "--", rel)  # undo staging of a file that was untracked
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def checkpoint_note(root: Path, ref: Path, st: dict) -> str:
    done = st.setdefault("checkpointed", [])
    key = norm(ref)
    status = "clean" if key in done else git_checkpoint(root, ref)
    if status == "committed":
        done.append(key)
        return f" A local git checkpoint of '{ref.name}' was committed, so its current version stays recoverable."
    if status == "clean":
        return f" '{ref.name}' is saved in git, so its current version stays recoverable."
    return ""


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def pre_tool(kind: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": kind,
                                   "permissionDecisionReason": reason}}


def confirm_message(rel: str, conf: list[dict]) -> str:
    issues = " ".join(f["issue"] for f in conf)
    alts: list[str] = []
    for f in conf:
        if f.get("alt") and f["alt"] not in alts:
            alts.append(f["alt"])
    options = alts + [f"create '{rel}' anyway", "use a different name or location"]
    listed = "; ".join(f"({i}) {o}" for i, o in enumerate(options, 1))
    return (f"[{TAG}] {issues} Do not decide this yourself: ask the user, with your multiple-choice "
            f"question tool if you have one, choosing between {listed}. If they choose to create it "
            f"anyway, retry the same Write; they will get a one-time confirmation prompt.")


def handle_pre_write(data: dict, cfg: dict) -> None:
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return
    root = project_root(data)
    target = Path(file_path)
    if not target.is_absolute():
        target = Path(data.get("cwd") or root) / target
    target = target.resolve()

    if target.exists() or not is_within(target, root):
        return  # editing existing files, or writing outside the project, is never sprawl
    scratchpad = data.get("scratchpad_dir")
    if scratchpad and is_within(target, Path(scratchpad).resolve()):
        return
    rel = rel_of(target, root)
    ignore = {d.lower() for d in cfg["ignore_dirs"]}
    if any(p.lower() in ignore for p in rel.split("/")[:-1]):
        return

    sid = data.get("session_id") or "nosession"
    st = load_state(sid)
    unattended = truthy_env("SPRAWL_GUARD_UNATTENDED") or bool(st.get("unattended"))
    named, asked = requested(target, st.get("prompt", ""), cfg)
    findings = judge_new_file(target, rel, cfg, scratchpad, tool_input.get("content") or "")

    # Something the user explicitly asked for is never silently fixed: ask them instead.
    for f in findings:
        if f["mode"] == "deny" and f["soften"] and (named or asked):
            f["mode"] = "confirm"

    top = max((SEVERITY.get(f["mode"], 0) for f in findings), default=0)
    key = norm(target)
    out: dict = {}
    let_through = False

    if top == SEVERITY["deny"]:
        parts = []
        for f in findings:
            if f["mode"] != "deny":
                continue
            note = ""
            if f["kind"] in ("versioned", "backup") and f["ref"] is not None and cfg.get("git_checkpoint", True):
                note = checkpoint_note(root, f["ref"], st)
            parts.append(f"{f['issue']} {f['fix']}{note}")
        out = pre_tool("deny", f"[{TAG}] " + " ".join(parts))
    elif top == SEVERITY["confirm"]:
        conf = [f for f in findings if f["mode"] == "confirm"]
        if unattended:
            log_event(st, root, f"{rel} ({conf[0]['issue']})")
            let_through = True
        elif key in st.get("pending", []):
            out = pre_tool("ask", f"[{TAG}] Confirm creating '{rel}': " + " ".join(f["issue"] for f in conf))
            let_through = True
        else:
            st.setdefault("pending", []).append(key)
            out = pre_tool("deny", confirm_message(rel, conf))
    elif top == SEVERITY["ask"]:
        asks = [f for f in findings if f["mode"] == "ask"]
        if unattended:
            log_event(st, root, f"{rel} ({asks[0]['issue']})")
        else:
            out = pre_tool("ask", f"[{TAG}] " + " ".join(f"{f['issue']} {f['fix']}" for f in asks))
        let_through = True
    else:
        let_through = True

    # File budget: after N new files this session, each further one needs approval.
    created = st.setdefault("created", [])
    if let_through and key not in created:
        budget = int(cfg.get("file_budget") or 0)
        budget_mode = cfg.get("file_budget_mode", "ask")
        if not out and budget > 0 and len(created) >= budget and SEVERITY.get(budget_mode, 0) > 0:
            msg = f"'{rel}' would be new file number {len(created) + 1} this session (budget {budget})."
            if SEVERITY[budget_mode] == SEVERITY["warn"]:
                findings.append({"mode": "warn", "issue": msg, "fix": "Prefer editing existing files."})
            elif unattended:
                log_event(st, root, msg)
            else:
                out = pre_tool("ask", f"[{TAG}] {msg} Approve only if this file is really needed.")
        created.append(key)

    warns = [f for f in findings if f["mode"] == "warn"]
    if warns and (not out or out["hookSpecificOutput"].get("permissionDecision") == "ask"):
        out = out or {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
        out["hookSpecificOutput"]["additionalContext"] = (
            f"[{TAG}] " + " ".join(f"{f['issue']} {f['fix']}" for f in warns))

    save_state(sid, st)
    debug(f"pre-write {rel} -> {out or 'allow'}")
    if out:
        emit(out)


def handle_prompt(data: dict, cfg: dict) -> None:
    sid = data.get("session_id") or "nosession"
    prompt = normalize_prompt(data.get("prompt") or "")
    st = load_state(sid)
    st["prompt"] = prompt
    st["unattended"] = any(k in prompt for k in cfg["unattended_keywords"])
    save_state(sid, st)
    if cfg.get("reminder") == "always":
        emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": REMINDER}})


def list_files(root: Path, cfg: dict) -> set[str]:
    """Untracked-but-not-ignored files via git when possible, else a bounded walk."""
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, timeout=8,
        )
        if res.returncode == 0:
            # untracked files only: anything Claude creates shows up here, .gitignored outputs do not
            return {p for p in res.stdout.decode("utf-8", "replace").split("\0") if p}
    except (OSError, subprocess.SubprocessError):
        pass
    ignore = {d.lower() for d in cfg["ignore_dirs"]}
    found: set[str] = set()
    deadline = time.monotonic() + 3.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in ignore]
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            found.add(rel.replace("\\", "/"))
        if len(found) > 50000 or time.monotonic() > deadline:
            break
    return found


def handle_session_start(data: dict, cfg: dict) -> None:
    sid = data.get("session_id") or ""
    if sid and cfg.get("stop_check", "list") != "off":
        st = load_state(sid)
        if st.get("snapshot") is None:
            st["snapshot"] = sorted(list_files(project_root(data), cfg))
            save_state(sid, st)
    if data.get("source") == "compact" and cfg.get("reminder") in ("compact", "always"):
        emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": REMINDER}})


def handle_stop(data: dict, cfg: dict) -> None:
    sid = data.get("session_id") or ""
    if not sid:
        return
    st = load_state(sid)
    mode = cfg.get("stop_check", "list")
    turn_log = st.pop("turn_log", [])
    new: list[str] = []
    if mode != "off":
        current = list_files(project_root(data), cfg)
        previous = st.get("snapshot")
        st["snapshot"] = sorted(current)
        if previous is not None:
            new = sorted(current - set(previous))
    save_state(sid, st)

    shown = ", ".join(new[:10]) + (f" (+{len(new) - 10} more)" if len(new) > 10 else "")
    if mode == "force" and new and not data.get("stop_hook_active"):
        emit({"decision": "block", "reason":
              f"[{TAG}] New files were created in the project this turn: {shown}. "
              "Before finishing, merge any that duplicate existing files back into them and delete "
              "throwaway ones; keep files the user explicitly asked for. Then say which you kept."})
        return
    parts = []
    if new:
        parts.append(f"{len(new)} new file(s) this turn: {shown}")
    if turn_log:
        parts.append("allowed because unattended (logged): " + "; ".join(turn_log[:10]))
    if st.get("unattended") or truthy_env("SPRAWL_GUARD_UNATTENDED"):
        parts.append("unattended mode was on")
    if parts:
        emit({"systemMessage": f"[{TAG}] " + " | ".join(parts)})


HANDLERS = {
    "pre-write": handle_pre_write,
    "prompt": handle_prompt,
    "session-start": handle_session_start,
    "stop": handle_stop,
}


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    if event in ("--version", "-V"):
        print(f"{TAG} {VERSION}")
        return 0
    handler = HANDLERS.get(event)
    if handler is None:
        sys.stderr.write(f"{TAG}: unknown event '{event}'\n")
        return 0
    if os.environ.get("SPRAWL_GUARD", "").lower() in ("off", "0", "false", "disabled"):
        return 0
    try:
        data = read_input()
        cfg = load_config(project_root(data))
        if not cfg.get("enabled", True):
            return 0
        handler(data, cfg)
    except Exception as exc:  # fail open: never break the user's session
        debug(f"error in {event}: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
