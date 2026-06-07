<!-- skill:claude_code_deep_think -->

## Using `claude_code_deep_think`

This is a brain-on-demand. Calling it costs real API tokens and is
billed against your daily budget. The local model handles your routine
work; this skill is reserved for situations the local model is not
reliable enough to handle alone.

### When to invoke

- UX critique that needs nuance — "what's wrong with this onboarding
  flow from the perspective of <user>"
- Prioritisation when RICE scores are close or the inputs are uncertain
- Persona synthesis from a batch of new user feedback
- Drafting a hard stakeholder-facing note (founder-facing, board-facing,
  customer-escalation)
- Cross-product strategic reasoning (CPO-level)
- Code-architecture decisions that will be expensive to undo

### When NOT to invoke

- Status updates
- Backlog hygiene (closing stale items, fixing labels)
- Sprint planning mechanics
- Routine triage of well-understood issue types
- Anything with an obvious right answer

### How to invoke

Add a `deep_think` field to your `---REFLECTION---` suffix — the
question or conclusion you want frontier help with, full context
inline. The runtime subshells to the stronger model and folds the
answer back into your memory, where it surfaces next cycle.

```
---REFLECTION---
{
  "outcome": "...",
  "deep_think": "I've concluded we should retire the Navida line.
    Before I file that: argue the strongest case AGAINST retiring it,
    given <inline context>. What am I missing?"
}
```

Use it both ways: for reasoning the local model can't finish, and — as
above — for a **second opinion that argues against your own
conclusion** before you commit. The answer returns as a high-importance
memory tagged `deep_think`; read it next cycle and decide. It is not an
oracle — you remain the decision-maker.

### Budget discipline

Watch your daily budget. Roughly:

- A short critique runs £0.10–£0.30.
- A long synthesis runs £0.30–£1.20.
- A worst-case multi-pass invocation can hit £5.

If your daily budget is more than 75% spent before 17:00 local time,
stop using this for routine work. Reserve remaining budget for hard
stakeholder-facing notes.

### Failure handling

The skill can fail for three reasons:

1. The `claude` CLI is not installed on the node. The runtime will
   report this clearly. There is nothing you can do as an agent — the
   operator needs to install it.
2. The `ANTHROPIC_API_KEY` is not configured. Same — operator action.
3. The call times out (default 180s). If a single call times out,
   simplify the prompt and retry once. If two consecutive calls time
   out, fall back to your local reasoning and note the limitation in
   your decisions log.

<!-- /skill:claude_code_deep_think -->
