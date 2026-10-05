import unittest
from argparse import Namespace

import _util  # noqa: F401  (sys.path)

from loop_agents import (ClaudeAgent, CodexAgent, CustomAgent, GeminiAgent, resolve_options,
                         select_agent)


def which_of(*present):
    return lambda name: f"/bin/{name}" if name in present else None


class SelectAgentTest(unittest.TestCase):
    def pick(self, choice=None, cmd=None, bin_=None, env=None, present=()):
        return select_agent(choice, cmd, bin_, env or {}, which_of(*present))

    def test_auto_prefers_claude(self):
        agent, how = self.pick(present=("claude", "codex", "gemini"))
        self.assertIsInstance(agent, ClaudeAgent)
        self.assertEqual(how, "auto-detected")

    def test_auto_single_other(self):
        self.assertIsInstance(self.pick(present=("codex",))[0], CodexAgent)
        self.assertIsInstance(self.pick(present=("gemini",))[0], GeminiAgent)

    def test_auto_ambiguous(self):
        agent, err = self.pick(present=("codex", "gemini"))
        self.assertIsNone(agent)
        self.assertIn("found codex and gemini", err)

    def test_auto_none(self):
        agent, err = self.pick()
        self.assertIsNone(agent)
        self.assertIn("no agent CLI found", err)

    def test_env_then_flag(self):
        agent, how = self.pick(env={"AI_PLAN_LOOP_AGENT": "Gemini"}, present=("claude",))
        self.assertIsInstance(agent, GeminiAgent)
        self.assertEqual(how, "AI_PLAN_LOOP_AGENT")
        agent, how = self.pick("codex", env={"AI_PLAN_LOOP_AGENT": "gemini"})
        self.assertIsInstance(agent, CodexAgent)
        self.assertEqual(how, "--agent")

    def test_explicit_needs_no_binary(self):
        self.assertIsInstance(self.pick("codex")[0], CodexAgent)

    def test_agent_cmd(self):
        self.assertIsInstance(self.pick(cmd="tool {prompt_file}")[0], CustomAgent)
        self.assertIsInstance(self.pick("custom", cmd="tool")[0], CustomAgent)
        agent, err = self.pick("claude", cmd="tool")
        self.assertIsNone(agent)
        agent, err = self.pick("custom")
        self.assertIn("needs --agent-cmd", err)

    def test_agent_bin_needs_explicit_agent(self):
        agent, err = self.pick(bin_="/opt/codex", present=("claude",))
        self.assertIsNone(agent)
        self.assertIn("explicit --agent", err)

    def test_unknown(self):
        agent, err = self.pick("cursor")
        self.assertIsNone(agent)
        self.assertIn("unknown agent 'cursor'", err)


class ResolveOptionsTest(unittest.TestCase):
    def ns(self, **kw):
        base = dict(model=None, fallback_model=None, effort=None, permission_mode=None,
                    context_window=None, max_budget_usd=0.0)
        base.update(kw)
        return Namespace(**base)

    def test_explicit_values_win(self):
        c = self.ns(model="sonnet", fallback_model="", effort="low", context_window=5)
        self.assertEqual(resolve_options(ClaudeAgent(), c), ([], []))
        self.assertEqual((c.model, c.fallback_model, c.effort, c.context_window),
                         ("sonnet", "", "low", 5))

    def test_codex_effort_validation(self):
        errors, _ = resolve_options(CodexAgent(), self.ns(effort="extreme"))
        self.assertTrue(errors and "--effort extreme is not valid for codex" in errors[0])


if __name__ == "__main__":
    unittest.main()
