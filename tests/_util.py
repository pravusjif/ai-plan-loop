"""Shared helpers for the tests: throwaway git repos and driver runs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
FIXTURES = TESTS / "fixtures"
PLAN_LOOP = ROOT / "plan_loop.py"
FAKE_AGENT = TESTS / "fake_agent.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Log:
    """Stand-in for plan_loop.Log that keeps the lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str = "") -> None:
        self.lines.append(msg)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stdout}{p.stderr}")
    return p.stdout.strip()


def make_repo(plan_text: str, plan_rel: str = "PLAN.md", branch: str = "main",
              extra: dict[str, str] | None = None) -> Path:
    """A fresh git repo in a temp dir holding the plan, committed once."""
    repo = Path(tempfile.mkdtemp(prefix="ai-plan-loop-test-")).resolve()
    git(repo, "init", "-q", "-b", branch)
    git(repo, "config", "user.name", "AI Plan Loop Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    files = {plan_rel: plan_text, **(extra or {})}
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def run_loop(repo: Path, plan_rel: str, *args: str, env: dict[str, str] | None = None,
             timeout: int = 300) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env or {})
    full_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(PLAN_LOOP), str(repo / plan_rel), *args],
                          cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=full_env,
                          stdin=subprocess.DEVNULL)


def prompt_sections(dry_run_stdout: str) -> str:
    """The two prompts as the dry run prints them (everything from the first
    prompt marker on), which is the part that must not drift."""
    marker = "--- first prompt"
    at = dry_run_stdout.find(marker)
    if at < 0:
        raise AssertionError(f"no prompt section in dry-run output:\n{dry_run_stdout}")
    return dry_run_stdout[at:].replace("\r\n", "\n")


GOLDEN_PLAN = """\
# Golden plan

- [x] **1. Done already.** Nothing to do.
- [ ] **2. Scripts.** Write the scripts.
  - [ ] a sub-task that is not a milestone
- [ ] **3. Build.** Build it.
"""
