---
name: panel
description: Run the five-agent dokime design review panel on a proposal, a diff, or a question, then synthesize. Use when the owner says /panel, "get the panel to review", or asks for panel feedback before a change.
---
# dokime review panel

Five reviewers live in `.claude/agents/`: `panel-redteam`, `panel-skeptic`, `panel-implementer`, `panel-theorist`, `panel-newcomer`. The owner's standing rules: every change the owner suggests goes to the panel before it is built; newcomer feedback carries more weight than the others; intent.md and pinned done conditions are never edited without asking.

1. **Write a brief** to `<scratch>/panel/<topic>-brief.md`, where `<scratch>` is this session's scratchpad directory. It states: the repo path and commit (`git rev-parse --short HEAD`), the line count (`grep -cvE '^\s*(#|$)' dokime.py`), the question or the proposal with options, what is already decided and must not be reopened (cite intent.md Decisions), numbered questions each reviewer must answer, the output file `<scratch>/panel/<topic>-review-<role>.md`, and a word limit. Put the diff or the draft in the brief or beside it; reviewers do not read the conversation.
2. **Spawn all five in one message** with the Agent tool, `subagent_type` set to each agent name, in the background. Each prompt: "Read the brief at <path> in full first, then review in your role. Write to <output file>. Return the full text." Nothing else; the role lives in the agent file.
3. **Wait for all five.** Do not synthesize early and do not predict a missing review.
4. **Synthesize** for the owner: a table of position per reviewer per question; where they converge, the decision; where they split, both sides in one line each and your recommendation with the reason, weighting the newcomer; anything a reviewer verified against the installed Claude Code that changes the design; anything a reviewer claimed about the world that you did not verify, marked as unverified. Under 400 words.
5. **Then build**, only what the owner approves. Open a unit before work; commit with the `Unit: <name>` trailer; record any new decision in intent.md only after the owner says yes.

Do not run the panel on a one-line fix or a question you can answer from the code; it costs five Opus runs. Run it on defaults, hooks, formats, new commands, and anything the owner asks to have reviewed.
