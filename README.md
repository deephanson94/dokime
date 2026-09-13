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
- **(b) work with no named unit.** Commits since the last handoff, when that
  handoff names no unit that was open on its date, print `work-without-unit`.

Stdlib Python plus git. One file, under 600 lines. No dependencies.

## Install

```
pip install .            # gives you the `dokime` command
# or just copy dokime.py into the repo and run python3 dokime.py
```

## Adopting in an existing repo

dokime is added to a repository that already has history, a `CLAUDE.md`, and
possibly Claude Code hooks. Nothing is written until you say so.

```
cd your-repo
pip install /path/to/dokime        # or: cp /path/to/dokime/dokime.py .
dokime init                        # report only: what is missing, and the exact diff per file
dokime init --write=all            # the intent interview needs a terminal or DOKIME_INTERVIEW
dokime check                       # baseline; exit 0 on a clean adoption
git add -A && git commit -m "Adopt dokime"
```

What `init` does to files you already have:

- `.claude/settings.json`: if the `hooks` key is absent or empty, the two dokime
  hooks are added and every other setting is kept; the file is rewritten as
  two-space JSON. If hooks already exist, init prints the snippet and does not
  write; you paste it in.
- `CLAUDE.md`: three lines are appended between `<!-- dokime -->` markers. The
  rest of the file is untouched.
- `.gitignore`: one line, `.dokime/`, is appended if absent. The session clock
  lives there, uncommitted: it is per checkout, not counted across clones or in
  CI, and it can be edited without a diff. It is a counter, not an audit record.
- Existing commits and branches: never touched. `check` reads history and
  writes nothing to git.

The hook command depends on where dokime lives. If `dokime` is on PATH the hook
runs `dokime session-start`. Otherwise, with `dokime.py` inside the repo, it
runs `python3 "$CLAUDE_PROJECT_DIR/dokime.py" session-start`, which survives a
clone; with `dokime.py` elsewhere it is an absolute path, which does not.

The first session: open a unit and commit `units/`, start Claude Code and expect
the status block in its context, work and commit, end with
`handoffs/YYYY-MM-DD-<topic>.md` containing `unit: <name>`, then close the unit
with the work commit as evidence. dokime records and reports; it does not stop
the agent.

`dokime uninstall` removes the hooks, the CLAUDE.md block and the `.gitignore`
line, and keeps `intent.md`, `units/`, `handoffs/` and `.dokime/`.

## Use

```
dokime init                      # report what is missing; writes nothing
dokime init --write=all          # scaffold everything (intent.md via interview)
dokime open build --condition "run: python3 -m unittest discover -s tests -q" --ceiling 3
dokime check                     # status block; exit 1 on any flag
dokime status                    # one line
dokime close build --evidence commit:$(git rev-parse --short HEAD)
dokime scan --condition "run: pytest -q"   # earliest recent commit where it passed
dokime uninstall                 # remove hooks, the CLAUDE.md block and the .gitignore line
```

Every command takes `--json`.

A handoff is `handoffs/YYYY-MM-DD-<topic>.md`, written at the end of a session,
with a line `unit: <name>`. Detection is day-granular: two sessions on one day
are one boundary.

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
intent: present
unit build: open  condition pass  evidence valid  pin unchanged  sessions 4/3  MET-BUT-OPEN  OVER-CEILING
handoffs: 3  last 2026-09-03-parser.md  unit build
sessions: 4 (clock)  handoffs 3  commit-days 3
flags: met-but-open(build) over-ceiling(build)
```

`sessions` comes from `.dokime/sessions.log`, which only the SessionStart hook
appends to. When the hook is not installed it prints `unknown` and the ceiling is
compared against nothing. Handoff and commit-day counts are printed beside it and
marked `DIVERGENT` when a session ended without a handoff or commits landed on a
day with no session.

## Hooks

`dokime init --write=hooks` adds two hooks to `.claude/settings.json`
(verified against Claude Code 2.1.270):

- `SessionStart` runs `dokime session-start` on every source. It ticks the
  clock on `startup` and `clear`, and on a `resume` more than eight hours after
  the last tick; a `compact` or a quick resume is not a new session. A run with
  no hook payload never ticks. It prints the full block when it ticks, after
  `compact`, or when flagged, and one status line on a clean resume. It cannot
  block. Per unit, the session count is the highest of clock, handoffs and
  commit days since open, so the agent can only inflate its own count.
- `Stop` runs `dokime stop-hook`: ledger-integrity rules only (pin, evidence),
  never `run:` conditions. By default it is silent when clean, prints a
  `systemMessage` when flagged, and exits 0. With `--strict` it exits 2, which
  sends the flags back to the model.

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
- A unit with a `run:` condition cannot be closed while it exits non-zero.
- `unverified` never counts as `pass`.
- A closed unit is not re-verified. Its evidence was checked at close; a later
  rebase does not reopen it.
- A unit file that was ever committed and is now missing prints `unit-missing`.

dokime does not:

- Prevent an agent working past done. It makes that visible at the next session
  start.
- Know whether a commit is relevant to a unit.
- See work that produces no commit and no handoff.
- Keep a tamper-proof session count. The clock is a local, uncommitted file.
- Verify a prose done condition. You do.

## Tests and fixture

```
python3 -m unittest discover -s tests -q
python3 tests/fixture.py a /tmp/replay-a         # incident (a), flags at session 3
python3 tests/fixture.py b /tmp/replay-b         # incident (b), flags at session 2
python3 tests/fixture.py handoffs path/to/repo/handoffs /tmp/replay   # a real repo
```

## Claude Code skill

`.claude/skills/dokime/SKILL.md` lets `/dokime` run `init` as a command. Copy the
directory into another repo's `.claude/skills/` to use it there.
