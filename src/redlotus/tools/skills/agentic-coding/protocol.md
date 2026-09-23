# PACT Loop - Agentic Coding Protocol

PACT keeps AI coding predictable under time pressure.

## P: Problem Framing

Write a compact brief before editing:
- Target behavior
- Current gap
- Scope boundary
- Definition of done

Output format:

```text
Objective:
Gap:
Scope:
Done when:
```

## A: Acceptance Design

Define checks before touching code.

Minimum acceptance set:
- One check that fails before changes
- One check that must pass after changes
- One guardrail that must remain unaffected

Examples:
- Bug fix: failing reproduction, then passing reproduction
- Feature: new test plus unchanged baseline tests
- Refactor: behavior parity checks plus static quality gate

## C: Change Set

Implement only what the contract requires.

Rules:
- Keep file count as low as possible
- Avoid broad renames during behavioral changes
- Split optional cleanup into follow-up contracts

## T: Trace and Test

Produce concrete evidence and decision support.

Report:
- Commands executed
- Pass or fail outcomes
- Residual risk with explicit rationale
- Recommended next action

## Stop Conditions

Stop and escalate when:
- Two implementation attempts fail
- Acceptance cannot be measured with available tooling
- The requested scope conflicts with project constraints

Escalation packet:
- What was attempted
- What blocked progress
- Two alternative paths with tradeoffs
