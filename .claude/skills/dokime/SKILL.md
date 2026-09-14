---
name: dokime
description: Check or set up dokime governance (intent.md, units/ ledger, hooks). Use for /dokime, "dokime status", or "is this repo governed".
---
Run `python3 dokime.py check` and show the block verbatim, `next:` line included; do not summarise it. `next:` names the one command that applies.
When pieces are missing, run `python3 dokime.py init` and show its report; write only the pieces the user confirms, with `python3 dokime.py init --write=<pieces>`.
`--write=intent` runs an interview: ask the user its four questions, feed the answers, and write only after they say yes. Never invent goals, non-goals, criteria or invariants.
Never edit intent.md or a unit's done_condition without asking.
