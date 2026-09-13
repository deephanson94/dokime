---
name: dokime
description: Set up or check dokime governance in this repo (intent.md, units/ ledger, session hooks). Use when the user says /dokime, asks to initialise dokime, or asks whether the repo is governed.
---

# dokime

Run `dokime init` (or `python3 dokime.py init` if it is not on PATH) and show the
report verbatim. It writes nothing.

If the user wants the missing pieces written, run `dokime init --write=<pieces>`
with exactly the pieces they confirmed. `intent` runs an interview: ask the user
the four questions it prints, feed their answers, show the draft, and write only
after they say yes. Never invent goals, non-goals, criteria or invariants.

Then run `dokime check` and show the status block. Do not summarise it.

Never edit `intent.md` or a unit's `done_condition` without asking.
