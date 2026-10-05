import json
import time
import unittest
from argparse import Namespace

from _util import Log

from loop_agents import (GEMINI_PROMPT_TAIL, GeminiAgent, TurnResult, TurnSpec,
                         resolve_options)


def cfg(**kw):
    base = dict(model=None, fallback_model=None, effort=None, permission_mode=None,
                context_window=None, max_budget_usd=0.0)
    base.update(kw)
    c = Namespace(**base)
    errors, warnings = resolve_options(GeminiAgent(), c)
    return c, errors, warnings


def parse(events, exit_code=0):
    agent = GeminiAgent()
    res, log = TurnResult(), Log()
    parser = agent.new_parser("prompt")
    for ev in events:
        parser.feed(json.dumps(ev), res, log)
    res.exit_code = exit_code
    parser.finish(res, log)
    return agent, res, log


class GeminiArgvTest(unittest.TestCase):
    def test_defaults(self):
        c, errors, warnings = cfg()
        self.assertEqual((errors, warnings), ([], []))
        argv = GeminiAgent().argv(["gemini"], c, TurnSpec(c.model, None, "n", None, "p", "P", "."))
        self.assertEqual(argv, ["gemini", "--output-format", "stream-json",
                                "--approval-mode", "yolo", "-p", GEMINI_PROMPT_TAIL])

    def test_model(self):
        c, _, _ = cfg(model="gemini-3-pro", permission_mode="auto_edit")
        argv = GeminiAgent().argv(["gemini"], c, TurnSpec(c.model, None, "n", None, "p", "P", "."))
        self.assertEqual(argv[3:7], ["--approval-mode", "auto_edit", "-m", "gemini-3-pro"])

    def test_effort_warns(self):
        _, errors, warnings = cfg(effort="high")
        self.assertEqual(errors, [])
        self.assertTrue(any("--effort is ignored" in w for w in warnings))

    def test_no_resume(self):
        self.assertFalse(GeminiAgent().supports_resume)


class GeminiParserTest(unittest.TestCase):
    def test_deltas_are_joined(self):
        _, res, log = parse([
            {"type": "init", "session_id": "g-1", "model": "gemini-3-pro"},
            {"type": "message", "role": "user", "content": "the prompt"},
            {"type": "message", "role": "assistant", "content": "Working.", "delta": True},
            {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t",
             "parameters": {"command": "git status"}},
            {"type": "tool_result", "tool_id": "t", "status": "success"},
            {"type": "message", "role": "assistant", "content": "Done.\nLOOP_", "delta": True},
            {"type": "message", "role": "assistant", "content": "STATUS: LANDED 2. B",
             "delta": True},
            {"type": "result", "status": "success", "stats": {"total_tokens": 9, "tool_calls": 1}},
        ])
        self.assertFalse(res.failed)
        self.assertEqual(res.session_id, "g-1")
        self.assertEqual(res.loop_status, "LANDED 2. B")
        self.assertIn("  -> run_shell_command(git status)", log.lines)

    def test_result_error(self):
        _, res, _ = parse([{"type": "result", "status": "error",
                            "error": {"type": "Error", "message": "boom"}}], exit_code=1)
        self.assertTrue(res.failed)
        self.assertIn("boom", res.haystack)

    def test_quota(self):
        agent, res, _ = parse([
            {"type": "error", "severity": "error",
             "message": "You have exhausted your daily quota on this model. "
                        "Your quota will reset after 2h5m."},
            {"type": "result", "status": "error"}], exit_code=1)
        sig = agent.limit_signal(res, 1200)
        self.assertAlmostEqual(sig.epoch, time.time() + 7500, delta=5)

    def test_resource_exhausted_without_time_probes(self):
        agent, res, _ = parse([{"type": "error", "message": "429 RESOURCE_EXHAUSTED"},
                               {"type": "result", "status": "error"}], exit_code=1)
        sig = agent.limit_signal(res, 600)
        self.assertIsNone(sig.epoch)
        self.assertIn("probing every 10m 00s", sig.how)


if __name__ == "__main__":
    unittest.main()
