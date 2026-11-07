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

    def write(self, rel, content="...", env=None, tool="Write", **extra):
        payload = {"session_id": self.session, "cwd": str(self.root), "hook_event_name": "PreToolUse",
                   "tool_name": tool, "tool_input": {"file_path": str(self.root / rel), "content": content}}
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
        self.assertIn("1 allowed because unattended", msg)
        self.assertNotIn("README_v2.md", msg)  # summary mode: no file names
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
        self.assertIn("allowed because unattended", self.stop()["systemMessage"])
        self.assertIn("budget 1", (Path(HOME) / "sprawl-guard.log").read_text())

    def test_budget_off(self):
        self.config(file_budget=0)
        for i in range(12):
            self.assertIsNone(self.write(f"mod{chr(97 + i)}.py"))


class Context(Base):
    def test_prompt_is_silent_by_default(self):
        self.assertIsNone(self.prompt("hi"))

    def session_start(self, source):
        out = run("session-start", {"session_id": self.session, "cwd": str(self.root), "source": source})
        return out["hookSpecificOutput"]["additionalContext"] if out else None

    def test_rules_at_every_session_start_by_default(self):
        for source in ("startup", "resume", "clear", "compact"):
            self.assertIn("Workspace rules", self.session_start(source), source)

    def test_compact_mode_only_after_compaction(self):
        self.config(reminder="compact", file_index=False)
        self.assertIsNone(self.session_start("startup"))
        self.assertIn("Workspace rules", self.session_start("compact"))

    def test_prose_line_follows_prose_setting(self):
        self.assertIn("no em dashes", self.session_start("startup"))
        self.config(prose="off")
        self.assertNotIn("no em dashes", self.session_start("startup"))

    def test_subagents_get_the_rules(self):
        out = run("subagent-start", {"session_id": self.session, "cwd": str(self.root), "agent_type": "Explore"})
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        self.assertIn("Workspace rules", out["hookSpecificOutput"]["additionalContext"])

    def test_subagent_rules_can_be_turned_off(self):
        self.config(subagent_rules=False)
        self.assertIsNone(run("subagent-start", {"session_id": self.session, "cwd": str(self.root)}))
        self.config(reminder="off")
        self.assertIsNone(run("subagent-start", {"session_id": self.session, "cwd": str(self.root)}))

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


class FileIndex(Base):
    def context(self, source="startup"):
        out = run("session-start", {"session_id": self.session, "cwd": str(self.root), "source": source})
        return out["hookSpecificOutput"]["additionalContext"] if out else ""

    def test_lists_files_by_folder(self):
        self.touch("train.py")
        self.touch("src/model.py")
        self.touch("src/data.py")
        self.touch(".hidden/secret.py")
        self.touch("outputs/run1/train.log")
        ctx = self.context()
        self.assertIn("./: train.py", ctx)
        self.assertIn("src/: data.py, model.py", ctx)
        self.assertNotIn("secret.py", ctx)
        self.assertNotIn("train.log", ctx)

    def test_not_repeated_on_resume(self):
        self.touch("train.py")
        self.context("startup")
        self.assertNotIn("train.py", self.context("resume"))
        self.assertIn("train.py", self.context("compact"))

    def test_big_project_gets_folder_counts(self):
        self.config(file_index_max_files=5)
        for i in range(8):
            self.touch(f"src/m{i}.py")
        ctx = self.context()
        self.assertIn("src/ (8)", ctx)
        self.assertNotIn("m3.py", ctx)

    def test_can_be_turned_off(self):
        self.config(file_index=False)
        self.touch("train.py")
        self.assertNotIn("train.py", self.context())


class Profiles(Base):
    def cli(self, *args, home=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SPRAWL_GUARD", "CLAUDE_PROJECT_DIR"))}
        env["SPRAWL_GUARD_HOME"] = home or HOME
        env["CLAUDE_PROJECT_DIR"] = str(self.root)
        res = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, env=env, timeout=60)
        return res.returncode, res.stdout.decode()

    def test_relaxed_turns_denies_into_warnings(self):
        self.config(profile="relaxed")
        self.touch("train.py")
        self.assertEqual(self.decision(self.write("train_v2.py")), "warn")

    def test_strict_tightens(self):
        self.config(profile="strict")
        self.assertEqual(self.decision(self.write("design.md")), "confirm")
        self.assertEqual(self.decision(self.write("model.pt")), "deny")

    def test_own_keys_override_profile(self):
        self.config(profile="relaxed", versioned_files="deny")
        self.touch("train.py")
        self.assertEqual(self.decision(self.write("train_v2.py")), "deny")

    def test_off_profile_disables(self):
        self.config(profile="off")
        self.touch("train.py")
        self.assertIsNone(self.write("train_v2.py"))

    def test_cli_sets_and_shows_profile(self):
        self.config(file_budget=3)
        _, out = self.cli("profile", "strict")
        self.assertIn("Profile set to 'strict'", out)
        saved = json.loads((self.root / ".claude" / "sprawl-guard.json").read_text())
        self.assertEqual(saved, {"file_budget": 3, "profile": "strict"})  # other keys kept
        self.assertIn("override the profile: file_budget", out)
        _, out = self.cli("profile")
        self.assertIn("Profile: strict", out)
        self.assertIn("prose=strict", out)

    def test_cli_global_and_unknown(self):
        with tempfile.TemporaryDirectory() as home:
            self.cli("profile", "relaxed", "--global", home=home)
            self.assertEqual(json.loads((Path(home) / "sprawl-guard.json").read_text())["profile"], "relaxed")
        _, out = self.cli("profile", "maximum")
        self.assertIn("Unknown profile", out)


class Prose(Base):
    TEXT = "# Setup\n\nThis tool is seamless — it just works.\n"

    def post(self, rel, content=None, tool="Write", **tool_input):
        if content is not None:
            tool_input["content"] = content
        payload = {"session_id": self.session, "cwd": str(self.root), "hook_event_name": "PostToolUse",
                   "tool_name": tool, "tool_input": {"file_path": str(self.root / rel), **tool_input}}
        out = run("post-write", payload)
        return out["hookSpecificOutput"]["additionalContext"] if out else None

    def scan(self, text, **cfg):
        sys.path.insert(0, str(SCRIPT.parent))
        import sprawl_guard
        conf = dict(sprawl_guard.DEFAULT_CONFIG, **cfg)
        return [(f["kind"], f["match"]) for f in sprawl_guard.prose_scan(text, conf)]

    def test_write_to_doc_is_checked(self):
        msg = self.post("README.md", self.TEXT)
        self.assertIn("line 3: '—' (em dash)", msg)
        self.assertIn("'seamless' (stock phrase)", msg)

    def test_code_files_and_other_places_skipped(self):
        self.assertIsNone(self.post("train.py", self.TEXT))
        self.assertIsNone(self.post("LICENSE.txt", self.TEXT))
        self.assertIsNone(self.post("outputs/run/notes.md", self.TEXT))
        self.assertIsNone(self.post(".claude/notes.md", self.TEXT))
        self.config(prose_skip_paths=["docs/**"])
        self.assertIsNone(self.post("docs/a.md", self.TEXT))

    def test_off(self):
        self.config(prose="off")
        self.assertIsNone(self.post("README.md", self.TEXT))

    def test_edit_checks_only_new_text_with_file_line_numbers(self):
        self.touch("README.md", "# Title\n\nOld seamless text by the user.\n\nNew part — here.\n")
        msg = self.post("README.md", tool="Edit", old_string="x", new_string="New part — here.")
        self.assertIn("line 5:", msg)
        self.assertNotIn("seamless", msg)

    def test_multiedit(self):
        self.touch("notes.md", "a\nLet's dive in.\n")
        msg = self.post("notes.md", tool="MultiEdit", edits=[{"old_string": "x", "new_string": "Let's dive in."}])
        self.assertIn("line 2:", msg)

    def test_patterns(self):
        cases = {
            "This isn't just a tool, it's a habit.": "contrast frame",
            "It's not about speed. It's about trust.": "contrast frame",
            "Not a bug, but a feature.": "contrast frame",
            "The result? Fewer files.": "self-answered question",
            "Hope this helps!": "closing pleasantry",
            "It could potentially fail.": "stacked hedge",
            "- \U0001F680 Fast install": "emoji",
            "Values range 1 -- 5.": "em dash",
        }
        for text, kind in cases.items():
            self.assertIn(kind, [k for k, _ in self.scan(text)], text)

    def test_near_misses(self):
        for text in ("Does it need a config file? No.", "Use the test harness.", "It is not ready yet.",
                     "A robust model under shift.", "Pages 1-5 and 2026-11-08.", "Rated 4–5 stars."):
            self.assertEqual(self.scan(text), [], text)

    def test_code_quotes_links_front_matter_skipped(self):
        text = ("---\ntitle: seamless\n---\nUse `delve` here.\n```\nseamless\n```\n> seamless quote\n"
                "[link](https://x.com/seamless) and <!-- seamless -->\n")
        self.assertEqual(self.scan(text), [])

    def test_strict_extras_and_allow_list(self):
        self.assertEqual(self.scan("A crucial step."), [])
        self.assertIn(("stock phrase", "crucial"), self.scan("A crucial step.", prose="strict"))
        self.assertIn("title case heading", [k for k, _ in self.scan("## Getting Started With Profiles", prose="strict")])
        self.assertEqual(self.scan("A seamless step.", prose_allow=["seamless"]), [])
        self.assertIn(("stock phrase", "blazing"), self.scan("blazing fast", prose_words_extra=["blazing"]))

    def test_cli(self):
        self.touch("a.md", self.TEXT)
        res = subprocess.run([sys.executable, str(SCRIPT), "prose", str(self.root / "a.md")],
                             capture_output=True, env={**os.environ, "SPRAWL_GUARD_HOME": HOME}, timeout=60)
        self.assertEqual(res.returncode, 1)
        self.assertIn("2 finding(s)", res.stdout.decode())


class TurnCheck(Base):
    def test_summary_by_default_without_names(self):
        self.touch("existing.py")
        self.start()
        self.touch("made_by_claude.py")
        msg = self.stop()["systemMessage"]
        self.assertTrue(msg.startswith("Files this turn: 1 created"), msg)
        self.assertNotIn("sprawl-guard", msg)  # no tag: Claude Code labels the message itself
        self.assertNotIn("made_by_claude.py", msg)
        self.assertIsNone(self.stop())  # already reported

    def test_list_mode_shows_names(self):
        self.config(turn_check="list")
        self.touch("existing.py")
        self.start()
        self.touch("made_by_claude.py")
        msg = self.stop()["systemMessage"]
        self.assertIn("made_by_claude.py", msg)
        self.assertNotIn("existing.py", msg)

    def test_nothing_new_is_silent(self):
        self.start()
        self.assertIsNone(self.stop())

    def test_off(self):
        self.config(turn_check="off")
        self.start()
        self.touch("a.py")
        self.assertIsNone(self.stop())

    def test_old_stop_check_key_still_works(self):
        self.config(stop_check="off")
        self.start()
        self.touch("a.py")
        self.assertIsNone(self.stop())

    def test_hidden_folders_skipped(self):
        # the v0.2 problem: thousands of files under .antigravity-server and .gemini in a home folder
        self.start()
        for i in range(30):
            self.touch(f".antigravity-server/extensions/ext{i}/out.js")
        self.touch(".gemini/antigravity-ide/crashes/crash.log")
        self.assertIsNone(self.stop())

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_hidden_folders_skipped_in_git_repo(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.start()
        self.touch(".cache/tool/state.json")
        self.assertIsNone(self.stop())

    def test_outputs_and_env_folders_skipped(self):
        self.start()
        self.touch("outputs/run1/model.pt")
        self.touch("wandb/run-1/files/config.yaml")
        self.touch("miniconda3/lib/x.py")
        self.assertIsNone(self.stop())

    def test_force_mode_blocks_once(self):
        self.config(turn_check="force")
        self.start()
        self.touch("helper.py")
        self.assertEqual(self.stop()["decision"], "block")
        self.touch("another.py")
        self.assertIn("systemMessage", self.stop(stop_hook_active=True))

    def test_force_mode_off_when_unattended(self):
        self.config(turn_check="force")
        self.start()
        self.prompt("work autonomously until done")
        self.touch("helper.py")
        out = self.stop()
        self.assertNotIn("decision", out)
        self.assertIn("unattended mode was on", out["systemMessage"])

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_git_repo_respects_gitignore(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.touch(".gitignore", "checkpoints/\n")
        self.config(turn_check="list")
        self.start()
        self.touch("checkpoints/best.pt")
        self.touch("new_module.py")
        msg = self.stop()["systemMessage"]
        self.assertIn("new_module.py", msg)
        self.assertNotIn("best.pt", msg)

    def test_edits_and_deletions_counted(self):
        self.touch("train.py", "a = 1\n")
        self.touch("old.py", "b = 2\n")
        self.touch("README.md", "readme\n")
        self.start()
        self.prompt("refactor")
        self.touch("train.py", "a = 1\nb = 2\n")
        (self.root / "old.py").unlink()
        msg = self.stop()["systemMessage"]
        self.assertEqual(msg, "Files this turn: 1 edited, 1 deleted")

    def test_list_mode_names_each_kind(self):
        self.config(turn_check="list", orphan_check=False)
        self.touch("train.py", "a = 1\n")
        self.touch("old.py", "b = 2\n")
        self.prompt("go")
        self.touch("train.py", "a = 10\n")
        self.touch("model.py", "m = 1\n")
        (self.root / "old.py").unlink()
        msg = self.stop()["systemMessage"]
        self.assertIn("1 created, 1 edited, 1 deleted", msg)
        self.assertIn("(created: model.py; edited: train.py; deleted: old.py)", msg)

    def test_changes_between_turns_not_counted(self):
        self.touch("train.py", "a = 1\n")
        self.start()
        self.touch("train.py", "edited by you between turns\n")
        self.touch("yours.py", "x\n")
        self.prompt("next task")  # the turn starts here
        self.assertIsNone(self.stop())

    def test_unchanged_files_not_counted_as_edited(self):
        self.touch("a.py", "x\n")
        self.prompt("look at a.py")
        (self.root / "a.py").read_text()
        self.assertIsNone(self.stop())

    def test_v03_snapshot_in_state_still_works(self):
        self.touch("a.py", "x\n")
        state_dir = Path(tempfile.gettempdir()) / "sprawl-guard"
        state_dir.mkdir(exist_ok=True)
        (state_dir / f"{self.session}.json").write_text(json.dumps({"snapshot": ["a.py"]}))
        self.touch("a.py", "edited\n")  # no mtime in the old format: not an edit
        self.touch("b.py", "x\n")
        self.assertEqual(self.stop()["systemMessage"], "Files this turn: 1 created")
        self.touch("a.py", "edited again\n")
        self.assertEqual(self.stop()["systemMessage"], "Files this turn: 1 edited")

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_tracked_files_edits_and_deletes_in_git_repo(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.touch("train.py", "a = 1\n")
        self.touch("eval.py", "e = 1\n")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.prompt("go")
        self.touch("train.py", "a = 2\nb = 3\n")
        (self.root / "eval.py").unlink()
        self.assertEqual(self.stop()["systemMessage"], "Files this turn: 1 edited, 1 deleted")

    def test_outputs_folder_edits_not_counted(self):
        self.touch("outputs/run1/train.log", "1\n")
        self.prompt("go")
        self.touch("outputs/run1/train.log", "1\n2\n")
        self.assertIsNone(self.stop())

    def test_missing_snapshot_starts_tracking(self):
        self.touch("a.py")
        self.assertIsNone(self.stop())
        self.touch("b.py")
        self.assertIn("1 created", self.stop()["systemMessage"])


class Orphans(Base):
    def test_unreferenced_code_counted(self):
        self.start()
        self.touch("math_ops.py", "def add(a, b):\n    return a + b\n")
        self.touch("pipeline.py", "from math_ops import add\n")
        msg = self.stop()["systemMessage"]
        self.assertIn("Files this turn: 2 created; 1 new file(s) not referenced anywhere", msg)

    def test_list_mode_names_orphans(self):
        self.config(turn_check="list")
        self.start()
        self.touch("math_ops.py", "x = 1\n")
        self.touch("pipeline.py", "import math_ops\n")
        msg = self.stop()["systemMessage"]
        self.assertIn("not referenced: pipeline.py", msg)
        self.assertIn("created: math_ops.py, pipeline.py", msg)

    def test_mention_in_readme_counts_as_reference(self):
        self.touch("README.md", "Run `python evaluate.py` to evaluate.\n")
        self.start()
        self.touch("evaluate.py", "print(1)\n")
        self.assertNotIn("not referenced", self.stop()["systemMessage"])

    def test_tests_and_non_code_ignored(self):
        self.start()
        self.touch("test_model.py", "x = 1\n")
        self.touch("notes.md", "x\n")
        self.assertNotIn("not referenced", self.stop()["systemMessage"])

    def test_can_be_disabled(self):
        self.config(orphan_check=False)
        self.start()
        self.touch("lonely.py", "x = 1\n")
        self.assertNotIn("not referenced", self.stop()["systemMessage"])


class Outputs(Base):
    def test_checkpoint_and_log_warn(self):
        for name in ("model.pt", "train.log", "weights.safetensors"):
            out = self.write(name)
            self.assertEqual(self.decision(out), "warn", name)
            self.assertIn("outputs/<run>/", out["hookSpecificOutput"]["additionalContext"])

    def test_results_data_files_warn(self):
        self.assertEqual(self.decision(self.write("results.csv")), "warn")
        self.assertEqual(self.decision(self.write("eval_metrics.json")), "warn")
        self.assertEqual(self.decision(self.write("results.txt")), "warn")  # not treated as a report

    def test_plain_data_and_config_not_flagged(self):
        self.assertIsNone(self.write("data/train.csv"))
        self.assertIsNone(self.write("config.json"))

    def test_inside_outputs_folder_anything_goes(self):
        self.assertIsNone(self.write("outputs/run1/model.pt"))
        self.assertIsNone(self.write("outputs/run1/debug_notes_final_v2.txt"))

    def test_custom_outputs_dir(self):
        self.config(outputs_dir="runs")
        self.assertIsNone(self.write("runs/exp3/metrics.json"))
        self.assertIn("runs/<run>/", self.write("metrics.json")["hookSpecificOutput"]["additionalContext"])


class RootFolder(Base):
    def test_off_by_default(self):
        self.touch("src/model.py")
        self.assertIsNone(self.write("helpers.py"))

    def test_warn_points_to_existing_code_folder(self):
        self.config(root_files="warn")
        self.touch("src/model.py")
        out = self.write("helpers.py")
        self.assertEqual(self.decision(out), "warn")
        self.assertIn("'src/'", out["hookSpecificOutput"]["additionalContext"])

    def test_no_code_folders_no_warning(self):
        self.config(root_files="warn")
        self.touch("docs/guide.md")
        self.assertIsNone(self.write("helpers.py"))

    def test_entry_points_and_nested_files_allowed(self):
        self.config(root_files="warn")
        self.touch("src/model.py")
        self.assertIsNone(self.write("main.py"))
        self.assertIsNone(self.write("src/layers.py"))


class Protected(Base):
    def test_new_file_in_protected_glob_denied(self):
        self.config(protected_paths=["data/raw/**"])
        out = self.write("data/raw/extra.csv")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("protected path", self.reason(out))

    def test_edit_of_existing_file_denied(self):
        self.config(protected_paths=["data/raw/**"])
        self.touch("data/raw/ecg.csv")
        self.assertEqual(self.decision(self.write("data/raw/ecg.csv", tool="Edit")), "deny")
        self.assertEqual(self.decision(self.write("data/raw/ecg.csv", tool="Write")), "deny")

    def test_plain_folder_entry_covers_subfolders(self):
        self.config(protected_paths=["results/final"])
        self.assertEqual(self.decision(self.write("results/final/table.tex")), "deny")
        self.assertIsNone(self.write("results/draft/table.tex"))

    def test_protected_even_when_named_or_unattended(self):
        self.config(protected_paths=["data/raw"])
        self.prompt("overwrite data/raw/ecg.csv, work autonomously")
        self.assertEqual(self.decision(self.write("data/raw/ecg.csv")), "deny")

    def test_edits_elsewhere_untouched(self):
        self.config(protected_paths=["data/raw/**"])
        self.touch("train.py")
        self.assertIsNone(self.write("train.py", tool="Edit"))

    def test_edit_tool_never_runs_sprawl_rules(self):
        self.touch("train_v2.py")
        self.assertIsNone(self.write("train_v2.py", tool="Edit"))
        self.assertIsNone(self.write("nb.ipynb", tool="NotebookEdit"))

    def test_other_tools_ignored(self):
        self.config(protected_paths=["data/raw/**"])
        self.assertIsNone(self.write("data/raw/x.csv", tool="Read"))


def tidy(root, *args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPRAWL_GUARD", "CLAUDE_PROJECT_DIR"))}
    e["SPRAWL_GUARD_HOME"] = HOME
    e["CLAUDE_PROJECT_DIR"] = str(root)
    e.update(env or {})
    res = subprocess.run([sys.executable, str(SCRIPT), "tidy", *args], capture_output=True, env=e,
                         cwd=str(root), timeout=60)
    assert res.returncode == 0, res.stderr.decode()
    return res.stdout.decode()


class Tidy(Base):
    def scan(self, *args):
        return json.loads(tidy(self.root, *args, "--json"))

    def test_versioned_group_with_original(self):
        self.touch("train.py", "a\n")
        self.touch("train_v2.py", "a\nb\n")
        self.touch("train_final.py", "a\nc\n")
        g = self.scan()["versioned"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["original"], "train.py")
        self.assertEqual(sorted(g[0]["copies"]), ["train_final.py", "train_v2.py"])
        self.assertEqual(g[0]["kind"], "versioned")

    def test_backup_group(self):
        self.touch("src/model.py", "m\n")
        self.touch("src/model_backup.py", "m\n")
        g = self.scan()["versioned"][0]
        self.assertEqual(g["kind"], "backup")
        self.assertEqual(g["original"], "src/model.py")

    def test_versions_without_original(self):
        self.touch("model_v2.py", "a\n")
        self.touch("model_v3.py", "b\n")
        g = self.scan()["versioned"][0]
        self.assertIsNone(g["original"])

    def test_single_versioned_name_is_not_sprawl(self):
        self.touch("resnet_v2.py", "a\n")
        self.assertEqual(self.scan()["versioned"], [])

    def test_different_folders_not_grouped(self):
        self.touch("a/train.py", "a\n")
        self.touch("b/train_v2.py", "a\n")
        self.assertEqual(self.scan()["versioned"], [])

    def test_near_copies(self):
        self.touch("train.py", CODE)
        self.touch("train_resnet.py", CODE.replace("print('train')", "print('resnet')"))
        self.touch("other.py", "\n".join(f"v{i} = {i}" for i in range(20)))
        g = self.scan()["near_copies"]
        self.assertEqual(len(g), 1)
        self.assertEqual([f["path"] for f in g[0]["files"]], ["train.py", "train_resnet.py"])

    def test_oneoffs_and_reports(self):
        self.touch("debug_loss.py", "x\n")
        self.touch("IMPLEMENTATION_SUMMARY.md", "x\n")
        self.touch("README.md", "x\n")
        self.touch("src/model.py", "x\n")
        res = self.scan()
        self.assertEqual([f["path"] for f in res["oneoffs"]], ["debug_loss.py"])
        self.assertEqual([f["path"] for f in res["reports"]], ["IMPLEMENTATION_SUMMARY.md"])

    def test_skips_hidden_outputs_and_protected(self):
        self.config(protected_paths=["data/raw"])
        self.touch(".cache/debug_x.py", "x\n")
        self.touch("outputs/run1/debug_x.py", "x\n")
        self.touch("data/raw/debug_y.py", "x\n")
        res = self.scan()
        self.assertEqual(res["oneoffs"], [])
        self.assertEqual(res["protected_skipped"], 1)

    def test_rule_off_skips_kind(self):
        self.config(oneoff_scripts="off", report_files="off")
        self.touch("debug_loss.py", "x\n")
        self.touch("SUMMARY.md", "x\n")
        res = self.scan()
        self.assertEqual((res["oneoffs"], res["reports"]), ([], []))

    def test_folder_argument_limits_scope(self):
        self.touch("a/debug_x.py", "x\n")
        self.touch("b/debug_y.py", "x\n")
        self.assertEqual([f["path"] for f in self.scan("a")["oneoffs"]], ["a/debug_x.py"])

    def test_text_report(self):
        self.touch("paper.tex", "a\n")
        self.touch("paper_final.tex", "a\nb\n")
        out = tidy(self.root)
        self.assertIn("Versioned copies and backups", out)
        self.assertIn("'paper.tex' <- paper_final.tex", out)
        self.assertIn("2 lines", out)

    def test_nothing_found(self):
        self.touch("src/model.py", "x\n")
        self.assertIn("No sprawl found.", tidy(self.root))

    @unittest.skipUnless(HAS_GIT, "git not installed")
    def test_untracked_marked_in_git_repo(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.touch("train.py", "a\n")
        subprocess.run(["git", "-C", str(self.root), "add", "train.py"], check=True)
        self.touch("train_v2.py", "a\n")
        files = {f["path"]: f for f in self.scan()["versioned"][0]["files"]}
        self.assertTrue(files["train.py"]["tracked"])
        self.assertFalse(files["train_v2.py"]["tracked"])
        self.assertIn("untracked", tidy(self.root))

    def test_scan_never_changes_files(self):
        self.touch("train.py", "a\n")
        self.touch("train_v2.py", "b\n")
        before = sorted(p.name for p in self.root.rglob("*"))
        tidy(self.root)
        self.assertEqual(before, sorted(p.name for p in self.root.rglob("*")))


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
            self.assertEqual(set(data["hooks"]), {"PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart",
                                                  "SubagentStart", "Stop"})
            self.assertEqual(data["hooks"]["PreToolUse"][1]["matcher"], "Write|Edit|MultiEdit|NotebookEdit")
            subprocess.run([sys.executable, str(installer), "--project", proj, "--uninstall"],
                           check=True, capture_output=True)
            data = json.loads(settings.read_text())
            self.assertEqual(data["hooks"], {"PreToolUse": [other]})

    def test_skills_installed_and_removed(self):
        installer = Path(__file__).resolve().parent / "install.py"
        with tempfile.TemporaryDirectory() as proj:
            subprocess.run([sys.executable, str(installer), "--project", proj], check=True, capture_output=True)
            skill = Path(proj) / ".claude" / "skills" / "tidy" / "SKILL.md"
            text = skill.read_text()
            self.assertIn("name: tidy", text)
            self.assertIn("sprawl_guard.py tidy $ARGUMENTS`", text)
            self.assertIn("allowed-tools:\n  - Bash(", text)
            profile = Path(proj) / ".claude" / "skills" / "sprawl" / "SKILL.md"
            self.assertIn("sprawl_guard.py profile $ARGUMENTS`", profile.read_text())
            subprocess.run([sys.executable, str(installer), "--project", proj, "--uninstall"],
                           check=True, capture_output=True)
            self.assertFalse(skill.exists())
            self.assertFalse(profile.exists())

    def test_existing_foreign_tidy_skill_left_alone(self):
        installer = Path(__file__).resolve().parent / "install.py"
        with tempfile.TemporaryDirectory() as proj:
            skill = Path(proj) / ".claude" / "skills" / "tidy" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("my own tidy")
            out = subprocess.run([sys.executable, str(installer), "--project", proj], check=True,
                                 capture_output=True).stdout.decode()
            self.assertIn("Skipped /tidy", out)
            subprocess.run([sys.executable, str(installer), "--project", proj, "--uninstall"],
                           check=True, capture_output=True)
            self.assertEqual(skill.read_text(), "my own tidy")


class Plugin(unittest.TestCase):
    HERE = Path(__file__).resolve().parent

    def test_plugin_files_match_installer(self):
        sys.path.insert(0, str(self.HERE))
        import install
        for path, text in install.plugin_files().items():
            self.assertEqual(path.read_text(encoding="utf-8"), text,
                             f"{path.name} is out of date: run python install.py --build-plugin")

    def test_manifest_versions_match_script(self):
        sys.path.insert(0, str(self.HERE))
        import sprawl_guard
        manifest = json.loads((self.HERE / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((self.HERE / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(manifest["version"], sprawl_guard.VERSION)
        self.assertEqual(market["plugins"][0]["name"], manifest["name"])

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_plugin_hook_command_runs(self):
        hooks = json.loads((self.HERE / "hooks" / "hooks.json").read_text())["hooks"]
        command = hooks["SubagentStart"][0]["hooks"][0]["command"]
        command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(self.HERE))
        with tempfile.TemporaryDirectory() as proj:
            env = {**os.environ, "SPRAWL_GUARD_HOME": HOME, "CLAUDE_PROJECT_DIR": proj}
            res = subprocess.run(["bash", "-c", command], input=b'{"session_id": "p"}',
                                 capture_output=True, env=env, timeout=60)
        self.assertIn("Workspace rules", json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
