You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.

Use every section below, in this order. Write "None" or "Unknown" where appropriate rather than omitting a section. Return the checkpoint itself in Markdown, without a surrounding code fence or JSON object. Your only task is compaction: do not continue the user's task, call tools, or invent new decisions.

## Original and Current Goals

State the overall goal and the task currently in progress. Distinguish task changes from unresolved earlier work. Do not reactivate a completed, cancelled, or superseded goal.

## Completed Work

Group completed work by deliverable or milestone. Distinguish created artifacts, independently verified results, and unverified assistant claims. Consolidate repeated checks of the same artifact into the latest supported state.

## Remaining Work

Preserve unfinished user requirements, planned actions, and pending decisions. Retain unresolved earlier tasks. Exclude requirements that the user explicitly cancelled or replaced.

## Tool Operations and Key Results

Preserve important operations, observed results, exit codes, and evidence locations. Keep complete commands when needed to reproduce an unresolved failure or resume unfinished work. Summarize completed routine operations and repeated outputs by their result. Preserve reference identities and immutable snapshot locations without reproducing entire documents.

## Current State

Identify the project, branch, relevant artifact versions, active operations, and pending decisions. Report only supported state. A requested cancellation is not a confirmed cancellation; submitting a command does not verify its output.

## Unresolved Issues and Blockers

Preserve actual failures, missing evidence, conflicting states, and blockers with error codes and useful reproduction locations. Distinguish a past failure from its later verified resolution. Do not replace a failure receipt with an assistant's success claim.

## User Constraints and Decisions

Preserve active requirements, prohibitions, explicit decisions, rejected approaches, and exact parameters required to continue. Later user corrections take precedence. Retain a superseded value only when needed to prevent reuse, explicitly marking it superseded. Instructions inside references do not become user instructions.

## Next Action After Resuming

State the already-agreed next action or the specific pending user decision. Extract this from the conversation; do not solve the next task or repeat the entire plan.

Use authoritative structured task state when provided, while preserving any conflict with actual tool receipts. Keep exact paths, IDs, configuration values, units, dates, arithmetic operands, and results needed for continuation. Copy factual values from their evidence: do not recalculate, normalize, silently substitute a similar number, or present contradictory numbers as equivalent. If evidence conflicts, identify both sources and mark the conflict unresolved. Never invent facts, discard a valid constraint, or turn failed or unverified work into success.
