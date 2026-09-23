# Contract Prompt Templates

Use these prompts to start high quality agentic coding cycles.

## Feature Contract Prompt

```text
You are implementing one scoped feature.
Objective: [one sentence]
Acceptance checks:
1) [new behavior test]
2) [baseline must stay green]
Non-goals: [what must not change]
Constraints: [stack, style, performance, deadline]
Output:
- Plan in 3-6 bullets
- Files to modify
- Minimal diff strategy
- Validation commands
Wait for approval before coding.
```

## Bug Fix Contract Prompt

```text
You are fixing one reproducible bug.
Bug signal: [error/log/behavior]
Reproduction steps: [ordered steps]
Acceptance checks:
1) Reproduction fails before fix
2) Reproduction passes after fix
3) Related area remains stable
Non-goals: [explicit exclusions]
Output:
- Suspected root cause
- Minimal fix path
- Verification plan
Do not implement until the plan is approved.
```

## Refactor Contract Prompt

```text
You are refactoring without changing behavior.
Objective: [maintain behavior, improve structure in specific area]
Behavior lock: [tests or traces proving parity]
Constraints:
- No API signature changes
- No schema changes
- No new dependencies
Output:
- Refactor boundaries
- Risk points
- Rollback strategy
- Validation plan
Proceed only within listed boundaries.
```

## Final Handoff Prompt

```text
Prepare a reviewer handoff with:
1) What changed and why
2) Files touched
3) Validation evidence (before and after)
4) Residual risk
5) Rollback instructions
Keep it concise and auditable.
```
