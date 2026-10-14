"""Tests for sprawl-guard. Run with:  python -m unittest test_sprawl_guard -v

Each test feeds the script the same JSON Claude Code would send on stdin,
inside a throwaway project folder, and checks the decision it prints.
Your own ~/.claude/sprawl-guard.json is ignored while testing.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sprawl_guard.py"
HOME = tempfile.mkdtemp(prefix="sprawl-guard-home-")  # isolates global config and log
HAS_GIT = shutil.which("git") is not None

CODE = "\n".join(
    ["import argparse", "import torch", "", "def build_model(cfg):", "    layers = []"]
    + [f"    layers.append(torch.nn.Linear({i}, {i + 1}))" for i in range(1, 13)]
    + ["    return torch.nn.Sequential(*layers)", "", "if __name__ == '__main__':", "    print('train')"]
)


def run(event, payload, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SPRAWL_GUARD", "CLAUDE_PROJECT_DIR"))}
    env["SPRAWL_GUARD_HOME"] = HOME
    env.update(env_extra or {})
    res = subprocess.run([sys.executable, str(SCRIPT), event], input=json.dumps(payload).encode(),
                         capture_output=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr.decode()
    out = res.stdout.decode().strip()
    return json.loads(out) if out else None


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.session = "test-" + uuid.uuid4().hex

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def config(self, **cfg):
        self.touch(".claude/sprawl-guard.json", json.dumps(cfg))

    def prompt(self, text, **env):
        return run("prompt", {"session_id": self.session, "cwd": str(self.root), "prompt": text}, env)

    def write(self, rel, content="...", env=None, **extra):
        payload = {"session_id": self.session, "cwd": str(self.root), "hook_event_name": "PreToolUse",
                   "tool_name": "Write", "tool_input": {"file_path": str(self.root / rel), "content": content}}
        payload.update(extra)
        return run("pre-write", payload, env)

    def stop(self, **extra):
        return run("stop", {"session_id": self.session, "cwd": str(self.root), **extra})

    def start(self):
        run("session-start", {"session_id": self.session, "cwd": str(self.root), "source": "startup"})

    @staticmethod
    def decision(out):
        if out is None:
            return None
        hso = out["hookSpecificOutput"]
        if hso.get("permissionDecision") == "deny" and "Do not decide this yourself" in hso["permissionDecisionReason"]:
            return "confirm"
        return hso.get("permissionDecision") or ("warn" if "additionalContext" in hso else None)

    @staticmethod
    def reason(out):
        return out["hookSpecificOutput"].get("permissionDecisionReason", "")


class Basics(Base):
    def test_existing_file_is_allowed(self):
        self.touch("train.py")
        self.assertIsNone(self.write("train.py"))

    def test_standard_repo_docs_allowed(self):
        for name in ("README.md", "CHANGELOG.md", "CLAUDE.md", "LICENSE.txt", "requirements.txt",
                     "CODE_OF_CONDUCT.md"):
            self.assertIsNone(self.write(name), name)

    def test_plain_new_code_file_allowed(self):
        self.assertIsNone(self.write("src/dataset.py", CODE))

    def test_outside_project_allowed(self):
        with tempfile.TemporaryDirectory() as other:
            payload = {"session_id": self.session, "cwd": str(self.root),
                       "tool_input": {"file_path": os.path.join(other, "debug_x.py")}}
            self.assertIsNone(run("pre-write", payload))

    def test_scratchpad_inside_project_allowed(self):
        self.assertIsNone(self.write(".scratch/debug_x.py", scratchpad_dir=str(self.root / ".scratch")))

    def test_ignored_dirs_allowed(self):
        self.assertIsNone(self.write("node_modules/pkg/notes.md"))

    def test_allow_paths_config(self):
        self.config(allow_paths=["docs/**"])
        self.assertIsNone(self.write("docs/weekly_report.md"))
        self.assertEqual(self.decision(self.write("weekly_report.md")), "confirm")

    def test_disabled_by_env(self):
        self.touch("train.py")
        self.assertIsNone(self.write("train_v2.py", env={"SPRAWL_GUARD": "off"}))

    def test_disabled_by_config(self):
        self.touch("train.py")
        self.config(enabled=False)
        self.assertIsNone(self.write("train_v2.py"))

    def test_garbage_input_fails_open(self):
        res = subprocess.run([sys.executable, str(SCRIPT), "pre-write"], input=b"not json",
                             capture_output=True, timeout=30)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, b"")


class VersionedCopies(Base):
    def test_a_spontaneous_v2_is_merged_quietly(self):
        self.touch("train.py")
        self.prompt("improve the training loop")
        out = self.write("train_v2.py")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("Edit 'train.py' in place", self.reason(out))
        self.assertIn("Do not ask the user", self.reason(out))

    def test_v3_when_v2_exists(self):
        self.touch("src/model_v2.py")
        self.assertEqual(self.decision(self.write("src/model_v3.py")), "deny")

    def test_final_and_stacked_suffixes(self):
        self.touch("paper.tex")
        self.assertEqual(self.decision(self.write("paper_final.tex")), "deny")
        self.assertEqual(self.decision(self.write("paper_v2_final.tex")), "deny")

    def test_b_named_file_asks_with_options_then_confirms(self):
        self.touch("README.md")
        self.prompt("create README_v2.md file that adds current time")
        out = self.write("README_v2.md")
        self.assertEqual(self.decision(out), "confirm")
        self.assertIn("edit 'README.md' in place", self.reason(out))
        self.assertIn("create 'README_v2.md' anyway", self.reason(out))
        self.assertEqual(self.decision(self.write("README_v2.md")), "ask")  # retry after the user chose

    def test_c_unnamed_new_version_request_asks(self):
        self.touch("train.py")
        self.prompt("keep the original and make a new version of train.py with dropout")
        self.assertEqual(self.decision(self.write("train_v2.py")), "confirm")

    def test_c_unattended_allows_and_logs(self):
        self.touch("README.md")
        self.prompt("create README_v2.md, I'll be away so work autonomously")
        self.assertIsNone(self.write("README_v2.md"))
        msg = self.stop()["systemMessage"]
        self.assertIn("README_v2.md", msg)
        self.assertIn("unattended mode was on", msg)
        self.assertIn("README_v2.md", (Path(HOME) / "sprawl-guard.log").read_text())

    def test_unattended_by_env(self):
        self.assertIsNone(self.write("SUMMARY.md", env={"SPRAWL_GUARD_UNATTENDED": "1"}))

    def test_unattended_resets_on_next_prompt(self):
        self.prompt("run this overnight")
        self.prompt("now summarize")
        self.assertEqual(self.decision(self.write("SUMMARY.md")), "confirm")

    def test_unattended_never_skips_self_fixable_denies(self):
        self.touch("train.py")
        self.prompt("work autonomously until done")
        self.assertEqual(self.decision(self.write("train_v2.py")), "deny")

    def test_d_version_name_without_base_only_warns(self):
        self.assertEqual(self.decision(self.write("models/resnet_v2.py")), "warn")

    def test_e_backup_denied_even_when_named(self):
        self.touch("train.py")
        self.prompt("make train_backup.py before you refactor")
        out = self.write("train_backup.py")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("Backups are not kept", self.reason(out))

    def test_windows_copy_names(self):
        self.touch("paper.tex")
        self.assertEqual(self.decision(self.write("paper (1).tex")), "deny")
        self.assertEqual(self.decision(self.write("paper - Copy.tex")), "deny")

    def test_words_ending_in_suffix_not_flagged(self):
        self.assertIsNone(self.write("renew.py"))
        self.assertIsNone(self.write("finalize.py"))


@unittest.skipUnless(HAS_GIT, "git not installed")
class GitCheckpoint(Base):
    def setUp(self):
        super().setUp()
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def log(self):
        r = subprocess.run(["git", "-C", str(self.root), "log", "--oneline"], capture_output=True)
        return r.stdout.decode()

    def test_untracked_original_is_committed(self):
        self.touch("train.py", "print(1)\n")
        out = self.write("train_v2.py")
        self.assertIn("checkpoint of 'train.py' was committed", self.reason(out))
        self.assertIn("sprawl-guard: checkpoint train.py", self.log())

    def test_clean_original_is_not_recommitted(self):
        self.touch("train.py", "print(1)\n")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "init"], check=True)
        out = self.write("train_v2.py")
        self.assertIn("is saved in git", self.reason(out))
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_other_staged_changes_are_not_committed(self):
        self.touch("train.py", "print(1)\n")
        self.touch("other.py", "print(2)\n")
        subprocess.run(["git", "-C", str(self.root), "add", "other.py"], check=True)
        self.write("train_v2.py")
        files = subprocess.run(["git", "-C", str(self.root), "show", "--name-only", "--format="],
                               capture_output=True).stdout.decode()
        self.assertIn("train.py", files)
        self.assertNotIn("other.py", files)

    def test_checkpoint_can_be_disabled(self):
        self.config(git_checkpoint=False)
        self.touch("train.py", "print(1)\n")
        self.write("train_v2.py")
        self.assertEqual(self.log(), "")


class RunSprawl(Base):
    def test_near_copy_denied(self):
        self.touch("train.py", CODE)
        variant = CODE.replace("print('train')", "print('resnet run')")
        out = self.write("train_resnet.py", variant)
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("identical to existing 'train.py'", self.reason(out))
        self.assertIn("config entry", self.reason(out))

    def test_near_copy_with_unrelated_name_denied(self):
        self.touch("train.py", CODE)
        self.assertEqual(self.decision(self.write("experiment.py", CODE + "\n# lr 0.01")), "deny")

    def test_named_near_copy_asks(self):
        self.touch("train.py", CODE)
        self.prompt("create train_resnet.py for the resnet run")
        self.assertEqual(self.decision(self.write("train_resnet.py", CODE)), "confirm")

    def test_different_content_sibling_only_warns(self):
        self.touch("train.py", CODE)
        other = "\n".join(f"value_{i} = {i * 7}" for i in range(20))
        out = self.write("train_resnet.py", other)
        self.assertEqual(self.decision(out), "warn")
        self.assertIn("variant of existing 'train.py'", out["hookSpecificOutput"]["additionalContext"])

    def test_numbered_run_sibling_warns(self):
        self.touch("run_exp2.py", "x = 1\n")
        self.assertEqual(self.decision(self.write("run_exp3.py")), "warn")

    def test_small_files_skip_similarity(self):
        self.touch("a.py", "x = 1\n")
        self.assertIsNone(self.write("b.py", "x = 1\n"))

    def test_similar_config_denied(self):
        cfg = "\n".join(f"key_{i}: {i}" for i in range(15))
        self.touch("configs/base.yaml", cfg)
        self.assertEqual(self.decision(self.write("configs/exp7.yaml", cfg + "\nlr: 0.01")), "deny")

    def test_other_directories_not_compared(self):
        self.touch("a/train.py", CODE)
        self.assertIsNone(self.write("b/model.py", CODE))


class DocsAndScripts(Base):
    def test_g_report_asks_then_confirms(self):
        out = self.write("IMPLEMENTATION_SUMMARY.md")
        self.assertEqual(self.decision(out), "confirm")
        self.assertIn("chat reply", self.reason(out))
        self.assertEqual(self.decision(self.write("IMPLEMENTATION_SUMMARY.md")), "ask")

    def test_report_txt_and_camelcase(self):
        self.assertEqual(self.decision(self.write("final_report.txt")), "confirm")
        self.assertEqual(self.decision(self.write("ChangesSummary.md")), "confirm")
        self.assertEqual(self.decision(self.write("Report_FINAL.MD")), "confirm")

    def test_other_new_doc_warns(self):
        self.assertEqual(self.decision(self.write("design.md")), "warn")

    def test_code_named_analysis_not_flagged(self):
        self.assertIsNone(self.write("src/analysis.py"))

    def test_report_mode_off(self):
        self.config(report_files="off", other_docs="off")
        self.assertIsNone(self.write("SUMMARY.md"))

    def test_oneoff_script_denied_with_scratchpad(self):
        out = self.write("debug_loss.py", scratchpad_dir="/tmp/pad")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("/tmp/pad", self.reason(out))

    def test_quick_test_is_oneoff_but_quicksort_is_not(self):
        self.assertEqual(self.decision(self.write("quick_test.py")), "deny")
        self.assertIsNone(self.write("quick_sort.py"))

    def test_named_oneoff_asks(self):
        self.prompt("write debug_loss.py to print the loss")
        self.assertEqual(self.decision(self.write("debug_loss.py")), "confirm")


class Naming(Base):
    def test_too_many_separators(self):
        out = self.write("preprocess_ecg_signals_with_bandpass.py")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("shorter name", self.reason(out))

    def test_too_long_without_separators(self):
        self.assertEqual(self.decision(self.write("averyveryverylongfilenamewithoutseps.py")), "deny")

    def test_short_names_fine(self):
        self.assertIsNone(self.write("ecg_filter.py"))

    def test_named_long_name_asks(self):
        self.prompt("create preprocess_ecg_signals_with_bandpass.py")
        self.assertEqual(self.decision(self.write("preprocess_ecg_signals_with_bandpass.py")), "confirm")

    def test_limits_configurable(self):
        self.config(max_name_separators=6, max_name_length=40)
        self.assertIsNone(self.write("preprocess_ecg_signals_with_bandpass.py"))

    def test_generic_names(self):
        for name in ("utils2.py", "misc.py", "helper_new.py", "untitled.py", "main2.py"):
            self.assertEqual(self.decision(self.write(name)), "deny", name)

    def test_dates_in_names(self):
        self.assertEqual(self.decision(self.write("log_20261014.py")), "deny")
        self.assertEqual(self.decision(self.write("eval_2026-10-14.py")), "deny")

    def test_dotfiles_skipped(self):
        self.assertIsNone(self.write(".env_local_dev_config_extra"))


class Budget(Base):
    def test_budget_asks_after_limit(self):
        self.config(file_budget=2)
        self.assertIsNone(self.write("alpha.py"))
        self.assertIsNone(self.write("beta.py"))
        out = self.write("gamma.py")
        self.assertEqual(self.decision(out), "ask")
        self.assertIn("budget 2", self.reason(out))

    def test_same_file_not_counted_twice(self):
        self.config(file_budget=1)
        self.assertIsNone(self.write("alpha.py"))
        self.assertIsNone(self.write("alpha.py"))

    def test_budget_unattended_logs(self):
        self.config(file_budget=1)
        self.prompt("work on this overnight")
        self.write("alpha.py")
        self.assertIsNone(self.write("beta.py"))
        self.assertIn("budget 1", self.stop()["systemMessage"])

    def test_budget_off(self):
        self.config(file_budget=0)
        for i in range(12):
            self.assertIsNone(self.write(f"mod{chr(97 + i)}.py"))


class Context(Base):
    def test_prompt_is_silent_by_default(self):
        self.assertIsNone(self.prompt("hi"))

    def test_reminder_after_compaction(self):
        base = {"session_id": self.session, "cwd": str(self.root)}
        self.assertIsNone(run("session-start", {**base, "source": "startup"}))
        out = run("session-start", {**base, "source": "compact"})
        self.assertIn("Workspace rules", out["hookSpecificOutput"]["additionalContext"])

    def test_reminder_always_mode(self):
        self.config(reminder="always")
        self.assertIn("Workspace rules", self.prompt("hi")["hookSpecificOutput"]["additionalContext"])

    def test_reminder_off(self):
        self.config(reminder="off")
        out = run("session-start", {"session_id": self.session, "cwd": str(self.root), "source": "compact"})
        self.assertIsNone(out)

    def test_v01_boolean_config_still_works(self):
        self.config(reminder=True)
        self.assertIsNone(self.prompt("hi"))


class StopCheck(Base):
    def test_lists_new_files_once(self):
        self.touch("existing.py")
        self.start()
        self.touch("made_by_claude.py")
        out = self.stop()
        self.assertIn("made_by_claude.py", out["systemMessage"])
        self.assertNotIn("existing.py", out["systemMessage"])
        self.assertIsNone(self.stop())

    def test_nothing_new_is_silent(self):
        self.start()
        self.assertIsNone(self.stop())

    def test_force_mode_blocks_once(self):
        self.config(stop_check="force")
        self.start()
        self.touch("helper.py")
        self.assertEqual(self.stop()["decision"], "block")
        self.touch("another.py")
        self.assertIn("systemMessage", self.stop(stop_hook_active=True))

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_git_repo_respects_gitignore(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.touch(".gitignore", "outputs/\n")
        self.start()
        self.touch("outputs/checkpoint.pt")
        self.touch("new_module.py")
        msg = self.stop()["systemMessage"]
        self.assertIn("new_module.py", msg)
        self.assertNotIn("checkpoint", msg)

    def test_missing_snapshot_starts_tracking(self):
        self.touch("a.py")
        self.assertIsNone(self.stop())
        self.touch("b.py")
        self.assertIn("b.py", self.stop()["systemMessage"])


class Installer(unittest.TestCase):
    def test_install_and_uninstall_keep_other_settings(self):
        installer = Path(__file__).resolve().parent / "install.py"
        with tempfile.TemporaryDirectory() as proj:
            settings = Path(proj) / ".claude" / "settings.local.json"
            settings.parent.mkdir()
            other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
            settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
            for _ in range(2):  # re-running must not duplicate
                subprocess.run([sys.executable, str(installer), "--project", proj], check=True, capture_output=True)
            data = json.loads(settings.read_text())
            self.assertEqual(data["model"], "x")
            self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
            self.assertEqual(set(data["hooks"]), {"PreToolUse", "UserPromptSubmit", "SessionStart", "Stop"})
            subprocess.run([sys.executable, str(installer), "--project", proj, "--uninstall"],
                           check=True, capture_output=True)
            data = json.loads(settings.read_text())
            self.assertEqual(data["hooks"], {"PreToolUse": [other]})


if __name__ == "__main__":
    unittest.main()
