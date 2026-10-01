"""Live-path scenario matrix; all decisions cross resolve_live_route -> payload."""
import importlib.util
import subprocess
from copy import deepcopy
import pytest
from app import routes, eta, school_slack
from test_school_slack_live import live, stop


def test_pre_school_done_post_school_available(live):
    ns, _ = live([stop(1), stop(2, 'school', '3:30 PM'), stop(3)], served=[1])
    assert ns['stopOrder'] == 3


def test_no_fit_truthful_early_reason(live, monkeypatch):
    monkeypatch.setattr(school_slack, 'config', lambda: school_slack.Config(filler_dwell=100))
    ns, _ = live()
    assert ns['stopOrder'] == 1
    assert 'No stop fits' in ns['reason']  # 2pm is not the 3:05 leave cutoff


def test_exactly_one_fits(live, monkeypatch):
    def travel(*args, **kw):
        return {'eta_min': 100 if args[4] == stop(2)['lat'] else 5}
    monkeypatch.setattr(eta, 'live_eta', travel)
    ns, _ = live([stop(1, 'school', '3:30 PM'), stop(2), stop(3)])
    assert ns['stopOrder'] == 3


def test_sticky_jitter_delay_binding_and_release(live, monkeypatch):
    state = {}
    plan = [stop(1, 'school', '3:30 PM'), stop(2), stop(3)]
    # Initially only filler 3 fits. Later filler 2 becomes feasible: keep 3.
    monkeypatch.setattr(eta, 'live_eta', lambda *a, **k: {'eta_min': 90 if a[4] == stop(2)['lat'] else 5})
    assert live(plan, chooser_state=state)[0]['stopOrder'] == 3
    for duration in [6, 4, 7, 3]:
        monkeypatch.setattr(eta, 'live_eta', lambda *a, d=duration, **k: {'eta_min': d})
        assert live(plan, now=880, chooser_state=state)[0]['stopOrder'] == 3
    assert live(plan, now=918, chooser_state=state)[0]['stopOrder'] == 1
    assert live(plan, now=920, chooser_state=state)[0]['stopOrder'] == 1
    assert live(plan, now=921, served=[1], chooser_state=state)[0]['stopOrder'] == 2


def test_hysteresis_enter_vs_keep(live):
    # required = 5 + 10 + 5 + 5 margin; due-buffer=915.
    state = {}
    assert live(now=888, chooser_state=state)[0]['stopOrder'] == 2
    assert live(now=889, chooser_state=state)[0]['stopOrder'] == 2
    assert live(now=889, chooser_state={})[0]['stopOrder'] == 1


@pytest.mark.parametrize('bad', [None, float('nan'), -1, True])
def test_eta_unavailable_is_legacy(live, monkeypatch, bad):
    monkeypatch.setattr(eta, 'live_eta', lambda *a, **k: {'eta_min': bad})
    ns, _ = live()
    assert ns['stopOrder'] == 1 and ns['reasonCode'] == 'eta_unavailable'


def test_eta_exception_falls_back(live, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError('transport problem')
    monkeypatch.setattr(eta, 'live_eta', fail)
    ns, _ = live()
    assert ns['stopOrder'] == 1 and ns['reasonCode'] == 'eta_unavailable'


@pytest.mark.parametrize('position', [None, {'lat': 37, 'lng': -122, 'fix_age_s': 151},
                                      {'lat': 37, 'lng': -122}])
def test_stale_or_missing_position(live, monkeypatch, position):
    monkeypatch.setattr(routes, 'read_truck_position', lambda *a, **k: position)
    ns, _ = live()
    if position is None:
        assert ns['stopOrder'] == 1 and ns['reasonCode'] == 'eta_unavailable'
    else:
        assert ns['stopOrder'] == 2 and ns['reasonCode'] == 'filler_before_anchor'


def test_no_coords_filler_skipped(live):
    missing = stop(2); missing['lat'] = None
    ns, _ = live([stop(1, 'school', '3:30 PM'), missing, stop(3)])
    assert ns['stopOrder'] == 3


def test_skipped_not_readvised(live):
    assert live(skipped=[2])[0]['stopOrder'] == 1


def test_plan_complete(live):
    assert live(served=[1, 2])[0] is None


def test_school_served(live):
    ns, _ = live(served=[1])
    assert ns['stopOrder'] == 2 and ns['reasonCode'] == 'planned'


def test_real_origin_and_ordered_feasible_two_school_chain(live, monkeypatch):
    calls = []
    def travel(*a, **kw):
        calls.append((a[2], a[4]))
        return {'eta_min': 5}
    monkeypatch.setattr(eta, 'live_eta', travel)
    plan = [stop(1, 'school', '3:30 PM'), stop(2, 'school', '4:15 PM'), stop(3)]
    ns, _ = live(plan)
    assert ns['stopOrder'] == 3
    assert (37.0, stop(3)['lat']) in calls
    assert (stop(3)['lat'], stop(1)['lat']) in calls
    assert (stop(1)['lat'], stop(2)['lat']) not in calls
    assert (stop(3)['lat'], stop(2)['lat']) not in calls
    assert ns['anchorContext']['arriveEst'] == '2:20 PM'
    assert ns['anchorContext']['leaveBy'] == '3:05 PM'


def test_event_end_window_blocks_later_anchor(live):
    event = dict(id='party', title='Party', address='Event', startTime='3:00 PM', endTime='4:00 PM', lat=37.1, lng=-122)
    ns, _ = live([stop(1, 'school', '4:15 PM'), stop(2)], events=[event])
    assert not ns['anchor'] and ns['anchorContext']['due'] == '3:00 PM'


def test_completed_event_not_readvised(live):
    event = dict(id='party', address='Event', startTime='4:00 PM', status='COMPLETED', lat=37.1, lng=-122)
    assert live([], events=[event])[0] is None


@pytest.mark.parametrize('name', ['Preparatory', 'Montessori', 'Elem.', 'Elementary', 'High School'])
def test_school_name_protected(live, name):
    row = stop(1, due='3:30 PM'); row['address'] = name
    ns, _ = live([row, stop(2)], now=925)
    assert ns['stopOrder'] == 1 and ns['anchor']


def test_invalid_config_auditable_fallback(live, monkeypatch):
    def invalid():
        raise ValueError('invalid config')
    monkeypatch.setattr(school_slack, 'config', invalid)
    state = {}
    ns, _ = live(chooser_state=state)
    assert ns['reasonCode'] == 'eta_unavailable'
    assert state['choice'] == 'stop:1'


@pytest.mark.skip(reason='Kill switch removed in 440416e; legacy revision absent from deploy repository')
def test_flag_off_full_resolver_differential(live, monkeypatch, tmp_path):
    # Execute the EXACT original resolver, not a reimplementation of its policy.
    source = subprocess.check_output(['git', 'show', '1ae2da5:backend/app/routes.py'], text=True)
    path = tmp_path / 'legacy_routes.py'; path.write_text(source)
    spec = importlib.util.spec_from_file_location('app.legacy_routes', path)
    assert spec is not None and spec.loader is not None
    legacy = importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
    for name in ['truck_id_for', 'read_truck_position', '_grade_stop', '_attach_live_eta', 'build_map_nav']:
        monkeypatch.setattr(legacy, name, getattr(routes, name))
    monkeypatch.delenv(school_slack.FLAG, raising=False)
    for plan in [[], [stop(1)], [stop(1, 'school', '3:30 PM'), stop(2)],
                 [stop(1), stop(2, 'school', '4:00 PM'), stop(3)]]:
        for now in [780, 840, 905, 960, 1020]:
            for served, skipped in [(set(), set()), ({1}, set()), (set(), {2}), ({1, 2, 3}, set())]:
                for events in [[], [dict(id='party', address='Event', startTime='4:00 PM', lat=37.1, lng=-122)]]:
                    args = dict(route_cluster_id=42, dow='Monday', frozen_plan=deepcopy(plan),
                                served_orders=served, skipped_orders=skipped, events=events, now_minutes=now)
                    assert routes.resolve_live_route(None, 7, **args) == legacy.resolve_live_route(None, 7, **args)


def test_school_day_replay():
    from scripts.replay_school_day import replay
    assert len(replay()) == 6


def test_eta_total_budget(live, monkeypatch):
    import time
    clock = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    budgets = []
    def travel(*a, **kw):
        budgets.append(kw['timeout_s'])
        clock[0] += 3
        return {'eta_min': 5}
    monkeypatch.setattr(eta, 'live_eta', travel)
    ns, _ = live()
    assert budgets == [4, 1]
    assert ns['reasonCode'] == 'eta_unavailable'


def test_plan_scope_change_resets_binding(live):
    state = {}
    assert live(now=920, chooser_state=state)[0]['anchor']
    assert live([stop(1, 'school', '5:30 PM'), stop(2)], chooser_state=state)[0]['stopOrder'] == 2


def test_duplicate_event_deduplicated(live):
    event = dict(id='party', address='Event', startTime='4:00 PM', lat=37.1, lng=-122)
    _, result = live([], events=[event, event])
    assert len(result['match_plan']) == 1


def test_event_metadata_from_real_formatter(live):
    from app.events import filter_and_format
    node = dict(id='visit1', title='Party', startAt='2026-09-30T22:00:00Z', endAt='2026-09-30T23:00:00Z',
                visitStatus='SCHEDULED', job={'jobType': 'ONE_OFF'},
                assignedUsers={'nodes': [{'name': {'full': 'Test Driver'}}]},
                property={'address': {'street': 'Event'}})
    event = filter_and_format([node], 'Test Driver')[0]
    assert event['id'] == 'visit1' and event['endTime'] == '4:00 PM'
    event.update(lat=37.1, lng=-122)
    ns, _ = live([stop(1, 'school', '4:15 PM'), stop(2)], events=[event])
    assert not ns['anchor'] and ns['anchorContext']['due'] == '3:00 PM'


def test_untimed_geotab_fix_is_not_fresh(live, monkeypatch):
    from app.geotab_live import parse_device_status
    position = parse_device_status([dict(device={'id': 'dev'}, latitude=37, longitude=-122)], 'dev')
    monkeypatch.setattr(routes, 'read_truck_position', lambda *a, **kw: position)
    assert live()[0]['reasonCode'] == 'filler_before_anchor'
