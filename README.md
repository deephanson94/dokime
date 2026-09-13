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

## Use

```
dokime init                      # report what is missing; writes nothing
dokime init --write=all          # scaffold everything (intent.md via interview)
dokime open build --condition "run: python3 -m unittest -q" --ceiling 3
dokime check                     # status block; exit 1 on any flag
dokime status                    # one line
dokime close build --evidence commit:abc1234
dokime scan --condition "run: pytest -q"   # earliest recent commit where it passed
dokime uninstall                 # remove hooks and the CLAUDE.md block; keep records
```

Every command takes `--json`.

A handoff is `handoffs/YYYY-MM-DD-<topic>.md`, written at the end of a session,
with a line `unit: <name>`.

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

- `SessionStart` runs `dokime session-start`: ticks the clock and prints the
  full check into the agent's context. It cannot block.
- `Stop` runs `dokime stop-hook`: ledger-integrity rules only (pin, evidence),
  never `run:` conditions. By default it prints and exits 0. With `--strict` it
  exits 2, which sends the flags back to the model.

If `settings.json` already has hooks, init prints the snippet and does not merge.

## Guarantees

dokime guarantees:

- A unit's done condition is pinned at open. If it later differs, `check` says
  `CHANGED` and names the commit.
- A unit cannot be closed without evidence that resolves: a SHA reachable from
  HEAD, dated after open, touching a non-empty file outside the ledger; a
  tracked path; or a `path:sha256` that matches. `--force <reason>` is recorded.
- A unit with a `run:` condition cannot be closed while it exits non-zero.
- `unverified` never counts as `pass`.

dokime does not:

- Prevent an agent working past done. It makes that visible at the next session
  start.
- Know whether a commit is relevant to a unit.
- See work that produces no commit and no handoff.
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
