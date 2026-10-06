"""The whole driver -- subprocess, stream parsing, checkpoints, state -- against
tests/fake_agent.py speaking each agent's output format. Free and offline."""

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _util import FAKE_AGENT, git, make_repo, run_loop

FAKE = f'"{sys.executable}" "{FAKE_AGENT}"'
PLAN = """\
# Offline plan

- [ ] **1. One.** First.
- [ ] **2. Two.** Second.
- [ ] **3. Three.** Third.
"""
FAST = ["--cooldown-s", "0", "--turn-timeout-s", "120", "--max-sessions", "5",
        "--max-consecutive-stalls", "1", "--max-consecutive-errors", "1"]


class DriverOfflineTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo(PLAN)
        self.state_dir = self.repo / ".ai-loop" / "PLAN-md"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def run_loop(self, *args, env=None):
        p = run_loop(self.repo, "PLAN.md", *args, *FAST, env=env)
        log = (self.state_dir / "driver.log")
        self.driver_log = log.read_text(encoding="utf-8") if log.exists() else ""
        return p

    def state(self):
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def assert_plan_done(self, p, sessions):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PLAN COMPLETE", self.driver_log)
        self.assertFalse((self.state_dir / "HALTED").exists())
        plan = (self.repo / "PLAN.md").read_text(encoding="utf-8")
        self.assertEqual(plan.count("- [x]"), 3)
        self.assertEqual(plan.count("- [ ]"), 0)
        self.assertEqual(sorted(f.name for f in self.repo.glob("milestone-*.txt")),
                         ["milestone-1.txt", "milestone-2.txt", "milestone-3.txt"])
        self.assertEqual(int(git(self.repo, "rev-list", "--count", "HEAD")), 4)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        st = self.state()
        self.assertEqual(st["milestones"], 3)
        self.assertEqual(st["sessions"], sessions)
        self.assertEqual(len(st["history"]), 3)
        return st

    def test_claude_resume_then_rotate(self):
        p = self.run_loop("--agent", "claude", "--agent-bin", FAKE, "--model", "fake",
                          "--fallback-model", "", "--context-threshold", "0.9",
                          "--max-turns-per-session", "2")
        st = self.assert_plan_done(p, sessions=2)
        h = st["history"]
        self.assertEqual([e["agent"] for e in h], ["claude"] * 3)
        self.assertEqual(h[0]["session_id"], h[1]["session_id"])
        self.assertEqual((h[0]["turn"], h[1]["turn"]), (1, 2))
        self.assertTrue(h[0]["decision"].endswith("continue this session"), h[0]["decision"])
        self.assertTrue(h[1]["decision"].startswith("--max-turns-per-session"), h[1]["decision"])
        self.assertNotEqual(h[2]["session_id"], h[0]["session_id"])
        self.assertEqual(h[0]["ctx_pct"], 0.01)
        self.assertEqual(st["total_cost_usd"], 0.03)
        self.assertEqual(len(list((self.state_dir / "logs").glob("*.jsonl"))), 2)

    def test_claude_usage_limit_benches(self):
        before = time.time()
        p = self.run_loop("--agent", "claude", "--agent-bin", FAKE, "--model", "fake",
                          "--fallback-model", "", env={"FAKE_AGENT_SCENARIO": "limit"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("USAGE LIMIT on 'fake' (server reset time (five_hour)", self.driver_log)
        until = self.state()["limited_until"]["fake"]
        self.assertGreater(until, before + 3600)

    def test_restart_clears_benches(self):
        args = ("--agent", "claude", "--agent-bin", FAKE, "--model", "fake",
                "--fallback-model", "")
        self.run_loop(*args, env={"FAKE_AGENT_SCENARIO": "limit"})
        self.assertGreater(self.state()["limited_until"]["fake"], time.time())
        (self.state_dir / "STOP").unlink()
        p = self.run_loop(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("-- cleared: a restart re-checks the quota", self.driver_log)
        self.assertIn("PLAN COMPLETE", self.driver_log)
        self.assertNotIn("every model is usage-limited", self.driver_log.split("cleared")[-1])

    def test_codex_resumes_with_rollout_context(self):
        home = tempfile.mkdtemp()
        try:
            p = self.run_loop("--agent", "codex", "--agent-bin", FAKE,
                              "--context-threshold", "0.9", "--max-turns-per-session", "2",
                              env={"FAKE_AGENT_FORMAT": "codex", "CODEX_HOME": home,
                                   "FAKE_AGENT_ROLLOUT": "1"})
            st = self.assert_plan_done(p, sessions=2)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        h = st["history"]
        self.assertEqual(h[0]["model"], "codex:default")
        self.assertEqual(h[0]["model_id"], "fake-codex")
        self.assertEqual(h[0]["ctx_pct"], 0.05)
        self.assertEqual(h[0]["session_id"], h[1]["session_id"])
        self.assertNotEqual(h[2]["session_id"], h[0]["session_id"])

    def test_codex_without_rollout_rotates(self):
        home = tempfile.mkdtemp()
        try:
            p = self.run_loop("--agent", "codex", "--agent-bin", FAKE,
                              env={"FAKE_AGENT_FORMAT": "codex", "CODEX_HOME": home})
            st = self.assert_plan_done(p, sessions=3)
        finally:
            shutil.rmtree(home, ignore_errors=True)
        self.assertEqual(st["history"][0]["decision"], "context unknown -> fresh session")

    def test_codex_usage_limit(self):
        p = self.run_loop("--agent", "codex", "--agent-bin", FAKE,
                          env={"FAKE_AGENT_FORMAT": "codex", "FAKE_AGENT_SCENARIO": "limit"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("USAGE LIMIT on 'codex:default' (reset time read as 'try again in 2 hours'",
                      self.driver_log)

    def test_gemini_fresh_session_per_milestone(self):
        p = self.run_loop("--agent", "gemini", "--agent-bin", FAKE,
                          env={"FAKE_AGENT_FORMAT": "gemini"})
        st = self.assert_plan_done(p, sessions=3)
        self.assertEqual(st["history"][0]["decision"],
                         "gemini cannot resume a session -> fresh session")
        self.assertTrue(st["history"][0]["session_id"])

    def test_gemini_quota(self):
        p = self.run_loop("--agent", "gemini", "--agent-bin", FAKE,
                          env={"FAKE_AGENT_FORMAT": "gemini", "FAKE_AGENT_SCENARIO": "limit"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("reset time read as 'reset after 1h30m'", self.driver_log)

    def test_custom_prompt_file_with_echo(self):
        p = self.run_loop("--agent-cmd", f"{FAKE} --prompt-file {{prompt_file}}",
                          env={"FAKE_AGENT_FORMAT": "text", "FAKE_AGENT_ECHO": "1"})
        st = self.assert_plan_done(p, sessions=3)
        self.assertTrue(all(e["loop_status"].startswith("LANDED") for e in st["history"]))
        self.assertEqual(len(list((self.state_dir / "logs").glob("*.prompt.md"))), 3)


if __name__ == "__main__":
    unittest.main()
