"""TDL-C model selection, strict transport scope and final truth checks."""
import collections
import hashlib
import json
import os
import subprocess
import sys
import threading

import numpy as np
import pytest

from test_radio_broker_grc_cfo import MemoryTruth, broker, profiles, schedules, runtime, ROOT
from radio_static_tdl import TdlReferenceChannel, TdlCReferenceChannel


def inputs(model='c'):
    profile = json.loads((ROOT / 'config/radio_broker/fixed_reference.fixture.json').read_text())
    profile.update(schema_version=profiles.TDL_C_SCHEMA if model == 'c' else profiles.TDL_SCHEMA,
                   channel_semantics_version=profiles.TDL_C_SEMANTICS if model == 'c' else profiles.TDL_SEMANTICS,
                   sample_rate_hz=23_040_000, master_seed=41)
    for name, leg in profile['directions'].items():
        leg.update(mode='identity' if name == 'DL' else 'fixed', reference_power=1, desired_gain=1,
                   tdl_enabled=False, noise=dict(enabled=False, snr_db=0),
                   cw=dict(enabled=False, sir_db=0, frequency_hz=0))
    schedule = dict(schema_version=schedules.TDL_C_SCHEMA if model == 'c' else schedules.TDL_SCHEMA,
                    qualification='development_only',
                    profile_sha256=hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest(),
                    study_id='tdl_transport', protocol_id='tdl_transport', trial_id='fixture',
                    pipeline_id='local', directions={})
    for name in ('DL', 'UL'):
        base = schedules.mutable_settings(schedules.profile_base(profile['directions'][name]))
        schedule['directions'][name] = dict(duration_samples=4097, events=[
            dict(sample_offset=0, event_id='baseline', kind='set', settings=dict(base)),
            dict(sample_offset=17, event_id='onset', kind='set', settings=dict(base, tdl_enabled=name == 'UL')),
            dict(sample_offset=2048, event_id='restore', kind='restore', settings=dict(base))])
    return profile, schedule


def private_inputs(tmp_path, model='c'):
    profile, schedule = inputs(model)
    directory = tmp_path / 'radio'
    directory.mkdir(mode=0o700)
    (directory / 'plan.wire').write_bytes(schedules.compile_plan(profile, schedule))
    (directory / 'plan.wire').chmod(0o600)
    (directory / 'control.token').write_bytes(b'a' * 64)
    (directory / 'control.token').chmod(0o600)
    return profile, schedules.load_wire(directory / 'plan.wire'), directory


def test_existing_tdl_a_profile_and_wire_bytes_preserved():
    # Computed before adding transport C support from the already deployed A code.
    profile, schedule = inputs('a')
    assert hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest() == '8dfa8b2d3999a09689ae90079d38ffea302fd51b2b4d640ea19237782dbeda45'
    assert hashlib.sha256(schedules.compile_plan(profile, schedule)).hexdigest() == 'd2307ccc1a4b75fed86e208da205a6d84592525bb934e6404abbc878ec57ad24'


def test_c_wire_selects_c_and_keeps_only_grc_preparation(tmp_path):
    profile, schedule = inputs()
    raw = schedules.compile_plan(profile, schedule)
    assert raw.startswith(b'RADIO_SCHEDULE_GRC_STATIC_TDL_C_WIRE_V1\n')
    plan = schedules.parse_wire(raw)
    assert plan.channel_semantics_version == profiles.TDL_C_SEMANTICS
    assert plan.directions['UL'].events[1].settings['tdl_enabled'] is True
    assert broker.study_channel_type(plan.channel_semantics_version) is TdlCReferenceChannel
    with pytest.raises(profiles.ProfileError, match='grc backend'):
        profiles.broker_arguments(profile, 'c')
    for name, obj in [('profile.json', profile), ('schedule.json', schedule)]:
        (tmp_path / name).write_text(json.dumps(obj))
    directory = tmp_path / 'radio'; directory.mkdir(mode=0o700)
    prepared = schedules.prepare(tmp_path / 'profile.json', tmp_path / 'schedule.json', directory)
    assert set(prepared['broker_arguments']) == {'grc'}
    assert profiles.TDL_C_SEMANTICS in prepared['broker_arguments']['grc']


@pytest.mark.parametrize('field,value', [
    ('schema_version', profiles.TDL_SCHEMA), ('channel_semantics_version', profiles.TDL_SEMANTICS),
    ('sample_rate_hz', 11_520_000), ('delay_spread_ns', 300), ('max_doppler_hz', 0)])
def test_c_profile_rejects_wrong_model_rate_and_extra_knobs(field, value):
    profile, _ = inputs(); profile[field] = value
    with pytest.raises(profiles.ProfileError):
        profiles.validate_profile(profile)


@pytest.mark.parametrize('value', [1, 'true', None])
def test_c_enable_requires_boolean(value):
    profile, schedule = inputs()
    profile['directions']['UL']['tdl_enabled'] = value
    with pytest.raises(profiles.ProfileError):
        profiles.validate_profile(profile)
    profile, _ = inputs()
    schedule['directions']['UL']['events'][1]['settings']['tdl_enabled'] = value
    with pytest.raises(schedules.ScheduleError):
        schedules.compile_plan(profile, schedule)


@pytest.mark.parametrize('header', [schedules.WIRE_VERSION, schedules.CFO_WIRE_VERSION, 'RADIO_SCHEDULE_GRC_STATIC_TDL_D_WIRE_V1'])
def test_c_wire_rejects_wrong_layout_or_unknown_header(header):
    profile, schedule = inputs()
    raw = schedules.compile_plan(profile, schedule)
    with pytest.raises(schedules.ScheduleError):
        schedules.parse_wire(raw.replace(schedules.TDL_C_WIRE_VERSION.encode(), header.encode()))


def test_c_profile_rejects_a_schedule():
    profile, schedule = inputs(); schedule['schema_version'] = schedules.TDL_SCHEMA
    with pytest.raises(schedules.ScheduleError, match='unsupported schedule schema'):
        schedules.compile_plan(profile, schedule)


@pytest.mark.parametrize('model,correct,wrong', [('c', TdlCReferenceChannel, TdlReferenceChannel),
                                             ('a', TdlReferenceChannel, TdlCReferenceChannel)])
def test_runtime_rejects_wrong_model_despite_identical_settings(tmp_path, model, correct, wrong):
    _, plan, directory = private_inputs(tmp_path, model)
    channels = {name: correct(name, plan.sample_rate_hz, plan.master_seed, **dict(leg.base))
                for name, leg in plan.directions.items()}
    runtime.validate_inputs(directory / 'plan.wire', directory, channels)
    channels['UL'] = wrong('UL', plan.sample_rate_hz, plan.master_seed, **dict(plan.directions['UL'].base))
    with pytest.raises(ValueError, match='initial_channel_semantics_mismatch'):
        runtime.validate_inputs(directory / 'plan.wire', directory, channels)
    assert sorted(p.name for p in directory.iterdir()) == ['control.token', 'plan.wire']


def test_c_metrics_rejected_before_output_creation(tmp_path):
    _, plan, directory = private_inputs(tmp_path)
    channels = {name: TdlCReferenceChannel(name, plan.sample_rate_hz, 41, **dict(leg.base))
                for name, leg in plan.directions.items()}
    with pytest.raises(ValueError, match='metrics_unsupported'):
        runtime.validate_inputs(directory / 'plan.wire', directory, channels, metrics_every=1)
    assert sorted(p.name for p in directory.iterdir()) == ['control.token', 'plan.wire']


def test_c_cli_realization_and_scope():
    profile, _ = inputs()
    base = [sys.executable, str(ROOT / 'scripts/ocudu_channel_broker.py'), '--no-gui',
            *profiles.broker_arguments(profile, 'grc'), '--validate-config-only']
    result = subprocess.run(base, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    validated = json.loads(result.stdout.removeprefix('RADIO_CONFIG_VALIDATED: '))
    realization = validated['directions']['UL']['realization']
    assert realization['channel_semantics_version'] == profiles.TDL_C_SEMANTICS
    assert realization['numerical_recipe']['profile'] == 'tdl-c'
    assert realization['numerical_recipe']['settings']['delay_spread_ns'] == 300
    assert realization['common_delay_samples'] == 15
    for flags in (['--doppler', '1'], ['--ul-cfo', '1'], ['--profile', 'epa'],
                  ['--radio-metrics-every-messages', '1'], ['--ul-tdl-enabled', '--ul-tdl-enabled']):
        failed = subprocess.run(base + flags, capture_output=True, text=True, timeout=10)
        assert failed.returncode != 0 and 'RADIO_CONFIG_VALIDATED:' not in failed.stdout


def test_actual_c_parser_rejects_c_model_wire(tmp_path):
    _, _, directory = private_inputs(tmp_path)
    program = tmp_path / 'c-broker'
    subprocess.run(['clang-18', '-std=c17', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-Werror',
                    str(ROOT / 'scripts/zmq_channel_broker.c'), '-o', str(program),
                    '-lzmq', '-lm', '-pthread'], check=True, capture_output=True, timeout=30)
    original = profiles.load_profile(ROOT / 'config/radio_broker/fixed_reference.fixture.json')[0]
    result = subprocess.run([str(program), *profiles.broker_arguments(original, 'c'),
        '--radio-plan-file', str(directory / 'plan.wire'), '--radio-control-dir', str(directory),
        '--validate-config-only'], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'RADIO_CONFIG_VALIDATED:' not in result.stdout
    assert sorted(p.name for p in directory.iterdir()) == ['control.token', 'plan.wire']


@pytest.mark.parametrize('forward', [True, False])
def test_c_scheduled_final_exposure_and_forwarding(forward, tmp_path):
    profile, schedule = inputs()
    plan = schedules.parse_wire(schedules.compile_plan(profile, schedule))
    channels = {name: TdlCReferenceChannel(name, plan.sample_rate_hz, 41, **dict(leg.base))
                for name, leg in plan.directions.items()}
    engine = runtime.ScheduleRuntime((plan, tmp_path, b'0' * 64), channels,
                                     threading.Event(), collections.deque(maxlen=1))
    engine.writer = MemoryTruth(); engine.ready = True
    ledgers = {name: broker.RelayAccounting(name, name == 'DL') for name in channels}
    packet = f'RBCTRL1 1 ARM {engine.instance_id} {plan.sha256} {"0" * 64}\n'.encode()
    assert json.loads(engine.control_packet(packet, os.geteuid()))['ok']
    count = 5003 if forward else 18
    for name, ledger in ledgers.items():
        ledger.record_input(count); engine.input_received(name, ledger)
        output = engine.process(name, np.ones(count, np.complex64))
        assert len(output) == count
        if forward:
            ledger.record_output(count); engine.forwarded(name, ledger)
        engine.relay_final(name, ledger)
    assert engine.finish() is forward
    final = next(r['details'] for r in engine.writer.records if r['direction'] == 'UL' and r['event_type'] == 'final')
    state = final['state_at_finish']
    assert state['channel_semantics_version'] == profiles.TDL_C_SEMANTICS
    assert state['tdl_applied_samples'] == (2031 if forward else 1)
    assert state['tdl_enabled'] is (not forward)
    assert final['restoration_observed'] is forward
    assert final['forwarded_samples'] == (count if forward else 0)
