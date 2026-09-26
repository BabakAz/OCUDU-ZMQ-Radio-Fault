"""L0 finite-plan compiler/ownership contract; no broker or radio is launched."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import radio_broker_profile as profiles
import radio_broker_schedule as schedules

PROFILE = ROOT / 'config/radio_broker/fixed_reference.fixture.json'
SCHEDULE = ROOT / 'config/radio_broker/finite_schedule.fixture.json'


def inputs():
    return profiles.load_profile(PROFILE)[0], schedules.load_schedule(SCHEDULE)[0]


def test_fixture_wire_round_trip_preserves_every_direction_and_sample_boundary():
    profile, value = inputs()
    raw = schedules.compile_plan(profile, value)
    plan = schedules.parse_wire(raw)
    assert raw == schedules.compile_plan(profile, schedules.validate_schedule(profile, value))
    assert plan.sha256 == hashlib.sha256(raw).hexdigest()
    assert plan.profile_sha256 == hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest()
    for direction in ('DL', 'UL'):
        actual = plan.directions[direction]
        expected = value['directions'][direction]
        assert actual.duration_samples == expected['duration_samples']
        assert actual.base == schedules.profile_base(profile['directions'][direction])
        assert [(e.sample_offset, e.event_id, e.kind, dict(e.settings)) for e in actual.events] == [
            (e['sample_offset'], e['event_id'], e['kind'], e['settings']) for e in expected['events']]
    with pytest.raises((TypeError, FrozenInstanceError)):
        plan.directions['DL'].base['gain'] = 0
    with pytest.raises((TypeError, FrozenInstanceError)):
        plan.directions['DL'].events[0].sample_offset = 7
    with pytest.raises(TypeError):
        plan.directions['UL'] = plan.directions['DL']


@pytest.mark.parametrize('path,value', [
    (('schema_version',), 'unversioned'), (('qualification',), 'confirmed'),
    (('profile_sha256',), '0' * 64), (('trial_id',), 'bad id'),
    (('pipeline_id',), 'x\nARM'), (('study_id',), 'é'),
    (('directions','DL','duration_samples'), True),
    (('directions','DL','duration_samples'), 0),
    (('directions','DL','duration_samples'), 4097.0),
    (('directions','DL','duration_samples'), 2**63),
    (('directions','DL','events',1,'sample_offset'), 0),
    (('directions','DL','events',1,'sample_offset'), 4097),
    (('directions','DL','events',1,'sample_offset'), True),
    (('directions','DL','events',1,'event_id'), 'baseline'),
    (('directions','DL','events',1,'kind'), 'reset'),
    (('directions','DL','events',1,'settings','gain'), -1),
    (('directions','DL','events',1,'settings','noise_enabled'), 1),
    (('directions','DL','events',1,'settings','cw_freq_hz'), 12e6),
    (('directions','DL','events',1,'settings','noise_snr_db'), float('nan')),
    (('directions','DL','events',-1,'settings','gain'), 0.5),
    (('directions','DL','events',-1,'kind'), 'set'),
])
def test_unsupported_or_ambiguous_schedules_fail_before_materialization(path, value):
    profile, schedule = inputs()
    target = schedule
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(schedules.ScheduleError):
        schedules.compile_plan(profile, schedule)


def test_unknown_fields_event_limits_and_identity_mutations_are_rejected():
    profile, value = inputs()
    extra = copy.deepcopy(value)
    extra['command'] = 'anything'
    with pytest.raises(schedules.ScheduleError, match='exactly'):
        schedules.compile_plan(profile, extra)
    for count in (0, 33):
        extra = copy.deepcopy(value)
        extra['directions']['DL']['events'] = [value['directions']['DL']['events'][0]] * count
        with pytest.raises(schedules.ScheduleError, match='1..32'):
            schedules.compile_plan(profile, extra)
    profile['directions']['DL']['mode'] = 'identity'
    value['profile_sha256'] = hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest()
    with pytest.raises(schedules.ScheduleError, match='restore/identity'):
        schedules.compile_plan(profile, value)
    base = schedules.mutable_settings(schedules.profile_base(profile['directions']['DL']))
    for event in value['directions']['DL']['events']:
        event['settings'] = dict(base)
    parsed = schedules.parse_wire(schedules.compile_plan(profile, value))
    assert parsed.directions['DL'].base['noise_enabled'] is False
    assert parsed.directions['DL'].base['cw_enabled'] is False


@pytest.mark.parametrize('seconds,rate,expected', [
    ('30', 23_040_000, 691_200_000), ('0.001', 23_040_000, 23040),
    ('1.500', 1000, 1500), ('0', 23_040_000, 0),
])
def test_duration_conversion_is_integer_and_exact(seconds, rate, expected):
    assert schedules.seconds_to_samples(seconds, rate) == expected


@pytest.mark.parametrize('seconds,rate', [
    ('0.0000001', 23_040_000), ('-1', 1000), ('1e3', 1000), ('01', 1000),
    (0.1, 1000), ('NaN', 1000), ('1', True), ('999999999999999999', 250e6),
])
def test_fractional_samples_and_noncanonical_durations_cannot_be_rounded(seconds, rate):
    with pytest.raises(schedules.ScheduleError):
        schedules.seconds_to_samples(seconds, rate)


@pytest.mark.parametrize('mutate', [
    lambda b:b.replace(b'\n', b'\r\n'),
    lambda b:b.rstrip(b'\n'), lambda b:b + b'junk\n',
    lambda b:b.replace(b'direction DL', b'direction UL'),
    lambda b:b.replace(b'event 17 ', b'event 017 '),
    lambda b:b.replace(b'event 17 ', b'event +17 '),
    lambda b:b.replace(b'event 17 ', b'event 18446744073709551616 '),
    lambda b:b.replace(b'0x1.0000000000000p+0', b'1.0',1),
    lambda b:b.replace(b'0x1.0000000000000p+0', b'0x1.0p+999999',1),
    lambda b:b.replace(b'ids ', b'ids  '),
    lambda b:b.replace(b'profile ', b'profile\t'),
    lambda b:b.replace(b'baseline', b'bad\x00id'),
])
def test_wire_cannot_hide_overflow_truncation_unknown_fields_or_token_changes(mutate):
    profile, value = inputs()
    with pytest.raises(schedules.ScheduleError):
        schedules.parse_wire(mutate(schedules.compile_plan(profile,value)))


def test_json_loading_rejects_duplicates_nonfinite_and_oversized_files(tmp_path):
    path = tmp_path / 'schedule'
    for content in ('{"events":[],"events":[]}', '{"a":NaN}', ' ' * (schedules.MAX_PLAN_BYTES + 1)):
        path.write_text(content)
        with pytest.raises((ValueError, schedules.ScheduleError)):
            schedules.load_schedule(path)


@pytest.fixture
def private_dir():
    with tempfile.TemporaryDirectory(prefix='rad6-') as directory:
        yield Path(directory)


def test_prepare_is_exclusive_private_and_does_not_launch_or_publish_credentials(private_dir):
    result = schedules.prepare(PROFILE, SCHEDULE, private_dir)
    assert '--no-gui' in result['broker_arguments']['grc']
    assert '--no-gui' not in result['broker_arguments']['c']
    assert sorted(p.name for p in private_dir.iterdir()) == ['control.token','plan.wire']
    for path in private_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    token = (private_dir / 'control.token').read_text()
    assert len(token) == 64 and token not in json.dumps(result)
    assert schedules.load_wire(private_dir / 'plan.wire').sha256 == result['plan_sha256']
    for backend in ('c','grc'):
        assert result['broker_arguments'][backend][-4:] == [
            '--radio-plan-file',str(private_dir/'plan.wire'),'--radio-control-dir',str(private_dir)]
    original = {p.name:p.read_bytes() for p in private_dir.iterdir()}
    with pytest.raises(schedules.ScheduleError, match='empty'):
        schedules.prepare(PROFILE,SCHEDULE,private_dir)
    assert {p.name:p.read_bytes() for p in private_dir.iterdir()} == original


def test_private_wire_rejects_symlink_fifo_public_mode_and_hardlink(private_dir):
    schedules.prepare(PROFILE,SCHEDULE,private_dir)
    wire = private_dir/'plan.wire'
    link = private_dir/'link';link.symlink_to(wire)
    with pytest.raises(OSError):schedules.load_wire(link)
    fifo = private_dir/'fifo';os.mkfifo(fifo,0o600)
    with pytest.raises(schedules.ScheduleError,match='regular'):schedules.load_wire(fifo)
    wire.chmod(0o644)
    with pytest.raises(schedules.ScheduleError,match='mode0600'):schedules.load_wire(wire)
    wire.chmod(0o600)
    os.link(wire,private_dir/'hardlink')
    with pytest.raises(schedules.ScheduleError,match='one link'):schedules.load_wire(wire)


def test_prepare_rejects_unsafe_directory_without_removing_its_contents(private_dir):
    private_dir.chmod(0o755)
    sentinel=private_dir/'keep';sentinel.write_text('user data')
    with pytest.raises(schedules.ScheduleError,match='mode0700'):
        schedules.prepare(PROFILE,SCHEDULE,private_dir)
    assert sentinel.read_text()=='user data'
