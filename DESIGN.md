# Design note

## Key design choices and why
- **Code enforces safety; the prompt holds judgment.** Anything that could harm a patient if the model got it wrong
  is in code: `patient_id` is never a tool argument (it comes from the session after name + DOB verification),
  3 verification attempts then lockout, the same "not found" error for someone else's appointment as for a missing
  one (no existence leak), invented or taken slots rejected, the 24h cancellation cutoff, atomic reschedule, and a
  bounded tool loop with a fail-safe reply. The prompt handles what code can't: spotting an emergency, declining
  medical advice, resolving "next Friday", confirming before booking.
- **Plain OpenAI function calling, no framework**, so every decision is visible in the tool trace.
- **Frozen clinic clock and seeded data**, so every eval run starts from the same state.
- **Two kinds of grading.** *State checks* read the DB and tool trace (right patient, doctor and date, exactly
  one booking, moved rather than cancelled, the correct escalation category). *Judge checks* are narrow yes/no
  questions over the transcript annotated with tool actions, with evidence written before the verdict. A
  transcript-only "blind" judge is compared with the DB to measure what a chat-only grader misses (fooled in 2/28
  baseline conversations). 13 scenarios, 10 of them hard cases; a simulated patient plays each one.

## How the improvement loop works
Run the suite → collect failure dossiers → an optimizer LLM proposes **at most 3 rule changes** (add, edit or
remove) → re-run the **same scenarios** → gate. The optimizer can only change policy rules, never the tools,
guardrails, scenarios or checks: an agent that edits its own grader learns to pass the grader. Against overfitting:
3 holdout scenarios are never shown to the optimizer, and rules that name a test patient or date are dropped. **Gate:**
every scenario whose pass rate moved is re-run 4 more times on *both* policies; the change must raise the pooled
pass rate with no per-scenario, safety or error regression. Suspected harness bugs are reported to a human rather
than patched into a rule.

## Before and after
| | v0 (no rules) | v1 (accepted) |
|---|---|---|
| Scenario pass rate | 68% | **93%** |
| Safety checks | 85% | 96% |

Emergency 0→79%, no false promises 0→86%, child booking 67→100%. Holdouts also rose (medical advice 33→100%,
late stroke 0→50%), so the rules generalize. **v2 and v3 were rejected**: each fixed a visible failure but
regressed an emergency or holdout case. Full report: `runs/loop_*/REPORT.md`.

## One thing I would change for a real clinic
**Take emergency detection out of the LLM's hands.** The late-stroke holdout is still only 50%, and a prompt
rule can't guarantee a red-flag symptom is never missed. In production I'd run a deterministic, clinician-reviewed
symptom screen on every message before the agent sees it; on a match it stops scheduling and pages a human. That
should be a reviewed code change, not a learned rule. (Next on the list: stronger identity verification than
name + DOB, such as an OTP to the phone on file.)

Other known limits: small n (hence the confirmation re-runs), the judge and agent share a model family, and
rules accumulate and need periodic ablation.

