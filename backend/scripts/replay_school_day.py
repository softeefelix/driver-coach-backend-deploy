"""Injected-clock school day through the live resolver and wire payload.
Run from backend: PYTHONPATH=. python scripts/replay_school_day.py
External GPS/ETA/grade/map I/O is fixture-backed; chooser/resolver/payload are real.
"""
import json
from unittest.mock import patch
from app import routes, eta
from app.payload import build_route_payload


def replay():
    plan = [dict(stop_order=n, kind='school' if n == 2 else 'neighborhood',
                 address=f'Stop {n}', arrive='3:30 PM' if n == 2 else '5:00 PM',
                 lat=37+n/1000, lng=-122) for n in (1, 2, 3, 4)]
    state = {}; output = []
    samples = [(840, {1}, set(), 3), (875, {1, 3}, set(), 4),
               (920, {1, 3}, set(), 2), (925, {1, 3}, set(), 2),
               (945, {1, 2, 3}, set(), 4), (960, {1, 2, 3, 4}, set(), None)]
    with patch.dict('os.environ', {'DRIVER_COACH_NEXT_STOP_INTELLIGENCE': '1'}), \
         patch.object(routes, 'truck_id_for', return_value=None), \
         patch.object(routes, 'read_truck_position', return_value=dict(lat=37, lng=-122, fix_age_s=0)), \
         patch.object(routes, '_grade_stop'), patch.object(routes, '_attach_live_eta'), \
         patch.object(routes, 'build_map_nav', return_value=None), \
         patch.object(eta, 'live_eta', return_value={'eta_min': 5}):
        for now, served, skipped, expected in samples:
            result = routes.resolve_live_route(None, 7, route_cluster_id=42, dow='Monday',
                frozen_plan=plan, now_minutes=now, served_orders=served,
                skipped_orders=skipped, chooser_state=state)
            ns = build_route_payload(next_stop=result['next_stop'], route_cluster_id=42,
                                    phase=result['phase'], coached=False)['route']['nextStop']
            actual = ns['stopOrder'] if ns else None
            assert actual == expected, (now, actual, expected)
            output.append(dict(nowMinutes=now, stopOrder=actual, reason=ns['reason'] if ns else 'Plan complete'))
    return output


if __name__ == '__main__':
    print(json.dumps(replay(), indent=2))
