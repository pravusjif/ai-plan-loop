"""A stand-in agent CLI for the offline driver tests.

It does what a well-behaved session does -- tick the next open checkbox in the
plan, write a file, commit -- and reports it in the output format of the agent
it imitates. Driven by environment variables (the driver passes them through):

  FAKE_AGENT_FORMAT    claude | codex | gemini | text      (default claude)
  FAKE_AGENT_SCENARIO  ok | limit                          (default ok)
  FAKE_AGENT_ECHO      1: echo the prompt first (text format only)
  FAKE_AGENT_ROLLOUT   1: codex format also writes a rollout file in $CODEX_HOME

The `limit` scenario reports a usage limit in that agent's words, does no work,
and creates the plan's STOP file so the driver stops instead of sleeping.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

OPEN_RE = re.compile(r"^([-*+]) \[ \] (.+)$", re.MULTILINE)


def emit(obj) -> None:
    print(json.dumps(obj) if not isinstance(obj, str) else obj, flush=True)


def read_prompt(argv: list[str]) -> str:
    if "--prompt-file" in argv:
        return Path(argv[argv.index("--prompt-file") + 1]).read_text(encoding="utf-8")
    return sys.stdin.read()


def session_id(fmt: str, argv: list[str]) -> str:
    if fmt == "claude" and "--resume" in argv:
        return argv[argv.index("--resume") + 1]
    if fmt == "codex" and "resume" in argv:
        return argv[argv.index("resume") + 1]
    return str(uuid.uuid4())


def do_milestone(plan: Path) -> str | None:
    """Tick the next open item, write a file, commit. Returns its label."""
    text = plan.read_text(encoding="utf-8")
    m = OPEN_RE.search(text)
    if not m:
        return None
    label = " ".join(m.group(2).replace("**", "").split())
    plan.write_text(text[:m.start()] + f"{m.group(1)} [x] {m.group(2)}" + text[m.end():],
                    encoding="utf-8", newline="\n")
    n = len(list(Path.cwd().glob("milestone-*.txt"))) + 1
    Path(f"milestone-{n}.txt").write_text(f"{label}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", f"fake: {label}"], check=True,
                   capture_output=True)
    return label


def write_rollout(sid: str) -> None:
    home = os.environ.get("CODEX_HOME")
    if not home or os.environ.get("FAKE_AGENT_ROLLOUT") != "1":
        return
    day = datetime.now()
    folder = Path(home) / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{day:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
    if not any(folder.glob(f"rollout-*{sid}.jsonl")):
        path.touch()
    path = sorted(folder.glob(f"rollout-*{sid}.jsonl"))[-1]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "turn_context", "payload": {"model": "fake-codex"}}) + "\n")
        fh.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 4000, "cached_input_tokens": 3000,
                                          "output_tokens": 1000, "total_tokens": 5000},
                     "model_context_window": 100000},
            "rate_limits": {"primary": {"used_percent": 12.0, "resets_at": 1}}}}) + "\n")


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] in (['--version'], ['--help']):
        print('fake-agent 0.0 --approval-mode')
        return 0
    fmt = os.environ.get("FAKE_AGENT_FORMAT", "claude")
    scenario = os.environ.get("FAKE_AGENT_SCENARIO", "ok")
    prompt = read_prompt(argv)
    plan_rel = os.environ["AI_PLAN_LOOP_PLAN"]
    sid = session_id(fmt, argv)

    if scenario == "limit":
        slug = re.sub(r"[^A-Za-z0-9]+", "-", plan_rel).strip("-")
        stop = Path(".ai-loop") / slug / "STOP"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("fake agent: stop after the limit\n", encoding="utf-8")
        if fmt == "claude":
            emit({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"})
            emit({"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "resetsAt": int(time.time()) + 3600,
                "rateLimitType": "five_hour"}})
            emit({"type": "result", "subtype": "success", "is_error": True,
                  "api_error_status": 429, "result": "You've hit your limit · resets 1am",
                  "session_id": sid, "total_cost_usd": 0})
        elif fmt == "codex":
            emit({"type": "thread.started", "thread_id": sid})
            msg = "You've hit your usage limit. Upgrade to Pro or try again in 2 hours."
            emit({"type": "error", "message": msg})
            emit({"type": "turn.failed", "error": {"message": msg}})
        elif fmt == "gemini":
            emit({"type": "init", "session_id": sid, "model": "fake-gemini"})
            msg = ("You have exhausted your daily quota on this model. "
                   "Your quota will reset after 1h30m.")
            emit({"type": "error", "severity": "error", "message": msg})
            emit({"type": "result", "status": "error", "error": {"message": msg}})
        else:
            print("Error: usage limit reached, try again in 45 minutes", flush=True)
        return 1

    label = do_milestone(Path(plan_rel))
    final = (f"Did {label}.\nLOOP_STATUS: LANDED {label}" if label
             else "Nothing left.\nLOOP_STATUS: PLAN_COMPLETE")

    if fmt == "claude":
        emit({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"})
        emit({"type": "assistant", "parent_tool_use_id": None, "message": {
            "usage": {"input_tokens": 600, "cache_read_input_tokens": 300, "output_tokens": 100},
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git commit"}},
                        {"type": "text", "text": final}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": final,
              "session_id": sid, "total_cost_usd": 0.01, "num_turns": 2,
              "modelUsage": {"fake-model": {"contextWindow": 100000, "inputTokens": 600}}})
    elif fmt == "codex":
        emit({"type": "thread.started", "thread_id": sid})
        emit({"type": "turn.started"})
        emit({"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                                 "command": "git commit", "exit_code": 0}})
        emit({"type": "item.completed", "item": {"id": "i2", "type": "agent_message",
                                                 "text": final}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 4000,
                                                  "cached_input_tokens": 3000,
                                                  "output_tokens": 1000}})
        write_rollout(sid)
    elif fmt == "gemini":
        emit({"type": "init", "session_id": sid, "model": "fake-gemini"})
        emit({"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "t1",
              "parameters": {"command": "git commit"}})
        emit({"type": "tool_result", "tool_id": "t1", "status": "success"})
        head, tail = final.split("LOOP_")
        for chunk in (head, "LOOP_", tail):
            emit({"type": "message", "role": "assistant", "content": chunk, "delta": True})
        emit({"type": "result", "status": "success",
              "stats": {"total_tokens": 5000, "tool_calls": 1}})
    else:
        if os.environ.get("FAKE_AGENT_ECHO") == "1":
            print(prompt, flush=True)
        print(final, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
