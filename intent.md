# intent — dokime

dokime (δοκιμή, doh-kee-MAY): a test that proves a thing genuine. The Athenian
dokimasia was the examination every official passed before taking office. It
checked eligibility against records the candidate did not author. It never
judged merit.

## Goals

1. Give any git repository three things: an agreement (`intent.md`), a ledger of
   work units (`units/*.json`), and a boundary check (`dokime check`) run by
   Claude Code hooks at session start.
2. Make two failure modes visible and attributable at the next session boundary:
   - (a) done-condition met, work continued.
   - (b) work with no named unit.
3. Stay small enough to be read in one sitting and trusted without study: one
   Python file, stdlib plus git, under 600 lines excluding tests.
4. Be honest about reach. Every status the tool prints is derived from a record it
   can name; anything it cannot examine prints as `unverified` or `unknown`, never
   as a pass and never as a number.

## Non-goals

Fixed by the specification:

- No transcript reading.
- No summarisation.
- No model calls in `check`.
- No web UI.
- No multi-repo view.
- No dependency beyond stdlib and git.

Added by the Phase 0 review:

- No prevention. dokime does not stop an agent from working past done. It makes
  that visible at the next session start and at `close`.
- No relevance judgement. dokime knows that a commit exists, is reachable from
  HEAD, postdates the unit, and touches real files. It does not know whether the
  commit relates to the unit.
- No verification of prose done conditions. Only `run:` conditions are executed.
  A prose condition is `unverified` for its whole life. The human verifies it.
- No visibility into work that produces no commit.
- No beads backend. Beads has no immutable pinned-condition field, so the pin
  check would degrade to "skip" there, which pulls a second workflow into
  `check`. The spec's own drop condition applies.
- No config management. `init` is a one-time scaffold. It never merges into an
  existing hooks array and never rewrites a file that already carries its marker.
- No hand-edited ledger. Unit files are machine-written JSON. Hand edits are
  detected by the pin, not supported by a parser.

## Acceptance criteria

1. `dokime init` runs clean on a bare repository. On a fresh `git init` with no
   commits it reports every missing piece, writes nothing, and exits 0. With
   `--write=all` it produces a repository in which `dokime check` exits 0.
2. `dokime check` replays the Phase 4 fixture and flags both incidents:
   incident (a), a unit whose `run:` condition passes at session 2 while sessions
   3 to 6 continue, is flagged `met-but-open` at session 3; incident (b), four
   sessions of commits with no `Unit:` trailer, is flagged `work-without-unit`
   at session 2.
   Verified by `run: python3 -m unittest discover -s tests -q`.
3. Adopted in two repositories for two sessions each with no change to dokime's
   code.
4. The whole build fits in 3 sessions, counted by dokime's own clock.

## Invariants

- Never set `met` without evidence. `close` refuses when the evidence list is
  empty, when any ref fails to resolve, or when a `run:` condition exits non-zero.
  `close --force <reason>` records the reason in the unit and is itself a status.
- Never edit `intent.md` or a pinned `done_condition` without asking.
- `done_condition_sha256` is computed at `open` and never edited. The commit
  that first added the unit file is derived from git history, never stored.
  `check` compares the current condition against both and reports `unchanged`,
  `CHANGED since <sha>`, or `UNVERIFIABLE`. `close` refuses on `CHANGED`
  without `--force`.
- Every command has `--json`.
- `check` prints statuses only. No prose judgements.
- `unverified` never counts as `pass`.
- The session count is never typed. It comes from `.dokime/sessions.log`, which
  only the SessionStart hook appends to. When the log is absent the count prints
  as `unknown` and the ceiling is compared against nothing. Handoff files and
  commit days are reported beside it and marked `DIVERGENT` when they disagree.
  The log is gitignored: the clock is per checkout.
- Every commit that touches work (anything outside the ledger and the governance
  files) carries the trailer `Unit: <name>`, naming a unit that was open on the
  commit's date. `check` reports attribution since the previous session start
  and flags `work-without-unit` while the clock is live. Handoffs are optional
  session records in whatever form the user already keeps.
- The status block ends with one `next:` line naming the single command the
  state admits. It names a command, never a reason, and never appears in the
  Stop hook's output.
- Evidence refs: a `commit` is an abbreviated or full SHA (never a symbolic ref),
  reachable from HEAD, dated after `opened_at`, touching at least one non-empty
  file outside `handoffs/` and `units/`. An `artifact` is a path that exists and is
  tracked. A `sha256` is `path:hash` and the hash matches the file's content.
- Stored timestamps (`opened_at`, `closed_at`, the clock) are UTC. Commit days are
  taken in the committer's own zone, the date a person would write for that day.
- The Stop hook never executes `run:` conditions. By default it prints and exits
  0. Under `--strict` it exits 2 on ledger-integrity rules only (pin, evidence
  refs). It always short-circuits when `stop_hook_active` is set.
- The only unconditional gate is `dokime close`.
- `init` writes nothing without `--write`. `uninstall` removes the hooks, the
  CLAUDE.md block, the `.gitignore` line and the `dokime` skill directory, and
  keeps every record: `intent.md`, `units/`, `handoffs/`, `.dokime/`. It never
  rewrites a file it changes nothing in.
- Under 600 lines of code excluding tests. No dependencies.

## Decisions

Recorded so the deviations from the original specification are visible.

1. Ledger files are `units/<name>.json`, not `.yaml`. A stdlib subset YAML parser
   costs 75 lines and is a bypass surface: a file that fails to parse is a rule
   that does not run.
2. The beads backend is dropped (see non-goals).
3. The Stop hook does not block by default and there is no two-session
   `--warn-only` cliff. Blocking is opt-in via `--strict`.
4. `evidence-without-close` is not built. Evidence is batched at `close` in
   practice, so the flag would fire only on honest incremental bookkeeping. Prose
   units get a status line (`unverified, open N sessions`), not a flag.
5. Session count comes from a hook-written log via `dokime session-start`, not
   from handoff filenames.
6. Two commands added: `scan` (walk recent commits with a `run:` condition and
   print the earliest commit where it passed and how many followed) and
   `uninstall`.
7. The `init` interview runs only under `--write=intent`: four questions, draft
   shown before writing, nothing invented. Without a terminal it writes the four
   headings with TODO markers and `check` reports `intent: stub`, so a first run
   is never red for lack of a terminal.
8. Unit membership comes from a `Unit: <name>` commit trailer, which lives inside
   the commit object, not from a line in a handoff file. Handoffs are optional and
   their format is the user's own. `close` without `--evidence` lists the commits
   that carry the unit's trailer and refuses; evidence stays a human choice.
