import itertools
import json
import time
import unittest
from pathlib import Path

from _util import Log

import plan_loop
from loop_agents import ClaudeAgent, TurnResult, resolve_options


def legacy_argv(claude, cfg, chain, unavailable, limited_until, model, resume_id, name):
    """Driver.argv exactly as it was before the agent adapters (frozen copy)."""
    argv = [claude, "-p", "--model", model, "--effort", cfg.effort,
            "--permission-mode", cfg.permission_mode,
            "--output-format", "stream-json", "--verbose"]
    if resume_id:
        argv += ["--resume", resume_id]
    else:
        argv += ["--name", name]
    others = [m for m in chain if m != model and m not in unavailable
              and float(limited_until.get(m, 0)) <= time.time()]
    if others:
        argv += ["--fallback-model", others[0]]
    if cfg.permission_mode == "bypassPermissions":
        argv += ["--allow-dangerously-skip-permissions"]
    if cfg.max_budget_usd:
        argv += ["--max-budget-usd", str(cfg.max_budget_usd)]
    return argv


def driver(*args):
    cfg = plan_loop.parse_args(["PLAN.md", *args])
    agent = ClaudeAgent()
    errors, _ = resolve_options(agent, cfg)
    assert not errors, errors
    return plan_loop.Driver(cfg, Path(".").resolve(), Path("PLAN.md").resolve(), agent,
                            [r"C:\bin\claude.exe"])


def parse(events, exit_code=0):
    agent = ClaudeAgent()
    res, log = TurnResult(), Log()
    parser = agent.new_parser("prompt")
    for ev in events:
        parser.feed(ev if isinstance(ev, str) else json.dumps(ev), res, log)
    res.exit_code = exit_code
    parser.finish(res, log)
    return agent, res, log


class ClaudeArgvTest(unittest.TestCase):
    def test_defaults_unchanged(self):
        d = driver()
        self.assertEqual((d.cfg.model, d.cfg.fallback_model, d.cfg.effort,
                          d.cfg.permission_mode, d.cfg.context_window),
                         ("fable", "opus", "high", "bypassPermissions", 200_000))

    def test_argv_matches_legacy(self):
        perms = ["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"]
        for perm, budget, fallback, resume, benched in itertools.product(
                perms, ["0", "2.5"], ["opus", ""], [None, "sid-1"], [False, True]):
            with self.subTest(perm=perm, budget=budget, fallback=fallback, resume=resume,
                              benched=benched):
                d = driver("--permission-mode", perm, "--max-budget-usd", budget,
                           "--fallback-model", fallback, "--effort", "max")
                d.state = {"limited_until": {"opus": time.time() + 600} if benched else {}}
                got = d.argv("fable", resume, "ai-plan-loop #3")
                want = legacy_argv(r"C:\bin\claude.exe", d.cfg, d.chain, d.unavailable,
                                   d.limited_until(), "fable", resume, "ai-plan-loop #3")
                self.assertEqual(got, want)

    def test_unavailable_fallback_is_skipped(self):
        d = driver()
        d.state = {"limited_until": {}}
        d.unavailable.add("opus")
        self.assertNotIn("--fallback-model", d.argv("fable", None, "x"))

    def test_invalid_permission_is_an_error(self):
        cfg = plan_loop.parse_args(["PLAN.md", "--permission-mode", "yolo"])
        errors, _ = resolve_options(ClaudeAgent(), cfg)
        self.assertTrue(errors and "not valid for claude" in errors[0])


class ClaudeParserTest(unittest.TestCase):
    def test_success(self):
        _, res, log = parse([
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-4-8"},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "assistant", "parent_tool_use_id": None, "message": {
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                          "cache_read_input_tokens": 20000, "output_tokens": 500},
                "content": [{"type": "thinking"},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}},
            {"type": "assistant", "parent_tool_use_id": "tu1", "message": {
                "usage": {"input_tokens": 999999},
                "content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}},
            "not json at all",
            {"type": "result", "subtype": "success", "is_error": False, "result":
                "Done.\nLOOP_STATUS: LANDED 2. Scripts", "total_cost_usd": 1.25,
             "num_turns": 7, "session_id": "s1",
             "modelUsage": {"claude-opus-4-8": {"contextWindow": 1000000},
                            "claude-haiku": {"contextWindow": 200000}}},
        ])
        self.assertFalse(res.failed)
        self.assertEqual(res.session_id, "s1")
        self.assertEqual(res.ctx_tokens, 21510)  # the subagent message is not context
        self.assertEqual(res.ctx_window, 1_000_000)
        self.assertEqual(res.cost_usd, 1.25)
        self.assertEqual(res.loop_status, "LANDED 2. Scripts")
        self.assertIn("  -> Read(a.py)", log.lines)
        self.assertIn("      [sub] -> Grep(x)", log.lines)
        self.assertIn("[raw] not json at all", log.lines)

    def test_no_result_is_an_error(self):
        _, res, _ = parse([{"type": "system", "subtype": "init", "session_id": "s1"}])
        self.assertTrue(res.failed)

    def test_rate_limit_event_gives_the_epoch(self):
        agent, res, _ = parse([
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": 1790982000, "rateLimitType": "five_hour"}},
            {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
             "result": "You've hit your limit · resets 1am (Europe/Berlin)"},
        ], exit_code=1)
        sig = agent.limit_signal(res, 1200)
        self.assertEqual(sig.epoch, 1790982000.0)
        self.assertEqual(sig.how, "server reset time (five_hour)")

    def test_limit_text_without_time_probes(self):
        agent, res, _ = parse([{"type": "result", "is_error": True,
                                "result": "You've hit your weekly limit"}], exit_code=1)
        sig = agent.limit_signal(res, 1200)
        self.assertIsNone(sig.epoch)
        self.assertEqual(sig.how, "no reset time in the message -- probing every 20m 00s")

    def test_epoch_in_message(self):
        agent, res, _ = parse([{"type": "result", "is_error": True,
                                "result": "Claude AI usage limit reached|1790982000000"}], 1)
        self.assertEqual(agent.limit_signal(res, 1200).epoch, 1790982000.0)

    def test_model_unavailable(self):
        agent, res, _ = parse([{"type": "result", "is_error": True, "api_error_status": 404,
                                "result": "There's an issue with the selected model "
                                          "(claude-bogus-9)."}], exit_code=1)
        self.assertTrue(agent.model_unavailable(res))
        self.assertIsNone(agent.limit_signal(res, 1200))

    def test_overload(self):
        agent, res, _ = parse([{"type": "result", "is_error": True,
                                "result": "API Error: 529 overloaded"}], exit_code=1)
        self.assertTrue(agent.overloaded(res))


if __name__ == "__main__":
    unittest.main()
