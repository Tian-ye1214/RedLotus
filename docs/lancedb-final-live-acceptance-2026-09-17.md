# LanceDB final live acceptance — 2026-09-17

## Scope

The frozen worktree source and `src/redlotus/config.json` were used for three
real acceptance batches. `E:\代码\Agent\.env` was supplied by path only; its
contents were not read or recorded. The configured model and RAG services were
called only with generated acceptance fixtures.

Configuration copies, core-memory state, project fixtures, sessions, logs,
references, and runtime data were isolated under:

`E:\代码\Agent\WorkDatabase\evaluation\lance-final-20260917`

Native LanceDB tables were isolated under the authorized per-root directories:

`C:\Users\Administrator\.redlotus\evaluation\storage-final-live\<root-hash>`

No user session or existing personal-memory content was read, removed, or
included in this report.

## Results

| Batch | E: root | Result | Duration | Requests | RAG calls | Tool commands | Evidence |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| short | `lance-final-20260917` | Passed | 133.375 s | 16 | 3 | 3 | 0 failures, 0 warnings; all compact checks true |
| lifecycle | `lance-final-20260917\lifecycle` | Passed | 99.766 s | 13 | 5 | 0 | 0 failures, 0 warnings; all compact checks true |
| long | `lance-final-20260917\long` | Timed out | 600 s cap | 76 | 9 | 8 | Exit 124 after 38 of 60 turns; no recorded warning, exception, or assertion failure |

The short batch confirmed a real worker command receipt, original-image
recognition, project-memory save and recall through RAG, a project-local session,
usage persistence, and a project-local task log.

The lifecycle batch confirmed `/clear`, cancelled `/load` without a model
request, restore of the selected session ID and turn count, project switching,
cross-project global-memory recall, forced compression, project-local logs, and
usage persistence.

The long batch did not satisfy the required 60-turn, three-automatic-window
acceptance within the fixed 600-second deadline. It reached 38 turns. All 76
observed model HTTP responses were 200; its compact evidence recorded no warning
or exception. It passed the turn-19 recovery boundary before continuing past
turn 20. Read-only persisted-session metadata confirms the first automatic window
(`0–20`, 20 new turns, 0 overlap) completed and indexed; the run did not reach
the required second and third windows.

## LanceDB residue retained for diagnosis

| Batch | LanceDB evaluation directory | Tables | Files / bytes | E: retained session JSONs / logs |
| --- | --- | --- | ---: | ---: |
| short | `...\4d870782e90522d5` | `conversation_turns`, `memory_records` | 9 / 16,871 | 1 / 1 |
| lifecycle | `...\ad38588d182ab1f1` | `memory_records`, `semantic_memories` | 9 / 17,831 | 4 / 4 |
| long | `...\cddcf8b668bb274a` | `conversation_turns`, `memory_records` | 18 / 42,307 | 2 / 2 |

The retained session JSON byte totals are 55,227 for short, 89,918 for lifecycle,
and 581,014 for long. The two long-session JSON files are one session index
manifest and one actual 38-turn transcript, not two initialized sessions. No
session or log was manually deleted.

The long wrapper deliberately replaced its verbose child evidence with its compact
`acceptance-evidence.json`; it removed `live-evidence.json` and `provenance.json`
as part of that existing sanitization behavior. The retained compact report is at:

`E:\代码\Agent\WorkDatabase\evaluation\lance-final-20260917\long\acceptance-evidence.json`

The separate read-only persisted-session metadata snapshot is:

`docs\lancedb-final-live-session-metadata-2026-09-17.json`

## Long-batch timing evidence

One inspected slow turn took 73.516 seconds. Its trace had one coordinator
invocation and three successful tools: `write_file`, `run_command`, and
`edit_file`. Only the `run_command` tool produced a subprocess receipt, which
returned zero inside the project workdir. The trace records tool completion times
but not individual tool start times. Request evidence has no turn ID, so it does
not support attributing an exact model-call count or full model-stream duration to
that turn.

This is evidence of a deadline/per-turn-latency failure, not evidence of an HTTP
service failure: every observed model response was successful. It does not by
itself distinguish model generation time from application orchestration. No
parameter, assertion, source file, or 60-turn requirement was relaxed, and the
long batch was not automatically retried.
