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

    def test_progress_log_and_checklist(self):
        out = self.dry("--agent", "claude", "--agent-bin", FAKE)
        first, cont = prompt_sections(out).split("--- continue prompt")
        self.assertIn("progress    docs/PLAN.progress.md (not yet created)", out)
        self.assertIn("**last entry only** of `docs/PLAN.progress.md`", first)
        self.assertIn("5. The progress log `docs/PLAN.progress.md` extended", first)
        # The continue prompt carries the ledger's view, so the session need not
        # re-read the plan; a 70-char label cap keeps it short.
        self.assertIn("- [x] 1. Done already. Nothing to do.\n- [ ] 2. Scripts. Write the "
                      "scripts.\n- [ ] 3. Build. Build it.\n", cont)
        self.assertIn("Do not re-read the plan in full", cont)
        self.assertNotIn("@docs/PLAN.progress.md", first + cont)
        out = self.dry("--agent", "claude", "--agent-bin", FAKE, "--progress-file", "docs/LOG.md",
                       "--until", "2. Scripts")
        self.assertIn("appended to `docs/LOG.md`", out)
        self.assertNotIn("PLAN.progress.md", out)
        self.assertNotIn("3. Build", out.split("--- continue prompt")[1])

    def test_codex_prompt(self):
        out = self.dry("--agent", "codex", "--agent-bin", FAKE)
        prompts = prompt_sections(out)
        self.assertIn("the plan in docs/PLAN.md, unattended", prompts)
        self.assertIn("`docs/PLAN.progress.md`, the progress log", prompts)
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
