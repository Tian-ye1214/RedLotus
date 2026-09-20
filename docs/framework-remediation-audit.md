# Framework remediation audit — 2026-09-20

## Scope and current result

Behavior baseline: remote `master@86553b6d`. The work started at `develop@1df51729`, with existing user edits and unfinished changes from earlier work. This report separates this audit's changes from those inherited changes. The latest user instruction is to review and upload the current work to `develop`; it does not authorize treating the release gates as passed. The approved keyboard/completion follow-up below has installed the candidate wheel in the existing daily Miniconda environment and run an actual onedir executable.

**The application is not ready for full acceptance.** The earlier audit's unresolved command and loading issues have received the focused repairs documented below. Focused real installed-package checks do not satisfy the original three-environment 360-turn, automatic-compression and per-environment cache gates. Historical results and their failures remain separate from current verification.

The restored Markdown prompts were not edited except for the subsequently approved Worker structured-output section. Configuration was not copied, backed up, distributed, or supplied to a model. No deleted test source was restored and no new test source file was created.

## Changes made in this audit

| Change | Function and evidence |
| --- | --- |
| Replace `tools/toolkit.py` with identity modules | `base_tools.py` implements common tools; `manager_tools.py` owns planning and its callable list; `worker_tools.py` owns execution groups and factory delegation. Existing implementations were moved, not replaced with stubs. |
| Explicit registration at Agent creation | Coordinator combines execution and delegation lists; Manager receives its planning list; Worker receives native resident/deferred SDK toolsets; perception lists its three tools directly. |
| Function docstrings define tools | Removed Markdown description overrides and runtime `__doc__` mutation. Native Pydantic AI checks confirm names, signatures, descriptions, and descriptions of every parameter. |
| Preserve queued child ownership | Capture the session, save target, toolkit and memory service before a factory task waits for capacity. A later project switch cannot change those captured references. |
| Restore usable resource loading | Skills readers return their original bodies; references carry identity and original text or binary content. No replacement `runtime.md` or `tool_instructions.md` was created. |
| Render the user's existing prompt slots | Populate existing Skills, system information and memory placeholders. Preserve original template text and restored-session instructions. |
| Keep five result categories | One shared business type includes `success`, `failed`, `cancelled`, `needs_input`, `unverified`; it no longer attempts to load a missing Markdown schema. |
| Remove configuration copying | Packaging excludes `config.json` and `.env`; startup no longer writes a bundled/generated configuration template. Existing source selection and explicit configuration editing remain. |
| Authorized configuration deletion | Following the latest user answer, removed only `conversation_log.save_model_thinking_chain` and its empty parent group. All other bytes and parsed values were verified unchanged. The switch had no reader; this does not implement a new thinking-storage policy. |
| Authorized SDK model identifiers | Changed only the five role names from `deepseek-v4-flash` to `deepseek:deepseek-v4-flash`. No `provider` field was added. Pydantic AI chooses the provider and native model class; the actual API model remains `deepseek-v4-flash`. |
| Explicitly approved parallel calls | Set `parallel_tool_calls=True` in model settings as requested. Actual tool-bearing requests carry `true`; the SDK correctly omits the field when no tools are registered. |
| Runtime failure receipts | Removed the runner's dependency on absent Markdown fragments for status and interrupted-call receipts. Receipts contain factual status data, preserve completed tool returns and mark unresolved outcomes unknown. A recording failure no longer replaces the original request error. |

## Actual role tools

These lists describe the current code, including inherited capabilities. They are not a claim that every capability has passed a live model test.

| Role | Explicit registration |
| --- | --- |
| Coordinator, owner channel | The 31 Worker execution functions below plus `execute_task_with_manager` and `execute_task_with_worker`. |
| Manager, planning | `create_todo_list`, `get_todo_list`, `ask_user`, and owner-authorized `search_memory`. The final-report phase has no planning tools. |
| Worker | Six resident functions; the remaining functions are in six progressively loaded native SDK capability groups. |
| Perception | `search_memory`, `read_evidence`, `read_reference`. |
| Compressor and title | No business tools. |

Worker functions, listed in `worker_tool_groups`:

- Core: `list_files`, `read_file`, `search_in_files`, `search_web`, `ask_user`, `read_reference`.
- File changes: `write_file`, `edit_file`.
- Execution: `run_command`, `execution_environment`.
- Media: `generate_image`, `extract_text`. Local image references are native SDK attachments, not a reading tool.
- Owner-authorized memory: `search_memory`, `remember`, `update_memory`, `delete_memory`.
- Skills: `list_available_skills`, `get_skill_instructions`, `load_skill_resource`, `refresh_skills`, `execute_skill_script`.
- Browser: `browser_navigate`, `browser_get_content`, `browser_screenshot`, `browser_click`, `browser_fill`, `browser_press_key`, `browser_wait_for_selector`, `browser_evaluate`, `browser_close`.

`remember` is the production entry; the three memory-consumption tools remain search, update and delete. Non-owner memory exclusion passed a local registration check. The inherited Coordinator currently excludes all tools for non-owner channels; whether that broader boundary matches intended chatbot behavior remains a review item, not a new permission change.

## Configuration field-to-function review

This table contains field names and responsibilities, not configuration contents. Dynamic dictionary unpacking was checked as well as literal readers: a missing textual key match alone does not prove that a field is unused.

| Fields | Reader and function | Decision |
| --- | --- | --- |
| `BASE_URL`, `API_KEY` | `get_env`, `ModelTarget.from_values`: selected model connection | Preserve values and source priority. |
| `SILICONFLOW_BASE`, `SILICONFLOW_KEY`, `RAG_models.embedding`, `RAG_models.reranker` | RAG client, embedding and rerank requests | Preserve. |
| `models.<role>.name`, `temperature`, `max_tokens`, `top_p`, `reasoning_effort`, `thinking` | `get_model_and_params`, thinking translation, SDK factory | Preserve. Full request equivalence is blocked by gateway construction. |
| `models.<role>.auto_compress_ratio`, `compress_head_turns`, `compress_tail_turns` | Context policy and `compact_request_messages` | Preserve the user's policy; do not pass internal context fields to the model API. |
| `lifecycle.invocation_history_per_session`, `shutdown_grace_seconds` | Registry retention and shutdown | Preserve. |
| `agent_run_policy.max_concurrent_threads_per_session`, `max_command_timeout_seconds` | `AgentRunPolicy.from_config`, factory capacity and command timeout | Both are consumed. The timeout field is read through dataclass construction, not a literal lookup. |
| `request_limit`, `MODEL_HTTP_TIMEOUT` | SDK usage limits and model HTTP client | Preserve. Reusing the latter for connection timeout is a proposal, not an implemented change. |
| `memory_perception.window_turns`, `overlap_turns`, `model_role` | Current-session window selection and perception target | Preserve the current-session 20-new-turn plus 3-overlap rule. |
| `short_term_memory.db_path`, `table_name`; `long_term_memory.table_name` | Memory store and LanceDB namespace | Preserve project isolation and global L2 access rules. |
| `short_term_memory.vector_search_limit`, `final_top_k`, `use_rerank`, `min_similarity` | Candidate retrieval, rerank and result filtering | Preserve real vector retrieval; do not replace it with text-only search. |
| `short_term_memory.turn_token_limit`, `turn_chunk_overlap_tokens` | Embedding chunks of complete produced memories | Preserve; these are embedding budgets, not permission to truncate a query or perception evidence. |
| `short_term_memory.index.*` | LanceDB vector-index lifecycle and metric | Preserve until a separately approved backend/performance change. |
| `input_limits.defaults.max_files`, `max_file_bytes`, `max_request_bytes` | Reference admission and encoded-request budget | Preserve. |
| `model_metadata.url`, `timeout`, `supported_thinking_efforts` | Context/capability lookup | Preserve; no model call was made to validate this path in this audit. |
| `rag_service.timeout`, `http2`, `embedding_batch_size`, `index_batch_size` | RAG networking and batch indexing | Preserve. |
| `storage.state_dir`, `project_dir`, `sessions_dir`, `project_logs_dir`, `references_dir`, `runtime_dir` | Global/project/session/reference paths | Preserve. |
| `storage.cleanup.enabled`, `session_retention_days`, `execution_cache` | Low-space cleanup and active-session protection | Preserve; execution-cache resolution still depends on the removed execution group. |
| `conversation_log.save_model_thinking_chain` | No implemented reader | Temporarily removed by explicit user instruction, with its empty group. |

No additional field deletion is approved or performed. Removed `model_gateway` and `execution` must not be restored from an old global configuration or replaced by hidden values.

The earlier proposal for top-level `provider` and `parallel_tool_calls` fields was rejected and was never written. The approved replacement uses SDK-prefixed model identifiers and sets parallel calls directly in code. Connection and request timeouts reuse existing `MODEL_HTTP_TIMEOUT`; no new timeout field was added. Configured credentials and service addresses are injected into the SDK-selected provider, without model-name heuristics or environment-credential fallback.

The final source configuration SHA-256 after the two separately authorized edits is `9e7ffde0c7ee520c1d2fbbf5faf04b422694d2d552de128a680bbdc1c56570a6`. No global JSON or `.env` was edited. The existing execution-policy dependency remains separate.

## Blocking findings and proposed next changes

| ID | Evidence and impact | Proposed action; approval status |
| --- | --- | --- |
| F01 | The initial failing check reproduced `KeyError: model_gateway` in `from_values`. That dependency is removed. The approved identifiers now use SDK routing, with configured credentials, addresses and timeouts. | Focused fix verified by eight real model requests across the project virtual environment and daily Miniconda. Five role constructors and OpenAI Chat, OpenAI Responses, Anthropic, Google and DeepSeek SDK routing pass auxiliary checks. Non-DeepSeek services are not live-accepted. |
| F02 | Actual `run_command` cannot execute even a harmless echo because environment creation and permission checks index missing `execution`. Skills use the same execution path. | Separate interpreter/environment requirements from ownership rules, then agree how required policy is represented. Restoring the rejected group, moving its lists into code, or silently allowing commands are not authorized fixes. |
| F03 | The user's removal of `prompt_fragment` made imports fail; its earlier implementation also depended on the deleted `runtime.md`. | Removed the dead imports and calls. Compression payloads and validation/control receipts now carry structured data; RAG receives the original query. Existing validation conditions remain. Startup, recovery and stream checks pass; no Markdown prompt or missing resource was recreated. Full compression and memory acceptance remain separate. |
| F04 | The restored Worker prompt requires `SUCCESS:`, `CONFIRM:` or `FAILED:` text; the current SDK output contract is `SubagentResult`. The restored Manager prompt says its Workers lack browser tools, while current Worker registration includes browser tools. | Reconcile the output adapter and role boundary with the frozen prompts and approved capabilities. No prompt rewrite or capability removal performed. |
| F05 | Local failure injection reproduces destructive load failure: `AgentCliController.reset_session` clears the original session before calling a failing restore callback, then says the original session was preserved. Actual original session ID becomes empty. | Prepare and validate the new state before replacing the old session. Preserve the original state on failure. This finding is not yet fixed; it belongs to the later session stage. |
| F06 | `scripts/pack.ps1` still provisions a separate build environment. QQ initialization has import-time configuration effects; channel queue configuration is absent from the current source file. | Review packaging against the required existing environments. Keep optional-channel missing configuration explicit; do not add defaults or test against real chat recipients. |
| F07 | Channel response retries may duplicate a reply if delivery succeeded but acknowledgement timed out. QQ media checks size after reading the body. These are code-path risks, not live reproductions. | Propose channel-specific acknowledgement/streaming changes before implementation; no claim of a verified exploit or system-level isolation. |
| F08 | Pending perception jobs saved under the old `openai-chat` routing identity may fail the strict snapshot comparison against the new SDK `deepseek` identity even when the configured address is unchanged. | Compatibility handling remains unimplemented. No saved job, historical snapshot or personal memory was changed. Do not describe pending-job recovery as accepted. |

OpenRouter comparison: its service exposes a normalized client API and handles upstream-provider differences server-side. Its model catalog publishes context and supported-parameter metadata. This can inform a small local SDK adapter; it does not make an arbitrary direct provider endpoint self-describing, or authorize switching the user's service. Sources: [API](https://openrouter.ai/docs/quickstart), [tools](https://openrouter.ai/docs/guides/features/tool-calling), [models](https://openrouter.ai/docs/api/api-reference/models/get-models).

## Source review coverage

| Area and files | Reviewed responsibility / remaining acceptance |
| --- | --- |
| `core/agents.py`, `gateway.py`, `system.py` | Factory/registry ownership, role construction and dispatch. Focused gateway requests pass; remaining execution and prompt dependencies block full end-to-end acceptance. |
| `core/config.py` | Source resolution, explicit writes, paths, cleanup, clients and startup. Removed configuration copying; obsolete-group dependencies remain visible. |
| `core/session.py`, `history.py` | Single-file transactions, turn counts, tool pairing and actual-usage compression. No full long-session or crash matrix rerun. |
| `core/console.py`, `cli_commands.py`, `tui.py`, `presentation.py` | Input/selector lifecycle, commands, review/goal routes and accounting. Load failure reproduced; complete TUI behavior not reaccepted. |
| `tools/base_tools.py`, `manager_tools.py`, `worker_tools.py` | Implementation, visible role lists and factory delegation; SDK schema and direct-tool checks below. |
| `tools/execution.py`, `registry.py`, `interaction.py`, `references.py` | Shared command policy, progressive Skills, input/review/ref lifecycle, browser/doc tools. Execution-policy reconstruction remains pending. |
| `memory/perception.py`, `records.py`, `service.py`, `store.py`, `retrieval.py` | L1/L2, evidence IDs, search-before-insert, window reservation, publication and index retry. Missing prompt dependencies block real memory acceptance. |
| `prompts/prompt.py`, `message_text.py`, existing Markdown | Resource loading, source identity and snapshots. Markdown hashes preserved; unresolved authored-instruction references recorded above. |
| `api/base.py`, `QQ.py`, `WeChat.py`, `qq_media_helpers.py`, `__init__.py` | Channel identity, replies and attachments. No external messages sent; no live channel acceptance. |
| `main.py`, `pyproject.toml`, `MANIFEST.in`, `build.spec`, `scripts/pack.ps1` | Launch/config source and package contents. No build, install or release performed in this audit. |

## Verification and limits

- Project `.venv` and daily Miniconda: native Pydantic AI tool names, signatures, description preservation, parameter schemas, no duplicate tools, and one resident/six deferred Worker groups passed. These checks imported current source; they are not pip-entry or PyInstaller acceptance.
- Local task graph: dependency ordering, preserved completed results and rejected cyclic changes passed.
- Direct application tools: real file write/read/edit, diff record and reject restoring the file, project traversal refusal, original Skills/resource loading, immutable text/image references, and 20/21-file count boundary passed.
- Real Playwright browser: page navigation, form input, click and content reading passed without model dispatch.
- Five outcome schemas and perception tool parameter schemas passed. Independent configuration-return copies passed.
- Final local checks: all 29 application Python files plus `main.py` parsed; native SDK role checks passed again in project `.venv` and Miniconda; all 12 restored Markdown prompt hashes match the start of this audit. Packaging exclusion declarations and the exact authorized configuration deletion passed. The first packaging-check expression used the wildcard key instead of the existing `redlotus` package key; correcting that check required no application change.
- Failure evidence: schema descriptions initially missed a blank line before `Args`; fixed and rechecked. A reference assertion initially assumed LF instead of the file's CRLF; changed only the check to compare original bytes. The first gateway smoke attempt incorrectly asserted a parallel-tool field in requests with no tools; its HTTP hook rejected the request before transmission. This revealed the separate runner receipt defect. The check now distinguishes tool-bearing requests, and the defect was fixed without altering Markdown prompts. Load failure and command execution remain unresolved as described above.
- **Real successful model requests in this audit: 8.** Each Python environment ran two connected chat turns (two requests), followed by one Worker file-reading task (two requests). In each task, the model called the actual `read_file` tool twice in one batch, read random verification codes from two temporary files, and returned both correctly; call/return IDs matched. The eight wire requests retained `deepseek-v4-flash` and the configured `max_tokens=393216`, without the incorrect `max_completion_tokens` field. Tool-bearing requests sent `parallel_tool_calls=true`.
- Project environment chat durations: 1.656 s and 1.203 s; two-tool task: 2.250 s. Daily Miniconda chat: 1.766 s and 0.984 s; two-tool task: 2.906 s. Reported total input: 2,018 tokens; total output: 515; cached input: 891. This tiny gateway-only sample does not establish the greater-than-90% long-session cache target. Successful paths produced no WARNING/ERROR. Two temporary file projects were removed immediately after the checks.
- Runner auxiliary fault checks passed: preserve completed tool returns, close failed/cancelled pending IDs with unknown execution outcomes, round-trip through SDK message serialization, and propagate the original request error even if recording fails.
- Live full-role delegation, embedding/rerank, memory production, 60-turn counting, automatic compression, full TUI, installed pip entry and PyInstaller remain unaccepted. Both Python checks imported current source using the user's existing interpreters; neither is described as a pip installation test.
- The one direct-tool temporary directory occupied 2,074 bytes and was removed. No configuration copy, additional environment, stored full request or test source was created. Existing `WorkDatabase` content was not removed as part of these checks.
- Structure is still noncompliant: five boards remain, but `core` has 10 Python files and `tools` has 7; several core files exceed 500 effective lines. Identity extraction is not treated as a waiver of that gate.

## First-input silence: real TUI reproduction and repair

The user's `hi` screenshot exposed a path omitted from the earlier gateway-only checks. Before the first foreground turn, the CLI awaits `generate_task_title`. Its `next(iterator, default)` evaluated the default eagerly, calling the removed `runtime.md` even for nonempty input. This failed before any title API request. The FIFO queue stored the exception in its result future, but interactive `process_line(..., wait_for_turn=False)` did not report that future's failure. The screen therefore stopped after `input admitted`, without a session or reply.

- Before-fix evidence: the actual project `.venv` running `main.py` in a terminal reproduced the screenshot after `hi`. A direct CLI-controller check also failed with `session_created=False`, `input_still_first=True`, and `error_visible_to_system=False`.
- Repair: derive fallback titles from the existing user input; do not load or recreate the deleted fragment. Report asynchronous preparation failures through the existing turn-error handler, with their original traceback. Awaiting callers still receive their own exception once; cancellation does not create a spurious error. No scheduler, timeout, configuration or Markdown-prompt change was added.
- Real terminal verification: restarted `main.py` with the same project `.venv`, entered `hi`, received the normal greeting, then entered `37乘以24是多少？只回复结果。` and received `888`. First admission was 13:58:39, reply completion 13:58:42 (main Agent 0.84 s); second admission 13:59:53, completion 13:59:55 (main Agent 2.24 s). The session saved exactly two completed turns, in one 24,845-byte `model_messages.json`. Normal-path logs contained no WARNING/ERROR. The test process exited normally.
- Auxiliary fault injection in daily Miniconda confirmed one visible error with the actual exception traceback, no double reporting to awaiting callers, subsequent FIFO inputs, and cancellation without an error. This is separate from the real model result above.
- This TUI run used source config SHA-256 `86f2043d93ab5c2601e5529632b18cbc74626c8cc58fddf01ad1e54c90a58c4f`, last modified at 13:52:08, before this bug investigation and terminal launch. An initial final-check assertion incorrectly reused the previous turn's configuration hash and failed; the pre-existing edit was preserved. No configuration or prompt file was written in this fix.
- The installed daily package resolves to Miniconda `site-packages`; this source fix has not been installed into pip or rebuilt into EXE. The two real TUI turns supplement the eight earlier gateway requests; they do not establish full application or installed-package acceptance.
- Test-session identity: `1273b3a161df41648ed3fca01344375a`. Only that confirmed test session and its `打招呼_20260920_135841.log` are eligible for cleanup after checking ownership and file state; unrelated sessions, memory, configuration and project artifacts are excluded.
- Cleanup was deliberately stopped: the session acquired another process's use marker (PID 43144, 14:00:21) and grew to 63,030 bytes with new writes at 14:06:16. It is no longer an untouched test fixture. Both its session file and original log were preserved; the cleanup guard deleted nothing. Our own terminal process (PID 13040) shut down at 14:01:37 and exited successfully.

Next decisions concern F02 execution-policy representation and F03/F04 instruction/contract conflicts; F01's configuration decision is now implemented and live-tested. Resume the same failing paths after those targeted changes, then the original three-environment acceptance. No master merge or release is justified by the present evidence.

## Local images, role journals and input follow-up — 2026-09-20

- Removed the remaining startup dependency on `prompt_fragment` without restoring `runtime.md` or editing authored Markdown. A diagnostic mistakenly passed `file_only` to `logger.debug`; the user's Enter traceback reproduced that exact regression. Replaced it with the existing `info_file_only` API and retested both actual Python environments. No logger API or configuration group was added.
- Local references reject explicit URL references and leave ordinary URLs as text. Local image MIME comes from the original bytes, including PNG files named `.jpg`; SDK requests contained the actual PNG base64 URI. Explicit channel image URLs retain the SDK URL form. No `read_image` tool or Ctrl+J submission alias is registered.
- Incremental batches use indented `json.dump` output. Non-main roles use lazy sibling journals with Agent/invocation identities. Worker writes share the durable-save pause/retry path; completed Worker bodies can be discarded after the main return is durable, while active sibling contexts and cumulative usage remain. Old mixed journals split only after durable role writes succeed.
- Main thinking and answer deltas are distinct; only returned text is displayed. Thinking expands while received, folds at completion/stop, and remains manually inspectable. Stale session callbacks are ignored. Supplemental messages use the ordinary user presentation; queued normal tasks remain dimmed.

| Final focused check | Actual result and limit |
| --- | --- |
| Project `.venv`, real service through actual TUI components | Eight real requests covered greeting, Worker file reading, random image recognition, native PNG payload and thinking display. Passed; this is not a physical-key test. |
| Installed daily Miniconda package | Import resolved to `D:\Software\miniconda\Lib\site-packages`. Eleven real requests covered the same scenarios plus the real `/load` selector and subsequent image recall. Session ID, file and turn count survived loading. All eight focused assertions passed. |
| Daily CMD `redlotus` entry | Actual installed launcher started outside the repository root and answered the requested greeting; completed in 2.35 s and exited normally. |
| Actual PyInstaller onedir | Built with the existing project `.venv`, exit code 0, about 163 s. EXE started in a temporary E-drive project, answered a greeting and recognized a random image. After exit/restart and selecting the original session, it returned the same image code with the same session ID. |
| EXE inner-input boundaries | Six independently submitted CSI-u Ctrl+Enter sequences were recorded in order, all belonging to the same active turn; ordinary Enter remained a separate queued input. These are protocol-input checks, not physical keyboard simulation. |
| Known failure retained | The EXE Worker chose command execution for a calculation and encountered the existing missing `execution` policy (F02). It tried alternative tools; the task was cancelled. The next queued prompt spent a long time in actual model thinking and was also stopped. Cancellation was recorded and the EXE exited normally. This business scenario is failed; it is not replaced by the simpler successful file-reading case. |
| Physical user keys | User confirmed Windows Terminal works after the explicitly approved Ctrl+Enter mapping. PyCharm 2026.1.2 Classic still queues the input; the user declined adding an IDE plugin, so this remains an explicit limitation. See `keyboard-input.md`; no PyCharm extension has been installed. |
| Auxiliary fault checks | 24 partial-write positions, middle-corruption byte preservation, 12 concurrent role contexts, legacy split, Worker disk pause/retry without duplicate execution, actual image MIME, thinking/signature separation, and synthetic TUI key/stream checks passed. These are separate from real API results. |

The source config fingerprint remained `86f2043d93ab5c2601e5529632b18cbc74626c8cc58fddf01ad1e54c90a58c4f`. No RedLotus config or `.env` was copied or modified, and no authored Markdown prompt was changed in this follow-up. Tests used the existing interpreters; no replacement virtual environment or test source file was created. Small live projects were placed on E. Build/temporary directories were absent on the final filesystem check; existing user sessions were outside the cleanup scope, and the original `打招呼_20260920_135841.log` remained present. No merge, push or release was performed. PyCharm input, F02 and the remaining full-application gates prevent an all-passed claim.

## Approved keyboard/completion/simplification implementation — 2026-09-20

This section supersedes the earlier proposal status for F02/F04–F08. Work is in progress; these local checks do not satisfy the three-environment release gate.

| Item | Implemented change | Evidence / remaining work |
| --- | --- | --- |
| Keyboard detection | Optional startup detector and input-row retest; compare actual Enter and Ctrl+Enter events, preserve draft, support cancel and manual Windows Terminal settings preview. Removed continuous key logging. | Textual component checks distinguish separate events, identical events and cancellation. Synthetic events are not physical-key evidence. PyCharm Classic cannot recover modifiers its terminal does not transmit. |
| F02 execution | Removed rejected `execution` readers, automatic venv provisioning and its state files. Use the launch interpreter; frozen apps resolve external Python. Project cache/TEMP remain configured runtime paths. Commands and Skills share fixed approved process checks. | Harmless command formerly failed with missing `execution`; now executes. Existing Miniconda command/Python/pip, UTF-16 PowerShell, environment and 4 allowed / 9 rejected command cases pass. No environment created. |
| Dependency location | Pin pip/uv's target to the selected existing Python through their documented environment options. | Project `.venv` contains no pip; the first check found bare pip using Miniconda. After correction, actual `sys.executable` is the project interpreter and uv reports the project environment. A test initially confused pip's own code location with its target interpreter; package-location checks replace that assertion. No package installed by this check. |
| F04 Worker contract | Only the conflicting Worker output-format section/examples now describe structured five-outcome results. Explicit tool groups distinguish Coordinator's Worker (browser) from Manager's Worker (no browser). | Prompt/schema check and actual source, installed CMD and EXE delegation passed. Browser availability is also checked by inspecting the actual two callable sets; this does not prove arbitrary browser tasks. |
| F05 load | Prepare target messages, repaired tool pairs, tasks and logging before committing; preserve rejected input on preparation failure. Remove duplicate load/read wrapper. Release old resources only after the switch commits. | Failure injection preserves original session ID/history/draft/memory and does not close the original toolkit or cancel its factory. Interrupted tool calls get paired unknown-result receipts, without replay or duplicate turn counting. Actual source, installed CMD and EXE restart tests restored the same ID and image association. |
| F06 packaging / QQ init | Packaging uses an existing interpreter; QQ SDK import deferred until explicit initialization after checking the user-owned channel config. No configuration seed/copy. | Candidate wheel and onedir built successfully with existing interpreters. Following the user's explicit approval, QQ/WeChat now construct the existing unlimited FIFO without reading `bot.session_queue_maxsize`. Thirty queued inputs remained ordered; the configured Agent thread limit remains 16. |
| F07 reply / attachment | Do not resend an ambiguously acknowledged reply. Stream QQ attachments and apply existing gateway byte limits before retaining the full body. | Lost-ack fixture reproduced two deliveries before, one after. Declared oversize fixture read all 100 chunks before, none after. No external chat recipients contacted; live QQ/WeChat unaccepted. |
| F08 perception identity | Accept an old protocol label only with identical service/model/options/timeout and the same SDK model class. Original snapshots and completed-job state remain unchanged. | Corrected async alias check passes. Earlier diagnostic drivers failed for missing fixture events and missing running loop; those are not product red/green evidence. |

Removed-call-site review: replaced synchronous compression and request-preparation wrappers, unused context estimator, old config setter/path helper, unused status wrapper, unused repository alias, automatic-environment helpers and duplicate saved-session reader. SDK callbacks, dynamic tools, public memory contracts and the new-input text estimator remain. No deleted test sources were restored and no new test source file was written.

Same-algorithm count after the FIFO and command-description changes: 10,400 → 10,373 effective application lines (net −27; tokenized logical statements, excluding blank/comment/docstring-only lines; multiline signatures count once). All 29 application modules parse. Existing structure gates remain unmet: core has 10 files and tools has 7; config/console/session/system/tui/references exceed 500 effective lines. No formatting compression or removed behavior assertion was used to obtain this count.

Protected-file hash check passes: config and `.env` unchanged; only the explicitly approved Worker Markdown section changed. No credentials printed, copied or sent as model material. Small fixtures were created under E and removed through their scoped temporary-directory owners. Source code remains uncommitted on the user's existing worktree; no master merge or publication.

### Follow-up verification and limits

- Physical keyboard evidence comes from the user: the startup detector passes in Windows Terminal and fails in PyCharm Classic. New machines remain unverified until actual detection. No plugin, Hook, replacement shortcut or terminal setting was added by this follow-up.
- A startup storage error previously left the composer disabled; the error/cleanup path now unlocks it, and cancelling the manual detector restores focus. Component checks also cover exiting with the detector open. These checks use Textual events, not invented physical-key evidence.
- Twenty-four factory jobs in one session allocate exactly 16 Agent threads and leave 8 unstarted. Sixteen owner-loop memory callbacks complete while the slots are occupied; cancellation frees both admitted and queued work. This is a local concurrency check, not 24 real model calls.
- Live source component flow: 4 user turns, 12 model requests; file/Python Worker task, real image, thinking display and restore. Live installed component flow: 4 turns, 13 requests. Source and installed component main/ordinary-Worker cache ratios were 76.72% and 79.68%; these small cold samples do not pass the >90% target.
- Before the final FIFO-only package refresh, actual daily CMD `redlotus` and actual onedir each completed 4 turns with Worker pip/Python queries, an unknown six-digit image, panel display, two normal exits and restore to the same session ID. Durations were 212.7 s and 65.6 s. The Worker assertion checked its response and saved usage, not every child command; a separate source command observer provides the command-level evidence below. These entry checks do not cover the full product.
- The command observer recorded real selected-environment `pip --version` and `sys.executable` success. It also preserved a genuine exit-1 probe: `python -X utf8 -m pip --version` fails because this existing project venv has no pip module. The basic pip launcher can manage that venv through `PIP_PYTHON`; the tool description now distinguishes pip's program location from its install target. Extra interpreter flags are not silently discarded and no pip module/environment was installed to conceal this limitation. That Worker used 12 responses, so this run is not evidence of efficient delegation or zero failed commands.
- The 20-turn source memory batch retained a **600.11 s timeout**. The pending automatic window was ready at 09:25:41.488 UTC and started its first HTTP request at 09:25:43.062 UTC; the request remained in flight for 134.38 s until the batch deadline. Preparation was approximately 1.6 s. Actual request delay is distinguished from scheduling backlog.
- Explicit L2 production created one record (`375eb739107b11bc1152546709537e9d`) for the user's actual existing-Python constraint, using two real model calls and real indexing. The main/ordinary-Worker usage for these 20 turns was 1,810,384 input / 1,783,296 cached tokens (98.50%). Auxiliary perception usage is separate.
- Loading the 20-turn session did not schedule work. Explicit `/STM retry` resumed the original pending window (`65ccbd6ef53e61fa133b38cf2306dd5e`) and completed/indexed it in 265.91 s, without adding a turn or another window. Its two API calls took 219.263 s and 43.116 s. They reported 46,224 output / 45,959 reasoning tokens and 10,707 output / 8,516 reasoning tokens respectively. No model parameter changed to obtain this result; the earlier timeout remains a failed batch.
- That window produced one L1 record (`da03ebbe39c7bbe95ae5a81abd1cc124`), covering the three-group checklist and handoff, rather than fragmenting it. Independent reads of both actual Markdown artifacts agree with the memory: 22 checklist items, 1 verified, 4 partial and 17 pending. The memory's successful goal is delivery of the checklist, and it explicitly retains 21 unverified items; it does not assert complete application acceptance. Temporary no-install/no-weather task conditions were not promoted to L2.
- Remaining: the complete original 360-turn suite and actual 921,600-token automatic compression gates, current-version 60-turn/three-window quality audit, real QQ/WeChat recipient flows, and the existing structure exceptions. Browser startup currently uses Playwright's default driver process environment; project placement of all browser temporary files is a code-path concern, not a measured storage pass. No new browser backend or process-wide environment mutation was introduced to hide it.

Final FIFO candidate build: wheel exit 0 / 4.33 s; onedir exit 0 / 110.55 s. Both use existing interpreters. The wheel contains the exact current 29 application modules, without config/.env/session/memory/log files. SHA-256: wheel `01b882c01a27dc2a9a4dc255f61349b39aa744af2f26e995aafce99bd5eae13c`; EXE `aa05057e7b7bf2f370cb49e2f8f6898dc8184f86f228f7551e096ca756f35451`. Miniconda installation is a **1.0.1 candidate replacement**, not a public version upgrade.

### Final targeted results

The user selected **finish this targeted acceptance first** after confirming that the original frozen 360-turn question file was removed during the previous cleanup. No replacement question bank was created and the full-acceptance gate remains open.

| Current candidate check | Result | Evidence boundary |
| --- | --- | --- |
| Daily CMD `redlotus` | 4 real user turns, 68.5 s, 8 entry checks passed; Worker had 5 responses | Actual installed command, chat, Worker, image, panel, exit/restart and same-ID recall; no unexpected WARNING/ERROR. This is not every TUI command. |
| Actual onedir `Agent.exe` | 4 real user turns, 154.9 s, 8 entry checks passed; Worker had 7 responses | Actual EXE and external Miniconda Python. Same entry scope and no unexpected WARNING/ERROR. |
| Installed-package memory consumption | 5 real user turns, 358.17 s, 7 checks passed | Fresh session retrieved L1 without loading the old transcript. Another project retrieved L2 but could not read the foreign L1 ID. Repeat/update kept the original L2 ID at versions 2 and 3; version 4 is deleted and actual search returned no record. Eight real embedding calls and six real rerank calls were observed. |
| Preservation after forgetting | Passed by independent store read | L2 remains deleted/version 4; the original project's L1 remains active/version 1 with 20 source turns. No personal memory was involved. The Agent's phrase “none in this test database” was overly broad: its search was scoped to the new project; the independent check, not that phrase, establishes preservation. |
| Source Skills and direct-Worker browser | 2 real turns, 13 responses, 43.73 s; actual HTML and screenshot | Actual Skill list/body calls. Worker tool logs record navigation, visible-text read and screenshot; independently viewed 5,713-byte PNG contains the requested Chinese sentence. Screenshot SHA-256 `bd2d3e68cfe7d957052df06fa19113d14a4f641b97dbb830cc2b95aa8bff77c5`. Test process TEMP was explicitly on E; default browser temporary placement is still not proven. |
| Diff/review and goal control | 4 local behavior checks passed | Rejected/retained hunks change the actual file correctly, outside edits are preserved, rejection removes only a newly created file, and goal continuation retains the goal/evidence until DONE. Model-driven goal is recorded separately. |
| Source real goal | 1 user turn, 3 real responses, passed | RedLotus itself wrote the required three-line handoff file, called `read_file`, ended the goal and released its active state. Independent file content matched exactly; no extra user turn or automatic perception was added. |

The browser observer initially searched completed Worker transcripts and falsely reported missing tool calls. Completed child message bodies are intentionally pruned after the task; the role file retained its eight usage responses and empty context, as required for minimal recovery storage. The corrected read-only observation used the actual Worker tool log and screenshot. No product code, expected browser operations or model response was changed; the original observer failure is retained here. Additional setup-only driver mistakes (namespace-package `__file__` and a missing MemoryStore workspace argument) occurred before requests and are not product failures.

The installed-memory deletion turn took 211.52 s; it functionally passed but does not establish a fast deletion path. No timeout/model parameters were altered. Optional live QQ/WeChat delivery is still unverified without a configured account and authorized recipient; local adapter/FIFO checks do not substitute for it.

Cleanup scope is limited to this follow-up's known E-drive build directory, `WorkDatabase/runtime/keyboard-completion-build`, `WorkDatabase/runtime/packaging`, `.memory-live-ioyfg1kv`, `.browser-skill-z8lm4jne`, and the two `keyboard-completion-*20260920` evaluation namespaces under the user's global data directory. They contain this follow-up's artifacts and synthetic/test memory only. Existing project `.redlotus` sessions, personal memory/configuration, and other WorkDatabase content are excluded. `src/RedLotus.egg-info` predates this follow-up and is retained as existing editable-install metadata.

Cleanup completed: all seven exact directories above were resolved and checked for reparse points/process use before removal; none remains. Their files totaled **624,172,520 bytes (595.26 MiB)**. The successful entry/goal/local fixtures were also removed by their scoped temporary-directory owners. Installed Miniconda RedLotus remains available; the verified onedir/build/wheel copies were removed as requested. Final protected-file verification still reports the same config/.env hashes and only the approved Worker Markdown change. No master merge or release was performed.

### Final develop upload review — 2026-09-20

The final local rerun verified all 29 application modules parse, the installed daily package matches those 29 source files byte for byte, load preparation failure retains the original session and live resources, 24 jobs create only 16 Agent threads, cancellation releases the running/queued jobs, and 30 channel inputs remain FIFO. Injected load errors were expected and are not normal-path failures. No model request or replacement environment was needed for these checks.

Publication review excludes the locally edited `src/redlotus/config.json`; `.env` remains untracked and excluded. Neither file is copied, backed up, staged or sent as model material. Candidate source and report files were checked for known local credential values and common token/private-key patterns without printing those values; no match was found. The remote configuration is not updated by this upload: deployments must retain their own explicitly configured SDK-prefixed model identifiers. The user's existing source changes and test deletions are preserved.

The full diff whitespace check reports one trailing space in the user-preserved `manager_planning_new.md` prompt. Its text is left untouched under the prompt-editing restriction; this is recorded rather than presenting the check as clean.

Known limits remain: PyCharm Classic transmits indistinguishable keys; the existing source venv cannot run every explicit `python ... -m pip` form without its own pip module; default Playwright driver environment/temporary-file placement is not verified as isolated; actual QQ/WeChat recipient flows and the full three-environment suite remain unaccepted. The earlier perception timeout and low-cache cold samples remain in the results. No claim of an entirely bug-free application, master merge or public release is made.
