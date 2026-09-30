# S1 implementation handoff — independent Warden review required

Branch: `forge/dc-s1-school-slack`, base `1ae2da5`. Supersedes the incomplete checkpoint notes in S1-INCOMPLETE.md. No deploy/push, Render edits, client changes, credential changes, role grants, or crumb/pellet derivation changes.

## Reproduce

From `/tmp/dc-nsi-s1/backend`:

```
PYTHONPATH=. uv run --python 3.11 --with-requirements requirements.txt --with pgserver python -m pytest -q -s
PYTHONPATH=. uv run --python 3.11 --with-requirements requirements.txt python scripts/replay_school_day.py
```

Observed baseline 1 passed -> first checkpoint 4 passed -> expanded RED 5 failed / 28 passed -> final 44 passed. Replay produces stop identities 3, 4, 2, 2, 4, null at injected times 840, 875, 920, 925, 945, 960. External GPS, traffic, grade and map calls are fixture-backed; resolver, chooser, payload, decision store and service outcome/poll functions are real. Tests do not claim live Mapbox or an iPad field run.

PostgreSQL tests create ONE local disposable instance under TMPDIR, serially, with try/finally stop and removal. Observed final before/after df: 31 GiB free both times; cleanup printed. No remote database migration was applied. pgserver is test-only, not a production requirement. Python 3.11 was an already installed interpreter.

## A1–A9 evidence (paths relative to backend)

- A1: `app/routes.py:668` calls chooser in live resolver; `app/school_slack.py:195` calculates cutoff, not the legacy horizon. `tests/test_school_slack_live.py:35` proves 2pm -> filler for 3:30pm school at payload boundary.
- A2: `app/routes.py:622` single real truck fix; `:685` endpoint-specific driving-traffic adapter; `app/school_slack.py:214` ordered current -> filler -> school1 -> school2 simulation. Waits until booked time (or later explicit window_start), then max(configured anchor dwell, booked leave/end time). Both feasible and infeasible two-school live-path tests assert ordered legs and waiting, not weakened unit expectations.
- A3: `app/school_slack.py:17` flag, default OFF. `tests/test_school_slack_matrix.py:137` executes original resolver source from git 1ae2da5 and compares full result dictionaries over empty/ordinary/school plans, times, served/skipped states and events. Original advisor untouched. Operator enables `DRIVER_COACH_NEXT_STOP_INTELLIGENCE=1`, disables `=0` or unset; operator must apply migration first. No Render changes made.
- A4: enabled chooser considers event-only remainders; live test `test_plan_finished_booking_remains`. `app/events.py:94,167` preserves Jobber identity and endTime when enabled; tests cover end-window blocking, duplicate IDs and completed events. Events only leave eligibility on upstream COMPLETED/CANCELLED/CANCELED status or removal from today's feed, not clock expiry.
- A5: `app/school_slack.py:134,203` state, sticky feasible filler, two-minute admission/zero-minute retention hysteresis; anchor latch. `app/routes.py:674` durable session load and route/plan/Pacific-date scope. Reconfirmation explicitly resets latch. Tests jitter times, delay mid-filler, anchor persistence, release by served status, and actual DB-backed poll/skip/done/reconfirmation.
- A6: invalid/nonfinite/negative/missing ETA, adapter exceptions, missing/stale/untimed GPS and invalid operator config fail back to original advice, suppressing fits-first copy. Missing-coordinate filler is excluded. `app/routes.py:684` aggregate chooser ETA allocation 4 seconds, passed as remaining timeout to existing adapter. This bounds allocated socket timeouts, not an OS/DNS hard real-time guarantee. A missing ETA preserves the latch/sticky memory but displays the legacy fallback, per H5; next successful poll resumes the retained state.
- A7: `app/payload.py:268` additive reason/reasonCode/anchor/due/decisionAuditId/anchorContext. `app/decision_store.py:17,28` schema-qualified session lock/load/append. `migrations/006_school_slack_decisions.sql` is the complete migration text. Idempotency and UPDATE/DELETE/TRUNCATE rejection executed twice against each of driver_coach and driver_coach_test; no public table created. `service.py` passes its resolved schema through all writers. Actual DB tests verify persisted audit UUID, input/state JSON, reconnect durability, rollback, row-lock contention and service commits/outcomes.
- A8: `tests/test_school_slack_live.py`, `tests/test_school_slack_matrix.py`, `tests/test_decision_postgres.py`; runnable `scripts/replay_school_day.py`. Full matrix, not helper-only coverage. Exact commands/counts above.
- A9: `scripts/audit_school_names.py` was executed read-only against authorized Master Route source with sslmode=require and default_transaction_read_only=on; server confirmed on. Raw evidence `../evidence/h9-evidence.json`, summary `../evidence/h9-summary.json`. 187 school-like timetable rows, 159 route clusters, 144 distinct stop clusters; 174 rows generated 2026-09-30, remaining dates listed in summary. Unrecognized rows: none. This is a current timetable audit, not proof every location with an opaque street-only address is correctly classified. Enabled matcher additionally protects Academy/Prep/Preparatory/Montessori/Elem with executable live-path tests; OFF matcher unchanged. Existing substring matcher can false-positive on School Court/Mowry School Road; not silently relaxed.

## Config and policy

All durations minutes except fix age seconds:

- DRIVER_COACH_ANCHOR_BUFFER_MIN = 15
- DRIVER_COACH_FILLER_DWELL_MIN = 10
- DRIVER_COACH_ANCHOR_DWELL_MIN = 10
- DRIVER_COACH_ETA_MARGIN_MIN = 5 (charged per anchor leg)
- DRIVER_COACH_SLACK_HYSTERESIS_MIN = 2
- DRIVER_COACH_MAX_FIX_AGE_S = 150

These are explicit new chooser policy defaults, NOT a claim the old advisor had a 15-minute buffer. Old advisor used a fixed 90-minute horizon and plan leave_by text; existing Mapbox driving-traffic adapter is reused. Config invalid -> auditable conservative fallback. Preference is frozen order, with sticky selection first; unique frozen stop_order makes a separate detour tie-break unnecessary. No plan rows are mutated. An early no-fit reason says “No stop fits first”, not the inaccurate “Leave now”. Binding thereafter stays latched even if travel improves.

## Limits / rollout cautions

- No guarantee of real-world arrival under arbitrary future traffic. Estimates are current driving-traffic, with explicit margins, rechecked per poll.
- For enabled mode schools require explicit Done; early Park cannot silently complete an anchor. Existing current/outcome API remains frozen-plan-only. Event completion comes from Jobber, not a new event Done/Skip API. Item 3 manual anchor confirmation/undo/moved-away remains out of scope.
- The locked events outage rule remains [] (no stale events); this task does not change that source-of-truth contract. ETA fallback intentionally retains old event-horizon behavior under unavailable inputs.
- Original inferred passed-order skipping is suppressed when enabled, because picking a later filler must not silently skip a school; crumb derivation is unchanged.
- Audit missing migration/storage errors fail the enabled request, rather than silently advise without durable audit. Apply migration to the actual service schema before enabling. No role widening. Audit history grows append-only; retention policy is an operations follow-up, not a delete implemented here.
- No physical device or hosted production verification performed. Independent Warden must pass before Felipe seeks release approval.
- hotspot: backend/app/routes.py is also changed by S3; merge its small crumb-path diff independently, never deploy predecessor 8e3c4ed.
