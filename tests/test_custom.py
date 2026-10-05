import sys
import time
import unittest
from argparse import Namespace

from _util import ROOT, Log

from loop_agents import CustomAgent, TurnResult, TurnSpec, resolve_options

PY = sys.executable
SPEC = TurnSpec(model="m-1", resume_id=None, name="ai-plan-loop #1", fallback=None,
                prompt_file="/tmp/p.prompt.md", plan="docs/PLAN.md", repo="/repo")


def cfg(agent, **kw):
    base = dict(model=None, fallback_model=None, effort=None, permission_mode=None,
                context_window=None, max_budget_usd=0.0)
    base.update(kw)
    c = Namespace(**base)
    errors, warnings = resolve_options(agent, c)
    return c, errors, warnings


def run(agent, prompt, stdout_lines, exit_code=0):
    res, log = TurnResult(), Log()
    parser = agent.new_parser(prompt)
    for line in stdout_lines:
        parser.feed(line, res, log)
    res.exit_code = exit_code
    parser.finish(res, log)
    return res


class CustomTemplateTest(unittest.TestCase):
    def test_prompt_file_template(self):
        agent = CustomAgent(f'"{PY}" run.py --model {{model}} --in {{prompt_file}} --plan={{plan}}')
        c, errors, warnings = cfg(agent, model="m-1")
        self.assertEqual((errors, warnings), ([], []))
        exe = agent.resolve(None)
        self.assertEqual(exe, [PY])
        self.assertEqual(agent.argv(exe, c, SPEC),
                         [PY, "run.py", "--model", "m-1", "--in", "/tmp/p.prompt.md",
                          "--plan=docs/PLAN.md"])
        self.assertIsNone(agent.stdin_payload("hello"))

    def test_stdin_template(self):
        agent = CustomAgent(f'"{PY}" run.py')
        self.assertEqual(agent.stdin_payload("hello"), "hello")
        self.assertFalse(agent.supports_resume)

    def test_model_warnings(self):
        _, _, warnings = cfg(CustomAgent("tool run"), model="m-1")
        self.assertTrue(any("--model is ignored" in w for w in warnings))
        _, _, warnings = cfg(CustomAgent("tool run --model {model}"))
        self.assertTrue(any("becomes empty" in w for w in warnings))

    def test_missing_binary(self):
        self.assertIsNone(CustomAgent("surely-not-a-real-binary-xyz {prompt_file}").resolve(None))


class CustomOutputTest(unittest.TestCase):
    def test_status_from_the_end(self):
        res = run(CustomAgent("t"), "prompt", ["working", "Done.", "LOOP_STATUS: LANDED 1. A",
                                               "", "tokens used: 1234"])
        self.assertFalse(res.failed)
        self.assertEqual(res.loop_status, "LANDED 1. A")

    def test_echoed_prompt_is_not_a_status(self):
        prompt = (ROOT / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("LOOP_STATUS: PLAN_COMPLETE", prompt)
        res = run(CustomAgent("t"), prompt, prompt.splitlines() + ["", "crashed"], exit_code=1)
        self.assertEqual(res.loop_status, "")
        # prompt.md mentions "usage limit"; an echo must not read as one
        self.assertIsNone(CustomAgent("t").limit_signal(res, 1200))

    def test_echo_then_real_status(self):
        prompt = (ROOT / "prompt.md").read_text(encoding="utf-8")
        res = run(CustomAgent("t"), prompt, prompt.splitlines() + ["LOOP_STATUS: BLOCKED — x"])
        self.assertEqual(res.loop_status, "BLOCKED — x")

    def test_exit_code_decides_failure(self):
        self.assertTrue(run(CustomAgent("t"), "p", ["LOOP_STATUS: LANDED a"], exit_code=2).failed)

    def test_limit_wording(self):
        agent = CustomAgent("t")
        res = run(agent, "p", ["Error: usage limit reached, try again in 45 minutes"], 1)
        self.assertAlmostEqual(agent.limit_signal(res, 1200).epoch, time.time() + 2700, delta=5)


if __name__ == "__main__":
    unittest.main()
