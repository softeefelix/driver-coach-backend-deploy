# No-idle and break API

Contract for the iPad web client. The cab polls `GET /driver-coach/v1/route?session_id=...` about every 20 seconds. That poll is the source of truth for idle nudges and for the running lunch or break. Sign-in and route confirm do not include these fields; treat a missing `nudge` or `break` key as "not evaluated yet" and leave the cab unchanged. The route poll always sends both keys.

`banner` is unchanged. It is still only the evening refuel command, `{ "kind": "refuel" }`, or it is absent. Idle copy never arrives on `banner`.

Times are UTC ISO-8601 with a `Z` suffix. Durations are whole seconds.

## Route poll additions

```json
{
  "nudge": null,
  "break": {
    "active": false,
    "type": null,
    "startedAt": null,
    "endsAt": null,
    "remainingS": 0,
    "allowedS": null,
    "lunchUsed": 0,
    "lunchAllowed": 1,
    "breakUsed": 0,
    "breakAllowed": 2,
    "latestFlag": null
  }
}
```

`nudge` is `null` when the driver should not be nudged. Clear any idle banner or speech immediately. Do not keep the previous nudge on screen.

`break` is always an object on the poll.

### `nudge` object

Present only while the truck should move on.

| Field | Type | Meaning |
| --- | --- | --- |
| `level` | `"banner"` \| `"voice"` \| `"manager"` | How loudly to present it. |
| `message` | string | Ready to show and, at `voice` and `manager`, to speak. |
| `since` | string | When this idle episode started. |
| `idleForS` | number | Seconds since `since`. |
| `noSaleForS` | number or null | Seconds since the last Square sale for this truck. `null` means no sale has been seen today. |
| `atStop` | boolean | `true` if the idle started at a planned pin. `false` if it started parked away from every pin. |
| `episodeId` | string | Idle episode id. Stable until the truck moves, sells, or starts a break. |
| `nextStopName` | string or null | Friendly name of the advised next stop, when there is one. |
| `managerAlert` | boolean | `true` only at level `manager`. |
| `managerAlertAt` | string or null | When the Felix flag was raised. |
| `managerAlertFor` | string or null | `"Felix"` when `managerAlert` is true. |

Levels, measured from the moment the banner first appears (not from when the truck stopped):

1. `banner` — show `message` until `nudge` becomes `null` or the level rises. Do not speak yet.
2. `voice` — keep the banner and speak `message`. This is the stronger nudge.
3. `manager` — speak `message` and show that Felix has been alerted. The server has already stored the flag.

Default timing (see thresholds below): voice 60 seconds after the banner, manager 180 seconds after the banner. A single poll never skips the banner. If the cab was offline and the next poll is already past a later threshold, that response's `level` is the highest reached, and the episode history records each step.

Example, parked at a pin with no sale:

```json
{
  "level": "banner",
  "message": "No sale for 90 seconds. Move to the next stop (Panama Street).",
  "since": "2026-10-15T18:00:00Z",
  "idleForS": 91,
  "noSaleForS": 400,
  "atStop": true,
  "episodeId": "6f1c0e4a-1d2b-4c3a-9e8f-0a1b2c3d4e5f",
  "nextStopName": "Panama Street",
  "managerAlert": false,
  "managerAlertAt": null,
  "managerAlertFor": null
}
```

Parked away from every pin:

```json
{
  "level": "banner",
  "message": "Parked off the route with no sale. Move to the next stop (Panama Street).",
  "atStop": false
}
```

Voice and manager use their own sentences. The manager line says Felix has been alerted.

### When a nudge is sent

All of the following have to be true:

- The Geotab fix is fresh. `fix_age_s` is greater than `DRIVER_COACH_MAX_FIX_AGE_S` when that env var is set to a non-negative number; otherwise it is greater than `motion.max_fix_age_s` in `config/thresholds.json` (150). A missing age is stale. Stale fixes do not nudge, do not escalate, and do not count as "the truck started moving."
- The truck is stationary on that fresh fix: speed at or below `motion.settled_speed_mph` (2 mph), or the route resolver already has this truck parked (`phase` `parked` or a Park-at-pin match) and speed is still under `motion.moving_speed_mph` (6 mph). Speed from live Geotab is miles per hour.
- Square can be attributed to this truck (see below) and there has been no completed sale for `no_sale_move_on_s` seconds (90).
- No lunch or break is active.

At a planned pin the idle clock also has to reach 90 seconds. The pin test is distance to any planned stop or event coordinate within `motion.parked_radius_m` (40 meters), or the existing Park-at-pin / `phase: parked` signal. The clock starts when the server first sees the stationary fix, so a driver who just arrived gets the full 90 seconds to ring a sale.

Parked away from every pin, the clock only has to reach `off_stop_grace_s` (15 seconds), and the 90-second no-sale rule still applies. Fifteen seconds is the motion classifier's existing settle-to-park debounce (`motion.settle_to_park_s`). One jitter sample or a rolling stop does not page the driver. The next poll after that grace does, because there is no stop to work. A sale in the last 90 seconds (a walk-up) still suppresses it.

Once an episode is open it stays open until the truck reaches `moving_speed_mph` (6), a sale is rung, or a break starts. Creeping between 2 and 6 mph does not clear it and does not reset the escalation clock.

If Square cannot be read, or the signed-in driver cannot be matched and the payment does not name the truck, the poll sends `nudge: null`. The server will not tell a driver to leave a stop it cannot see is quiet.

A sale is the latest Square payment with status `COMPLETED` or `APPROVED` and a positive amount. The server caches that lookup for about 15 seconds so the 20-second poll does not refetch Square every time. A sale can therefore take one poll to clear a nudge. It is attributed to this truck when either:

- the payment's team member is the signed-in driver's Square name, or
- the payment note, reference, or device name matches this truck (`truck 7`, `truck #7`, `truck-7`, `t7`) and not a longer number (`truck 17`, `t17`).

### `break` object

| Field | Type | Meaning |
| --- | --- | --- |
| `active` | boolean | A lunch or break is running. While this is true, `nudge` is null. Hide idle UI. |
| `type` | `"lunch"` \| `"break"` or null | Which one is running. |
| `startedAt` | string or null | When it started. |
| `endsAt` | string or null | When the allotment ends. The cab can count down locally; the next poll reconciles. |
| `remainingS` | number | Seconds left. `0` when nothing is running or the allotment is already over. |
| `allowedS` | number or null | Allotted length of the active break. |
| `lunchUsed` | number | Lunches started this shift, including the active one. |
| `lunchAllowed` | number | Default 1. |
| `breakUsed` | number | Short breaks started this shift, including the active one. |
| `breakAllowed` | number | Default 2. |
| `latestFlag` | object or null | Most recent overrun or over-limit flag this shift. |

`latestFlag`:

```json
{
  "kind": "overrun",
  "type": "lunch",
  "overrunS": 125,
  "at": "2026-10-15T19:32:05Z",
  "detail": "lunch ran 125s past the 1800s allotment"
}
```

`kind` is `overrun` or `over_limit`. `overrunS` is null on an over-limit attempt.

The shift is the signed-in session. A new sign-in gets a new allowance.

While a break is active the server suppresses idle nudges. The break ends when any of these happens:

- the driver calls end
- the allotment is over on a poll (`endsAt`)
- a fresh fix shows speed at or above `moving_speed_mph` (6)

A stale fix does not end a break. Ending within `overrun_grace_s` (60 seconds) of the allotment is not flagged; that covers the 20-second poll. Longer than that is an overrun: the row is flagged and `latestFlag.kind` is `overrun`. Starting another lunch or break after the allowance is refused, logged, and returned as `latestFlag.kind` `over_limit`.

## `POST /driver-coach/v1/break/start`

```json
{ "session_id": "…", "type": "lunch" }
```

`type` is `lunch` or `break`.

Success `200`:

```json
{
  "session_id": "…",
  "break": {
    "active": true,
    "type": "lunch",
    "startedAt": "2026-10-15T19:00:00Z",
    "endsAt": "2026-10-15T19:30:00Z",
    "remainingS": 1800,
    "allowedS": 1800,
    "lunchUsed": 1,
    "lunchAllowed": 1,
    "breakUsed": 0,
    "breakAllowed": 2,
    "latestFlag": null
  }
}
```

Starting a break also ends any open idle episode. Clear the nudge as soon as this returns; the next poll sends `nudge: null` as well.

Errors:

| Status | `detail.error` | When |
| --- | --- | --- |
| 400 | `bad_type` | `type` is not `lunch` or `break`. |
| 404 | | Unknown `session_id`. `detail` is a string. |
| 409 | `break_active` | A break is already running. `detail.break` is the current state. |
| 409 | `over_limit` | The shift has used its lunch or its two breaks. The attempt is flagged. `detail.break.latestFlag.kind` is `over_limit`. |
| 422 | | `session_id` or `type` missing. |

Over-limit body:

```json
{
  "detail": {
    "error": "over_limit",
    "message": "This shift already used 2 breaks.",
    "type": "break",
    "break": { "active": false, "latestFlag": { "kind": "over_limit", "type": "break" } }
  }
}
```

## `POST /driver-coach/v1/break/end`

```json
{ "session_id": "…" }
```

Success `200`:

```json
{
  "session_id": "…",
  "break": { "active": false, "remainingS": 0, "lunchUsed": 1, "lunchAllowed": 1 },
  "ended": {
    "id": "…",
    "type": "lunch",
    "reason": "driver",
    "durationS": 600,
    "overrunS": 0,
    "flagged": false
  }
}
```

`ended.reason` is `driver` for this call. Polls that end a break on their own use `expired` or `moving` and do not return `ended`; the next poll's `break.active` is simply false, and `latestFlag` is set when it was an overrun.

`404` unknown session. `409` with `detail.error` `no_break` when nothing is running.

After a break ends, a truck that is still parked starts a new idle clock on the following polls. It is not nudged on the poll that closes the break.

## Thresholds

`config/thresholds.json`:

```json
"no_idle": {
  "no_sale_move_on_s": 90,
  "off_stop_grace_s": 15,
  "voice_after_s": 60,
  "manager_alert_after_s": 180
},
"breaks": {
  "lunch_duration_s": 1800,
  "break_duration_s": 900,
  "lunch_allowance": 1,
  "break_allowance": 2,
  "overrun_grace_s": 60
}
```

Stationary, moving, and pin radius are the existing `motion` block: `settled_speed_mph` 2, `moving_speed_mph` 6, `parked_radius_m` 40, `max_fix_age_s` 150. `DRIVER_COACH_MAX_FIX_AGE_S` overrides the age cap for nudges and for ending a break because the truck moved.

## What is stored

In the `driver_coach` schema (never `public`):

- `idle_episode` — one row per idle stretch: `started_at`, `ended_at`, `duration_s`, `lat`, `lng`, `at_stop`, `stop_order`, `end_reason` (`moved`, `sale`, or `break`), and `nudges` (each `{level, at, message, managerAlert}`). `manager_alerted`, `manager_alerted_at`, and `manager_alert_for` (`Felix`) are the manager flag. No email is sent.
- `shift_break` — each lunch or break, including `end_reason` (`driver`, `expired`, `moving`), `duration_s`, `overrun_s`, `flagged`.
- `shift_break_flag` — `over_limit` and `overrun` rows.

`end_reason` `sale` means a Square sale arrived while the truck was still stationary. The sale closes the episode and a new clock starts at the sale time.

Apply `backend/migrations/007_no_idle_breaks.sql` to the service schema before relying on the flags. Until that migration is present the route poll still returns navigation, with `nudge: null` and an empty `break` object.
