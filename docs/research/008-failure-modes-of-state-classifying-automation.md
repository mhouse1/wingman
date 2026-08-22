# Research 008 — Failure Modes of State-Classifying Automation

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-21 | 1.8.5           |

## Question

Wingman perceives an environment it cannot query, classifies it into a state,
and acts on that classification. On 2026-08-21 it spent six consecutive
sessions unable to reach a battle, and a memory-leak fix that had been declared
confirmed turned out not to be one.

What generalizable lessons does that day hold for any system built on
*perceive → classify → act*, beyond the specific fixes recorded in ADR 086,
ADR 087, and Performance 008?

This document is a retrospective rather than an adoption spike, so it differs
in genre from Research 001–007. It records lessons intended to outlive this
codebase; the mechanics stay in the ADRs.

## Lesson 1 — Gate behaviour on evidence, not on the state label

**The strongest finding of the incident.** Three defects, written at different
times by different reasoning, all shared one root:

| Gate | Trusted | Consequence |
|------|---------|-------------|
| Anomaly screenshot capture (ADR 074) | state is `GAME_UNKNOWN` | A blind-but-classified state produced no evidence |
| Stall recovery (ADR 084) | state in `STALL_ACTION_STATES` | The one dialog the system creates itself was never scanned for |
| Click-to detector suppression | state is `GAME_LOBBY` | The detector that would clear the screen was disabled |

Each rule was individually correct when written. Each encoded "the classifier
is right" as an unstated premise, and that premise is exactly what fails during
a classification fault.

**The pattern.** A state label is a *conclusion*. Gating on it means the gate
inherits every way the conclusion can be wrong. Gating on the underlying
evidence — "are the crops that define this state actually present?" — keeps the
gate working precisely when the label does not.

**Applicable check.** For any condition of the form `if state == X`, ask: *what
happens when the system believes X and is wrong?* If the answer is "this gate
does the opposite of what it should", gate on the evidence instead.

## Lesson 2 — A forced state is a lie that can disable its own correction

The root cause was a recovery mechanism:

```
GAME_END_B timeout — click-to OCR may be stuck; forcing recovery to GAME_LOBBY
```

A timeout fired and *asserted* a state rather than verifying one. The game was
still on the post-match screen, so the assertion was false. The click-to
detector self-suppresses in the asserted state — a correct rule, added after a
real double-actuation bug. So the forced state disabled the one detector that
could have cleared the screen that caused the timeout.

**The pattern.** "Recovery" that changes the system's belief rather than the
world is not recovery; it is renaming the problem. It is especially dangerous
when other components legitimately trust that belief, because the lie
propagates into their logic as truth.

**Applicable check.** When a timeout handler sets state, enumerate every
consumer that reads that state. If any of them *stops looking* as a result, the
handler can deadlock the system. Prefer moving to an explicit "unknown" or
"degraded" state over asserting a specific one — unknown is honest, and
downstream code already handles it conservatively.

## Lesson 3 — Diagnostics must not be gated on the assumption that failed

The anomaly recorder captured screenshots only in `GAME_UNKNOWN`. So the
*confidently wrong* state produced **less** evidence than the honestly unknown
one — exactly backwards, because a confidently wrong state is harder to
diagnose: every subsystem reports itself healthy.

Adding the capture on a blackout answered a 17-minute mystery within minutes of
shipping, and did so twice more as each fix revealed the next layer.

**The pattern.** Instrumentation gated on a healthy-path assumption goes dark
in precisely the scenarios it exists for.

**Applicable check.** For each diagnostic, ask which failure it is meant to
catch, then ask whether its own trigger condition survives that failure.

## Lesson 4 — Recovery actions can manufacture the condition they respond to

The lobby-stall response pressed ESC. In this game's lobby, ESC *opens* an
"Exit to Desktop" modal — which matches no lobby crop, which is a lobby stall.
Three uncoordinated ESC sources (a 45s loop, a 10s beat, a 20s recovery) then
fought one 20s-cooldown attempt to cancel it, on a 23-second cycle:

```
10:48:27  clicking CANCEL
10:48:33  not found            <- dialog cleared
10:48:34  stall threshold reached — pressing ESC   <- re-opened
```

Worse, the modal's default highlighted button was **Exit**. The system spent
eight minutes holding open a dialog where one stray Enter would have quit the
game.

**The pattern.** A remedy whose side effect satisfies its own trigger is a
latch. It is invisible in unit tests, because the loop closes through the
environment, not through the code.

**Applicable check.** For each automated remedy, ask what the environment looks
like *immediately after* it acts, and whether that new state re-satisfies the
trigger. Also count the actuators: three independent sources issuing the same
input with different periods is a design smell regardless of correctness.

## Lesson 5 — The measurement window must exceed the phenomenon's onset time

A memory leak was declared fixed on a 56-minute session showing flat OCR
latency and +120 MB/h growth. A later 5h43m session, same build, same
mitigation:

| Hour | OCR median | RSS |
|------|-----------|-----|
| 1 | 0.30 s | 2.7 GB |
| 3 | 0.54 s | 5.8 GB |
| 6 | **1.97 s** | **10.9 GB** |

Hour 1 reads ~0.30 s *whether or not the leak is fixed*. The short session could
not have distinguished the two outcomes — it had no discriminating power, yet
it was treated as confirmation.

The mitigation was real (a 69% rate reduction, onset delayed from hour ~2 to
hour ~3), which is what made the premature conclusion so plausible: partial
fixes look exactly like complete fixes when measured early.

**The pattern.** Confirming a fix requires a window in which the *unfixed*
system would have visibly failed. Otherwise the result is compatible with both
hypotheses and confirms neither.

**Applicable check.** Before accepting evidence of a fix, state what the broken
system would have shown over the same window. If the answer is "the same
thing", the measurement is uninformative regardless of how clean it looks.

Adopted as a standing rule in Performance 008: *no leak claim from a session
shorter than four hours.*

## Lesson 6 — Do not let a metric answer a question it cannot distinguish

The leak was attributed to wingman on `VmRSS` growth. But RSS counts shared
pages, and this process receives capture buffers from a pipeline the game
feeds — so the same number is consistent with "wingman retains its own
allocations" and "mapped buffers accumulate". The attribution was probably
right, on other evidence, but the headline metric could not settle it.

Fixed by recording the split from `/proc/self/smaps_rollup`: `anon` rising with
`rss` means heap; `rss` rising without `anon` means mapped buffers.

**The pattern.** An aggregate that sums distinct mechanisms cannot attribute
between them, no matter how many samples are taken. More data does not fix an
under-determined metric; a different metric does.

## Lesson 7 — Observability that depends on the subject under investigation

Diagnosing the stuck screen required seeing it. Every route failed: `mss`
`XGetImage` (Wayland), `import`/`xwd` on the XWayland window, and the GNOME
Shell screenshot API (`AccessDenied`). The hotkey needed a real keypress; the
injection library needed root.

**The only working capture path on the host was the portal session owned by the
process being debugged** — which was not being asked to use it. A stale HUD
artefact was briefly and wrongly read as evidence that capture had died; it was
simply a file written only in a state the system was no longer in.

**The pattern.** On a locked-down host, the subject may hold the sole capability
needed to observe it. Build the self-capture *before* it is needed, and know
which artefacts are state-conditional so a stale one is not misread as a signal.

## Lesson 8 — Grep answers questions; ranking finds questions

A memory leak was investigated across several sessions. Frames, threads, file
descriptors and Python object counts were ruled out; a heap-versus-mapped-buffer
metric was designed and added to settle attribution; `MALLOC_ARENA_MAX=2` was
found and cut the rate 69%. All of that was competent, targeted work, and none
of it found the cause.

The cause was one line in every log, printed 1,213 times per session:

```
1213  OCR thread N: initialized EasyOCR reader (CPU)
 1138  GAME_STARTING health probe #N: no digits
  639  Altitude: N | Speed: N | Nose: +Ndeg
```

A health probe was running its OCR on a fresh thread per probe. EasyOCR readers
are thread-local and hold ~300 MB of model weights, so every probe loaded a
model, used it once, and discarded it — roughly 350 GB of allocate/free churn
per session, in exactly the block size that fragments a glibc heap.

It had been present in every substantial log for as long as those logs existed
(933 initialisations in one, 180 in another). Several of those logs had been
read, that same day, by the same investigator, repeatedly.

**Why it survived.** Every query run against those logs was hypothesis-driven —
*show me the climbs*, *show me the dive*, *show me RESOURCE lines*, *show me
time-to-ground*. `grep` returns what you already suspect. Not one query asked
the hypothesis-free question: **what is in this file, by volume?**

It was finally found not while hunting the leak, but while answering an
unrelated request to reduce console noise — a request whose natural first step
is to rank log lines by frequency. That ranking is the cheapest anomaly
detector available, and it had never been run, because tidying output and
diagnosing a leak had not been recognised as the same operation.

**The pattern.** Targeted instrumentation can only confirm or refute the
hypothesis it was built for. A well-aimed metric is evidence *of what you
aimed at*, and its precision is exactly what makes it blind elsewhere. Search
strategy is subject to the same failure as Lesson 3, one level up: there, a
diagnostic was gated on the assumption that failed; here, the entire search was
gated on the investigator's hypotheses.

Noise compounds it. A message appearing 1,213 times in a stream running at 1.3
lines/second reads as normal chatter. The same message with a count beside it is
obviously wrong. Volume does not merely bury signal — it disguises an anomaly as
background.

**Applicable check.** Before forming a hypothesis about a log, rank it:

```
grep "\[INFO\]" run.log | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -20
```

Do this *first*, not as cleanup. Ask of the top entries: should this be here at
all, and should it be here this many times? Anything in the top ten that is not
a deliberate periodic report is worth explaining before anything else is
investigated.

The same move generalises past logs — rank allocations by size, syscalls by
count, queries by frequency. Any place a targeted search is about to be run
against a large body of data, rank it first and let the distribution suggest
the hypothesis, rather than bringing one.

## Meta-lesson — iteration count as a diagnostic signal

The symptom took four rounds to fix: recovery gate → recovery action →
suppression → root cause. The first three were real improvements and none of
them fixed it, because each addressed an amplifier rather than the cause.

Repeatedly finding the previous fix insufficient is evidence of
under-diagnosis, not of progress. Each round's evidence came from a single
session, and treating "the fix did not work" as a prompt to change more code —
rather than to widen the diagnosis — is what produced the sequence.

**Applicable check.** After the second failed fix on one symptom, stop changing
code and re-derive the causal chain from evidence. Ask specifically what the
system was doing *before* the symptom, not only during it.

## Recommendations

1. Adopt Lesson 1 as a review question for any new `if state == X` gate,
   particularly in detectors and diagnostics.
2. Prefer degrading to an explicit unknown over asserting a specific state in
   timeout handlers (Lesson 2).
3. Audit remaining single-actuator assumptions: how many independent code paths
   can issue the same input, and do they coordinate? (Lesson 4)
4. Keep the four-hour rule for leak claims, and generalise it: any "fixed"
   claim should state the window over which the unfixed system would have
   failed (Lesson 5).
5. Treat the ESC-in-lobby question as open. It is the clearest live instance of
   Lesson 4 remaining in the codebase.
6. Rank before grepping, on any new investigation (Lesson 8). Frequency ranking
   costs one command and is the only step here that has ever found something
   nobody was looking for.

## References

- ADR 087 — the blackout chain, root cause, and all four fixes
- ADR 086 — climb exit attitude; d6/d7 are instances of Lesson 5 (a ceiling
  tested against a stale reading reacts a sample too late)
- Performance 008 — the leak, the retraction, the four-hour rule, and the
  per-probe reader reload found by ranking rather than by searching
- Design 007 — the telemetry split; partly justified by Lesson 8, since the
  clue was buried in a console emitting 1.3 lines/second
- ADR 074 / ADR 084 — the gates whose premises failed
