# dokime

**dokime** (Greek δοκιμή, pronounced *doh-kee-MAY*): a test that proves a thing
genuine. The Athenian *dokimasia* was the examination every official passed
before taking office. It checked eligibility against records the candidate did
not author. It never judged merit.

dokime gives any git repository three things:

1. **An agreement**: `intent.md`, with goals, non-goals, acceptance criteria and
   invariants. The tool only checks that it exists and is complete.
2. **A ledger**: `units/<name>.json`, one file per unit of work, each with a
   done condition pinned at open and evidence required at close.
3. **A boundary check**: `dokime check`, run by Claude Code hooks at session
   start. Statuses only. No prose judgements.

It exists to catch two failure modes of an AI coding agent working across
sessions:

- **(a) done-condition met, work continued.** A unit whose `run:` condition
  passes while the unit is still open prints `MET-BUT-OPEN`.
- **(b) work with no named unit.** A commit since the previous session start
  (or since the earliest open unit, before the clock has ticked) whose message
  carries no `Unit: <name>` trailer naming a unit open on that day prints
  `work-without-unit`. It clears at the next session start once later commits
  carry trailers; history is never rewritten to fix it.

Stdlib Python plus git. One file, under 600 lines. No dependencies.

## Install

```
pip install .            # gives you the `dokime` command
# or just copy dokime.py into the repo and run python3 dokime.py
```

## Adopting in an existing repo

dokime is added to a repository that already has history, its own docs, and
possibly Claude Code hooks. Nothing is written until you say so, and every
piece is optional.

Decide first where dokime lives. There are two honest placements:

| | Installed on the machine | Vendored in the repo |
|---|---|---|
| Command | `pip install git+https://github.com/deephanson94/dokime` | `cp dokime.py your-repo/tools/dokime.py` |
| Hook runs | `dokime session-start` | `python3 "$CLAUDE_PROJECT_DIR/tools/dokime.py" session-start` |
| Repo carries | records only | records plus a 600-line Python file |
| Survives a clone | only where dokime is installed | yes, wherever `python3` exists |
| Fits | a solo repo, a quick trial, a Python repo | a shared repo whose contributors must not need an install, at the cost of a Python file in, say, a Go tree |

In either placement a hook can be missing or broken. SessionStart's error goes
to the user's terminal, not the agent, so watch for it. `check` prints
`hooks: configured|absent` from the repo's settings, and flags `clock-absent`
when a unit is open or met and the clock has never ticked. A hook that ticked
once and later broke raises nothing; a repo with no units reads clean.

`init` picks the hook command by where it is run from: `dokime` if that is on
PATH, else a repo-relative path if the file is inside the repo, else it refuses
to write hooks and prints the snippet. So run init from the copy you mean to
keep, and do not have a stray `dokime` on PATH when vendoring.

A worked example, installed form, in a Go repo with its own process document
and no CLAUDE.md:

```
pip install git+https://github.com/deephanson94/dokime
cd your-go-repo
dokime init                                   # report only: every piece missing, one diff each
dokime init --write=units,handoffs,hooks,gitignore
# intent.md written by hand: four headings that point at README, SPEC and CI
# rather than restating them; dokime checks only that the headings exist
# the three working rules went into the repo's process doc, not a new CLAUDE.md
dokime check                                  # exit 0: intent present, no units yet
git add -A && git commit -m "Adopt dokime"
```

Two things that example chose, and why:

- No `CLAUDE.md`. The repo deliberately had none. The `claude-md` piece was left
  unwritten and the three lines went into the repo's own process doc. Claude
  Code auto-loads only `CLAUDE.md`, so the agent gets the hook's status block
  but no standing instruction; `init --write=claude-md` adds the
  marker-delimited block later if wanted.
- No first unit. A unit whose condition is "the adoption files exist" is true
  the moment it is written and certifies nothing. The next real change opens
  the first unit with a real test as its condition.

What `init` does to files you already have:

- `.claude/settings.json`: if the `hooks` key is absent or empty, the three
  dokime hooks are added and every other setting is kept; the file is rewritten
  as two-space JSON. If hooks already exist, init prints the snippet and does
  not write; you paste it in.
- `.claude/skills/dokime/SKILL.md`: written if absent, so `/dokime` (or "what is
  the dokime status") runs `check` and shows the block. Any existing file is
  left alone.
- `CLAUDE.md`: three lines are appended between `<!-- dokime -->` markers. The
  rest of the file is untouched.
- `.gitignore`: one line, `.dokime/`, is appended if absent. The session clock
  and the PostToolUse hook's last-seen HEAD live there, uncommitted: the clock
  is per checkout (each git worktree has its own),
  not counted across clones or in CI, and it can be edited without a diff. It
  is a counter, not an audit record.
- Existing commits and branches: never touched. `check` reads history and
  writes nothing to git. On a shallow clone it prints `history: shallow`, and
  `check --at stop` flags `history-shallow` once units exist because the pin
  rule cannot run there; give CI `fetch-depth: 0`.

The first session: open a unit and commit `units/`, start Claude Code and expect
the status block in its context with a `next:` line, work and commit with the
trailer `Unit: <name>`, then close the unit with a work commit as evidence.
After each commit the PostToolUse hook re-checks and speaks only when something
is flagged, so a commit that forgot its trailer is named at once, not at the
next session. dokime records and reports; it does not stop the agent.

`dokime uninstall` removes the hooks, the CLAUDE.md block, the `.gitignore`
line and `.claude/skills/dokime/` (only when its SKILL.md is dokime's own), and
keeps `intent.md`, `units/`, `handoffs/` and `.dokime/`.

## Use

```
dokime init                      # report what is missing; writes nothing
dokime init --write=all          # scaffold everything (intent.md via interview; a TODO stub without a terminal)
dokime open build --condition "run: python3 -m unittest discover -s tests -q" --ceiling 3
dokime check                     # status block; exit 1 on any flag
dokime status                    # one line
dokime close build               # refuses, and lists the commits carrying 'Unit: build'
dokime close build --evidence commit:<one of them>
dokime scan --condition "run: pytest -q"   # earliest recent commit where it passed
dokime uninstall                 # remove hooks, the CLAUDE.md block, the .gitignore line and the skill
```

Every command takes `--json`.

Once the hooks are installed the commands above are mostly for humans. The
agent gets the block at session start, a re-check after every commit and every
skill load (silent unless flagged), and `/dokime` from the skill piece when
someone wants the block on demand. Nothing needs to be remembered; the block's
`next:` line and the CLAUDE.md lines carry the two rules.

Work is tied to a unit by a git trailer: end each commit message with
`Unit: <name>`, in the same final block as any `Co-Authored-By:` line. Git
reads only the last paragraph as trailers, so a `Unit:` line followed by a blank
line and more text is not seen; the tell is a commit you trailered still
counted in `work-without-unit`. The block's last line,
`next:`, names the one command the current state admits, so nothing has to be
remembered. Handoffs are optional: if you keep `handoffs/YYYY-MM-DD-<topic>.md`
files they are counted as session records, in whatever format you already use.

`open` refuses a `run:` condition that already passes; a unit needs a condition
that is false now. `met` is `close` without `closed_at`: the condition and
evidence are verified and recorded, the unit waits for a human to close it.
Commit the unit file after `open`, or the pin reads `UNVERIFIABLE`. To correct
a pinned condition, close the unit with `--force <reason>` and open a new one;
the reason is printed by `check` as `FORCED(<reason>)`.

The intent interview needs a terminal. Without one, set `DOKIME_INTERVIEW` to a
file holding four blank-line-terminated blocks (goals, non-goals, acceptance
criteria, invariants) followed by a line `y`; the draft is printed before it is
written. The other pieces are written regardless.

## What check prints

```
intent: present  hooks: configured  history: full
unit build: open  condition pass  evidence valid  pin unchanged  sessions 4/3  MET-BUT-OPEN  OVER-CEILING
handoffs: 3  last 2026-09-03-parser.md
sessions: 4 (clock)  handoffs 3  commit-days 3
attribution: 5 of 5 commits since last session
flags: met-but-open(build) over-ceiling(build)
next: dokime close build --evidence commit:<sha>
```

The first line also reports `hooks: configured|absent` and `history: full|shallow`.
On a shallow clone every pin reads `UNVERIFIABLE (shallow history)` and
`check --at stop` flags `history-shallow`, since the pin rule cannot run; use
`fetch-depth: 0` in CI.

`attribution: N of M commits since last session` counts work commits carrying a
valid `Unit:` trailer; before the clock has ticked the window starts at the
earliest open unit, and with neither it reads `unavailable`.
`next:` names the applicable command: open a unit, commit under the open one,
close the met one, or install the hooks.

`sessions` comes from `.dokime/sessions.log`, which only the SessionStart hook
appends to. When the hook is not installed it prints `unknown` and the ceiling is
compared against nothing. Handoff and commit-day counts are printed beside it and
marked `DIVERGENT` when commits landed on a day with no session, or, only if a
`handoffs/` directory exists, when a session ended without a handoff.

## Hooks

`dokime init --write=hooks` adds three hooks to `.claude/settings.json`
(verified against Claude Code 2.1.270):

- `SessionStart` runs `dokime session-start` on every source. It ticks the
  clock on `startup` and `clear`, and on a `resume` more than eight hours after
  the last tick; a `compact` or a quick resume is not a new session. A run with
  no hook payload never ticks. It prints the full block when it ticks, after
  `compact`, or when flagged, and one status line on a clean resume. It cannot
  block. Per unit, the session count is the highest of clock, handoffs and
  commit days since open, so the agent can only inflate its own count.
- `Stop` runs `dokime stop-hook`: every flag that needs no `run:` condition
  (`over-ceiling`, `work-without-unit`, pin and evidence), never `met-but-open`
  and never `next:`. By default it is silent when clean, prints a
  `systemMessage` when flagged, and exits 0. With `--strict` it exits 2 on the
  ledger-integrity flags alone (pin, evidence, a missing unit file, shallow
  history), which sends the block back to the model; drift flags stay a
  `systemMessage`.
- `PostToolUse` (matcher `Bash|Skill`) runs `dokime post-tool` after every Bash
  command and every skill load. It is silent unless HEAD moved since its last
  run (a commit, amend, rebase or checkout, however it was made; the last-seen
  HEAD is cached in `.dokime/head`) or the tool was a skill. Then it runs the
  check without `run:` conditions and puts the status block into the agent's
  context only when a flag is raised; after a skill load it always adds the
  one-line status, so a handoff or kickoff skill of your own sees the ledger
  state without any dokime step of its own. It never prints `next:`, never
  echoes the tool's input, and cannot block. This is the channel an agent
  mid-session actually sees: it does not need to remember to run `dokime`.

**Unit files are code.** A `run:` condition is executed with the shell by
`check`, `session-start` and `open`, for every unit that is not closed. Review
`units/*.json` in pull requests as you would a script. Each run is limited to
120 seconds.

If `settings.json` already has hooks, init prints the snippet and does not merge.

## Guarantees

dokime guarantees:

- A unit's done condition is pinned at open. If it later differs, `check` says
  `pin CHANGED` (with the commit it was pinned in, once the unit file is
  committed) and `close` refuses without `--force`.
- A unit cannot be closed without evidence that resolves: a SHA reachable from
  HEAD, dated after open, touching a non-empty file outside the ledger; a
  tracked path; or a `path:sha256` that matches. `--force <reason>` is recorded.
- A unit with a `run:` condition cannot be closed while it exits non-zero,
  except with `--force <reason>`, which `check` then shows as `FORCED(reason)`.
- `unverified` never counts as `pass`.
- A closed unit is not re-verified. Its evidence was checked at close; a later
  rebase does not reopen it.
- A unit file that was ever committed and is now missing prints `unit-missing`.

dokime does not:

- Prevent an agent working past done. It makes that visible at the next session
  start or `dokime check`. The mid-session hooks (Stop, PostToolUse) never run
  `run:` conditions, so `met-but-open` is not raised the moment a commit meets
  the condition; a test suite would otherwise run inside every hook.
- Know whether a commit is relevant to a unit.
- See work that produces no commit and no handoff.
- Keep a tamper-proof session count. The clock is a local, uncommitted file.
- Detect a pin rewritten by `git commit --amend` or a rebase: the pin holds
  against edits, not against history rewriting, so it is load-bearing only on a
  branch that forbids force pushes.
- Stop a `Unit:` trailer set once in `git config commit.template` from
  attributing every commit thereafter. Attribution is a claim; evidence is
  what `close` checks.
- Verify a prose done condition. You do.

## Tests and fixture

```
python3 -m unittest discover -s tests -q
python3 tests/fixture.py a /tmp/replay-a         # incident (a), flags at session 3
python3 tests/fixture.py b /tmp/replay-b         # incident (b), flags at session 2
python3 tests/fixture.py handoffs path/to/repo/handoffs /tmp/replay   # a real repo
```

## Review panel

`.claude/agents/panel-*.md` defines the five reviewers every design change
goes through: red-teamer, adoption skeptic, minimalist implementer, governance
theorist, ease-first newcomer. `/panel <topic>` (`.claude/skills/panel/SKILL.md`)
writes a brief, runs all five in parallel, and synthesizes with dissent recorded.
They are repository files, so any Claude Code session on any clone has the same
panel.

## Claude Code skill

`dokime init --write=skill` (included in `all`) writes
`.claude/skills/dokime/SKILL.md`: `/dokime`, "dokime status" or "is this repo
governed" runs `check` and shows the block verbatim, and runs `init` for the
missing pieces only with the user's confirmation. The skill calls dokime the way
init was run: `dokime` when it is on PATH, else `python3 <path to the vendored
copy>`. It never edits `intent.md` or a pinned condition.
