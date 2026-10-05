"""The rendered prompts. Claude's must stay byte-identical to what the driver
sent before the agent adapters existed (goldens captured from that code)."""

import shutil
import unittest

from _util import FIXTURES, GOLDEN_PLAN, FAKE_AGENT, make_repo, prompt_sections, run_loop
import sys

FAKE = f'"{sys.executable}" "{FAKE_AGENT}"'


class PromptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo(GOLDEN_PLAN, "docs/PLAN.md")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def dry(self, *args):
        p = run_loop(self.repo, "docs/PLAN.md", "--dry-run", *args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def test_claude_golden(self):
        for args, golden in (([], "claude_prompts.golden.txt"),
                             (["--until", "3. Build"], "claude_prompts_until.golden.txt")):
            with self.subTest(golden=golden):
                out = self.dry("--agent", "claude", "--agent-bin", FAKE, *args)
                want = (FIXTURES / golden).read_text(encoding="utf-8")
                self.assertEqual(prompt_sections(out), want)

    def test_codex_prompt(self):
        out = self.dry("--agent", "codex", "--agent-bin", FAKE)
        prompts = prompt_sections(out)
        self.assertIn("the plan in docs/PLAN.md, unattended", prompts)
        self.assertNotIn("@docs/PLAN.md", prompts)
        self.assertIn("Read AGENTS.md (if the repo has one)", prompts)
        self.assertNotIn("CLAUDE.md", prompts)
        self.assertNotIn("subagents", prompts)
        self.assertNotIn("{{", prompts)
        self.assertIn("exec --json --dangerously-bypass-approvals-and-sandbox -", out)

    def test_gemini_prompt_has_no_continue(self):
        out = self.dry("--agent", "gemini", "--agent-bin", FAKE)
        self.assertIn("Read GEMINI.md or AGENTS.md (if the repo has one)", out)
        self.assertIn("resume      (not supported by gemini", out)
        self.assertNotIn("--- continue prompt", out)

    def test_custom_prompt_file(self):
        out = self.dry("--agent-cmd", f"{FAKE} --prompt-file {{prompt_file}}")
        self.assertIn("--- first prompt (file) ---", out)
        self.assertIn("--prompt-file <prompt-file>", out)

    def test_not_installed_still_dry_runs(self):
        out = self.dry("--agent", "codex", "--agent-bin", "surely-not-a-real-codex-xyz")
        self.assertIn("not found -- the dry run shows the command anyway", out)


if __name__ == "__main__":
    unittest.main()
