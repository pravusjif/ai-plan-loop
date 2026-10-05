import json
import tempfile
import time
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path

from _util import Log

from loop_agents import (CodexAgent, TurnResult, TurnSpec, find_codex_rollout,
                         read_codex_rollout, resolve_options)

EXE = ["codex"]


def cfg(**kw):
    base = dict(model=None, fallback_model=None, effort=None, permission_mode=None,
                context_window=None, max_budget_usd=0.0)
    base.update(kw)
    c = Namespace(**base)
    errors, warnings = resolve_options(CodexAgent(), c)
    return c, errors, warnings


def spec(model, resume=None):
    return TurnSpec(model=model, resume_id=resume, name="n", fallback=None,
                    prompt_file="p.md", plan="PLAN.md", repo=".")


def parse(events, exit_code=0):
    agent = CodexAgent()
    res, log = TurnResult(), Log()
    parser = agent.new_parser("prompt")
    for ev in events:
        parser.feed(json.dumps(ev), res, log)
    res.exit_code = exit_code
    parser.finish(res, log)
    return agent, res, log


class CodexArgvTest(unittest.TestCase):
    def test_defaults(self):
        c, errors, _ = cfg()
        self.assertEqual(errors, [])
        self.assertEqual(c.model, "codex:default")
        self.assertEqual(c.fallback_model, "")
        self.assertEqual(c.permission_mode, "bypass")
        self.assertEqual(CodexAgent().argv(EXE, c, spec(c.model)),
                         ["codex", "exec", "--json",
                          "--dangerously-bypass-approvals-and-sandbox", "-"])

    def test_model_effort_resume(self):
        c, errors, _ = cfg(model="gpt-5.5-codex", effort="max",
                           permission_mode="workspace-write")
        self.assertEqual(errors, [])
        self.assertEqual(CodexAgent().argv(EXE, c, spec(c.model, "th-1")),
                         ["codex", "exec", "--json", "--sandbox", "workspace-write",
                          "-m", "gpt-5.5-codex", "-c", "model_reasoning_effort=xhigh",
                          "resume", "th-1", "-"])

    def test_claude_permission_rejected(self):
        _, errors, _ = cfg(permission_mode="bypassPermissions")
        self.assertTrue(errors and "bypass, workspace-write" in errors[0])

    def test_budget_warns(self):
        _, _, warnings = cfg(max_budget_usd=3.0)
        self.assertTrue(any("--max-budget-usd" in w for w in warnings))


class CodexParserTest(unittest.TestCase):
    def test_success(self):
        _, res, log = parse([
            {"type": "thread.started", "thread_id": "th-1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}},
            {"type": "item.completed", "item": {"type": "command_execution",
                                                "command": "bash -lc 'git status'",
                                                "exit_code": 0}},
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "item.completed", "item": {"type": "file_change",
                                                "changes": [{"path": "a.py", "kind": "add"}]}},
            {"type": "item.completed", "item": {"type": "agent_message",
                                                "text": "Done.\nLOOP_STATUS: LANDED 1. A"}},
            {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 1}},
        ])
        self.assertFalse(res.failed)  # a reconnect notice is not a failure
        self.assertEqual(res.session_id, "th-1")
        self.assertEqual(res.loop_status, "LANDED 1. A")
        self.assertIsNone(res.ctx_tokens)  # thread totals are not context
        self.assertIn("  -> edit(a.py)", log.lines)

    def test_missing_turn_completed_is_an_error(self):
        _, res, _ = parse([{"type": "thread.started", "thread_id": "th-1"}])
        self.assertTrue(res.failed)

    def test_usage_limit(self):
        msg = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
               "visit https://chatgpt.com/codex/settings/usage to purchase more credits or "
               "try again at Sep 22nd, 2099 9:51 AM.")
        agent, res, _ = parse([
            {"type": "thread.started", "thread_id": "th-1"},
            {"type": "error", "message": msg},
            {"type": "turn.failed", "error": {"message": msg}},
        ], exit_code=1)
        self.assertTrue(res.failed)
        sig = agent.limit_signal(res, 1200)
        self.assertEqual(sig.epoch, datetime(2099, 9, 22, 9, 51).timestamp())

    def test_usage_limit_in_hours(self):
        agent, res, _ = parse([{"type": "turn.failed", "error": {
            "message": "You've hit your usage limit. Try again in 2 hours 30 minutes."}}], 1)
        sig = agent.limit_signal(res, 1200)
        self.assertAlmostEqual(sig.epoch, time.time() + 9000, delta=5)

    def test_unavailable(self):
        agent, res, _ = parse([{"type": "error", "message":
                                "The model `gpt-bogus` does not exist or you do not have "
                                "access to it."}], 1)
        self.assertTrue(agent.model_unavailable(res))
        self.assertIsNone(agent.limit_signal(res, 1200))


class CodexRolloutTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        day = datetime.now()
        self.folder = self.home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        self.folder.mkdir(parents=True)

    def write(self, thread_id, events):
        path = self.folder / f"rollout-2026-10-05T10-00-00-{thread_id}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return path

    def test_reads_last_token_count(self):
        self.write("th-9", [
            {"type": "session_meta", "payload": {"id": "th-9"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.5-codex"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 1000, "output_tokens": 10},
                "model_context_window": 272000}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 50000, "cached_input_tokens": 40000,
                                     "output_tokens": 2000, "total_tokens": 52000},
                "model_context_window": 272000},
                "rate_limits": {"primary": {"used_percent": 40.0, "resets_at": 1}}}},
        ])
        self.assertIsNotNone(find_codex_rollout("th-9", self.home))
        facts = read_codex_rollout("th-9", self.home)
        self.assertEqual(facts["ctx_tokens"], 52000)
        self.assertEqual(facts["ctx_window"], 272000)
        self.assertEqual(facts["model"], "gpt-5.5-codex")
        self.assertIsNone(facts["limit_epoch"])

    def test_exhausted_window_gives_reset(self):
        self.write("th-8", [{"type": "event_msg", "payload": {
            "type": "token_count", "info": None,
            "rate_limits": {"primary": {"used_percent": 100.0, "resets_at": 4102444800},
                            "secondary": {"used_percent": 20.0, "resets_at": 1}}}}])
        self.assertEqual(read_codex_rollout("th-8", self.home)["limit_epoch"], 4102444800.0)

    def test_missing(self):
        self.assertEqual(read_codex_rollout("nope", self.home), {})


if __name__ == "__main__":
    unittest.main()
