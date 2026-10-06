#!/usr/bin/env python3
"""
plan_loop.py -- the AI Plan Loop: an unattended chain of coding-agent sessions
(Claude Code, Codex CLI, Gemini CLI, or any CLI) that works through a markdown
plan one milestone at a time until the plan is done.

    python tools/ai_plan_loop/plan_loop.py docs/PLAN.md --dry-run
    python tools/ai_plan_loop/plan_loop.py docs/PLAN.md --max-milestones 1
    python tools/ai_plan_loop/plan_loop.py docs/PLAN.md
    python tools/ai_plan_loop/plan_loop.py docs/PLAN.md --until "6. Build"
    python tools/ai_plan_loop/plan_loop.py docs/PLAN.md --agent codex
    python tools/ai_plan_loop/plan_loop.py docs/PLAN.md --agent-cmd "mytool run {prompt_file}"

    create .ai-loop/<plan>/STOP   # stop cleanly after the current milestone
    Ctrl-C                        # stop now (the running session is killed)

The rules (README.md has the reasoning):

  * Agent: --agent, else $AI_PLAN_LOOP_AGENT, else auto: claude if it is on PATH,
    otherwise the one other supported CLI found. loop_agents.py has the adapters.
  * Model (Claude): Fable, rotating to Opus while Fable is unavailable or
    usage-limited. Effort: high. Other agents default to their CLI's own model.
  * One milestone per TURN. A turn ends with a `LOOP_STATUS:` line. After a turn
    that lands a milestone the driver measures the session's context. Under the
    threshold (30%), it resumes the SAME session (`claude -p --resume <id>`) for
    the next milestone. At or over the threshold, or when the agent cannot report
    context or resume, the next milestone gets a fresh session.
  * Usage limit: the model is benched until the reset time the server reported
    (the exact `resetsAt` epoch from the stream, else the "resets 1am" text), and
    the other model takes over. With every model limited, the driver sleeps until
    the earliest reset, re-probing at a low frequency when no time is known.
  * Progress is judged by the repo, not by what the session says: a commit, or a
    checkbox ticked in the plan. Two turns in a row without either halt the chain.

Each turn is a separate agent process. Nothing carries between sessions except
what is on disk: the plan, the code and the git history.

Stdlib only. Verified against Claude Code 2.1.173.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
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
    resolve_options, select_agent, split_command, unsafe_cmd_args,
)

DEFAULT_PROMPT = HERE / "prompt.md"
DEFAULT_CONTINUE = HERE / "continue.md"

# Default ledger: top-level markdown checkboxes ("- [ ] **2. Scripts.** ...").
# Indented sub-checkboxes are ignored so a milestone's own task list does not
# count as separate milestones.
DEFAULT_OPEN_RE = r"^[-*+] \[ \] (.+)$"
DEFAULT_DONE_RE = r"^[-*+] \[[xX]\] (.+)$"


class Halt(Exception):
    """Stop the chain and leave a HALTED file for a human."""


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


# --------------------------------------------------------------------- ledger

class Ledger:
    """The plan's checklist, read fresh every time it is asked.

    Items are matched line by line with two regexes (open / done); group 1 is the
    label. A plan with no matching lines is "untracked": the driver then judges
    progress by commits and the LOOP_STATUS line alone.
    """

    def __init__(self, plan: Path, open_re: str, done_re: str, until: str):
        self.plan = plan
        self.open_re = re.compile(open_re, re.MULTILINE)
        self.done_re = re.compile(done_re, re.MULTILINE)
        self.until = until.strip().lower()

    def items(self) -> list[tuple[int, str, bool]]:
        """[(offset, label, done)] in plan order, cut after the --until item."""
        text = self.plan.read_text(encoding="utf-8", errors="replace")
        found = [(m.start(), m.group(1), False) for m in self.open_re.finditer(text)]
        found += [(m.start(), m.group(1), True) for m in self.done_re.finditer(text)]
        found.sort()
        items = [(off, self._label(lbl), done) for off, lbl, done in found]
        if self.until:
            for i, (_off, label, _done) in enumerate(items):
                if self.until in label.lower():
                    return items[: i + 1]
        return items

    @staticmethod
    def _label(raw: str) -> str:
        flat = " ".join(raw.replace("**", "").replace("`", "").split())
        return flat if len(flat) <= 70 else flat[:67] + "..."

    def until_found(self) -> bool:
        text = self.plan.read_text(encoding="utf-8", errors="replace")
        labels = [self._label(m.group(1)) for r in (self.open_re, self.done_re)
                  for m in r.finditer(text)]
        return any(self.until in lbl.lower() for lbl in labels)

    @property
    def tracked(self) -> bool:
        return bool(self.items())

    def open_labels(self) -> list[str]:
        return [lbl for _o, lbl, done in self.items() if not done]

    def done_labels(self) -> list[str]:
        return [lbl for _o, lbl, done in self.items() if done]


# ---------------------------------------------------------------- turn runner

def _pump(stream, sink: "queue.Queue", tag: str) -> None:
    try:
        for line in stream:
            sink.put((tag, line.rstrip("\n")))
    except Exception as exc:  # a killed process closes the pipe mid-read
        sink.put((tag, f"<<reader error: {exc}>>"))
    finally:
        sink.put((tag, None))


# ---------------------------------------------------------------- the driver

class Driver:
    def __init__(self, cfg: argparse.Namespace, repo: Path, plan: Path, agent: Agent,
                 exe: list[str]):
        self.cfg = cfg
        self.repo = repo
        self.plan = plan
        self.plan_rel = os.path.relpath(plan, repo).replace("\\", "/")
        self.agent = agent
        self.exe = exe
        slug = re.sub(r"[^A-Za-z0-9]+", "-", self.plan_rel).strip("-")
        self.loop_root = repo / ".ai-loop"
        self.state_dir = self.loop_root / slug
        self.log_dir = self.state_dir / "logs"
        self.state_file = self.state_dir / "state.json"
        self.stop_file = self.state_dir / "STOP"
        self.halt_file = self.state_dir / "HALTED"
        self.ledger = Ledger(plan, cfg.open_regex, cfg.done_regex, cfg.until)
        self.chain = [cfg.model] + ([cfg.fallback_model]
                                    if cfg.fallback_model and cfg.fallback_model != cfg.model
                                    else [])
        self.unavailable: set[str] = set()  # for this run only
        self.branch = ""
        self.ran_this_run = 0
        self.milestones_this_run = 0
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

    # --- prompts ------------------------------------------------------------

    def render(self, template: str, ctx_pct: float | None = None) -> str:
        open_items = self.ledger.open_labels()
        if open_items:
            hint = (f"By the plan's checklist the next open item is `{open_items[0]}`. "
                    "Confirm that against the plan's own ordering and dependencies.")
        else:
            hint = "The plan has no open checklist item the driver can see; decide from the plan text."
        scope = (f"Stop after the item matching `{self.cfg.until}`: report PLAN_COMPLETE "
                 "once it is done." if self.cfg.until else "")
        return (template
                .replace("{{PLAN_REF}}", self.agent.plan_ref(self.plan_rel))
                .replace("{{INSTRUCTIONS_FILES}}", self.agent.instruction_files)
                .replace("{{DELEGATE_HINT}}", self.agent.delegate_hint)
                .replace("{{PLAN}}", self.plan_rel)
                .replace("{{BRANCH}}", self.branch)
                .replace("{{NEXT_HINT}}", hint)
                .replace("{{SCOPE}}", scope)
                .replace("{{THRESHOLD}}", f"{self.threshold():.0%}")
                .replace("{{CONTEXT_PCT}}", f"{ctx_pct:.1%}" if ctx_pct is not None else "?"))

    def first_prompt(self) -> str:
        text = Path(self.cfg.prompt_file).read_text(encoding="utf-8")
        for extra in self.cfg.append_prompt_file or []:
            text += "\n\n---\n\n" + Path(extra).read_text(encoding="utf-8")
        return self.render(text)

    def continue_prompt(self, ctx_pct: float | None) -> str:
        return self.render(Path(self.cfg.continue_file).read_text(encoding="utf-8"), ctx_pct)

    # --- one agent process ----------------------------------------------------

    def argv(self, model: str, resume_id: str | None, name: str,
             prompt_file: str = "<prompt-file>") -> list[str]:
        # The fallback goes to the CLI for overload inside a call (Claude only);
        # usage limits are per model and long-lived, so the driver rotates for
        # those itself.
        others = [m for m in self.chain if m != model and m not in self.unavailable
                  and float(self.limited_until().get(m, 0)) <= time.time()]
        spec = TurnSpec(model=model, resume_id=resume_id, name=name,
                        fallback=others[0] if others else None, prompt_file=prompt_file,
                        plan=self.plan_rel, repo=str(self.repo))
        return self.agent.argv(self.exe, self.cfg, spec)

    def run_turn(self, model: str, prompt: str, resume_id: str | None, name: str,
                 raw_path: Path, log: Log) -> TurnResult:
        prompt_path = raw_path.with_name(f"{raw_path.stem}-{stamp()}.prompt.md")
        argv = self.argv(model, resume_id, name, str(prompt_path))
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
        return res

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

    # --- main loop ------------------------------------------------------------

    def pick_model(self) -> str | None:
        lim = self.limited_until()
        for m in self.chain:
            if m not in self.unavailable and float(lim.get(m, 0)) <= time.time():
                return m
        return None

    def preflight(self) -> None:
        if not self.cfg.preflight:
            return
        for probe in range(1, self.cfg.preflight_max_probes + 2):
            code, out = run_cmd(self.cfg.preflight, self.repo, timeout=120, shell=True)
            if code == 0:
                return
            if probe > self.cfg.preflight_max_probes:
                raise Halt(f"preflight never passed (exit {code}): {out.strip()[:200]}")
            self.log(f"[{now()}] preflight failed (exit {code}): {out.strip()[:200]} -- "
                     f"probe {probe}/{self.cfg.preflight_max_probes}, retrying in "
                     f"{fmt_dur(self.cfg.preflight_probe_s)}")
            self.sleep(self.cfg.preflight_probe_s, "waiting for preflight")

    def finished(self, status: str) -> bool:
        if status.upper().startswith("PLAN_COMPLETE"):
            return True
        return self.ledger.tracked and not self.ledger.open_labels()

    def loop(self) -> int:
        cfg = self.cfg
        while True:
            if self.stop_file.exists():
                banner(self.log, "STOP file present -- stopping cleanly")
                return 0
            if cfg.max_sessions and self.ran_this_run >= cfg.max_sessions:
                banner(self.log, f"reached --max-sessions {cfg.max_sessions}")
                return 0
            if cfg.max_milestones and self.milestones_this_run >= cfg.max_milestones:
                banner(self.log, f"reached --max-milestones {cfg.max_milestones}")
                return 0
            if self.finished(""):
                banner(self.log, "every checklist item in the plan is done"
                       + (f" up to '{cfg.until}'" if cfg.until else "") + " -- PLAN COMPLETE")
                return 0
            if self.current_branch() != self.branch:
                raise Halt(f"on branch '{self.current_branch()}', expected '{self.branch}'")

            model = self.pick_model()
            if model is None:
                if all(m in self.unavailable for m in self.chain):
                    raise Halt(f"no model in the chain is available ({', '.join(self.chain)})")
                lim = self.limited_until()
                wake = min(float(lim[m]) for m in self.chain if m not in self.unavailable)
                wait = max(60.0, wake - time.time() + 5)
                self.log(f"[{now()}] every model is usage-limited -- sleeping until "
                         f"{fmt_epoch(time.time() + wait)} ({fmt_dur(wait)}), then a fresh "
                         f"session picks the plan up again")
                self.save_state()
                self.sleep(wait, "all models limited")
                continue
            if model != self.chain[0]:
                why = ("unavailable" if self.chain[0] in self.unavailable else
                       f"limited until {fmt_epoch(float(self.limited_until().get(self.chain[0], 0)))}")
                self.log(f"[{now()}] '{self.chain[0]}' is {why} -- using '{model}'")

            self.preflight()
            outcome = self.session(model)
            self.save_state()
            if outcome == "done":
                banner(self.log, "PLAN COMPLETE")
                self.log(f"{self.state['sessions']} sessions, {self.state['milestones']} "
                         f"milestones, ${self.state['total_cost_usd']:.2f} total.")
                return 0
            if outcome == "stop":
                return 0
            if outcome == "overload":
                back = min(300 * 2 ** (self.state["consecutive_errors"] - 1), 3600)
                self.log(f"[{now()}] API overloaded -- backing off {fmt_dur(back)}")
                self.sleep(back, "overload backoff")
            elif outcome == "error":
                self.sleep(cfg.cooldown_s * 2, "error cooldown")
            elif outcome in ("next", "stall"):
                self.sleep(cfg.cooldown_s, "cooldown")
            # "limit" / "fastfail" / "unavailable": straight back to model choice

    def session(self, model: str) -> str:
        """One session: a first turn, then resumed turns while context allows.

        Returns what the outer loop should do next.
        """
        cfg, state = self.cfg, self.state
        index = state["sessions"] + 1
        tag = f"{index:04d}-{stamp()}"
        log = Log(self.log_dir / f"{tag}.log")
        raw_path = self.log_dir / f"{tag}.jsonl"
        counted = False
        resume_id: str | None = None
        last_pct: float | None = None
        turn = 0
        dirty = self.git("status", "--porcelain")
        banner(log, f"session #{index} -- {now()}")
        log(f"branch      {self.branch}")
        log(f"HEAD        {self.head()[:12]}")
        log(f"agent       {self.agent.name}")
        log(f"model       {model} (effort {cfg.effort or 'agent default'})")
        log(f"open      {' | '.join(self.ledger.open_labels()) or '(untracked plan)'}")
        if dirty:
            log(f"NOTE: working tree already dirty ({len(dirty.splitlines())} paths) -- "
                "the session is told to inspect it")
        self.log(f"[{now()}] session #{index} on '{model}' -- log {log.fh.name if log.fh else ''}")

        try:
            while True:
                turn += 1
                head_before = self.head()
                done_before = self.ledger.done_labels()
                open_before = self.ledger.open_labels()
                if resume_id:
                    prompt = self.continue_prompt(last_pct)
                    banner(log, f"session #{index} turn {turn} (resumed {resume_id}) -- {now()}")
                else:
                    prompt = self.first_prompt()
                res = self.run_turn(model, prompt, resume_id, f"ai-plan-loop #{index}",
                                    raw_path, log)

                head_after = self.head()
                committed = head_after != head_before
                done_after = self.ledger.done_labels()
                landed = [d for d in done_after if d not in done_before] \
                    if len(done_after) > len(done_before) else []
                status = res.loop_status
                pct = res.ctx_pct(cfg.context_window)
                state["total_cost_usd"] = round(state["total_cost_usd"] + res.cost_usd, 4)

                log("")
                log(f"exit_code   {res.exit_code}")
                log(f"duration    {res.duration_s / 60:.1f} min")
                log(f"cost        ${res.cost_usd:.2f}")
                log(f"HEAD        {head_before[:12]} -> {head_after[:12]}"
                    + ("  (committed)" if committed else "  (NO COMMIT)"))
                log(f"ticked      {' | '.join(landed) or '(none)'}")
                log(f"context     {self._ctx_text(res)}")
                log(f"LOOP_STATUS {status or '(none reported)'}")

                # 1. Usage limit: not a failure. Bench this model and let the
                #    outer loop rotate or sleep. A fresh session resumes the plan.
                is_limit, wait, how = self.classify_limit(res)
                if is_limit:
                    until = time.time() + wait
                    self.limited_until()[model] = until
                    state["fast_failures"] = 0
                    others = [m for m in self.chain if m != model and m not in self.unavailable
                              and float(self.limited_until().get(m, 0)) <= time.time()]
                    self.log(f"[{now()}] USAGE LIMIT on '{model}' ({how}). Benched until "
                             f"{fmt_epoch(until)}. "
                             + (f"Switching to '{others[0]}'." if others else
                                "No other model available -- will sleep."))
                    if committed or landed:
                        counted = self._count(counted, index)
                        self._history(index, turn, model, res, status, committed, landed, pct,
                                      "usage limit")
                    return "limit"

                died_fast = (res.failed and not res.timed_out and not committed and not landed
                             and res.duration_s < cfg.fast_fail_s)

                # 2. The model itself is not available on this account.
                if died_fast and self.agent.model_unavailable(res):
                    self.unavailable.add(model)
                    self.log(f"[{now()}] model '{model}' is not available "
                             f"({' '.join(res.haystack.split())[:160]}) -- dropped for this run")
                    return "unavailable"

                # 3. Died in seconds without doing anything: almost always a limit
                #    wording the regexes have not learned yet, or a transient API
                #    error. Bench the model on a growing backoff and rotate.
                if died_fast:
                    state["fast_failures"] = n = state.get("fast_failures", 0) + 1
                    if n > cfg.max_fast_failures:
                        raise Halt(f"{n} consecutive turns died within {cfg.fast_fail_s}s "
                                   f"without doing work -- see {log.fh.name if log.fh else ''}")
                    back = min(300 * 2 ** (n - 1), 3600)
                    self.limited_until()[model] = time.time() + back
                    self.log(f"[{now()}] turn died in {res.duration_s:.0f}s with no work done "
                             f"(fast failure {n}/{cfg.max_fast_failures}); benching '{model}' "
                             f"for {fmt_dur(back)}. Last words: "
                             f"{' '.join(res.haystack.split())[:200] or '(silence)'}")
                    if turn > 1:
                        counted = self._count(counted, index)
                    return "fastfail"

                counted = self._count(counted, index)

                # 4. Hard errors.
                if res.failed:
                    state["consecutive_errors"] += 1
                    self._history(index, turn, model, res, status, committed, landed, pct, "error")
                    if self.agent.overloaded(res):
                        return "overload"
                    self.log(f"[{now()}] session #{index} turn {turn} FAILED (exit="
                             f"{res.exit_code} subtype={res.subtype} timed_out={res.timed_out}) "
                             f"-- {state['consecutive_errors']}/{cfg.max_consecutive_errors}")
                    if state["consecutive_errors"] >= cfg.max_consecutive_errors:
                        raise Halt(f"{state['consecutive_errors']} consecutive failed turns "
                                   f"-- see {log.fh.name if log.fh else ''}")
                    return "error"
                state["consecutive_errors"] = 0
                state["fast_failures"] = 0

                # 5. Plan finished.
                if self.finished(status):
                    self._history(index, turn, model, res, status, committed, landed, pct,
                                  "plan complete")
                    if committed or landed:
                        self._milestone()
                    return "done"

                # 6. Progress, judged by the repo: a commit or a ticked item.
                #    What the session SAYS it did is recorded, not trusted.
                progressed = committed or bool(landed)
                if not progressed:
                    state["consecutive_stalls"] += 1
                    self._history(index, turn, model, res, status, committed, landed, pct, "stall")
                    self.log(f"[{now()}] session #{index} turn {turn} STALLED (no commit, nothing "
                             f"ticked; status: {status or 'none'}) -- "
                             f"{state['consecutive_stalls']}/{cfg.max_consecutive_stalls}")
                    if state["consecutive_stalls"] >= cfg.max_consecutive_stalls:
                        raise Halt(f"{state['consecutive_stalls']} consecutive turns landed "
                                   f"nothing -- read {log.fh.name if log.fh else ''}")
                    return "stall"
                state["consecutive_stalls"] = 0
                if landed and not committed:
                    self.log(f"[{now()}] WARNING: plan ticked without a commit this turn")

                reached = status.upper().startswith("LANDED") or bool(landed)
                if not reached:
                    # PARTIAL / BLOCKED / no status: progress, but the milestone is
                    # not closed. A fresh context takes the next attempt.
                    self._history(index, turn, model, res, status, committed, landed, pct,
                                  "progress, milestone open -> fresh session")
                    self.log(f"[{now()}] session #{index} turn {turn}: progress but no milestone "
                             f"closed ({status or 'no status'}) -- next turn in a fresh session")
                    return "next"

                self._milestone()
                self.log(f"[{now()}] session #{index} turn {turn} LANDED on '{model}' -- "
                         f"{status or 'no status line'} | ticked: {' | '.join(landed) or 'none'} "
                         f"| {res.duration_s / 60:.0f} min | ${res.cost_usd:.2f} | "
                         f"context {self._ctx_text(res)}")

                # 7. Checkpoint: stop here if asked to.
                if self.stop_file.exists():
                    self._history(index, turn, model, res, status, committed, landed, pct, "STOP")
                    banner(self.log, "STOP file present -- stopping after this milestone")
                    return "stop"
                if cfg.max_milestones and self.milestones_this_run >= cfg.max_milestones:
                    self._history(index, turn, model, res, status, committed, landed, pct,
                                  "max milestones")
                    banner(self.log, f"reached --max-milestones {cfg.max_milestones}")
                    return "stop"

                # 8. Checkpoint: the context rule.
                limit = self.threshold()
                if not self.agent.supports_resume:
                    decision = f"{self.agent.name} cannot resume a session -> fresh session"
                elif pct is None:
                    decision = "context unknown -> fresh session"
                elif pct >= limit:
                    decision = f"context {pct:.1%} >= {limit:.0%} -> fresh session"
                elif cfg.max_turns_per_session and turn >= cfg.max_turns_per_session:
                    decision = f"--max-turns-per-session {turn} -> fresh session"
                elif not res.session_id:
                    decision = "no session id to resume -> fresh session"
                else:
                    decision = f"context {pct:.1%} < {limit:.0%} -> continue this session"
                self._history(index, turn, model, res, status, committed, landed, pct, decision)
                self.log(f"[{now()}] checkpoint: {decision}")
                self.save_state()
                if not decision.endswith("continue this session"):
                    return "next"
                if self.current_branch() != self.branch:
                    raise Halt(f"on branch '{self.current_branch()}', expected '{self.branch}'")
                self.preflight()
                resume_id = res.session_id
                last_pct = pct
        finally:
            log.close()

    def _count(self, counted: bool, index: int) -> bool:
        if not counted:
            self.state["sessions"] = index
            self.ran_this_run += 1
        return True

    def _milestone(self) -> None:
        self.state["milestones"] = self.state.get("milestones", 0) + 1
        self.milestones_this_run += 1

    def _ctx_text(self, res: TurnResult) -> str:
        pct = res.ctx_pct(self.cfg.context_window)
        if pct is None:
            return "unknown"
        window = res.ctx_window or self.cfg.context_window
        src = "" if res.ctx_window else " (window from --context-window)"
        return f"{res.ctx_tokens:,} / {window:,} tokens = {pct:.1%}{src}"

    def _history(self, index, turn, model, res: TurnResult, status, committed, landed,
                 pct, decision) -> None:
        self.state["history"].append({
            "session": index, "turn": turn, "at": now(), "agent": self.agent.name,
            "model": model,
            "model_id": res.model_name, "session_id": res.session_id,
            "exit_code": res.exit_code, "loop_status": status, "committed": committed,
            "ticked": landed, "head": self.head()[:12],
            "ctx_tokens": res.ctx_tokens, "ctx_window": res.ctx_window,
            "ctx_pct": round(pct, 4) if pct is not None else None,
            "cost_usd": round(res.cost_usd, 4), "minutes": round(res.duration_s / 60, 1),
            "decision": decision,
        })


class StopRequested(Exception):
    pass


# ----------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("plan", help="the plan to work through, e.g. docs/PLAN.md")
    g = p.add_argument_group("agent")
    g.add_argument("--agent", default=None,
                   help="auto (default), claude, codex, gemini or custom; also "
                        "$AI_PLAN_LOOP_AGENT. auto = claude if on PATH, else the one "
                        "other supported CLI found")
    g.add_argument("--agent-bin", default=None, metavar="CMD",
                   help="the command that starts the agent, when it is not the plain "
                        "binary on PATH (e.g. a full path, or 'npx @openai/codex')")
    g.add_argument("--agent-cmd", default=None, metavar="TEMPLATE",
                   help="run any CLI: a command template with {prompt_file}, {model}, "
                        "{effort}, {plan}, {repo}, {name}; without {prompt_file} the "
                        "prompt goes on stdin. Implies --agent custom")
    g = p.add_argument_group("scope")
    g.add_argument("--until", default="",
                   help="stop once the checklist item whose label contains this text is done")
    g.add_argument("--max-milestones", type=int, default=0,
                   help="stop after this many milestones in THIS run (0 = no limit)")
    g.add_argument("--max-sessions", type=int, default=0,
                   help="stop after this many sessions in THIS run (0 = no limit)")
    g.add_argument("--branch", default="",
                   help="required branch (default: the branch checked out at start)")
    # Defaults of None are filled per agent by loop_agents.resolve_options.
    g = p.add_argument_group("model")
    g.add_argument("--model", default=None,
                   help="claude: fable; others: the CLI's own default")
    g.add_argument("--fallback-model", default=None,
                   help="used while the primary is limited or unavailable ('' disables; "
                        "claude: opus, others: none)")
    g.add_argument("--effort", default=None,
                   help="claude: low|medium|high|xhigh|max (default high); codex: "
                        "minimal|low|medium|high|xhigh (default: codex config)")
    g.add_argument("--permission-mode", default=None,
                   help="claude: acceptEdits|auto|bypassPermissions|default|dontAsk|plan "
                        "(default bypassPermissions); codex: bypass|workspace-write "
                        "(default bypass); gemini: yolo|auto_edit (default yolo)")
    g.add_argument("--max-budget-usd", type=float, default=0.0,
                   help="per-turn cap (claude only); only bites on API-key billing")
    g = p.add_argument_group("context rotation")
    g.add_argument("--context-threshold", type=float, default=0.30,
                   help="at a milestone checkpoint, a session this full or fuller is ended "
                        "and the next milestone gets a fresh one (0.30 or 30; 1 and up "
                        "are percentages)")
    g.add_argument("--context-window", type=int, default=None,
                   help="window assumed when the stream does not report one "
                        "(claude 200000, codex 272000, gemini 1000000)")
    g.add_argument("--max-turns-per-session", type=int, default=0,
                   help="also rotate after this many milestones in one session (0 = off)")
    g = p.add_argument_group("prompts")
    g.add_argument("--prompt-file", default=str(DEFAULT_PROMPT),
                   help="first-turn prompt of every session")
    g.add_argument("--continue-file", default=str(DEFAULT_CONTINUE),
                   help="prompt for each further milestone in the same session")
    g.add_argument("--append-prompt-file", action="append", metavar="FILE",
                   help="plan-specific instructions appended to the first prompt (repeatable)")
    g = p.add_argument_group("ledger")
    g.add_argument("--open-regex", default=DEFAULT_OPEN_RE,
                   help="multiline regex for an open plan item; group 1 is the label")
    g.add_argument("--done-regex", default=DEFAULT_DONE_RE,
                   help="multiline regex for a done plan item; group 1 is the label")
    g = p.add_argument_group("timing and resilience")
    g.add_argument("--turn-timeout-s", type=int, default=4 * 3600)
    g.add_argument("--cooldown-s", type=int, default=60)
    g.add_argument("--limit-probe-s", type=int, default=1200,
                   help="usage-limit retry interval when no reset time is known")
    g.add_argument("--limit-margin-s", type=int, default=120,
                   help="wait this long past a reported reset time before retrying")
    g.add_argument("--limit-max-sleep-s", type=int, default=12 * 3600,
                   help="re-probe a limited model at least this often, even if its reset is later")
    g.add_argument("--fast-fail-s", type=int, default=120)
    g.add_argument("--max-fast-failures", type=int, default=10)
    g.add_argument("--max-consecutive-errors", type=int, default=3)
    g.add_argument("--max-consecutive-stalls", type=int, default=2)
    g.add_argument("--preflight", default="",
                   help="shell command that must exit 0 before every turn")
    g.add_argument("--preflight-probe-s", type=int, default=300)
    g.add_argument("--preflight-max-probes", type=int, default=24)
    p.add_argument("--dry-run", action="store_true",
                   help="print the parse, the command and the prompt; run nothing")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    cfg = parse_args(argv)

    plan = Path(cfg.plan).resolve()
    if not plan.is_file():
        print(f"FATAL: plan not found: {plan}")
        return 2
    code, top = run_cmd(["git", "rev-parse", "--show-toplevel"], plan.parent)
    if code != 0:
        print(f"FATAL: {plan} is not inside a git repository")
        return 2
    repo = Path(top.strip()).resolve()
    for f in [cfg.prompt_file, cfg.continue_file] + (cfg.append_prompt_file or []):
        if not Path(f).is_file():
            print(f"FATAL: prompt file missing: {f}")
            return 2
    agent, chosen = select_agent(cfg.agent, cfg.agent_cmd, cfg.agent_bin, dict(os.environ))
    if agent is None:
        print(f"FATAL: {chosen}")
        return 2
    errors, warnings = resolve_options(agent, cfg)
    for err in errors:
        print(f"FATAL: {err}")
    if errors:
        return 2
    exe = agent.resolve(cfg.agent_bin)
    if exe is None:
        wanted = cfg.agent_bin or agent.binary or "the first word of --agent-cmd"
        if not cfg.dry_run:
            print(f"FATAL: {wanted} not found for agent '{agent.name}' -- put it on PATH "
                  "or pass --agent-bin")
            return 2
        warnings.append(f"{wanted} not found -- the dry run shows the command anyway")
        exe = split_command(cfg.agent_bin) if cfg.agent_bin else (
            [agent.binary] if agent.binary else [])
        version = "not found"
    else:
        agent.prepare(exe)
        version = agent.version(exe)

    d = Driver(cfg, repo, plan, agent, exe)
    unsafe = unsafe_cmd_args(d.argv(d.chain[0], None, "ai-plan-loop #1"))
    if unsafe:
        print(f"FATAL: {exe[0]} is a .cmd/.bat shim and cmd.exe would mangle these "
              f"arguments: {unsafe}")
        return 2
    if cfg.until and not d.ledger.until_found():
        print(f"FATAL: --until '{cfg.until}' matches no checklist item in {d.plan_rel}")
        return 2
    d.branch = cfg.branch or d.current_branch()
    if d.branch in ("", "HEAD"):
        print("FATAL: detached HEAD -- check out a branch first")
        return 2
    if cfg.branch and d.current_branch() != cfg.branch:
        print(f"FATAL: on branch '{d.current_branch()}', --branch wants '{cfg.branch}'")
        return 2

    if not cfg.dry_run:
        d.state_dir.mkdir(parents=True, exist_ok=True)
        d.log_dir.mkdir(parents=True, exist_ok=True)
    d.state = d.load_state()
    d.log = Log(None if cfg.dry_run else d.state_dir / "driver.log")
    log = d.log

    banner(log, f"AI Plan Loop -- {now()}")
    log(f"repo        {repo}")
    log(f"plan        {d.plan_rel}" + (f" (until '{cfg.until}')" if cfg.until else ""))
    log(f"branch      {d.branch}")
    log(f"agent       {agent.name} ({' '.join(exe) or '?'}"
        + (f", {version}" if version else "") + f"; {chosen})")
    log(f"supports    {agent.capabilities()}"
        + ("" if agent.verified else " -- adapter not yet verified live"))
    log(f"models      {' -> '.join(d.chain)}, effort {cfg.effort or 'agent default'}, "
        f"permissions {cfg.permission_mode or 'agent default'}")
    log(f"rotation    fresh session at a checkpoint when context >= {d.threshold():.0%}"
        + ("" if agent.supports_resume else f" (always: {agent.name} cannot resume)"))
    log(f"state       {d.state_dir}")
    for warning in warnings:
        log(f"WARNING     {warning}")
    items = d.ledger.items()
    open_items = [lbl for _o, lbl, done in items if not done]
    if items:
        log(f"ledger      {len(items) - len(open_items)}/{len(items)} done; next: "
            f"{open_items[0] if open_items else '(none)'}")
    else:
        log("ledger      no checklist items matched -- progress is judged by commits and "
            "LOOP_STATUS only (see --open-regex/--done-regex)")
    lim = {m: t for m, t in d.limited_until().items() if float(t) > time.time()}
    if lim:
        log("benched     " + ", ".join(f"{m} until {fmt_epoch(float(t))}" for m, t in lim.items())
            + ("" if cfg.dry_run else " -- cleared: a restart re-checks the quota"))
    # A restart is the user's say-so that the quota may have changed (extra usage
    # bought, plan upgraded, window reset early). A limited model fails its first
    # call within seconds and is benched again, so re-checking is cheap; trusting
    # a stale bench can idle the loop for hours.
    if not cfg.dry_run:
        d.limited_until().clear()

    if cfg.dry_run:
        log("")
        log("--- DRY RUN ---")
        for _o, lbl, done in items:
            log(f"  [{'x' if done else ' '}] {lbl}")
        log(f"HEAD        {d.head()[:12]}")
        log(f"dirty       {len(d.git('status', '--porcelain').splitlines())} paths")
        log(f"command     {' '.join(d.argv(d.chain[0], None, 'ai-plan-loop #N'))}")
        if agent.supports_resume:
            log(f"resume      {' '.join(d.argv(d.chain[0], '<session-id>', ''))}")
        else:
            log(f"resume      (not supported by {agent.name}: a fresh session per milestone)")
        via = "stdin" if agent.stdin_payload("") is not None else "file"
        log("")
        log(f"--- first prompt ({via}) ---")
        log(d.first_prompt())
        if agent.supports_resume:
            log("")
            log(f"--- continue prompt ({via}), shown at 12% context ---")
            log(d.continue_prompt(0.12))
        return 0

    if d.halt_file.exists():
        log(f"FATAL: {d.halt_file} exists -- a previous run halted:")
        log(d.halt_file.read_text(encoding="utf-8"))
        log("Delete it to restart.")
        return 2
    if d.stop_file.exists():
        log(f"FATAL: {d.stop_file} exists. Delete it to run.")
        return 2
    lock = acquire_lock(d.loop_root / "driver.lock")
    if lock is None:
        log(f"FATAL: another plan_loop is running in {repo} (.ai-loop/driver.lock is held)")
        return 2
    d.ensure_git_exclude()

    try:
        return d.loop()
    except Halt as exc:
        d.save_state()
        d.halt_file.write_text(f"{now()}\n{exc}\n", encoding="utf-8")
        banner(log, f"HALTED -- {exc}")
        log(f"Logs: {d.log_dir}")
        log(f"Delete {d.halt_file} before restarting.")
        return 1
    except StopRequested as exc:
        d.save_state()
        banner(log, f"{exc} -- stopping")
        return 0
    except KeyboardInterrupt:
        d.save_state()
        banner(log, "interrupted by Ctrl-C")
        return 130
    finally:
        d.save_state()
        log.close()
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
