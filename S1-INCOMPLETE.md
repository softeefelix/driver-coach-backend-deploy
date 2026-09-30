# S1 checkpoint — historical INCOMPLETE checkpoint

Superseded by S1-HANDOFF.md in the subsequent implementation commit. This file records the prior disk-blocked attempt, not the current review status.

Fresh branch forge/dc-s1-school-slack from 1ae2da5. No production writes, push, deploy, Render edits, client edits, or changes to predecessor branch.

Observed verification:
- Baseline: `cd /tmp/dc-nsi-s1/backend && PYTHONPATH=. uv run --with-requirements requirements.txt python -m pytest -q` => 1 passed.
- Added live-entry regressions => 3 failed, 1 passed (school advised at 2pm, event lost, chain metadata absent).
- Same command after initial implementation => 4 passed. This is NOT acceptance.
- `git diff --check` passed.
- Read-only H9 script ran successfully using authorized DB_URL with sslmode=require and default_transaction_read_only=on. Artifact: /Users/felixtarnarider/.hermes/kanban/workspaces/t_aa3c5515/h9-evidence.json. No unmatched school-like names in timetable query. generated_at included on second run; final recency/count analysis remains.

Blocker: host disk exhausted. Python 3.12 extraction and then pgserver installation using existing Python 3.11 both failed with No space left on device. No existing postgres/initdb/psql/docker found on PATH. Local disposable PostgreSQL is needed to verify real migrations, append-only behavior, session state, commit/rollback, and restart persistence. No remote DB writes were attempted as a substitute. No other workers' files deleted.

Implementation checkpoint (NOT review-ready):
- Feature switch DRIVER_COACH_NEXT_STOP_INTELLIGENCE defaults off. Live resolver calls new chooser when enabled and frozen plan supplied; old advisor unchanged.
- Real-position freshness validation; current -> filler -> ordered anchors with waiting; sticky state skeleton; defaults buffer/dwell/margin/hysteresis = 15/10/5/2 minutes, anchor dwell 10, fix age 150 seconds.
- Additive fields reason/reasonCode/anchor/due/decisionAuditId/anchorContext.
- Schema-qualified decision store + idempotent named-schema SQL, currently UNEXECUTED. Session locking, caller commits, outcome validation against stored choice.
- Existing inferred passed-order advancement suppressed under switch because a post-school filler would otherwise auto-skip its school. Crumb/pellet derivation untouched.

Remaining engineering work (not attributed to disk blocker):
- Full live-path A8 scenario matrix, replay, differential fixtures against exact old module (only 3 new tests exist).
- PostgreSQL integration suite with idempotency, append-only triggers, durable restart, rollback, concurrent requests, service poll/outcome paths.
- Bound aggregate ETA I/O latency; current candidate loop could make excessive sequential 4-second calls.
- Event end time/status wiring, completion semantics, duplicate stable event identity; old insert_events does not preserve endAt/status.
- Fail-safe and binding interaction, exception-safe adapter, invalid configuration audit, missing-coordinate and stale position cases.
- Protect/reinitialize sticky state on route reconfirmation/day rollover.
- Truthful early no-fit reason (current Leave now copy can be over-urgent), parse school windows/dwell conservatively and validate M5 scoped facts.
- Service-level anchor served semantics: initial conservative guard currently requires manual Done for schools; not yet verified against actual Park evidence/window.
- Finish H9 report with exact counts/recency and matcher tests; enhanced matcher only applied in enabled chooser so switch-off parity is preserved.
- Fix static type diagnostics, update docs, full self-review, then Warden independent review. Do not send this checkpoint for approval.
