"""Exercise the production resolver and payload boundary; mock external I/O only."""
from copy import deepcopy
import pytest
from app import routes, eta
from app.payload import build_route_payload


def stop(order, kind='neighborhood', due='5:00 PM', **kw):
    return dict(stop_order=order, kind=kind, arrive=due, address=f'Stop {order}',
                lat=37.0 + order/1000, lng=-122.0, **kw)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv('DRIVER_COACH_NEXT_STOP_INTELLIGENCE', '1')
    monkeypatch.setattr(routes, 'truck_id_for', lambda *a: None)
    monkeypatch.setattr(routes, 'read_truck_position', lambda *a, **k: dict(lat=37.0, lng=-122.0, fix_age_s=0))
    monkeypatch.setattr(routes, '_grade_stop', lambda *a: None)
    monkeypatch.setattr(routes, '_attach_live_eta', lambda *a, **k: None)
    monkeypatch.setattr(routes, 'build_map_nav', lambda *a, **k: None)
    monkeypatch.setattr(eta, 'live_eta', lambda *a, **k: dict(eta_min=5, dist_mi=1, arrive_est='2:05 PM'))
    def resolve(plan=None, now=840, served=(), skipped=(), events=(), **kw):
        plan = plan if plan is not None else [stop(1, 'school', '3:30 PM'), stop(2)]
        before = deepcopy(plan)
        result = routes.resolve_live_route(None, 7, route_cluster_id=42, dow='Monday',
            frozen_plan=plan, served_orders=set(served), skipped_orders=set(skipped),
            events=list(events), now_minutes=now, **kw)
        payload = build_route_payload(next_stop=result['next_stop'], route_cluster_id=42,
                                      phase=result['phase'], coached=False)
        assert plan == before
        return payload['route']['nextStop'], result
    return resolve


def test_early_school_defect_live_payload(live):
    ns, _ = live()
    assert ns['stopOrder'] == 2
    assert ns['reasonCode'] == 'filler_before_anchor'
    assert ns['anchor'] is False
    assert ns['anchorContext']['due'] == '3:30 PM'


def test_plan_finished_booking_remains(live):
    ns, _ = live([stop(1)], served=[1], events=[dict(id='party', title='Party', address='Event', startTime='4:00 PM', lat=37.1, lng=-122)])
    assert ns is not None and ns['anchor'] is True
    assert ns['due'] == '4:00 PM'


def test_tight_school_chain_does_not_force_early_departure(live, monkeypatch):
    # A tight later school cannot force departure for the first school at 2pm.
    monkeypatch.setattr(eta, 'live_eta', lambda *a, **k: dict(eta_min=20, dist_mi=1, arrive_est='x'))
    ns, _ = live([stop(1, 'school', '3:30 PM'), stop(2, 'school', '4:00 PM'), stop(3)])
    assert ns['stopOrder'] == 3
    assert ns['reasonCode'] == 'filler_before_anchor'
