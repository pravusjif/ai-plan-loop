"""
loop_agents.py -- the agent CLIs plan_loop.py can drive, one adapter each.

An adapter knows four things about its CLI: how to find it, how to start or
resume a headless turn (argv + where the prompt goes), how to read the turn's
output stream into a TurnResult, and how that CLI words a usage limit, an
unknown model or an overload. Everything else -- the ledger, git progress,
state, checkpoints -- lives in plan_loop.py and is the same for every agent.

  claude  Claude Code    `claude -p --output-format stream-json`   verified live
  codex   OpenAI Codex   `codex exec --json -`                    from docs, not yet run live
  gemini  Gemini CLI     `gemini --output-format stream-json -p`  from docs, not yet run live
  custom  any CLI        `--agent-cmd "tool {prompt_file}"`       plain text output

What each adapter assumes about its CLI, where that comes from and whether it
was verified live, is written down at the top of that adapter's section.

Stdlib only. Imports nothing from plan_loop.py.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, NamedTuple

IS_WINDOWS = os.name == "nt"

# The last line of every turn. The driver reads it, but never trusts it alone.
STATUS_RE = re.compile(r"^\s*LOOP_STATUS:\s*(.+?)\s*$", re.MULTILINE)

# --- usage limits ------------------------------------------------------------
# Primary signal, verified live 2026-10-02: a capped model makes the stream carry
#   {"type":"rate_limit_event","rate_limit_info":{"status":"rejected",
#    "resetsAt":1790982000,"rateLimitType":"seven_day_overage_included",...}}
# followed by a `result` with subtype="success" (it lies), is_error=true,
# api_error_status=429 and the text "You've hit your limit · resets 1am
# (Europe/Berlin)". The same event with status="allowed" is just bookkeeping.
# The regexes below are the fallback for paths that do not emit the event.
LIMIT_EPOCH_RE = re.compile(r"usage limit reached\|(\d{10,13})", re.IGNORECASE)
# Keep the qualifier slot permissive: "hit your limit", "hit your session limit",
# "hit your weekly limit" have all been seen.
LIMIT_TEXT_RE = re.compile(
    r"hit your (?:\w+ ){0,3}limit|(?:usage|session|weekly|daily) limit|"
    r"limit reached|rate[_ ]limit|too many requests|\b429\b|"
    r"limit will reset|resets? (?:at )?\d{1,2}\s*[:.]?\d{0,2}\s*[ap]m|"
    r"out of (?:usage|credits)",
    re.IGNORECASE,
)
RESET_TIME_RE = re.compile(
    r"reset(?:s)?(?:\s+at)?\s+(\d{1,2})(?:[:.](\d{2}))?\s*([ap]m)?"
    r"(?:\s*\(([^)]{2,40})\))?",
    re.IGNORECASE,
)
OVERLOAD_RE = re.compile(r"overloaded|\b529\b|service unavailable|\b503\b", re.IGNORECASE)
# Verified live 2026-10-02 with `--model claude-bogus-9`: exit 1, api_error_status
# 404, "There's an issue with the selected model (claude-bogus-9). It may not
# exist or you may not have access to it."
MODEL_UNAVAILABLE_RE = re.compile(
    r"issue with the selected model|may not exist or you may not have access|"
    r"invalid model|not_found_error",
    re.IGNORECASE,
)

# Codex (unverified, from third-party reports): "You've hit your usage limit.
# Upgrade to Pro ... or try again at Sep 22nd, 2026 9:51 AM." Also seen:
# "try again at 5:36 PM" and "try again in 2 days 3 hours".
TRY_AGAIN_AT_RE = re.compile(
    r"try again at\s+(?:([A-Z][a-z]{2,8})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+)?"
    r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?",
)
TRY_AGAIN_IN_RE = re.compile(
    r"try again in\s+((?:\d+\s*(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|[dhms])"
    r"[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
CODEX_UNAVAILABLE_RE = re.compile(
    r"model_not_found|does not exist or you do not have access|unsupported model|"
    r"model is not supported",
    re.IGNORECASE,
)

# Gemini (unverified, from gemini-cli issues): "You have exhausted your daily
# quota on this model.", "Your quota will reset after 22h54m12s.", 429 with
# RESOURCE_EXHAUSTED / QUOTA_EXHAUSTED.
GEMINI_QUOTA_RE = re.compile(
    r"exhausted your (?:\w+ ){0,2}(?:quota|capacity)|RESOURCE_EXHAUSTED|QUOTA_EXHAUSTED|"
    r"quota (?:will )?reset",
    re.IGNORECASE,
)
RESET_AFTER_RE = re.compile(
    r"reset (?:after|in)\s+((?:\d+\s*[dhms]\s*){1,4})", re.IGNORECASE)
GEMINI_UNAVAILABLE_RE = re.compile(
    r"ModelNotFound|Requested entity was not found|model \S+ (?:is )?not found|"
    r"\bNOT_FOUND\b",
    re.IGNORECASE,
)

# Environment a parent agent session leaves behind. Started from inside one, the
# driver would hand that session's identity (id, messaging socket, sandbox flags)
# to every session it spawns. All of these are dropped for every agent, so a
# launch from an IDE terminal or an agent behaves like one from a plain shell.
# User settings (CLAUDE_CONFIG_DIR, CODEX_HOME, GEMINI_API_KEY, ...) pass through.
CLAUDE_PARENT_VARS = frozenset({
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION",
    "MCP_CONNECTION_NONBLOCKING", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_ENABLE_TASKS", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_SSE_PORT",
})
CODEX_PARENT_VARS = frozenset({"CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
                               "CODEX_THREAD_ID"})
GEMINI_PARENT_VARS = frozenset({"GEMINI_CLI"})

# cmd.exe re-parses the command line of a .cmd/.bat shim (npm installs codex and
# gemini that way), so these characters in an argument are mangled or worse.
CMD_UNSAFE_RE = re.compile(r'[\r\n"%^&|<>]')


# ------------------------------------------------------------------ plumbing

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_dur(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m {rem % 60:02d}s"


def split_command(text: str) -> list[str]:
    """shlex.split that keeps Windows backslashes: `C:\\x\\py.exe "a b"`."""
    if not IS_WINDOWS:
        return shlex.split(text)
    parts = shlex.split(text, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def find_exe(name: str) -> str | None:
    """shutil.which, but on Windows only a real launcher: .exe, then the .cmd /
    .bat shims npm installs. Never the extensionless sh script beside them."""
    if not IS_WINDOWS:
        return shutil.which(name)
    found = shutil.which(name)
    if found and Path(found).suffix.lower() in (".exe", ".cmd", ".bat", ".com"):
        return found
    for ext in (".exe", ".cmd", ".bat"):
        found = shutil.which(name + ext)
        if found:
            return found
    return None


def is_cmd_shim(exe: list[str]) -> bool:
    return IS_WINDOWS and bool(exe) and Path(exe[0]).suffix.lower() in (".cmd", ".bat")


def unsafe_cmd_args(argv: list[str]) -> list[str]:
    """Arguments cmd.exe would re-parse when argv[0] is a .cmd/.bat shim."""
    if not is_cmd_shim(argv):
        return []
    return [a for a in argv[1:] if CMD_UNSAFE_RE.search(a)]


class TurnResult:
    def __init__(self) -> None:
        self.exit_code = -1
        self.is_error = True
        self.subtype = "unknown"
        self.api_error_status = None
        self.result_text = ""
        self.stderr = ""
        self.cost_usd = 0.0
        self.num_turns = 0
        self.duration_s = 0.0
        self.session_id = ""
        self.model_name = ""
        self.timed_out = False
        self.rate_limit_rejected: dict | None = None
        self.ctx_tokens: int | None = None
        self.ctx_window: int | None = None
        self.model_usage: dict = {}
        # Error messages the stream carried as events (Codex `error`, Gemini
        # `error`): searched for limit / overload wording like stderr is.
        self.errors: list[str] = []
        # A reset epoch an adapter read from a structured source (Codex rollout).
        self.limit_epoch: float | None = None
        # Where LOOP_STATUS is read from, when not the whole result text.
        self.status_text: str | None = None

    @property
    def failed(self) -> bool:
        return self.timed_out or self.is_error or self.exit_code != 0

    @property
    def loop_status(self) -> str:
        source = self.result_text if self.status_text is None else self.status_text
        found = STATUS_RE.findall(source or "")
        return found[-1].strip() if found else ""

    @property
    def haystack(self) -> str:
        extra = "".join(f"\n{e}" for e in self.errors if e)
        return f"{self.stderr}\n{self.result_text}{extra}"

    def ctx_pct(self, fallback_window: int) -> float | None:
        if self.ctx_tokens is None:
            return None
        window = self.ctx_window or fallback_window
        return self.ctx_tokens / window if window else None


class LimitSignal(NamedTuple):
    """A usage limit: the reset epoch if one is known, and how it was decided.
    With no epoch, `how` is the full log text and the driver probes."""
    epoch: float | None
    how: str


class TurnSpec(NamedTuple):
    """Everything an adapter needs to build one turn's command line."""
    model: str
    resume_id: str | None
    name: str
    fallback: str | None
    prompt_file: str
    plan: str
    repo: str
    # A named agent persona for the session (`claude --agent <name>`); None
    # runs the CLI's default agent. Only Claude Code honours it today.
    agent: str | None = None


# ------------------------------------------------------------ reset parsing

def parse_reset_text(text: str) -> tuple[float | None, str]:
    """'resets 1am (Europe/Berlin)' -> an absolute epoch.

    Windows Python ships no IANA database (`pip install tzdata` fixes that), so
    a named zone may fall back to local time, and the log says so. If the time
    has just passed and the model is still limited, that is clock or zone skew,
    not a reset 24 hours away: return None so the caller probes instead.
    """
    m = RESET_TIME_RE.search(text or "")
    if not m:
        return None, "no reset time in the message"
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    tzname = (m.group(4) or "").strip()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, f"implausible reset time {m.group(0)!r}"
    tz, how = None, "local time (no zone given)"
    if tzname:
        try:
            from zoneinfo import ZoneInfo
            tz, how = ZoneInfo(tzname), tzname
        except Exception:
            how = f"local time ({tzname} unknown here; pip install tzdata)"
    ref = datetime.now(tz) if tz else datetime.now().astimezone()
    target = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= ref:
        if ref - target < timedelta(hours=3):
            return None, f"reset time {m.group(0)!r} just passed ({how})"
        target += timedelta(days=1)
    return target.timestamp(), how


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _duration_s(text: str) -> int:
    """'2 days 3 hours', '22h54m12s', '5 minutes' -> seconds."""
    total = 0
    for num, unit in re.findall(r"(\d+)\s*([a-zA-Z]+)", text):
        u = unit.lower()
        key = "m" if u.startswith("min") else u[0]
        total += int(num) * _UNIT_S.get(key, 0)
    return total


def parse_try_again(text: str) -> tuple[float | None, str]:
    """Codex: 'try again at Sep 22nd, 2026 9:51 AM' / 'at 5:36 PM' / 'in 2 hours'.
    Read in local time: Codex formats the time for the machine it runs on."""
    m = TRY_AGAIN_IN_RE.search(text or "")
    if m:
        secs = _duration_s(m.group(1))
        if secs > 0:
            return time.time() + secs, f"'try again in {m.group(1).strip()}'"
    m = TRY_AGAIN_AT_RE.search(text or "")
    if not m:
        return None, "no reset time in the message"
    mon, day, year, hour, minute, ampm = m.groups()
    hour, minute = int(hour), int(minute)
    ampm = (ampm or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, f"implausible reset time {m.group(0)!r}"
    ref = datetime.now()
    if mon and day and year:
        month = _MONTHS.get(mon[:3].lower())
        if not month:
            return None, f"unreadable reset date {m.group(0)!r}"
        try:
            target = datetime(int(year), month, int(day), hour, minute)
        except ValueError:
            return None, f"unreadable reset date {m.group(0)!r}"
        if target <= ref:
            return None, f"reset time {m.group(0)!r} already passed"
        return target.timestamp(), "local time"
    target = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= ref:
        if ref - target < timedelta(hours=3):
            return None, f"reset time {m.group(0)!r} just passed"
        target += timedelta(days=1)
    return target.timestamp(), "local time"


def parse_reset_after(text: str) -> tuple[float | None, str]:
    """Gemini: 'Your quota will reset after 22h54m12s.' -> now + duration."""
    m = RESET_AFTER_RE.search(text or "")
    if not m:
        return None, "no reset time in the message"
    secs = _duration_s(m.group(1))
    if secs <= 0:
        return None, f"implausible reset delay {m.group(0)!r}"
    return time.time() + secs, f"'reset after {m.group(1).strip()}'"


ResetParser = Callable[[str], "tuple[float | None, str]"]


def text_limit(res: TurnResult, probe_s: int, is_limit: bool,
               parsers: list[ResetParser]) -> LimitSignal | None:
    """The shared fallback once an adapter decided the turn hit a limit by its
    wording: an epoch in the message, then each reset-text parser, then probe."""
    if not is_limit:
        return None
    m = LIMIT_EPOCH_RE.search(res.haystack)
    if m:
        epoch = int(m.group(1))
        epoch = epoch // 1000 if epoch > 10**12 else epoch
        return LimitSignal(float(epoch), "reset epoch in the message")
    how = "no reset time in the message"
    for parse in parsers:
        epoch, why = parse(res.haystack)
        if epoch is not None:
            return LimitSignal(epoch, f"reset time read as {why}")
        if why != "no reset time in the message":
            how = why
    return LimitSignal(None, f"{how} -- probing every {fmt_dur(probe_s)}")


def tool_hint(tool_input: dict) -> str:
    for key in ("command", "file_path", "pattern", "skill", "description", "prompt", "url",
                "path", "query"):
        val = tool_input.get(key)
        if isinstance(val, list):
            val = " ".join(str(v) for v in val)
        if isinstance(val, str) and val.strip():
            return f"({' '.join(val.split())[:110]})"
    return ""


def context_window(model_usage: dict, model_name: str) -> int | None:
    """The session model's window from the result's modelUsage (1000000 for
    Opus here). Prefer the entry for the init model; subagents and helper
    models get their own entries."""
    if not model_usage:
        return None
    entry = model_usage.get(model_name)
    if entry is None:
        entry = max(model_usage.values(),
                    key=lambda e: int(e.get("inputTokens") or 0)
                    + int(e.get("cacheReadInputTokens") or 0)
                    + int(e.get("cacheCreationInputTokens") or 0))
    window = entry.get("contextWindow")
    return int(window) if window else None


def _json(line: str, log) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        log(f"[raw] {line[:400]}")
        return None
    return ev if isinstance(ev, dict) else None


# ------------------------------------------------------------------ adapters

class StreamParser:
    """Reads one turn's stdout, line by line, into a TurnResult."""

    def feed(self, line: str, res: TurnResult, log) -> None:
        raise NotImplementedError

    def finish(self, res: TurnResult, log) -> None:
        """Called once the process has exited (res.exit_code is set)."""


class Agent:
    name = ""
    binary = ""
    title = ""
    default_model: str | None = None      # None: the CLI's own default (no model flag)
    default_fallback = ""
    default_effort: str | None = None     # None: the CLI's own default
    efforts: dict[str, str] = {}          # accepted --effort -> value passed on
    default_permission: str | None = None
    permissions: dict[str, list[str]] = {}  # accepted --permission-mode -> argv fragment
    default_context_window = 200_000
    supports_resume = False
    supports_budget = False
    reports_context = False
    reports_cost = False
    uses_prompt_file = False
    instruction_files = "AGENTS.md"
    delegate_hint = ("and keep broad searches narrow so that only their conclusions enter\n"
                     "your context")
    parent_session_vars: frozenset = frozenset()
    verified = False

    # --- setup ----------------------------------------------------------------

    def default_model_label(self) -> str:
        """The chain label when no --model is given. Namespaced so benches in a
        state.json shared across agents cannot collide."""
        return self.default_model or f"{self.name}:default"

    def model_arg(self, model: str) -> str | None:
        """The model to pass on, or None to leave the CLI's default."""
        if not model or model == "default" or model == f"{self.name}:default":
            return None
        return model

    def resolve(self, override: str | None) -> list[str] | None:
        """The command prefix that starts this agent, or None if not found."""
        if override:
            parts = split_command(override)
            if not parts:
                return None
            if Path(parts[0]).is_file():
                return parts
            found = find_exe(parts[0])
            return [found, *parts[1:]] if found else None
        found = find_exe(self.binary)
        return [found] if found else None

    def version(self, exe: list[str]) -> str:
        try:
            p = subprocess.run([*exe, "--version"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, encoding="utf-8",
                               errors="replace", timeout=15)
            lines = (p.stdout or p.stderr or "").strip().splitlines()
            return lines[0].strip() if lines else "?"
        except Exception:
            return "?"

    def prepare(self, exe: list[str]) -> None:
        """One-time probing of the installed CLI (flag support)."""

    def validate(self, cfg, explicit: set[str]) -> tuple[list[str], list[str]]:
        """(errors, warnings) for the resolved options."""
        errors: list[str] = []
        warnings: list[str] = []
        if cfg.effort is not None:
            if not self.efforts:
                if "effort" in explicit:
                    warnings.append(f"--effort is ignored: {self.name} has no effort control here")
            elif cfg.effort not in self.efforts:
                errors.append(f"--effort {cfg.effort} is not valid for {self.name}; use one "
                              f"of: {', '.join(self.efforts)}")
        if cfg.permission_mode is not None:
            if not self.permissions:
                if "permission_mode" in explicit:
                    warnings.append(f"--permission-mode is ignored by {self.name}")
            elif cfg.permission_mode not in self.permissions:
                errors.append(f"--permission-mode {cfg.permission_mode} is not valid for "
                              f"{self.name}; use one of: {', '.join(self.permissions)}")
        if cfg.max_budget_usd and not self.supports_budget:
            warnings.append(f"--max-budget-usd is ignored: {self.name} has no budget cap")
        return errors, warnings

    # --- one turn ---------------------------------------------------------------

    def argv(self, exe: list[str], cfg, t: TurnSpec) -> list[str]:
        raise NotImplementedError

    def stdin_payload(self, prompt: str) -> str | None:
        """What goes on stdin; None means the prompt travels in a file."""
        return prompt

    def plan_ref(self, plan_rel: str) -> str:
        return plan_rel

    def new_parser(self, prompt: str) -> StreamParser:
        raise NotImplementedError

    def after_turn(self, res: TurnResult, repo: Path, log) -> None:
        """Hook for facts the stream does not carry (Codex context)."""

    # --- classification ---------------------------------------------------------

    def limit_signal(self, res: TurnResult, probe_s: int) -> LimitSignal | None:
        raise NotImplementedError

    def model_unavailable(self, res: TurnResult) -> bool:
        return False

    def overloaded(self, res: TurnResult) -> bool:
        return bool(OVERLOAD_RE.search(res.haystack))

    def capabilities(self) -> str:
        yn = {True: "yes", False: "no"}
        return (f"resume {yn[self.supports_resume]}, context {yn[self.reports_context]}, "
                f"cost {yn[self.reports_cost]}, budget cap {yn[self.supports_budget]}")


# --- Claude Code ---------------------------------------------------------------
#
# Verified live against Claude Code 2.1.173 (2026-10-02, re-checked 2026-10-05).
# The usage-limit and unknown-model facts sit above LIMIT_EPOCH_RE and
# MODEL_UNAVAILABLE_RE. Beyond those:
#   * A limited model's turn exits 1 in under a second and costs $0.
#   * Limits are per model: Fable was capped while Opus answered in the same
#     minute, which is why the driver benches models, not the whole agent.
#   * `claude -p --resume <id>` keeps the same session_id, context accumulates
#     across turns, and a resumed turn hits the prompt cache.
#   * result.modelUsage["claude-opus-4-8"].contextWindow was 1000000 for
#     `--model opus`; claude-sonnet-4-6 reported 200000.
#   * A fresh session starts at roughly 33-35k tokens of context.
#   * tests/smoke_test.py (2026-10-05, --model sonnet): 2 sessions, 3 milestones,
#     milestone 2 resumed at 16.7% context, $0.88 total ($0.19-0.40 per turn).

class ClaudeParser(StreamParser):
    """Claude Code stream-json, verified against 2.1.173."""

    def feed(self, line: str, res: TurnResult, log) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            log(f"[raw] {line[:400]}")
            return
        kind = ev.get("type")

        if kind == "system" and ev.get("subtype") == "init":
            res.session_id = ev.get("session_id", "") or res.session_id
            res.model_name = ev.get("model", "")
            log(f"[{now()}] session_id={res.session_id} model={res.model_name}")

        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if info.get("status") == "rejected":
                res.rate_limit_rejected = info
                log(f"[{now()}] rate limit REJECTED: type={info.get('rateLimitType')} "
                    f"resetsAt={info.get('resetsAt')}")

        elif kind == "assistant":
            sub = ev.get("parent_tool_use_id") is not None
            msg = ev.get("message") or {}
            # Context = what the model saw on its latest call. Subagents run in their
            # own context, so only top-level messages count.
            usage = msg.get("usage") or {}
            if not sub and usage:
                seen = sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
                if seen > 0:
                    res.ctx_tokens = seen
            pad = "    [sub] " if sub else ""
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text" and not sub:
                    text = (block.get("text") or "").strip()
                    if text:
                        log(text)
                elif btype == "tool_use":
                    log(f"  {pad}-> {block.get('name', '?')}{tool_hint(block.get('input') or {})}")
                elif btype == "thinking" and not sub:
                    log("  -> (thinking)")

        elif kind == "result":
            res.subtype = ev.get("subtype", "unknown")
            res.is_error = bool(ev.get("is_error", True))
            res.api_error_status = ev.get("api_error_status")
            res.result_text = ev.get("result") or ""
            res.cost_usd = float(ev.get("total_cost_usd") or 0.0)
            res.num_turns = int(ev.get("num_turns") or 0)
            res.session_id = ev.get("session_id", "") or res.session_id
            res.model_usage = ev.get("modelUsage") or {}
            res.ctx_window = context_window(res.model_usage, res.model_name)
            log("")
            log(f"[{now()}] --- result: subtype={res.subtype} is_error={res.is_error} "
                f"api_error_status={res.api_error_status} turns={res.num_turns} "
                f"cost=${res.cost_usd:.2f}")
            if res.result_text and res.is_error:  # on success it repeats the last text block
                log(res.result_text)


class ClaudeAgent(Agent):
    name = "claude"
    binary = "claude"
    title = "Claude Code"
    default_model = "fable"
    default_fallback = "opus"
    default_effort = "high"
    efforts = {e: e for e in ("low", "medium", "high", "xhigh", "max")}
    default_permission = "bypassPermissions"
    permissions = {p: [] for p in ("acceptEdits", "auto", "bypassPermissions", "default",
                                   "dontAsk", "plan")}
    supports_resume = True
    supports_budget = True
    reports_context = True
    reports_cost = True
    instruction_files = "CLAUDE.md"
    delegate_hint = ("and hand broad searches and big readings to\n"
                     "subagents so that only their conclusions enter your context")
    parent_session_vars = CLAUDE_PARENT_VARS
    verified = True

    def model_arg(self, model: str) -> str | None:
        return model

    def argv(self, exe: list[str], cfg, t: TurnSpec) -> list[str]:
        argv = [*exe, "-p", "--model", t.model, "--effort", cfg.effort,
                "--permission-mode", cfg.permission_mode,
                "--output-format", "stream-json", "--verbose"]
        if t.resume_id:
            argv += ["--resume", t.resume_id]
        else:
            argv += ["--name", t.name]
        if t.agent:
            argv += ["--agent", t.agent]
        # The CLI fallback only covers overload inside a call; usage limits are
        # per model and long-lived, so the driver rotates for those itself.
        if t.fallback:
            argv += ["--fallback-model", t.fallback]
        if cfg.permission_mode == "bypassPermissions":
            argv += ["--allow-dangerously-skip-permissions"]
        if cfg.max_budget_usd:
            argv += ["--max-budget-usd", str(cfg.max_budget_usd)]
        return argv

    def plan_ref(self, plan_rel: str) -> str:
        return f"@{plan_rel}"

    def new_parser(self, prompt: str) -> StreamParser:
        return ClaudeParser()

    def limit_signal(self, res: TurnResult, probe_s: int) -> LimitSignal | None:
        info = res.rate_limit_rejected
        if info is None:
            return text_limit(res, probe_s,
                              res.api_error_status == 429 or bool(LIMIT_TEXT_RE.search(res.haystack)),
                              [parse_reset_text])
        resets = info.get("resetsAt")
        kind = info.get("rateLimitType", "?")
        if resets:
            return LimitSignal(float(resets), f"server reset time ({kind})")
        return LimitSignal(None, f"no reset time ({kind}) -- probing")

    def model_unavailable(self, res: TurnResult) -> bool:
        return res.api_error_status == 404 or bool(MODEL_UNAVAILABLE_RE.search(res.haystack))


# --- OpenAI Codex CLI -------------------------------------------------------------
#
# NOT YET RUN LIVE. Read from the docs and the codex-rs source on 2026-10-05
# (exec/src/cli.rs, exec_events.rs, event_processor_with_jsonl_output.rs).
# Confirm with `python tests/smoke_test.py --agent codex`, then fix the adapter
# and this list.
#   * `codex exec --json -` reads the prompt from stdin and writes JSONL:
#     thread.started {thread_id}, turn.started, item.started|updated|completed
#     {item}, turn.completed {usage}, turn.failed {error}, error {message}. The
#     final answer is the last agent_message item.
#   * `codex exec resume <thread_id> -` continues a session. The exec flags
#     (--json, -m, -c, --sandbox, --dangerously-bypass-approvals-and-sandbox)
#     are global, so they also work before `resume`.
#   * turn.completed.usage holds thread totals, not the last call. Per-call
#     usage and model_context_window are only in the rollout file's token_count
#     events, which also carry rate_limits.{primary,secondary}.{used_percent,
#     resets_at}: see read_codex_rollout.
#   * A usage limit arrives on stdout as `error` then `turn.failed`; the
#     wording is above TRY_AGAIN_AT_RE.
#   * The exit code on failure is unknown, so a turn counts as failed unless
#     turn.completed arrived.
#   * `--sandbox workspace-write` may keep .git read-only, so every commit
#     would fail: the default is --dangerously-bypass-approvals-and-sandbox.

class CodexParser(StreamParser):
    """`codex exec --json` JSONL: thread.started, turn.started, item.*,
    turn.completed {usage}, turn.failed {error}, error {message}.

    turn.completed's usage is the thread's running total, not the last call, so
    it is logged but not used as context; CodexAgent.after_turn reads that from
    the session's rollout file instead."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.completed = False
        self.turn_failed = False

    def feed(self, line: str, res: TurnResult, log) -> None:
        ev = _json(line, log)
        if ev is None:
            return
        kind = ev.get("type")
        if kind == "thread.started":
            res.session_id = ev.get("thread_id") or res.session_id
            log(f"[{now()}] session_id={res.session_id}")
        elif kind == "item.completed":
            self._item(ev.get("item") or {}, res, log)
        elif kind == "turn.completed":
            self.completed = True
            usage = ev.get("usage") or {}
            log("")
            log(f"[{now()}] --- turn completed: input={usage.get('input_tokens')} "
                f"cached={usage.get('cached_input_tokens')} output={usage.get('output_tokens')} "
                f"(thread totals)")
        elif kind == "turn.failed":
            self.turn_failed = True
            msg = ((ev.get("error") or {}).get("message") or "").strip()
            res.errors.append(msg)
            log(f"[{now()}] --- turn FAILED: {msg}")
        elif kind == "error":
            msg = (ev.get("message") or "").strip()
            res.errors.append(msg)
            log(f"[{now()}] error: {msg}")

    def _item(self, item: dict, res: TurnResult, log) -> None:
        itype = item.get("type") or item.get("item_type")
        if itype == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                self.messages.append(text)
                log(text)
        elif itype == "reasoning":
            log("  -> (thinking)")
        elif itype == "command_execution":
            cmd = " ".join(str(item.get("command") or "").split())[:110]
            log(f"  -> exec({cmd}) exit={item.get('exit_code')}")
        elif itype == "file_change":
            paths = [c.get("path", "?") for c in item.get("changes") or [] if isinstance(c, dict)]
            log(f"  -> edit({', '.join(paths)[:110]})")
        elif itype == "mcp_tool_call":
            log(f"  -> {item.get('server', '?')}.{item.get('tool', '?')}")
        elif itype == "web_search":
            log(f"  -> web_search({str(item.get('query') or '')[:110]})")
        elif itype == "error":
            msg = (item.get("message") or "").strip()
            res.errors.append(msg)
            log(f"  -> error: {msg}")

    def finish(self, res: TurnResult, log) -> None:
        res.result_text = "\n\n".join(self.messages)
        # Exit-code conventions are unverified; a completed turn is the signal.
        res.is_error = self.turn_failed or not self.completed
        res.subtype = "success" if not res.is_error else "error"
        res.num_turns = 1 if self.completed else 0


class CodexAgent(Agent):
    name = "codex"
    binary = "codex"
    title = "OpenAI Codex CLI"
    efforts = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high",
               "xhigh": "xhigh", "max": "xhigh"}
    default_permission = "bypass"
    # workspace-write keeps .git read-only in some versions, so commits fail;
    # unattended commits need the bypass, like Claude's bypassPermissions.
    permissions = {"bypass": ["--dangerously-bypass-approvals-and-sandbox"],
                   "workspace-write": ["--sandbox", "workspace-write"]}
    default_context_window = 272_000
    supports_resume = True
    reports_context = True   # best effort, from the rollout file
    parent_session_vars = CODEX_PARENT_VARS

    def argv(self, exe: list[str], cfg, t: TurnSpec) -> list[str]:
        argv = [*exe, "exec", "--json", *self.permissions.get(cfg.permission_mode, [])]
        model = self.model_arg(t.model)
        if model:
            argv += ["-m", model]
        if cfg.effort:
            argv += ["-c", f"model_reasoning_effort={self.efforts.get(cfg.effort, cfg.effort)}"]
        if t.resume_id:
            argv += ["resume", t.resume_id]
        return argv + ["-"]  # the prompt comes on stdin

    def new_parser(self, prompt: str) -> StreamParser:
        return CodexParser()

    def after_turn(self, res: TurnResult, repo: Path, log) -> None:
        if not res.session_id:
            return
        try:
            facts = read_codex_rollout(res.session_id)
        except Exception as exc:  # never let a best-effort read break a turn
            log(f"[{now()}] codex rollout not readable: {exc}")
            return
        if not facts:
            return
        res.ctx_tokens = facts.get("ctx_tokens", res.ctx_tokens)
        res.ctx_window = facts.get("ctx_window", res.ctx_window)
        res.model_name = facts.get("model") or res.model_name
        res.limit_epoch = facts.get("limit_epoch")

    def limit_signal(self, res: TurnResult, probe_s: int) -> LimitSignal | None:
        wording = bool(LIMIT_TEXT_RE.search(res.haystack))
        if res.limit_epoch and (wording or res.limit_epoch > time.time()):
            return LimitSignal(float(res.limit_epoch), "rate limit reset from the session log")
        return text_limit(res, probe_s, wording, [parse_try_again, parse_reset_text])

    def model_unavailable(self, res: TurnResult) -> bool:
        return bool(CODEX_UNAVAILABLE_RE.search(res.haystack))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def find_codex_rollout(thread_id: str, home: Path | None = None) -> Path | None:
    """$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl, looking in
    the last few days' folders before searching the whole tree."""
    sessions = (home or codex_home()) / "sessions"
    if not sessions.is_dir():
        return None
    pattern = f"rollout-*{thread_id}.jsonl"
    today = datetime.now()
    for back in range(3):
        day = today - timedelta(days=back)
        folder = sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        hits = sorted(folder.glob(pattern)) if folder.is_dir() else []
        if hits:
            return hits[-1]
    hits = sorted(sessions.rglob(pattern))
    return hits[-1] if hits else None


def read_codex_rollout(thread_id: str, home: Path | None = None) -> dict:
    """Context and rate limits from the last `token_count` event of a session:
    {ctx_tokens, ctx_window, model, limit_epoch}. Empty if nothing is found."""
    path = find_codex_rollout(thread_id, home)
    if path is None:
        return {}
    facts: dict = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = ev.get("payload") if isinstance(ev, dict) else None
        if not isinstance(payload, dict):
            continue
        if ev.get("type") == "turn_context" and payload.get("model"):
            facts["model"] = payload["model"]
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        last = info.get("last_token_usage") or {}
        # input_tokens already includes the cached part; output is what the
        # model wrote on that call, which is context for the next one.
        seen = int(last.get("total_tokens") or 0) or (
            int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0))
        if seen:
            facts["ctx_tokens"] = seen
        if info.get("model_context_window"):
            facts["ctx_window"] = int(info["model_context_window"])
        facts["limit_epoch"] = _codex_reset(payload.get("rate_limits") or {})
    return facts


def _codex_reset(limits: dict) -> float | None:
    """The latest reset among windows that are used up (>= 100%)."""
    epochs = []
    for window in limits.values():
        if not isinstance(window, dict) or float(window.get("used_percent") or 0) < 100:
            continue
        if window.get("resets_at"):
            epochs.append(float(window["resets_at"]))
        elif window.get("resets_in_seconds") is not None:
            epochs.append(time.time() + float(window["resets_in_seconds"]))
    return max(epochs) if epochs else None


# --- Gemini CLI -----------------------------------------------------------------
#
# NOT YET RUN LIVE. Read from the gemini-cli docs (headless.md, cli-reference.md,
# session-management.md) and packages/core/src/output/types.ts on 2026-10-05.
# Confirm with `python tests/smoke_test.py --agent gemini`, then fix the adapter
# and this list.
#   * `-p` forces headless mode and is appended to whatever comes on stdin.
#     `--output-format stream-json` emits init {session_id, model}, message
#     {role, content, delta}, tool_use, tool_result, error {severity, message}
#     and result {status, error, stats}.
#   * Assistant text arrives in delta chunks, which GeminiParser joins.
#   * stats holds session totals only, so context is unknown.
#   * `--resume` exists, but with it the CLI ignores a prompt on stdin
#     (gemini-cli #14180), and the prompt cannot safely go on the command line
#     through a .cmd shim, so this adapter never resumes.
#   * `--approval-mode yolo` replaced `--yolo`; GeminiAgent.prepare checks
#     `gemini --help` and falls back on older versions.
#   * The quota wording is above GEMINI_QUOTA_RE. The CLI retries on its own
#     first, so a limited turn can outlast --fast-fail-s; harmless, because
#     the driver checks for a limit before it checks for a fast failure.

GEMINI_PROMPT_TAIL = "Follow the instructions above."


class GeminiParser(StreamParser):
    """`gemini --output-format stream-json`: init, message (assistant text in
    delta chunks), tool_use, tool_result, error, result {status, stats}."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.current = ""
        self.status = ""

    def _flush(self, log) -> None:
        text = self.current.strip()
        self.current = ""
        if text:
            self.messages.append(text)
            log(text)

    def feed(self, line: str, res: TurnResult, log) -> None:
        ev = _json(line, log)
        if ev is None:
            return
        kind = ev.get("type")
        if kind == "message":
            if ev.get("role") == "assistant":
                content = ev.get("content") or ""
                if not isinstance(content, str):
                    content = json.dumps(content)
                if ev.get("delta"):
                    self.current += content
                else:
                    self._flush(log)
                    self.current = content
                    self._flush(log)
            return
        self._flush(log)
        if kind == "init":
            res.session_id = ev.get("session_id") or res.session_id
            res.model_name = ev.get("model") or res.model_name
            log(f"[{now()}] session_id={res.session_id} model={res.model_name}")
        elif kind == "tool_use":
            log(f"  -> {ev.get('tool_name', '?')}{tool_hint(ev.get('parameters') or {})}")
        elif kind == "tool_result":
            if ev.get("status") not in (None, "success"):
                err = ev.get("error")
                log(f"     ! {err.get('message') if isinstance(err, dict) else err}")
        elif kind == "error":
            msg = (ev.get("message") or "").strip()
            res.errors.append(msg)
            log(f"[{now()}] {ev.get('severity', 'error')}: {msg}")
        elif kind == "result":
            self.status = ev.get("status") or "error"
            err = ev.get("error")
            if err:
                res.errors.append(err.get("message", "") if isinstance(err, dict) else str(err))
            stats = ev.get("stats") or {}
            res.num_turns = int(stats.get("tool_calls") or 0)
            log("")
            log(f"[{now()}] --- result: status={self.status} "
                f"tokens={stats.get('total_tokens')} tool_calls={stats.get('tool_calls')}")

    def finish(self, res: TurnResult, log) -> None:
        self._flush(log)
        res.result_text = "\n\n".join(self.messages)
        res.subtype = self.status or "unknown"
        res.is_error = self.status != "success"


class GeminiAgent(Agent):
    name = "gemini"
    binary = "gemini"
    title = "Gemini CLI"
    default_permission = "yolo"
    permissions = {"yolo": ["--approval-mode", "yolo"],
                   "auto_edit": ["--approval-mode", "auto_edit"]}
    default_context_window = 1_000_000
    instruction_files = "GEMINI.md or AGENTS.md"
    parent_session_vars = GEMINI_PARENT_VARS

    def prepare(self, exe: list[str]) -> None:
        """--approval-mode replaced --yolo; an older CLI only knows --yolo."""
        try:
            p = subprocess.run([*exe, "--help"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, encoding="utf-8",
                               errors="replace", timeout=30)
        except Exception:
            return
        help_text = (p.stdout or "") + (p.stderr or "")
        if help_text and "--approval-mode" not in help_text and "--yolo" in help_text:
            self.permissions = {"yolo": ["--yolo"], "auto_edit": []}

    def argv(self, exe: list[str], cfg, t: TurnSpec) -> list[str]:
        argv = [*exe, "--output-format", "stream-json",
                *self.permissions.get(cfg.permission_mode, [])]
        model = self.model_arg(t.model)
        if model:
            argv += ["-m", model]
        # -p forces headless mode and is appended to the prompt read from stdin.
        return argv + ["-p", GEMINI_PROMPT_TAIL]

    def new_parser(self, prompt: str) -> StreamParser:
        return GeminiParser()

    def limit_signal(self, res: TurnResult, probe_s: int) -> LimitSignal | None:
        hay = res.haystack
        is_limit = bool(GEMINI_QUOTA_RE.search(hay) or LIMIT_TEXT_RE.search(hay))
        return text_limit(res, probe_s, is_limit, [parse_reset_after, parse_reset_text])

    def model_unavailable(self, res: TurnResult) -> bool:
        return bool(GEMINI_UNAVAILABLE_RE.search(res.haystack))


# --- any CLI, from a command template ------------------------------------------------

CUSTOM_PLACEHOLDERS = ("{prompt_file}", "{model}", "{effort}", "{plan}", "{repo}", "{name}")


class CustomParser(StreamParser):
    """Plain text. The status is read from the end of the output only, and only
    after the last line of the prompt if the tool echoed it: prompt.md itself
    contains `LOOP_STATUS: PLAN_COMPLETE`."""

    KEEP = 400

    def __init__(self, prompt: str) -> None:
        self.lines: list[str] = []
        tail = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
        self.prompt_last = tail[-1] if tail else None

    def feed(self, line: str, res: TurnResult, log) -> None:
        line = line.rstrip()
        log(line)
        self.lines.append(line)
        if len(self.lines) > self.KEEP:
            del self.lines[: len(self.lines) - self.KEEP]

    def finish(self, res: TurnResult, log) -> None:
        lines = self.lines
        if self.prompt_last:
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip() == self.prompt_last:
                    lines = lines[i + 1:]
                    break
        res.result_text = "\n".join(lines[-50:])
        res.status_text = "\n".join([ln for ln in lines if ln.strip()][-5:])
        res.is_error = res.exit_code != 0
        res.subtype = "success" if not res.is_error else "error"


class CustomAgent(Agent):
    name = "custom"
    title = "command template"
    instruction_files = "AGENTS.md"

    def __init__(self, template: str) -> None:
        self.template = template
        self.uses_prompt_file = "{prompt_file}" in template
        if "{effort}" in template:
            self.efforts = {e: e for e in ("minimal", "low", "medium", "high", "xhigh", "max")}

    def resolve(self, override: str | None) -> list[str] | None:
        parts = split_command(self.template)
        if not parts:
            return None
        if Path(parts[0]).is_file():
            return [parts[0]]
        found = find_exe(parts[0])
        return [found] if found else None

    def version(self, exe: list[str]) -> str:
        return ""

    def validate(self, cfg, explicit: set[str]) -> tuple[list[str], list[str]]:
        errors, warnings = super().validate(cfg, explicit)
        if "model" in explicit and "{model}" not in self.template:
            warnings.append("--model is ignored: the --agent-cmd template has no {model}")
        if "{model}" in self.template and "model" not in explicit:
            warnings.append("the --agent-cmd template uses {model} but no --model was given; "
                            "it becomes empty")
        return errors, warnings

    def argv(self, exe: list[str], cfg, t: TurnSpec) -> list[str]:
        values = {"{prompt_file}": t.prompt_file, "{model}": self.model_arg(t.model) or "",
                  "{effort}": cfg.effort or "", "{plan}": t.plan, "{repo}": t.repo,
                  "{name}": t.name}
        out = []
        for i, part in enumerate(split_command(self.template)):
            if i == 0:
                out.append(exe[0] if exe else part)
                continue
            for key, val in values.items():
                part = part.replace(key, val)
            if part:
                out.append(part)
        return out

    def stdin_payload(self, prompt: str) -> str | None:
        return None if self.uses_prompt_file else prompt

    def new_parser(self, prompt: str) -> StreamParser:
        return CustomParser(prompt)

    def limit_signal(self, res: TurnResult, probe_s: int) -> LimitSignal | None:
        hay = res.haystack
        is_limit = bool(LIMIT_TEXT_RE.search(hay) or GEMINI_QUOTA_RE.search(hay))
        return text_limit(res, probe_s, is_limit,
                          [parse_reset_text, parse_try_again, parse_reset_after])


# ---------------------------------------------------------------- selection

AGENTS: dict[str, type] = {"claude": ClaudeAgent, "codex": CodexAgent, "gemini": GeminiAgent}
AUTO_ORDER = ("claude", "codex", "gemini")
ALL_PARENT_VARS = CLAUDE_PARENT_VARS | CODEX_PARENT_VARS | GEMINI_PARENT_VARS


def select_agent(choice: str | None, agent_cmd: str | None, agent_bin: str | None,
                 env: dict, which: Callable[[str], str | None] = find_exe
                 ) -> tuple[Agent | None, str]:
    """(agent, how it was chosen) or (None, error message).

    Precedence: --agent, then $AI_PLAN_LOOP_AGENT, then auto: claude if it is
    on PATH, else the single other supported CLI that is."""
    if choice:
        source = "--agent"
    elif env.get("AI_PLAN_LOOP_AGENT"):
        choice, source = env["AI_PLAN_LOOP_AGENT"], "AI_PLAN_LOOP_AGENT"
    else:
        choice, source = "auto", "auto-detected"
    choice = choice.strip().lower()
    if agent_cmd:
        if choice not in ("auto", "custom"):
            return None, f"--agent-cmd runs a custom agent; it cannot be combined with --agent {choice}"
        return CustomAgent(agent_cmd), "--agent-cmd"
    if choice == "custom":
        return None, "--agent custom needs --agent-cmd \"<command template>\""
    if choice != "auto":
        if choice not in AGENTS:
            return None, (f"unknown agent '{choice}' (from {source}); use one of: auto, "
                          f"{', '.join(AGENTS)}, custom")
        return AGENTS[choice](), source
    if agent_bin:
        return None, "--agent-bin needs an explicit --agent (which CLI does it start?)"
    if which("claude"):
        return ClaudeAgent(), source
    found = [n for n in AUTO_ORDER[1:] if which(n)]
    if len(found) == 1:
        return AGENTS[found[0]](), source
    if not found:
        return None, ("no agent CLI found on PATH (looked for " + ", ".join(AUTO_ORDER)
                      + "); install one, or pass --agent with --agent-bin, or --agent-cmd")
    return None, f"found {' and '.join(found)} on PATH; pick one with --agent"


OPTION_KEYS = ("model", "fallback_model", "effort", "permission_mode", "context_window")


def resolve_options(agent: Agent, cfg) -> tuple[list[str], list[str]]:
    """Fill the options the user left unset (None) from the agent's defaults and
    validate the rest. Returns (errors, warnings)."""
    explicit = {k for k in OPTION_KEYS if getattr(cfg, k, None) is not None}
    if cfg.model is None:
        cfg.model = agent.default_model_label()
    if cfg.fallback_model is None:
        cfg.fallback_model = agent.default_fallback
    if cfg.effort is None:
        cfg.effort = agent.default_effort
    if cfg.permission_mode is None:
        cfg.permission_mode = agent.default_permission
    if cfg.context_window is None:
        cfg.context_window = agent.default_context_window
    return agent.validate(cfg, explicit)
