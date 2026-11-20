#!/usr/bin/env python3
"""Benchmark: run the same coding tasks with and without sprawl-guard and count the sprawl.

Each task runs in a fresh copy of a small fixture project (a git repo), once with the plugin
loaded from this checkout (--plugin-dir) and once with sprawl-guard switched off
(SPRAWL_GUARD=off). Your user-level Claude Code settings are not loaded (--setting-sources
project), so a global sprawl-guard install does not affect either run.

  python3 bench/run.py                       # all tasks, 1 repeat
  python3 bench/run.py --tasks 1,3 --repeats 3
  python3 bench/run.py --allow-bash          # let Claude run shell commands (recommended)

Needs the `claude` CLI, signed in. Every run is a real Claude Code session and costs tokens.
With --allow-bash, Claude may run any shell command inside the throwaway project copy.

Counted per run (new files only, compared with the fixture):
  new_files    files that did not exist before
  versioned    _v2/_final/_backup style copies of an existing file
  near_copies  new files mostly identical to another file in the same folder
  oneoffs      debug_*, tmp_*, scratch* and similar scripts
  stray_docs   new .md/.txt/.rst files other than README-type files and the notes file
  orphans      new code files nothing in the project refers to

Results are written to bench/results/<time>.json (git-ignored) and printed as a table.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import sprawl_guard as sg  # noqa: E402

FIXTURE = {
    "README.md": (
        "# linreg\n\nA small linear regression trainer in pure Python.\n\n"
        "Train with `python train.py --config configs/base.cfg`. Run the tests with "
        "`python -m unittest discover tests`.\n"
    ),
    "train.py": (
        "import argparse\n\nfrom config import load_config\nfrom data import make_dataset, batches\n"
        "from model import LinearModel\n\n\ndef train(cfg):\n"
        "    data = make_dataset(int(cfg['n']), int(cfg['seed']))\n"
        "    model = LinearModel()\n    lr = float(cfg['lr'])\n"
        "    for epoch in range(int(cfg['epochs'])):\n        total = 0.0\n"
        "        for batch in batches(data, int(cfg['batch_size'])):\n"
        "            total += model.step(batch, lr)\n"
        "        print(f'epoch {epoch} loss {total:.4f}')\n    return model\n\n\n"
        "if __name__ == '__main__':\n    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('--config', default='configs/base.cfg')\n"
        "    ap.add_argument('--lr', type=float)\n    args = ap.parse_args()\n"
        "    cfg = load_config(args.config)\n    if args.lr is not None:\n"
        "        cfg['lr'] = args.lr\n    train(cfg)\n"
    ),
    "model.py": (
        "class LinearModel:\n    def __init__(self):\n        self.w = 0.0\n        self.b = 0.0\n\n"
        "    def predict(self, x):\n        return self.w * x + self.b\n\n"
        "    def step(self, batch, lr):\n        gw = gb = loss = 0.0\n        for x, y in batch:\n"
        "            err = self.predict(x) - y\n            gw += 2 * err * x / len(batch)\n"
        "            gb += 2 * err / len(batch)\n            loss += err * err / len(batch)\n"
        "        self.w -= lr * gw\n        self.b -= lr * gb\n        return loss\n"
    ),
    "data.py": (
        "import random\n\n\ndef make_dataset(n, seed):\n    rng = random.Random(seed)\n"
        "    xs = [rng.uniform(-1, 1) for _ in range(n)]\n"
        "    return [(x, 3 * x + 0.5 + rng.gauss(0, 0.1)) for x in xs]\n\n\n"
        "def batches(data, batch_size):\n"
        "    for i in range(0, len(data) - batch_size + 1, batch_size):\n"
        "        yield data[i:i + batch_size]\n"
    ),
    "config.py": (
        "def load_config(path):\n    cfg = {}\n    with open(path) as f:\n        for line in f:\n"
        "            line = line.strip()\n            if line and not line.startswith('#'):\n"
        "                key, value = line.split('=', 1)\n"
        "                cfg[key.strip()] = value.strip()\n    return cfg\n"
    ),
    "configs/base.cfg": "n = 200\nseed = 0\nlr = 0.1\nepochs = 20\nbatch_size = 16\n",
    "tests/test_model.py": (
        "import sys\nimport unittest\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
        "from model import LinearModel  # noqa: E402\n\n\nclass TestModel(unittest.TestCase):\n"
        "    def test_step_reduces_loss(self):\n        m = LinearModel()\n"
        "        batch = [(1.0, 3.0), (0.0, 0.5)]\n        first = m.step(batch, 0.1)\n"
        "        self.assertLess(m.step(batch, 0.1), first)\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n"
    ),
    ".gitignore": "__pycache__/\noutputs/\n",
}


def make_fixture(dest: Path) -> None:
    for rel, text in FIXTURE.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "bench@example.com"], ["config", "user.name", "bench"],
                 ["config", "commit.gpgsign", "false"], ["add", "-A"], ["commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(dest), *args], check=True, capture_output=True)


def measure(root: Path, before: set[str]) -> dict:
    cfg = dict(sg.DEFAULT_CONFIG)
    files, _ = sg.scan_files(root, cfg, budget=30.0)
    new = sorted(set(files) - before)
    newset = set(new)
    tidy = sg.tidy_scan(root, root, cfg)
    versioned = sorted(c for g in tidy["versioned"] for c in g["copies"] if c in newset)
    near = sorted({f["path"] for g in tidy["near_copies"] for f in g["files"] if f["path"] in newset})
    oneoffs = sorted(f["path"] for f in tidy["oneoffs"] if f["path"] in newset)
    allowed = {n.lower() for n in cfg["allowed_names"]}
    notes = {e.lower() for e in sg.notes_entries(cfg)}
    doc_exts = {".md", ".txt", ".rst", ".markdown"}
    stray = [r for r in new if Path(r).suffix.lower() in doc_exts
             and Path(r).stem.lower() not in allowed and r.lower() not in notes]
    orphans = sg.find_orphans(root, new, cfg, list(files))
    return {"new_files": new, "versioned": versioned, "near_copies": near, "oneoffs": oneoffs,
            "stray_docs": stray, "orphans": orphans}


def run_task(task: dict, guarded: bool, args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="sprawl-bench-"))
    root = tmp / "linreg"
    root.mkdir()
    home = tmp / "guard-home"  # keeps your sprawl-guard config, log and stats out of the benchmark
    try:
        make_fixture(root)
        before = set(sg.scan_files(root, dict(sg.DEFAULT_CONFIG))[0])
        cmd = [args.claude, "-p", task["prompt"], "--output-format", "json", "--no-session-persistence",
               "--setting-sources", "project", "--permission-mode", "acceptEdits"]
        if args.allow_bash:
            cmd += ["--allowedTools", "Bash"]
        if args.model:
            cmd += ["--model", args.model]
        if args.max_budget:
            cmd += ["--max-budget-usd", str(args.max_budget)]
        env = {k: v for k, v in os.environ.items() if not k.startswith("SPRAWL_GUARD")}
        env["SPRAWL_GUARD_HOME"] = str(home)
        if guarded:
            cmd += ["--plugin-dir", str(REPO)]
            env["SPRAWL_GUARD_UNATTENDED"] = "1"  # no one is there to answer confirm prompts
        else:
            env["SPRAWL_GUARD"] = "off"
        started = time.monotonic()
        try:
            res = subprocess.run(cmd, cwd=root, env=env, capture_output=True, timeout=args.timeout)
            out, code = res.stdout.decode("utf-8", "replace"), res.returncode
        except subprocess.TimeoutExpired:
            out, code = "", "timeout"
        info: dict = {}
        try:
            info = json.loads(out) if out.strip() else {}
        except ValueError:
            pass
        result = measure(root, before)
        result.update({
            "task": task["id"], "guarded": guarded, "exit": code,
            "seconds": round(time.monotonic() - started, 1),
            "cost_usd": info.get("total_cost_usd"), "turns": info.get("num_turns"),
            "is_error": info.get("is_error"),
        })
        if args.keep:
            result["kept_at"] = str(root)
        return result
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


METRICS = ["new_files", "versioned", "near_copies", "oneoffs", "stray_docs", "orphans"]


def table(results: list[dict], tasks: list[dict]) -> str:
    def avg(rows: list[dict], key: str) -> str:
        if not rows:
            return "-"
        v = sum(len(r[key]) for r in rows) / len(rows)
        return f"{v:.1f}".rstrip("0").rstrip(".")

    head = "| Task | " + " | ".join(f"{m} (off / on)" for m in METRICS) + " |"
    lines = [head, "|" + "---|" * (len(METRICS) + 1)]
    for t in tasks + [{"id": "all", "name": "All tasks"}]:
        rows = [r for r in results if t["id"] == "all" or r["task"] == t["id"]]
        off = [r for r in rows if not r["guarded"]]
        on = [r for r in rows if r["guarded"]]
        cells = [f"{avg(off, m)} / {avg(on, m)}" for m in METRICS]
        label = t["name"] if t["id"] == "all" else f"{t['id']}. {t['name']}"
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", help="comma-separated task ids (default: all)")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--model", help="model alias passed to claude --model")
    ap.add_argument("--allow-bash", action="store_true", help="allow Claude to run shell commands")
    ap.add_argument("--max-budget", type=float, help="max USD per run (claude --max-budget-usd)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per run")
    ap.add_argument("--only", choices=["on", "off"], help="run only with or only without sprawl-guard")
    ap.add_argument("--keep", action="store_true", help="keep the project copies for inspection")
    ap.add_argument("--claude", default=shutil.which("claude") or "claude", help="path to the claude CLI")
    args = ap.parse_args()

    tasks = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
    if args.tasks:
        wanted = {int(x) for x in args.tasks.split(",")}
        tasks = [t for t in tasks if t["id"] in wanted]
    modes = [False, True] if not args.only else [args.only == "on"]
    results: list[dict] = []
    for rep in range(args.repeats):
        for t in tasks:
            for guarded in modes:
                label = "on " if guarded else "off"
                print(f"[{rep + 1}/{args.repeats}] task {t['id']} sprawl-guard {label} ...", end=" ", flush=True)
                r = run_task(t, guarded, args)
                results.append(r)
                print(f"{len(r['new_files'])} new file(s), exit {r['exit']}, {r['seconds']}s", flush=True)

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    meta = {"version": sg.VERSION, "model": args.model, "allow_bash": args.allow_bash, "repeats": args.repeats}
    path.write_text(json.dumps({"meta": meta, "results": results}, indent=2) + "\n", encoding="utf-8")
    print()
    print(table(results, tasks))
    print(f"\nAverages per run. Full results: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
