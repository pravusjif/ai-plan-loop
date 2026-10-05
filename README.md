# AI Plan Loop

Works through a markdown plan unattended, one milestone at a time, as a chain of Claude Code
sessions. You give it a plan on the command line. It runs until every item is done, a human is
needed, or you stop it. Usage limits are waited out on their own, and long sessions are swapped
for fresh ones.

It takes any markdown plan with a checklist, keeps a session going while its context is small,
and can check the environment before each turn with an optional `--preflight` command.

## Install

The tool is four files and needs Python 3.9+ (stdlib only) and the `claude` CLI on `PATH`. Put
it in the repo whose plan it should work through. The examples below assume `tools/ai_plan_loop/`:

```powershell
git submodule add <this repo's URL> tools/ai_plan_loop   # or copy the files there
```

## Quick start

From anywhere inside the repo:

```powershell
python tools\ai_plan_loop\plan_loop.py docs\PLAN.md --dry-run           # shows the parse, the command and both prompts; runs nothing
python tools\ai_plan_loop\plan_loop.py docs\PLAN.md --max-milestones 1  # do this first: one milestone, then stop
python tools\ai_plan_loop\plan_loop.py docs\PLAN.md                     # until the plan is done
python tools\ai_plan_loop\plan_loop.py docs\PLAN.md --until "6. Build"  # stop once that item is ticked
```

| To stop | Do this |
| --- | --- |
| After the current milestone | Create `.ai-loop\<plan>\STOP`, for example `.ai-loop\docs-PLAN-md\STOP`. It also interrupts a usage-limit wait. |
| Right now | Press `Ctrl-C`. This kills the running session's process tree. Its uncommitted work stays in the tree, and the next session is told to inspect it. |

Restarting is safe and resumes from `state.json`, including model benches. Delete the `HALTED`
file first if the previous run halted. Only one driver can run per repo, enforced by an OS lock
on `.ai-loop/driver.lock`, because every session shares the working tree and the git index.

## The rules

1. **Model.** Each session uses `fable`. While Fable is usage-limited or unavailable on the
   account, sessions use `opus`. Fable comes back as soon as its bench expires. Change the
   models with `--model` / `--fallback-model`.
2. **Effort.** Sessions run at `high` effort (`--effort`).
3. **The plan is the only handoff.** Each session reads the plan named on the command line and
   takes the next milestone that is not done. Nothing else carries between sessions: only the
   plan, the code and the git history on disk.
4. **One milestone per turn.** A *turn* is one `claude -p` process. It must end with a
   `LOOP_STATUS:` line. The end of a turn that lands a milestone is a **checkpoint**.
5. **The context rule.** At each checkpoint the driver measures how full the session's context
   is.
   - **Under 30%:** the same session continues with the next milestone, through
     `claude -p --resume <session-id>`. The context and prompt cache are kept.
   - **At or over 30%:** the session ends and the next milestone starts in a **fresh**
     session.

   The check happens only at checkpoints and never cuts a milestone in half. A turn that ends
   without closing a milestone (`PARTIAL`, `BLOCKED`, or no status) always rotates, because a
   fresh context is the better second attempt. Set the threshold with `--context-threshold`.
6. **Usage limits.** A usage limit is not counted as a failure. The limited model is benched
   until the server's reset time, and the other model takes over immediately. When every model
   is benched, the driver sleeps until the earliest reset and then starts a fresh session. The
   reset time is worked out in this order:
   1. The exact `resetsAt` epoch from the stream's `rate_limit_event`. It has no timezone
      ambiguity, so the driver waits until 2 minutes after it (`--limit-margin-s`).
   2. A `usage limit reached|<epoch>` message.
   3. Reset text such as "resets 1am (Europe/Berlin)", read in that zone, or in local time if
      Python has no tz database.
   4. No time known: probe every 20 minutes (`--limit-probe-s`).

   A model is re-probed at least every 12 hours even if its reset is further away
   (`--limit-max-sleep-s`), which matters for weekly limits that turn out to be wrong. If a
   reported reset time has passed and the model is still limited, the driver probes every 20
   minutes instead of waiting until the same time tomorrow. A rejected call costs nothing.
7. **Progress is judged by the repo, not by what the session says.** A turn has made progress
   if HEAD moved (it committed) or a checklist item in the plan was ticked. `LOOP_STATUS:
   LANDED` with neither counts as a stall.
8. **When it halts for a human.** The driver writes `HALTED` with the reason and stops in any
   of these cases:
   - two stalls in a row
   - three hard failures in a row
   - ten fast failures in a row (died within 2 minutes having done nothing; see Architecture)
   - the branch changed under it
   - no model in the chain is available
   - `--preflight` never passed
9. **Finish.** The run finishes when every checklist item in scope is ticked, or a session
   reports `LOOP_STATUS: PLAN_COMPLETE`.

## Architecture

```
plan_loop.py docs/PLAN.md
 └─ outer loop ─────────────────────────────────────────────────────────────────
     checks: STOP · --max-* · plan complete · branch unchanged
     pick model: first in [fable, opus] not benched / unavailable
         none?  → sleep until the earliest reset (interruptible by STOP) → loop
     --preflight (optional)
     └─ session ────────────────────────────────────────────────────────────────
         turn 1:  claude -p --model M --effort high ...   stdin = prompt.md
         turn k:  claude -p --resume <id> ...              stdin = continue.md
           │ stream-json → readable log + raw .jsonl; harvest:
           │   session_id, rate_limit_event, last top-level usage (context),
           │   result (is_error, api_error_status, cost, modelUsage.contextWindow)
           ▼
         classify the turn, in this order:
           usage limit       → bench M until reset         → back to outer loop
           model unavailable → drop M for this run          → back to outer loop
           fast failure      → bench M 5→60 min (growing)   → back to outer loop
           hard error        → count; overload backoff / cooldown; halt at 3
           plan complete     → exit 0
           no commit, no tick→ stall; halt at 2             → fresh session
           progress, milestone not closed                   → fresh session
           milestone landed  → CHECKPOINT:
               STOP / --max-milestones → exit 0
               context ≥ threshold     → fresh session
               context <  threshold    → turn k+1 (resume)
```

How the pieces work:

- **The ledger.** The `Ledger` class re-reads the plan file every time it is asked. By default
  an item is a top-level markdown checkbox: `- [ ] ...` is open and `- [x] ...` is done.
  Indented sub-checkboxes are ignored, so a milestone's own task list is not counted as
  separate milestones. For a plan that tracks status another way, pass `--open-regex` and
  `--done-regex`; group 1 of each is the item's label. If a plan has no matching lines, the
  driver judges progress by commits and `LOOP_STATUS` alone, and only `PLAN_COMPLETE` ends the
  run. `--until "<text>"` cuts the ledger after the first item whose label contains that text.
- **Context measurement.** "Context used" means the input, cache-creation and cache-read
  tokens, plus output tokens, of the last top-level assistant message in the turn. That is what
  the model saw on its final call. Subagent messages are skipped because they run in their own
  context. The window comes from `modelUsage[<model>].contextWindow` on the `result` event.
  `--context-window` (200k) is used only if the stream ever stops reporting it. On a 1M-token
  window, a fresh session typically starts at a few percent (system prompt, tools and
  CLAUDE.md), so 30% leaves over 250k tokens of working room before rotation.
- **Fast-failure net.** The limit regexes will always lag behind new wordings. A failure within
  `--fast-fail-s` (120 s) that commits nothing is treated as a limit or transient error the
  driver does not recognise yet. It benches the model on a growing backoff rather than spending
  the hard-error budget.
- **Overload** inside a call is left to the CLI's own `--fallback-model`, which the driver
  passes whenever the other model is not benched. An overload that still kills a turn gets an
  exponential backoff.
- **Interrupted milestones.** A session cut off by a limit, a timeout or Ctrl-C leaves its work
  uncommitted on disk. The driver never resumes such a session, because the model may have
  changed and the cache is long gone. The next fresh session is told to start with
  `git status`, then finish or revert the work deliberately.

## The prompts

| File | Sent | Purpose |
| --- | --- | --- |
| `prompt.md` | first turn of every session | The task (read the plan, do the next milestone, decide design questions yourself and record them), then the loop protocol: one milestone per turn, start with `git status`, stay on the branch, never push, commit every verified milestone in the repo's convention, the six-part definition of done, honest status over optimistic, leave the environment usable, spend context carefully, and the `LOOP_STATUS` line. |
| `continue.md` | each further turn in the same session | "Checkpoint passed at N% context: re-read the plan from disk, check the tree, take the next milestone, same protocol." |

The prompts are generic. Anything specific to a repo or plan belongs in that repo's `CLAUDE.md`
and the plan itself, which every session reads first. For instructions that only make sense
while running unattended, add a file with `--append-prompt-file extra.md` (repeatable). It is
appended to the first prompt after a `---`. Use `--prompt-file` / `--continue-file` to replace a
prompt entirely.

Placeholders, substituted at send time:

| Placeholder | Becomes |
| --- | --- |
| `{{PLAN}}` | the plan's path relative to the repo |
| `{{BRANCH}}` | the branch the run is pinned to |
| `{{NEXT_HINT}}` | "By the plan's checklist the next open item is `…`" (or a note that none is visible) |
| `{{SCOPE}}` | the `--until` instruction, or empty |
| `{{THRESHOLD}}` | the context threshold, e.g. `30%` |
| `{{CONTEXT_PCT}}` | the measured context at the last checkpoint (continue prompt only) |

The `LOOP_STATUS` contract is the final line of every turn:

```
LOOP_STATUS: LANDED <milestone>
LOOP_STATUS: PARTIAL <milestone> — <reason>
LOOP_STATUS: BLOCKED — <reason>
LOOP_STATUS: PLAN_COMPLETE
```

Sessions also get the environment variables `AI_PLAN_LOOP=1` and `AI_PLAN_LOOP_PLAN=<plan>`, so
hooks or scripts can tell they are running unattended.

## Files

| Path | What it is |
| --- | --- |
| `plan_loop.py` | the driver |
| `prompt.md`, `continue.md` | the session prompts |
| `.ai-loop/driver.lock` | one driver per repo |
| `.ai-loop/<plan>/driver.log` | one line per decision, for the whole run |
| `.ai-loop/<plan>/logs/NNNN-<ts>.log` | readable transcript of session N, all its turns |
| `.ai-loop/<plan>/logs/NNNN-<ts>.jsonl` | that session's raw stream-json |
| `.ai-loop/<plan>/state.json` | counters, model benches (`limited_until`), cost, and per-turn history (context %, decision) |
| `.ai-loop/<plan>/STOP`, `HALTED` | control and halt sentinels |

`<plan>` is the plan path made into a slug (`docs/PLAN.md` → `docs-PLAN-md`), so different
plans keep separate state. On first run the driver adds `/.ai-loop/` to `.git/info/exclude`,
which is untracked, so sessions running `git add -A` can never stage it.

## Options

| Flag | Default | Notes |
| --- | --- | --- |
| `plan` (positional) | — | the plan file; the repo is found from its location |
| `--until` | whole plan | stop once the item whose label contains this text is ticked |
| `--max-milestones` / `--max-sessions` | `0` (no limit) | count this run only, not lifetime |
| `--branch` | branch at start | halts if HEAD moves to another branch |
| `--model` / `--fallback-model` | `fable` / `opus` | `--fallback-model ''` disables rotation |
| `--effort` | `high` | |
| `--permission-mode` | `bypassPermissions` | see Warnings |
| `--context-threshold` | `0.30` | `30` also works: values of 1 and up are percentages |
| `--context-window` | `200000` | only if the stream does not report a window |
| `--max-turns-per-session` | `0` (off) | extra rotation cap |
| `--prompt-file` / `--continue-file` / `--append-prompt-file` | `prompt.md` / `continue.md` / none | |
| `--open-regex` / `--done-regex` | top-level `- [ ]` / `- [x]` | ledger format |
| `--turn-timeout-s` | `14400` (4 h) | a hung turn is killed |
| `--limit-probe-s` | `1200` | retry interval when the reset time is unknown |
| `--limit-margin-s` | `120` | wait this long past a reported reset |
| `--limit-max-sleep-s` | `43200` | re-probe a benched model at least this often |
| `--max-consecutive-stalls` / `-errors` / `--max-fast-failures` | `2` / `3` / `10` | halt thresholds |
| `--preflight` | none | shell command (cmd.exe on Windows) that must exit 0 before each turn; probed every `--preflight-probe-s` (300) up to `--preflight-max-probes` (24) times |
| `--max-budget-usd` | none | per-turn cap; only bites on API-key billing |

## Verified CLI facts (Claude Code 2.1.173, 2026-10-02)

- A usage-limited model gives
  `{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":<epoch>,"rateLimitType":"seven_day_overage_included",…}}`,
  then a `result` with `subtype:"success"` (it lies), `is_error:true`, `api_error_status:429`,
  and the text "You've hit your limit · resets 1am (Europe/Berlin)". The process exits 1 in
  under a second and costs $0. `rate_limit_event` with `status:"allowed"` also appears on
  healthy calls; ignore it.
- Limits are per model: Fable was capped while Opus answered in the same minute.
- An unknown or inaccessible model gives exit 1, `api_error_status:404`, and "There's an issue
  with the selected model (X). It may not exist or you may not have access to it."
- `claude -p --resume <id>` keeps the same `session_id`, and context accumulates across turns.
  A resumed turn hits the prompt cache.
- `result.modelUsage["claude-opus-4-8"].contextWindow` was `1000000` for `--model opus`.
- The base context of a fresh session in the repo the tool was developed in was about 35k tokens.

## Warnings

- **`bypassPermissions` runs every tool call unattended, including `rm` and `git reset`.** The
  prompt forbids pushing, branch switching and rewriting history, but nothing *enforces* that.
  `--permission-mode auto` applies the account's normal allowlist instead. Calls outside it are
  then denied silently, which some milestones will not survive.
- **It commits to your branch while you are away, and it decides design questions itself.**
  Every decision is supposed to be recorded in the plan. Review `git log` and the plan's
  decision entries before merging or pushing, and use `--max-milestones 1` for the parts of the
  plan you care most about.
- **Shared, single-tenant resources.** Do not work in the same repo, or in the apps the plan
  drives (an editor, a game engine, a browser), while the chain runs. Your edits collide with a session's, and
  a session reads your actions as broken behaviour.
- **Keep the machine awake** for long runs, and make sure commit signing (if enabled) does not
  need a prompt nobody will answer.
- **Limit detection is best-effort beyond the structured event.** If the chain halts with
  consecutive fast failures exactly when a limit was expected, read the tail of that session's
  `.log` and teach `LIMIT_TEXT_RE` the new wording.
- **Cost is logged, not capped**, unless you pass `--max-budget-usd`, which only applies on
  API-key billing. `state.json` holds the running total.
