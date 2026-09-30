"""Item 2 only: protected-anchor advice, never edits plan/statuses.

Legacy advise_plan is deliberately untouched for the kill-switch differential.
Travel is supplied by the existing driving-traffic adapter in the live resolver.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import re

from .advisor import advise_plan, clock_minutes, insert_events
from .payload import fmt_clock_ampm


def enabled():
    return True


@dataclass(frozen=True)
class Config:
    buffer: float = 15
    filler_dwell: float = 10
    anchor_dwell: float = 10
    margin: float = 5
    hysteresis: float = 2
    max_fix_age: float = 150


def config():
    return Config()


def identity(stop):
    if not stop:
        return None
    return ('event:' + str(stop['event_id']) if stop.get('kind') == 'event'
            else 'stop:' + str(stop.get('stop_order')))


def minutes(value):
    return clock_minutes(fmt_clock_ampm(value))


def clock(value):
    if value is None:
        return None
    n = math.floor(value) % 1440
    return f'{n // 60 % 12 or 12}:{n % 60:02d} {"PM" if n >= 720 else "AM"}'


def school_name(value):
    # Preserve existing matches; additional whole words avoid e.g. "preparation".
    text = str(value or '').lower()
    return ('school' in text or 'elementary' in text or
            bool(re.search(r'\b(academy|prep|preparatory|montessori|elem)\b', text)))


def coordinates(row):
    if not isinstance(row, dict):
        return False
    for key, limit in [('lat', 90), ('lng', 180)]:
        n = row.get(key)
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or abs(n) > limit:
            return False
    return True


class Unavailable(Exception):
    pass


def choose(plan, *, served_orders, skipped_orders, events, now_minutes,
           position, travel, previous=None, cfg=None):
    """Return advice plus audit inputs and next sticky state (no I/O).

    Acceptance needs 2 minutes of spare slack to ENTER a filler, zero to KEEP it.
    A binding anchor is latched until explicit status removal. Every safety poll
    checks its current filler, not a fresh ranking; a delay preempts immediately.
    """
    invalid_config = False
    try:
        cfg = cfg or config()
    except (ValueError, TypeError, OverflowError):
        cfg = Config()
        invalid_config = True
    previous = previous or {}
    legacy = advise_plan(plan, served_orders=served_orders, skipped_orders=skipped_orders,
                         events=events, now_minutes=now_minutes)
    eligible_events = []
    seen_events = set()
    for event in events:
        if str(event.get('status', '')).upper() in {'COMPLETED', 'CANCELLED', 'CANCELED'}:
            continue
        event = dict(event)
        # Jobber id is authoritative. Older cached rows get a stable composite,
        # not title alone (two parties can have the same title).
        event['id'] = event.get('id') or '|'.join(str(event.get(k) or '')
            for k in ('title', 'address', 'startTime'))
        if event['id'] not in seen_events:
            seen_events.add(event['id'])
            eligible_events.append(event)
    rows = insert_events(plan, eligible_events)
    for row in rows:
        if row.get('kind') == 'event':
            source = next((e for e in eligible_events if
                str(e.get('id') or e.get('title') or e.get('address')) == row['event_id']), {})
            row['end_time'] = source.get('endTime')
            row['window_start'] = source.get('startTime')
    for row in rows:
        if row.get('kind') != 'event' and school_name(row.get('address')):
            row['kind'] = 'school'
    active = [r for r in rows if r.get('kind') == 'event' or
              r.get('stop_order') not in served_orders | skipped_orders]
    anchors = sorted([r for r in active if r.get('kind') in {'school', 'event'}],
                     key=lambda r: (minutes(r.get('arrive')) is None, minutes(r.get('arrive')) or 0))
    by_id = {identity(r): r for r in active}
    audit = dict(nowMinutes=now_minutes, position=position, config=asdict(cfg),
                 served=sorted(served_orders), skipped=sorted(skipped_orders),
                 anchors=deepcopy(anchors), candidates=[], legs=[])

    def finish(stop, reason, code, context=None, binding=None):
        state = dict(choice=identity(stop), sticky=identity(stop), binding=binding)
        audit.update(choice=identity(stop), reason=reason, reasonCode=code)
        return dict(plan=rows, remaining=active, next_stop=stop, reason=reason,
                    reason_code=code, anchor_context=context, state=state, audit=audit)

    def fallback(detail):
        audit['fallback'] = detail
        out = finish(legacy['next_stop'], legacy['reason'], 'eta_unavailable')
        # Keep binding/filler memory across transient ETA failures.
        out['state'] = dict(previous, choice=identity(legacy['next_stop']))
        return out

    if invalid_config:
        return fallback('invalid chooser configuration')
    if not active:
        return finish(None, 'Plan complete', 'plan_complete')
    if not anchors:
        ordinary = next((by_id.get('stop:' + str(p.get('stop_order'))) for p in plan
                         if 'stop:' + str(p.get('stop_order')) in by_id), None)
        return finish(ordinary, 'Next planned stop', 'planned')

    first = anchors[0]
    if previous.get('binding') in {identity(a) for a in anchors}:
        first = by_id[previous['binding']]
    due = minutes(first.get('arrive'))
    try:
        age = (position or {}).get('fix_age_s')
        if (not coordinates(position) or not isinstance(age, (int, float)) or
                not math.isfinite(age) or not 0 <= age <= cfg.max_fix_age):
            raise Unavailable('missing or stale truck position')
        if any(minutes(a.get('arrive')) is None for a in anchors):
            raise Unavailable('anchor has no usable booked time')
        cache = {}

        def leg(origin, target):
            if not coordinates(origin) or not coordinates(target):
                raise Unavailable('anchor or origin coordinates unavailable')
            key = (origin['lat'], origin['lng'], target['lat'], target['lng'])
            if key not in cache:
                try:
                    value = travel(origin, target)
                except Exception as exc:
                    raise Unavailable('travel adapter failed') from exc
                if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                        not math.isfinite(value) or value < 0):
                    raise Unavailable('travel unavailable')
                cache[key] = value
                audit['legs'].append(dict(origin=identity(origin), target=identity(target), minutes=value))
            return cache[key]

        direct = leg(position, first)
        direct_context = dict(name=first.get('title') or first.get('address'),
            due=fmt_clock_ampm(first.get('arrive')), leaveBy=clock(due-cfg.buffer-cfg.margin-direct),
            driveMin=direct, arriveEst=clock(now_minutes+direct), kind=first['kind'])

        def bind(reason):
            return finish(first, reason, 'anchor_binding', direct_context, identity(first))

        if previous.get('binding') == identity(first):
            urgent = now_minutes + direct + cfg.margin >= due - cfg.buffer
            return bind(('Leave now' if urgent else 'Anchor next') +
                        f' · due {fmt_clock_ampm(first.get("arrive"))}')
        if now_minutes + direct + cfg.margin >= due - cfg.buffer:
            return bind(f'Leave now · due {fmt_clock_ampm(first.get("arrive"))}')

        # Fillers retain the frozen plan order, NOT the time-sorted display order.
        fillers = [by_id['stop:' + str(p.get('stop_order'))] for p in plan
                   if 'stop:' + str(p.get('stop_order')) in by_id
                   and by_id['stop:' + str(p.get('stop_order'))].get('kind') not in {'school', 'event'}]
        sticky = next((f for f in fillers if identity(f) == previous.get('sticky', previous.get('choice'))), None)
        if sticky:
            fillers = [sticky] + [f for f in fillers if f is not sticky]
        for filler in fillers:
            record = dict(id=identity(filler), feasible=False)
            audit['candidates'].append(record)
            if not coordinates(filler):
                record['reason'] = 'coordinates_missing'
                continue
            elapsed = now_minutes + leg(position, filler) + cfg.filler_dwell
            departure = elapsed
            origin = filler
            slack = math.inf
            chain = []
            for anchor in anchors:
                drive = leg(origin, anchor)
                arrival = elapsed + drive + cfg.margin
                booked = minutes(anchor.get('arrive'))
                anchor_slack = booked - cfg.buffer - arrival
                slack = min(slack, anchor_slack)
                # Arrive early, WAIT until window; never serve a booking early.
                window = minutes(anchor.get('window_start'))
                service_start = max(arrival, booked, window or booked)
                end = minutes(anchor.get('end_time')) or minutes(anchor.get('leave_by'))
                elapsed = max(service_start + cfg.anchor_dwell, end or 0)
                chain.append(dict(id=identity(anchor), arrive=arrival, serviceStart=service_start,
                                  depart=elapsed, slack=anchor_slack, drive=drive))
                origin = anchor
            threshold = 0 if filler is sticky else cfg.hysteresis
            record.update(slack=slack, chain=chain, feasible=slack >= threshold)
            if slack >= threshold:
                drive = chain[0]['drive']
                context = dict(name=anchors[0].get('title') or anchors[0].get('address'),
                    due=fmt_clock_ampm(anchors[0].get('arrive')), kind=anchors[0]['kind'],
                    leaveBy=clock(minutes(anchors[0].get('arrive'))-cfg.buffer-cfg.margin-drive),
                    driveMin=drive, arriveEst=clock(departure+drive))
                label = 'school' if anchors[0]['kind'] == 'school' else 'booking'
                return finish(filler, f'Time before {label} · This stop fits first',
                              'filler_before_anchor', context)
            # Once an active filler becomes unsafe, bind; do not switch to another
            # marginal filler and oscillate with traffic estimates.
            if filler is sticky:
                break
        return bind(f'No stop fits first · due {fmt_clock_ampm(first.get("arrive"))}')
    except (Unavailable, ValueError, TypeError, OverflowError):
        return fallback('unreliable position, booking, or travel')
