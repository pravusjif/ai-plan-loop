"""Live end-to-end smoke test: a real agent works a tiny 3-milestone plan in a
throwaway git repo, and the result is checked mechanically. It costs real
tokens (about $0.90 with Claude sonnet at low effort: three turns, each paying
for a ~33k-token session start), so `unittest discover` never picks it up --
run it by hand:

    python tests/smoke_test.py                  # claude
    python tests/smoke_test.py --agent codex    # skipped if codex is not installed
    python tests/smoke_test.py --agent all --keep
    python tests/smoke_test.py --agent dsh -- --agent-bin "dsh --patch my.patch.yml" --model p/m

The flags force both session paths: --context-threshold 0.9 makes milestone 2
resume milestone 1's session, and --max-turns-per-session 2 makes milestone 3
start a fresh one. Agents that cannot resume get a fresh session each time.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _util import PLAN_LOOP, git, make_repo

from loop_agents import find_exe

PLAN = """\
# Smoke plan

Rules: touch only the files named here and this plan. One commit per milestone, message `smoke: <n>`.

- [ ] **1. Hello.** Create `hello.txt` whose only line is `hello from milestone 1`. Verify by printing it. Tick this box, commit.
- [ ] **2. Count.** Create `count.txt` with the numbers 1 to 5, one per line. Verify by printing it. Tick, commit.
- [ ] **3. Join.** Create `summary.txt` whose only line is the first line of hello.txt, then ` + `, then the line count of count.txt (expected: `hello from milestone 1 + 5`). Verify, tick, commit.
"""
INSTRUCTIONS = "Smoke repo: no build, no tests; verify with `cat` / `git diff`.\n"

COMMON = ["--context-threshold", "0.9", "--max-turns-per-session", "2",
          "--max-sessions", "3", "--max-milestones", "3", "--turn-timeout-s", "600",
          "--cooldown-s", "1", "--max-consecutive-stalls", "1",
          "--max-consecutive-errors", "1"]
PER_AGENT = {
    "claude": ["--model", "sonnet", "--fallback-model", "", "--effort", "low",
               "--max-budget-usd", "0.50"],
    "codex": ["--effort", "low"],
    "gemini": [],
    "dsh": [],
}


class Failed(Exception):
    pass


def check(cond: bool, what: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        raise Failed(what)


def smoke(agent: str, keep: bool, extra: list[str]) -> bool:
    print(f"\n=== smoke: {agent} ===")
    if not find_exe(agent):
        print(f"  SKIP: {agent} is not on PATH")
        return True
    repo = make_repo(PLAN, branch="smoke",
                     extra={"CLAUDE.md": INSTRUCTIONS, "AGENTS.md": INSTRUCTIONS,
                            "GEMINI.md": INSTRUCTIONS})
    state_dir = repo / ".ai-loop" / "PLAN-md"
    print(f"  repo {repo}")
    argv = [sys.executable, str(PLAN_LOOP), str(repo / "PLAN.md"), "--agent", agent,
            *COMMON, *PER_AGENT[agent], *extra]
    print("  $ " + " ".join(argv))
    try:
        code = subprocess.run(argv, cwd=str(repo), timeout=1800,
                              stdin=subprocess.DEVNULL).returncode
    except subprocess.TimeoutExpired:
        code = None
    ok = False
    try:
        driver_log = (state_dir / "driver.log").read_text(encoding="utf-8")
        st = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        h = st["history"]
        print("\n  checks:")
        # 1. the run finished cleanly
        check(code == 0, f"driver exit code 0 (got {code})")
        check("PLAN COMPLETE" in driver_log, "driver.log says PLAN COMPLETE")
        check(not (state_dir / "HALTED").exists(), "no HALTED file")
        # 2. the ledger
        plan = (repo / "PLAN.md").read_text(encoding="utf-8")
        check(plan.count("- [x]") == 3 and plan.count("- [ ]") == 0, "all 3 boxes ticked")
        # 3. the work itself; milestone 3 depends on 1 and 2
        read = lambda name: (repo / name).read_text(encoding="utf-8").strip() \
            if (repo / name).exists() else None  # noqa: E731
        check(read("hello.txt") == "hello from milestone 1", "hello.txt content")
        check((read("count.txt") or "").split() == ["1", "2", "3", "4", "5"], "count.txt content")
        check(read("summary.txt") == "hello from milestone 1 + 5", "summary.txt content")
        # 4. git
        commits = int(git(repo, "rev-list", "--count", "HEAD")) - 1
        check(3 <= commits <= 6, f"3 to 6 new commits (got {commits})")
        check(git(repo, "status", "--porcelain") == "", "clean working tree")
        check(git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "smoke", "still on branch smoke")
        # 5. state
        check(st["milestones"] == 3, f"state milestones == 3 (got {st['milestones']})")
        check(len(h) == 3, f"3 history entries (got {len(h)})")
        check(all(e["loop_status"].upper().startswith(("LANDED", "PLAN_COMPLETE")) for e in h),
              "every turn reported LANDED / PLAN_COMPLETE: "
              + " | ".join(e["loop_status"] for e in h))
        check(all(e["session_id"] for e in h), "every turn has a session id")
        # 6. session handling
        resumed = h[0]["ctx_pct"] is not None and agent != "gemini"
        if resumed:
            check(st["sessions"] == 2, f"2 sessions (got {st['sessions']})")
            check(h[0]["session_id"] == h[1]["session_id"] and h[1]["turn"] == 2,
                  "milestone 2 resumed milestone 1's session")
            check(h[0]["decision"].endswith("continue this session"),
                  f"checkpoint 1 decision: {h[0]['decision']}")
            check(h[1]["decision"].startswith("--max-turns-per-session"),
                  f"checkpoint 2 decision: {h[1]['decision']}")
            check(h[2]["session_id"] != h[0]["session_id"], "milestone 3 got a fresh session")
            check(len(list((state_dir / "logs").glob("*.jsonl"))) == 2, "2 session logs")
        else:
            check(st["sessions"] == 3, f"3 fresh sessions (got {st['sessions']})")
        # 7. cost (only Claude reports it)
        if agent == "claude":
            check(0 < st["total_cost_usd"] < 1.5, f"cost ${st['total_cost_usd']:.2f} < $1.50")
        print(f"\n  PASS: {agent} -- {st['sessions']} sessions, "
              f"${st['total_cost_usd']:.2f}")
        ok = True
    except (Failed, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"\n  FAIL: {agent}: {exc}")
        tail = state_dir / "driver.log"
        if tail.exists():
            print("  --- driver.log tail ---")
            for line in tail.read_text(encoding="utf-8").splitlines()[-25:]:
                print(f"  {line}")
    if ok and not keep:
        shutil.rmtree(repo, ignore_errors=True)
    else:
        print(f"  kept: {repo}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", default="claude",
                   choices=["claude", "codex", "gemini", "dsh", "all"],
                   help="all = claude, codex and gemini; dsh runs only by name")
    p.add_argument("--keep", action="store_true", help="keep the temp repo even on success")
    p.add_argument("extra", nargs="*", help="after --: more plan_loop.py flags")
    args = p.parse_args()
    agents = ["claude", "codex", "gemini"] if args.agent == "all" else [args.agent]
    results = {a: smoke(a, args.keep, args.extra) for a in agents}
    print("\n" + ", ".join(f"{a}: {'PASS' if ok else 'FAIL'}" for a, ok in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
