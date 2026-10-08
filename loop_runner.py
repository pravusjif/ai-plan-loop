"""
loop_runner.py -- the session mechanics shared by every unattended chain of
agent sessions: running one agent process and reading its stream, benching a
usage-limited model until its reset, dropping an unavailable model, choosing
the next model in the chain, sleeping interruptibly, and keeping the state file,
the lock and the logs.

`plan_loop.Driver` builds on this to work through a markdown plan. A different
driver (one that schedules its own kinds of sessions, with its own ledger and
prompts) imports `SessionRunner` and never touches the plan logic:

    from loop_runner import SessionRunner, Log, banner, Halt, StopRequested
    r = SessionRunner(cfg, repo, agent, exe, state_dir)
    r.state = r.load_state()
    res = r.run_turn(model, prompt, resume_id=None, name="my-chain #1",
                     raw_path=r.log_dir / "0001.jsonl", log=Log(...),
                     agent_name="game-designer")

`cfg` is an argparse namespace with the fields `plan_loop.parse_args` defines
(model, fallback_model, effort, permission_mode, max_budget_usd, turn_timeout_s,
limit_probe_s, limit_margin_s, limit_max_sleep_s, context_threshold,
context_window). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from loop_agents import (  # noqa: E402
    ALL_PARENT_VARS, IS_WINDOWS, Agent, TurnResult, TurnSpec, fmt_dur, now,
    unsafe_cmd_args,
)


class Halt(Exception):
    """Stop the chain and leave a HALTED file for a human."""


class StopRequested(Exception):
    """A STOP file appeared while the driver was sleeping."""


# ------------------------------------------------------------------ plumbing

def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def fmt_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


class Log:
    """Console + file, so an unattended run is readable after the fact."""

    def __init__(self, path: Path | None = None, echo: bool = True):
        self.fh = path.open("a", encoding="utf-8") if path else None
        self.echo = echo

    def __call__(self, msg: str = "") -> None:
        if self.echo:
            print(msg, flush=True)
        if self.fh:
            self.fh.write(msg + "\n")
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None


def banner(log: Log, msg: str) -> None:
    log("")
    log("=" * 78)
    log(f"  {msg}")
    log("=" * 78)


def run_cmd(args, cwd: Path, timeout: int = 60, shell: bool = False) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           shell=shell)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"not found: {args if shell else args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=20)
    except Exception:
        pass


def acquire_lock(path: Path):
    """One driver per repo: sessions share the working tree and the git index.

    An OS file lock rather than a PID file, so a crashed driver never leaves a
    stale lock behind. Returns the open handle (keep it alive), or None.
    """
    fh = path.open("a+", encoding="utf-8")
    try:
        if IS_WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _pump(stream, sink: "queue.Queue", tag: str) -> None:
    try:
        for line in stream:
            sink.put((tag, line.rstrip("\n")))
    except Exception as exc:  # a killed process closes the pipe mid-read
        sink.put((tag, f"<<reader error: {exc}>>"))
    finally:
        sink.put((tag, None))


# ------------------------------------------------------------- session runner

class SessionRunner:
    """One agent process at a time, with the model chain, benches and state.

    `extra_env` is added to every session's environment; `label` names the
    chain in the session's environment (`AI_PLAN_LOOP_PLAN`) and in the
    adapter's `{plan}` placeholder.
    """

    def __init__(self, cfg: argparse.Namespace, repo: Path, agent: Agent, exe: list[str],
                 state_dir: Path, label: str = ""):
        self.cfg = cfg
        self.repo = repo
        self.agent = agent
        self.exe = exe
        self.plan_rel = label
        self.loop_root = state_dir.parent
        self.state_dir = state_dir
        self.log_dir = state_dir / "logs"
        self.state_file = state_dir / "state.json"
        self.stop_file = state_dir / "STOP"
        self.halt_file = state_dir / "HALTED"
        self.chain = [cfg.model] + ([cfg.fallback_model]
                                    if cfg.fallback_model and cfg.fallback_model != cfg.model
                                    else [])
        self.unavailable: set[str] = set()  # for this run only
        self.extra_env: dict[str, str] = {}
        self.state: dict = {}
        self.log = Log(None)

    # --- small helpers ------------------------------------------------------

    def git(self, *args: str) -> str:
        code, out = run_cmd(["git", *args], self.repo)
        return out.strip() if code == 0 else ""

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    def threshold(self) -> float:
        # Below 1 is a fraction, 1 and up a percentage: "0.3" and "30" agree, and
        # "1" means 1%, never 100% (which would just disable rotation).
        t = float(self.cfg.context_threshold)
        return t / 100.0 if t >= 1 else t

    def load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"sessions": 0, "milestones": 0, "consecutive_stalls": 0,
                "consecutive_errors": 0, "fast_failures": 0, "total_cost_usd": 0.0,
                "limited_until": {}, "history": []}

    def save_state(self) -> None:
        self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def limited_until(self) -> dict:
        return self.state.setdefault("limited_until", {})

    def ensure_git_exclude(self) -> None:
        """Sessions run `git add` unattended; the loop's own output must never be
        staged. info/exclude is untracked, so no session can commit it away."""
        rel = self.git("rev-parse", "--git-path", "info/exclude")
        if not rel:
            return
        path = Path(rel) if Path(rel).is_absolute() else self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if "/.ai-loop/" not in text.splitlines():
            with path.open("a", encoding="utf-8") as fh:
                fh.write(("" if text.endswith("\n") or not text else "\n")
                         + "# AI Plan Loop runtime output (plan_loop.py)\n/.ai-loop/\n")

    def sleep(self, seconds: float, label: str) -> None:
        """Sleep in slices so STOP lands during a long usage-limit wait."""
        end = time.monotonic() + seconds
        next_note = time.monotonic()
        while time.monotonic() < end:
            if self.stop_file.exists():
                raise StopRequested(f"STOP file seen while {label}")
            if time.monotonic() >= next_note:
                self.log(f"[{now()}] {label}: {fmt_dur(end - time.monotonic())} remaining")
                next_note = time.monotonic() + (600 if seconds > 1800 else 300)
            time.sleep(min(5.0, max(0.0, end - time.monotonic())))

    # --- one agent process ----------------------------------------------------

    def argv(self, model: str, resume_id: str | None, name: str,
             prompt_file: str = "<prompt-file>", agent_name: str | None = None) -> list[str]:
        # The fallback goes to the CLI for overload inside a call (Claude only);
        # usage limits are per model and long-lived, so the driver rotates for
        # those itself.
        others = [m for m in self.chain if m != model and m not in self.unavailable
                  and float(self.limited_until().get(m, 0)) <= time.time()]
        spec = TurnSpec(model=model, resume_id=resume_id, name=name,
                        fallback=others[0] if others else None, prompt_file=prompt_file,
                        plan=self.plan_rel, repo=str(self.repo), agent=agent_name)
        return self.agent.argv(self.exe, self.cfg, spec)

    def run_turn(self, model: str, prompt: str, resume_id: str | None, name: str,
                 raw_path: Path, log: Log, agent_name: str | None = None) -> TurnResult:
        prompt_path = raw_path.with_name(f"{raw_path.stem}-{stamp()}.prompt.md")
        argv = self.argv(model, resume_id, name, str(prompt_path), agent_name)
        unsafe = unsafe_cmd_args(argv)
        if unsafe:
            raise Halt(f"{argv[0]} is a .cmd/.bat shim and cmd.exe would mangle these "
                       f"arguments: {unsafe}")
        payload = self.agent.stdin_payload(prompt)
        if payload is None:
            prompt_path.write_text(prompt, encoding="utf-8")
        log(f"[{now()}] $ {' '.join(argv)}")
        log(f"[{now()}] (prompt on stdin, {len(prompt)} chars)" if payload is not None
            else f"[{now()}] (prompt in {prompt_path}, {len(prompt)} chars)")
        res = TurnResult()
        res.session_id = resume_id or ""
        parser = self.agent.new_parser(prompt)
        started = time.monotonic()
        # Started from inside an agent session (Claude Code, Codex, Gemini), the
        # driver would hand that session's identity (id, messaging socket, sandbox
        # flags) to every session it spawns. Drop it so a launch from an IDE
        # terminal or an agent behaves exactly like one from a plain shell. User
        # settings such as CLAUDE_CONFIG_DIR or CODEX_HOME pass through.
        env = {k: v for k, v in os.environ.items() if k not in ALL_PARENT_VARS}
        env.update(PYTHONIOENCODING="utf-8", AI_PLAN_LOOP="1", AI_PLAN_LOOP_PLAN=self.plan_rel)
        env.update(self.agent.env(self.cfg))
        env.update(self.extra_env)
        proc = subprocess.Popen(argv, cwd=str(self.repo), env=env,
                                stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
        try:
            if payload is not None:
                try:
                    proc.stdin.write(payload)
                    proc.stdin.close()
                except Exception as exc:
                    log(f"[{now()}] could not write prompt to stdin: {exc}")

            q: "queue.Queue" = queue.Queue()
            for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
                threading.Thread(target=_pump, args=(stream, q, tag), daemon=True).start()

            deadline = started + self.cfg.turn_timeout_s
            open_streams = 2
            stderr_lines: list[str] = []
            with raw_path.open("a", encoding="utf-8") as raw:
                while open_streams > 0:
                    if time.monotonic() > deadline:
                        log(f"[{now()}] !! turn exceeded {self.cfg.turn_timeout_s}s -- killing")
                        res.timed_out = True
                        kill_tree(proc)
                        break
                    try:
                        tag, line = q.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if line is None:
                        open_streams -= 1
                    elif tag == "err":
                        stderr_lines.append(line)
                        log(f"[stderr] {line}")
                    else:
                        raw.write(line + "\n")
                        parser.feed(line, res, log)
            res.stderr = "\n".join(stderr_lines)
            try:
                res.exit_code = proc.wait(timeout=30)
            except Exception:
                kill_tree(proc)
                res.exit_code = proc.returncode if proc.returncode is not None else -9
        finally:
            kill_tree(proc)  # no-op unless Ctrl-C or an error got us here
        parser.finish(res, log)
        self.agent.after_turn(res, self.repo, log)
        res.duration_s = time.monotonic() - started
        log(f"[{now()}] --- session: {fmt_dur(res.duration_s)} · ${res.cost_usd:.2f} · "
            f"context {self.ctx_text(res)}")
        return res

    def ctx_text(self, res: TurnResult) -> str:
        pct = res.ctx_pct(self.cfg.context_window)
        if pct is None:
            return "unknown"
        window = res.ctx_window or self.cfg.context_window
        src = "" if res.ctx_window else " (window from --context-window)"
        return f"{res.ctx_tokens:,} / {window:,} tokens = {pct:.1%}{src}"

    # --- classification -------------------------------------------------------

    def classify_limit(self, res: TurnResult) -> tuple[bool, float, str]:
        """(usage limit?, seconds to bench the model, how that was decided)."""
        if not res.failed:
            return False, 0.0, ""
        signal = self.agent.limit_signal(res, self.cfg.limit_probe_s)
        if signal is None:
            return False, 0.0, ""
        if signal.epoch is not None:
            return (True, *self._until(signal.epoch, signal.how))
        return True, float(self.cfg.limit_probe_s), signal.how

    def _until(self, epoch: float, how: str) -> tuple[float, str]:
        """Bench until just after the reset, but re-probe at least every
        --limit-max-sleep-s in case the window ends early or was misreported."""
        left = epoch - time.time()
        if left <= 0:
            return float(self.cfg.limit_probe_s), (
                f"{how}: {fmt_epoch(epoch)} already passed but still limited -- "
                f"probing every {fmt_dur(self.cfg.limit_probe_s)}")
        wait = left + self.cfg.limit_margin_s
        if wait > self.cfg.limit_max_sleep_s:
            return float(self.cfg.limit_max_sleep_s), (
                f"{how}: resets {fmt_epoch(epoch)}; re-probing in "
                f"{fmt_dur(self.cfg.limit_max_sleep_s)} anyway")
        return max(60.0, wait), f"{how}: resets {fmt_epoch(epoch)}"

    # --- model choice ---------------------------------------------------------

    def pick_model(self) -> str | None:
        lim = self.limited_until()
        for m in self.chain:
            if m not in self.unavailable and float(lim.get(m, 0)) <= time.time():
                return m
        return None

