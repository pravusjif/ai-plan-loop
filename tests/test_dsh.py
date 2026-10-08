import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from _util import Log

from loop_agents import (DshAgent, TurnResult, TurnSpec, dsh_mcp_patch, dsh_model_patch,
                         read_dsh_session, resolve_options, select_agent)

EXE = ["dsh"]

# The shapes below were captured live from dsh 0.2.0-rc.2 (`--profile headless --json`).
OK_EVENTS = [
    {"type": "session", "sessionId": "session-e70b9715", "cwd": "/repo"},
    {"type": "status", "phase": "turn_start", "turn": 1},
    {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
    {"type": "text", "text": "I'll list the files first.\n\n"},
    {"type": "tool_call", "callId": "c1", "tool": "bash",
     "input": {"command": "ls -la", "description": "List files"}},
    {"type": "tool_result", "callId": "c1", "status": "completed", "result": "AGENTS.md\n"},
    {"type": "status", "phase": "step_end", "turn": 1, "step": 1,
     "usage": {"inputTokens": 8940, "outputTokens": 56, "totalTokens": 8996}},
    {"type": "status", "phase": "step_start", "turn": 1, "step": 2},
    {"type": "text", "text": "Done.\n\nLOOP_STATUS: DONE T-001 — listed"},
    {"type": "status", "phase": "step_end", "turn": 1, "step": 2,
     "usage": {"inputTokens": 270, "outputTokens": 144, "totalTokens": 9409,
               "cacheReadTokens": 8995}},
    {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
    {"type": "final", "text": "Done.\n\nLOOP_STATUS: DONE T-001 — listed"},
]


def error_events(code, message):
    return [
        {"type": "session", "sessionId": "session-6b224821", "cwd": "/repo"},
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1,
         "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}},
        {"type": "status", "phase": "turn_end", "turn": 1,
         "reason": {"kind": "error", "error": {"message": message, "code": code}}},
        {"type": "final", "text": ""},
    ]


def cfg(**kw):
    base = dict(model=None, fallback_model=None, effort=None, permission_mode=None,
                context_window=None, max_budget_usd=0.0)
    base.update(kw)
    c = Namespace(**base)
    errors, warnings = resolve_options(DshAgent(), c)
    return c, errors, warnings


def spec(model, resume=None):
    return TurnSpec(model=model, resume_id=resume, name="n", fallback=None,
                    prompt_file="p.md", plan="PLAN.md", repo=".", agent="game-engineer")


def parse(events, exit_code=0, stderr=""):
    agent = DshAgent()
    res, log = TurnResult(), Log()
    parser = agent.new_parser("prompt")
    for ev in events:
        parser.feed(json.dumps(ev), res, log)
    res.exit_code = exit_code
    res.stderr = stderr
    parser.finish(res, log)
    return agent, res, log


class DshArgvTest(unittest.TestCase):
    def test_defaults(self):
        c, errors, _ = cfg()
        self.assertEqual(errors, [])
        self.assertEqual(c.model, "dsh:default")
        self.assertEqual(c.permission_mode, "danger-full-access")
        # The persona in the spec is not passed: dsh has no named agents.
        self.assertEqual(DshAgent().argv(EXE, c, spec(c.model)),
                         ["dsh", "--profile", "headless", "--json", "-"])
        self.assertEqual(DshAgent().env(c), {"DSH_PERMISSION_MODE": "danger-full-access"})
        self.assertFalse(DshAgent.named_agents)

    def test_model_becomes_a_patch_and_resume_a_session_id(self):
        c, errors, _ = cfg(model="spark/qwen3.8-flash-next-q3")
        self.assertEqual(errors, [])
        argv = DshAgent().argv(EXE, c, spec(c.model, "session-1"))
        self.assertEqual(argv[:4], ["dsh", "--profile", "headless", "--patch"])
        self.assertEqual(argv[5:], ["--json", "--session-id", "session-1", "-"])
        patch = Path(argv[4]).read_text(encoding="utf-8")
        self.assertIn('provider: "spark"', patch)
        self.assertIn('model: "qwen3.8-flash-next-q3"', patch)

    def test_extra_patches_follow_the_model_patch(self):
        c, _, _ = cfg(model="spark/q3")
        agent = DshAgent()
        with tempfile.TemporaryDirectory() as tmp:
            mcp = dsh_mcp_patch("explorer2", "http://127.0.0.1:8127/unity-explorer-mcp", Path(tmp))
            agent.extra_patches.append(mcp)
            argv = agent.argv(EXE, c, spec(c.model))
            self.assertEqual(argv[3:7], ["--patch", argv[4], "--patch", str(mcp)])
            self.assertTrue(argv[4].endswith("spark_q3.patch.yml"))
            text = mcp.read_text(encoding="utf-8")
        self.assertIn('name: "@deepseek-ai/dsh-mcp-client"', text)
        self.assertIn('serverName: "explorer2"', text)
        self.assertIn('url: "http://127.0.0.1:8127/unity-explorer-mcp"', text)
        self.assertIn("failOnStartupError: false", text)
        self.assertEqual(DshAgent().extra_patches, [])  # per instance, not shared

    def test_mcp_patches_on_other_ports_do_not_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = dsh_mcp_patch("explorer2", "http://127.0.0.1:8127/unity-explorer-mcp", Path(tmp))
            b = dsh_mcp_patch("explorer2", "http://127.0.0.1:8130/unity-explorer-mcp", Path(tmp))
            self.assertNotEqual(a, b)
            self.assertEqual(a.name, "mcp-explorer2-8127.patch.yml")
            self.assertIn(":8127/", a.read_text(encoding="utf-8"))

    def test_agent_bin_can_pick_the_profile(self):
        c, _, _ = cfg()
        for exe in (["dsh", "--profile", "afk"], ["dsh", "afk"]):
            self.assertEqual(DshAgent().argv(exe, c, spec(c.model)), [*exe, "--json", "-"])
        self.assertEqual(DshAgent().argv(["dsh", "--patch", "x.yml"], c, spec(c.model)),
                         ["dsh", "--patch", "x.yml", "--profile", "headless", "--json", "-"])

    def test_model_needs_a_provider(self):
        _, errors, _ = cfg(model="deepseek-flash")
        self.assertEqual(len(errors), 1)
        self.assertIn("<provider>/<model>", errors[0])
        _, errors, _ = cfg(model="a/b", fallback_model="c")
        self.assertIn("--fallback-model c", errors[0])

    def test_effort_is_ignored_and_bad_permissions_rejected(self):
        _, errors, warnings = cfg(effort="high")
        self.assertEqual(errors, [])
        self.assertIn("--effort is ignored", warnings[0])
        _, errors, _ = cfg(permission_mode="bypassPermissions")
        self.assertIn("not valid for dsh", errors[0])

    def test_patch_file_is_rewritten_only_when_it_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = dsh_model_patch("p/m", root)
            stamp = first.stat().st_mtime_ns
            self.assertEqual(dsh_model_patch("p/m", root), first)
            self.assertEqual(first.stat().st_mtime_ns, stamp)
            self.assertNotEqual(dsh_model_patch("p/other", root), first)

    def test_selected_by_name_only(self):
        agent, how = select_agent("dsh", None, None, {})
        self.assertIsInstance(agent, DshAgent)
        agent, _ = select_agent(None, None, None, {}, which=lambda n: "/x/dsh" if n == "dsh" else None)
        self.assertIsNone(agent)  # auto never picks dsh


class DshParserTest(unittest.TestCase):
    def test_completed_turn(self):
        _, res, log = parse(OK_EVENTS)
        self.assertFalse(res.failed)
        self.assertEqual(res.session_id, "session-e70b9715")
        self.assertEqual(res.ctx_tokens, 9409)  # the last step's total, cache reads included
        self.assertEqual(res.num_turns, 2)
        self.assertEqual(res.loop_status, "DONE T-001 — listed")
        self.assertIn("  -> bash(ls -la)", log.lines)

    def test_completed_events_but_nonzero_exit_fails(self):
        _, res, _ = parse(OK_EVENTS, exit_code=1)
        self.assertTrue(res.failed)

    def test_missing_credential_and_unknown_model_are_unavailable(self):
        for code, msg in (
                ("MISSING_CREDENTIAL", 'llm-deepseek: no API key for provider route '
                                       '"deepseek-official"'),
                ("PI_AI_ERROR", "No API key for provider: spark"),
                ("UNKNOWN_MODEL", 'pi-ai provider "spark" has no configured model "q6"')):
            agent, res, _ = parse(error_events(code, msg), exit_code=1,
                                  stderr=f"dsh: {code}: {msg}")
            self.assertTrue(res.failed)
            self.assertEqual(res.subtype, "error")
            self.assertTrue(agent.model_unavailable(res), code)
            self.assertIsNone(agent.limit_signal(res, 1200), code)
            self.assertIsNone(res.ctx_tokens)

    def test_rate_limit_wording_is_a_limit(self):
        agent, res, _ = parse(error_events("PI_AI_ERROR", "429 Too Many Requests"), exit_code=1)
        sig = agent.limit_signal(res, 1200)
        self.assertIsNotNone(sig)
        self.assertIsNone(sig.epoch)
        self.assertFalse(agent.model_unavailable(res))


class DshSessionLogTest(unittest.TestCase):
    def test_window_and_model_from_the_session_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home / "sessions" / "--repo--" / "session-abc"
            folder.mkdir(parents=True)
            lines = [{"type": "session", "id": "session-abc"},
                     {"type": "request/context", "seq": 3,
                      "data": {"provider": "spark", "model": "q3", "contextWindow": 262144}}]
            (folder / "session.v4.jsonl").write_text(
                "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
            self.assertEqual(read_dsh_session("session-abc", home),
                             {"model": "spark/q3", "ctx_window": 262144})
            self.assertEqual(read_dsh_session("session-missing", home), {})
            self.assertEqual(read_dsh_session("../escape", home), {})


if __name__ == "__main__":
    unittest.main()
