---
name: proceed
description: Implement the recommendation Claude just gave, exactly as stated — no broadening, no substituting a different approach, no re-asking. Use when the operator says "/proceed", "proceed", "proceed with recommendation", "proceed with your recommendation", or "yes, proceed" after Claude has stated a specific recommended next step.
---

# Proceed

`/proceed` is authorization to execute the recommendation already on the
table. It is not an invitation to reconsider it, improve on it, or expand it.
The considered answer already happened — this is the go-ahead, not a second
round of judgment.

## Find the recommendation before acting

1. Look at the most recent message where a concrete next step was proposed —
   usually a "Want me to do X?" close, or a recommendation stated with its
   tradeoff. If turns happened since, use the most recent one still open, not
   one already acted on or superseded by a later recommendation.
2. If that message posed a genuine, undecided fork — multiple options, no
   stated lean — there is no single recommendation to proceed with. Say so
   and ask which option, rather than picking one.
3. If that message was a plain answer with nothing proposed, there is nothing
   to execute. Say so rather than inventing a task.

When in doubt about which recommendation is meant, or several are live at
once, ask — proceeding against the wrong one is worse than a short pause.

## Match scope exactly

Implement what was recommended — not a narrower version that cuts a part of
it, and not a broader one that folds in an adjacent fix, refactor, or
improvement that occurred to you while working, even if it seems like a
natural extension. If the recommendation drew a line ("add the requirement,
not also fix the code"), hold that line. Name anything adjacent as a
follow-up rather than doing it.

Worked example from this project: asked "should this be added to the
requirements?", the answer recommended formalizing a bounded-shutdown-time
property as a new requirement, explicitly separating that from fixing the
underlying signal-handling bug — two different pieces of work, only one
recommended for now. `/proceed` there means writing the requirement and
nothing else; the code fix stays a named, offered-but-not-taken next step,
exactly as the recommendation drew the line.

## Don't re-litigate

No re-asking "are you sure," no re-presenting the tradeoff, no second
confirmation pass. The case for the recommendation was already made in the
message /proceed is responding to.

The one exception: a genuine blocker that changes the actual situation —
a fact the recommendation didn't have, not a stylistic second thought. That
gets surfaced and paused on. A better-sounding alternative approach to the
same problem does not; that horse already left.

## After

Report what changed, tersely — the same shape as any other finished task.
Don't re-justify the recommendation; that case is already closed.
