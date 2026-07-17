# Manual Smoke Checklist

Run this checklist before deployment or after any change that touches experiment flow, provider routing, ASR, admin, or recovery behavior.

## Setup

- Start backend: `python3 -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers`
- Start frontend build or dev server as appropriate for the slice being checked.
- Use a clean browser profile or private window so participant and admin cookies do not bleed across checks.
- Record pass/fail notes for each item, including screenshots when a visible block or recovery state is involved.

## Participant Flow

- [ ] On a fresh production database, `GET /api/recruitment-status` reports closed and a new identity receives `503 recruitment_closed` without participant/attempt rows being created.
- [ ] With recruitment open, a new valid name and phone can enroll; after closing recruitment, a different new identity is rejected with Chinese copy while an existing active or completed participant can still log in and recover canonical state.
- [ ] Login page shows only name and Alipay-bound phone.
- [ ] An invalid phone shows Chinese validation copy and no raw exception or framework class name.
- [ ] Login page keeps `测试入口` and `管理入口` below the login form.
- [ ] Clicking `登录` with a failing formal environment gate stays on the login page and shows a visible message.
- [ ] Clicking `登录` when microphone permission is promptable requests microphone permission before formal login continues.
- [ ] Successful login shows the welcome card directly; the formal participant flow does not visit `/environment-check`.
- [ ] Existing participant cookie plus direct `/welcome` still shows login page.
- [ ] Existing participant cookie plus direct `/pretest` restores the current attempt.
- [ ] Existing participant cookie plus current `/experiment/:sessionId` reruns the formal environment gate and restores the same attempt and session identifier.
- [ ] Same-SPA return from restored `/pretest` or `/experiment/:sessionId` to `/welcome` shows login instead of the welcome card.
- [ ] Existing participant cookie plus old-attempt `/experiment/:sessionId` returns to login or shows a recovery block.
- [ ] Formal welcome does not show condition, subcondition, topic key, error type, or session id.
- [ ] Completed short participant relogin shows the completed message and no start button.
- [ ] Incomplete short attempt or incomplete long Day 1 relogin preserves the current attempt, pretest, incomplete formal session, turn/ASR state, and stored audio.
- [ ] Finalized Day 1 pretest, any active long Day 2/3 state, or a completed attempt plus direct `/pretest` navigation replaces the URL with the active `/experiment/:sessionId` or `/complete` and does not re-open the questionnaire or login page, even when current-day `pretest_status` is `not_started`.
- [ ] Direct navigation or refresh on a completed formal `/experiment/:sessionId` replaces the URL with `/complete` immediately; a transient `/api/me` failure does not leave the browser on the experiment URL.
- [ ] Revoking microphone permission or using an unsupported formal environment before `/pretest` recovery or `/experiment/:sessionId` refresh shows a blocking message without abandoning the attempt or deleting session/audio state; restoring a supported environment resumes the same session.
- [ ] A browser without `MediaRecorder` support fails the formal environment gate even when microphone permission is granted.
- [ ] Desktop requirement, unsupported browser, narrow viewport, insecure/HTTP context, microphone capability, denied permission, and other welcome environment failures all show Chinese in-app copy without raw exception names; only the browser/OS native permission dialog may use a language outside application control.
- [ ] A partial Day 1 pretest autosaves after a short pause; refreshing `/pretest` restores the last server-acknowledged values and questionnaire step.
- [ ] Simulating a draft network failure shows an autosave error while every unsaved local field remains unchanged; retrying after recovery saves the current values rather than the failed snapshot.
- [ ] Invalid or out-of-range supplied draft values are not autosaved. Final submission shows server `field_errors` beside affected fields without clearing other answers.
- [ ] Clicking final while a debounce is pending flushes the current draft before finalization. Repeating the identical `POST /api/pretest/final` returns the same persisted response, while a later draft or conflicting final returns `409 Conflict` and leaves the stored final unchanged.
- [ ] Pretest Likert and frequency questions use radio-style selection, not dropdown selects.
- [ ] Day 1 pretest rejects an untouched slider even when it visually shows 50.
- [ ] Pretest save-page `进入实验` starts a formal session and navigates to `/experiment/:sessionId`.
- [ ] Formal `/experiment/:sessionId` uses the `chatContainer`-style AI interaction layout and does not show `Formal Gate`, `DesktopGate`, provider, graph, assignment, planned error, or session id.
- [ ] Rating title is exactly `请基于本次对话进行评价`.
- [ ] Completion page uses original-interface completion messages.
- [ ] Formal backend rejects a direct `input_mode="text_test_only"` turn submission.
- [ ] Execution scenario keeps the right execution panel visible throughout the session.
- [ ] At 1366x768, 1180x800, and 1024x768, the execution panel remains beside the conversation without overlap or horizontal overflow; only widths below 1024 stack the panel.
- [ ] The execution panel shows stable Chinese empty, loading, awaiting-input, failure, and rating prompts. Loading, awaiting-input, and failure states preserve the latest successful table or copy result.
- [ ] A completed execution response with a missing, malformed, or wrong-kind artifact shows the safe failure state instead of rendering unvalidated payload content.
- [ ] Each turn requires stance/trust rating before the next turn.
- [ ] Submitting the fifth rating completes the formal session and opens `/complete` without a separate completion button.
- [ ] Simulating a lost fifth-rating response still reaches `/complete` through the idempotent completion recovery request.

## Test Channel

- [ ] Test channel uses admin username and password.
- [ ] Test control filters topic options by the selected condition/subcondition and resets invalid topic selections.
- [ ] Test control can launch all five subconditions: qa, planning, chat, decision, and execution.
- [ ] Test control launches a session and navigates the current tab to `/experiment/:sessionId`.
- [ ] Refreshing that test `/experiment/:sessionId` keeps the test experiment page instead of returning to `/welcome`.
- [ ] Test experiment defaults to text input.
- [ ] Test experiment page shows `返回测试入口`, keeps text and optional ASR voice input available together, and does not show the debug panel.
- [ ] Sending text or a successful ASR transcript immediately renders the user message and an assistant-thinking state before `/api/turns` completes.
- [ ] A failed `/api/turns` request removes the temporary user/assistant pending pair and restores the original text or recognized voice result for retry; a successful turn remains visible if only the following session refresh fails.
- [ ] Delay `/api/turns`, send a turn, and confirm `client_response_latency_ms` starts after ASR, ends only after the assistant text is painted, and for execution waits until the right-side result is visible. Confirm `turns.csv` includes all five client-timing fields; a background-tab interval sets `client_timing_interrupted=true`.
- [ ] Test sessions support both text turns and optional ASR voice turns. A successful test voice turn uses `/api/asr`, records test-scoped ASR/chat evidence, and leaves formal participant state unchanged.
- [ ] Denying or removing microphone access in a test session shows only fixed Chinese recovery text plus `请求麦克风权限` and `重新检查`, while text submission remains usable.
- [ ] Submitting the fifth test-session rating completes that test session without changing the formal participant-day state.
- [ ] Test chat requests use only the configured DeepSeek route with thinking disabled.
- [ ] The test channel is DeepSeek-only.
- [ ] Ordinary formal chat order is six GPT routes -> DeepSeek -> fixed fallback; planned non-system errors use the same external failover without local content fallback.
- [ ] DeepSeek request body adds `"thinking":{"type":"disabled"}` and the request stops within the configured 15-second hard wall-clock deadline.
- [ ] System Monitor shows DeepSeek configuration without credentials and the test action reports only provider, model, status, latency, and safe error code.

## Scientific Design Parity And AI Error Generation

- [ ] Complete Long Day 1, then verify Day 2 and Day 3 responses use prior completed-day context from the same attempt; confirm another attempt, a test session, and an unrelated incomplete session do not enter the prompt. Verify the latest four rounds remain verbatim and older history appears only as the bounded summary.
- [ ] Run both `valueDecision` and `preferenceDecision`; confirm they remain pure-text conversations with no decision matrix, preference cards, right-side decision panel, or artifact payload.
- [ ] With real configured providers, exercise `factual_minor`, `factual_major`, `logic_minor`, `logic_major`, `social_minor`, and `social_major` across text, execution, and weather paths. Confirm every participant-visible error is newly AI-generated and no fixed dismissive or assistance-withdrawal sentence appears.
- [ ] Force four evaluator rejections followed by one acceptance; confirm five fresh AI generations are possible and only the accepted candidate is persisted.
- [ ] Force five evaluator rejections; confirm the fifth AI candidate is shown and persisted unchanged, `error_presented=false`/`manipulation_status=failed` are recorded, `error_not_presented` is raised, and the turn is excluded from clean-data export. Repeat with invalid execution schema and confirm no fixed structured fallback replaces candidate five.
- [ ] For a successful planned-error evaluation in formal or `/test`, confirm sanitized research evidence records `route=evaluator`, `provider=deepseek`, and `model=DEEPSEEK_MODEL`, while the participant response exposes none of the evaluator prompt, response, reason, or provider metadata.
- [ ] Inspect a normal and planned-error provider request in a safe test double; confirm both use `general taxonomy + topic role + current specific instruction`, with `normal` on ordinary turns and the assigned error instruction only on the planned turn.
- [ ] On a later planned-error turn, confirm the evaluator receives every earlier user/assistant message from the current session plus the current input and candidate, but receives no prior-session history, hidden system prompt, participant identity, token, or credential.
- [ ] Force an evaluator rejection with a distinctive reason; confirm the next candidate prompt contains the bounded reason while turn evidence, exports, API logs, and participant responses do not. Confirm evaluator format attempts stop after two.
- [ ] Exhaust all external generation routes on a planned non-system error; confirm the turn returns a retryable `503`, persists no assistant turn/artifact, and shows no local fallback content.
- [ ] Run `system_failure`; confirm the exact text is `系统出现错误，请稍后再试。`, with no generation or evaluator provider call.

## Admin And Exports

- [ ] The admin saves one unified recruitment draft as `开放` or `暂停`. Switching `允许新被试注册` changes only the draft; `保存` updates `GET /api/recruitment-status`, shows Chinese failure feedback without raw exception names, and creates exactly one sanitized `set_recruitment` admin event when the state changes. Restore the deployment's intended recruitment state after smoke testing.
- [ ] `/admin` loads the React admin dashboard, not a Gradio page.
- [ ] Admin login uses the configured admin username/password and logout clears the admin session.
- [ ] `/admin/console` and `/admin/console/` redirect to `/admin`.
- [ ] Left sidebar sections are visible for 系统监控, 数据监控, 数据导出, 实验控制, and disabled 数据分析.
- [ ] 系统监控 shows provider/model API usage for both `全部累计` and `最近 24 小时`.
- [ ] 数据监控 loads metrics without `Request failed: 500`; 被试搜索 shows no rows before a query and returns rows after entering ID/name/phone suffix.
- [ ] 数据监控 places 被试搜索 above 未完成与需复核列表, and the 未完成记录/需复核记录 tables use aligned columns.
- [ ] Assignment control includes participant type, condition, subcondition, and error type.
- [ ] Recruitment and assignment drafts stay separate: editing the formal recruitment `开放`/`暂停` draft or any assignment-cell cap/enabled draft and then switching admin sections prompts to save; confirming saves the recruitment draft through its own action first, then saves dirty assignment cells, without combining or replacing either state.
- [ ] Starting an assignment batch action or assignment-cell save-all immediately disables the recruitment draft/save controls, every assignment cell, filter, keyword, batch-cap, refresh, and submit control; the rendered preview and confirmation show the same exact grouped cell identifiers and affected count. Attempting to click or type while preview/mutation is pending cannot change the visible revision. Canceling or forcing a preview/mutation failure re-enables controls without replacing unrelated assignment-cell drafts, the recruitment draft, the server-confirmed recruitment status, filters, or batch cap; a stale `409` remains visible and requires a new preview.
- [ ] Clean-data audit shows eligible/excluded reasons.
- [ ] A completed legacy session with missing or unscoped API success logs shows `review_needed / external_api_evidence_missing` and is absent from complete-no-external-error export; a planned `system_failure` turn requires ASR evidence but no generation/evaluator evidence.
- [ ] Experiment export defaults to excluding test data.
- [ ] Each compatibility trial exports `llmProvider` and `llmModel`, plus `llmRoute`; `integrated.csv` exports matching `llm_provider`, `llm_model`, and `llm_route` values for DeepSeek, GPT routes, fixed fallback, and planned system failure.
- [ ] Export date controls align with 清除日期 and export buttons.
- [ ] Export jobs can delete non-running job records and remove the corresponding server archive.
- [ ] Converted-short default export includes only source Day 1 data even if source long-attempt Day 2 history exists for audit.
- [ ] Complete no-external-error export includes only eligible data.
- [ ] Reimbursement export includes payment identifiers only in the reimbursement export.

## Provider And ASR

- [ ] Provider health dashboard stays sanitized and exposes no raw secrets.
- [ ] Tencent `CreateRecTask` sends only supported recording-file parameters and does not include `UsrAudioKey`; the real ASR success path returns `asr_status="success"` with non-empty `asr_text` that does not start with a Tencent `[minute:second,minute:second]` timestamp range, records audio, and allows the formal turn to continue.
- [ ] Formal `/api/asr` exposes only `asr_result_id`, transcript/status, and retry state. Missing, tampered, foreign-session, stale, or later-turn replay identifiers are rejected by `/api/turns`.
- [ ] ASR failure path allows re-record without a text fallback.
- [ ] Starting a recording shows a stable `MM:SS` countdown, does not overlap adjacent controls at desktop or narrow widths, and automatically stops and uploads at the configured maximum duration.
- [ ] Retrying a transient upload failure reuses the same recording and operation identifier; re-recording creates a new recording and operation identifier.
- [ ] The recorder loads its displayed maximum from `GET /api/runtime-config`; changing backend `ASR_MAX_DURATION_SECONDS` changes the countdown without rebuilding the frontend.
- [ ] A complete multipart request over `ASR_MAX_REQUEST_BYTES` or audio file over `ASR_MAX_UPLOAD_BYTES` returns `413`; unsupported media returns `415`; malformed, mismatched, or duration-less accepted media returns `400`. None calls ASR or leaves a file under `data/.asr-uploads/`.

## Restore And Recovery

- [ ] Refresh mid-session restores turn history, rating state, and the next-turn gate from backend data.
- [ ] Restart the deployed service, reload the participant page, and confirm `/api/health`, `/admin`, and `/` recover correctly.
- [ ] `python3 scripts/cleanup_participant_attempts.py --dry-run --today <next-day> --json` shows a planned conversion for a seeded missed Day 2/3 long attempt without writing changes.
- [ ] `python3 scripts/cleanup_participant_attempts.py --apply --json` converts the same seeded missed attempt into converted-short state and leaves default export scope at Day 1 only.

## Publication Gate Evidence - 2026-07-12

The publication gate remains **closed**. Deterministic local checks and the temporary backup/restore drill below passed, but unit and local integration tests do not replace real browser, provider, ASR, or Linux deployment validation. The current local-maintenance runtime recruitment flag is open for local operations only; this does not authorize external publication or invitations to real participants. External publication and formal real-participant recruitment remain prohibited until the pending checks below pass and their evidence is recorded.

### Executed Automated And Local Checks

- [x] Five-turn formal flow and automatic fifth-rating completion: `uv run pytest backend/tests/test_e2e_formal_short.py backend/tests/test_session_state.py::test_fifth_rating_completes_session_and_lost_response_retry_is_idempotent -q` -> `2 passed in 0.82s`.
- [x] Backend navigation/relogin/pretest/session recovery coverage: `uv run pytest backend/tests/test_attempts.py backend/tests/test_login_flow.py backend/tests/test_pretest.py backend/tests/test_session_state.py -q` -> `110 passed in 4.86s`. This covers attempt identity, relogin, draft/final pretest restore, refresh-style session restore, environment revalidation, and completed-session routing contracts. Browser Back-button behavior remains pending below.
- [x] Concurrent provider/evaluator/ASR calls release SQLite write locks: `uv run pytest backend/tests/test_turn_submission.py::test_api_turn_submission_releases_write_lock_during_provider_routing backend/tests/test_turn_submission.py::test_api_turn_submission_releases_write_lock_during_evaluator_call backend/tests/test_asr_policy.py::test_api_asr_releases_write_lock_during_transcription -q` -> `3 passed in 0.81s`.
- [x] Final-review recruitment, evidence, admin-concurrency, opaque-ASR, and migration regression slice: `uv run --extra dev pytest -q backend/tests/test_recruitment_gate.py backend/tests/test_login_flow.py backend/tests/test_assignment.py backend/tests/test_clean_data_audit.py backend/tests/test_export.py backend/tests/test_admin_permissions.py backend/tests/test_asr_policy.py backend/tests/test_turn_submission.py backend/tests/test_db_bootstrap.py` -> `280 passed in 14.92s`. This includes concurrent writers during admin provider testing, clean-data hashing, and compatibility archive building; fresh-production closure and audited opening; positive API evidence; and ASR response/scope/replay enforcement.
- [x] Seven error-condition contracts, missing/corrupted audio exclusion, and production readiness failures: `uv run pytest backend/tests/test_error_injection.py backend/tests/test_clean_data_audit.py::test_clean_data_audit_excludes_missing_audio_path backend/tests/test_clean_data_audit.py::test_clean_data_audit_excludes_missing_audio_file backend/tests/test_clean_data_audit.py::test_clean_data_audit_excludes_changed_audio_bytes backend/tests/test_export.py::test_clean_data_export_fails_explicitly_when_expected_audio_is_missing backend/tests/test_health.py -q` -> `115 passed in 1.72s`. The seven parametrized conditions are `factual_minor`, `factual_major`, `logic_minor`, `logic_major`, `social_minor`, `social_major`, and `system_failure`; they use deterministic test-channel/local doubles, not real provider calls.
- [x] Export lease/restart/publication and dashboard/bootstrap focus: `uv run pytest backend/tests/test_export.py backend/tests/test_admin_dashboard.py backend/tests/test_db_bootstrap.py -q` -> `78 passed in 2.75s`. This includes lease expiry during publication, transient/persistent commit and success-write failures, fresh-connection reconciliation, lifetime supervisor recovery after a still-valid orphan lease expires, shutdown thread cleanup, transient heartbeat retry, newer-owner fencing, and cross-token staging/canonical preservation.
- [x] Temporary operational backup drill outside the live data tree: created a migrated empty test database under a `0700` directory in `/private/tmp`, ran `uv run python scripts/backup_data.py --output <private-root>/backups/drill.zip`, `uv run python scripts/backup_data.py --verify <private-root>/backups/drill.zip`, and `uv run python scripts/restore_backup.py <private-root>/backups/drill.zip --destination <private-root>/restore-parent/restored-data`. Result: backup verified with 2 payload members, restore verified, `PRAGMA integrity_check=ok`, 12 migration rows. The first attempted `/tmp` path was correctly rejected as `live_data_directory_unsafe_type` because `/tmp` is a symlink on macOS; the successful drill used `/private/tmp`. All temporary artifacts were removed.
- [x] Full local regression on the AI-conversation alignment release revision: `uv run --extra dev pytest -q backend/tests` -> `999 passed, 1 skipped in 31.65s`; `cd frontend && npm run typecheck && npm run check:interface-parity && npm run build && npm run test:e2e` -> typecheck exit 0, `Interface parity check passed.`, Vite production build exit 0, Playwright `13 passed in 6.5s`. The Playwright suite covers formal zero-artifact execution startup, empty/loading/awaiting/success/failure/rating states, malformed payload handling, copy editing, bounded scrolling, and 1366x768, 1180x800, and 1024x768 layouts.
- [x] Real Open-Meteo acceptance: `RUN_LIVE_OPEN_METEO=1 uv run --extra dev pytest -q -m live_weather backend/tests/test_weather.py` -> `1 passed, 50 deselected in 1.88s`, using only `geocoding-api.open-meteo.com` and `api.open-meteo.com` for the Hangzhou structural lookup.

### Required Checks Still Pending

- [ ] **PENDING - real browser:** complete the visible desktop flow in Chrome/Edge with Back, refresh, relogin, pretest autosave recovery, fifth-rating automatic navigation, narrow-width recorder layout, admin controls, and screenshots. No browser automation or manual browser session was executed in this task.
- [ ] **PENDING - real provider and ASR:** `YIZHAN_API_KEY`, `AABAO_API_KEY`, `PACKYAPI_API_KEY`, `DEEPSEEK_API_KEY`, `TENCENT_SECRET_ID`, and `TENCENT_SECRET_KEY` were all unset during this verification. Run the formal and test-channel staging flows against configured production-like provider and Tencent ASR credentials, including concurrent calls, all seven conditions, audio recording/upload, retry, and sanitized logs. Local doubles cannot establish external service behavior.
- [ ] **PENDING - Linux deployment:** run systemd/reverse-proxy startup, unclean process restart with queued/expired export jobs, readiness probes, filesystem ownership/permissions, external encrypted backup destination, and restore drill on the target Linux host. The local drill ran on macOS and does not validate Linux `renameat2`, service-account, mount, or proxy behavior.
- [ ] **PENDING - publication decision:** do not externally publish the participant URL or invite real participants until the three checks above have recorded evidence and the full verification suite remains green on the release revision. The local-maintenance runtime flag may remain open for local operations, but it is not publication authorization.
