"""GRC-only finite CFO contract: independent phase oracle, no radio stack."""
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ocudu_channel_broker as broker
import radio_broker_profile as profiles
import radio_broker_schedule as schedules
import radio_schedule_runtime as runtime


def inputs(offset=500.0):
    profile = json.loads((ROOT / 'config/radio_broker/fixed_reference.fixture.json').read_text())
    profile.update(schema_version=profiles.CFO_SCHEMA, channel_semantics_version=profiles.CFO_SEMANTICS)
    for name, leg in profile['directions'].items():
        leg.update(mode='identity' if name == 'DL' else 'fixed', cfo_hz=0.0)
        leg['noise']['enabled'] = False
        leg['cw'].update(enabled=False, frequency_hz=0.0)
    schedule = dict(schema_version=schedules.CFO_SCHEMA, qualification='development_only',
                    profile_sha256=hashlib.sha256(profiles.canonical_bytes(profiles.validate_profile(profile))).hexdigest(),
                    study_id='cfo_l1', protocol_id='cfo_l1', trial_id='fixture', pipeline_id='local', directions={})
    for name in ('DL', 'UL'):
        base = schedules.mutable_settings(schedules.profile_base(profile['directions'][name]))
        pulse = dict(base, cfo_hz=offset if name == 'UL' else 0.0)
        schedule['directions'][name] = dict(duration_samples=4097, events=[
            dict(sample_offset=0, event_id='baseline', kind='set', settings=dict(base)),
            dict(sample_offset=17, event_id='onset', kind='set', settings=pulse),
            dict(sample_offset=2048, event_id='restore', kind='restore', settings=dict(base))])
    return profile, schedule


def test_original_v1_profile_and_wire_golden_bytes_are_unchanged():
    # These hashes were independently checked against the pre-change HEAD modules.
    profile = profiles.load_profile(ROOT / 'config/radio_broker/fixed_reference.fixture.json')[0]
    schedule = schedules.load_schedule(ROOT / 'config/radio_broker/finite_schedule.fixture.json')[0]
    assert hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest() == '518b2e236b18fadcf09dc25aff6adc3665e891d8b0d65c3247759093b2adfa5b'
    assert hashlib.sha256(schedules.compile_plan(profile, schedule)).hexdigest() == '09ed1cf9e18ed42fa660e2449274ebb10b56c6015b084b4be2ffbf6275b7477a'


def test_cfo_plan_round_trip_and_c_backend_rejection():
    profile, schedule = inputs()
    raw = schedules.compile_plan(profile, schedule)
    assert raw.startswith(b'RADIO_SCHEDULE_GRC_CFO_WIRE_V1\n')
    plan = schedules.parse_wire(raw)
    assert plan.channel_semantics_version == 'grc_cfo_v1'
    assert plan.directions['UL'].events[1].settings['cfo_hz'] == 500.0
    assert plan.directions['DL'].events[1].settings['cfo_hz'] == 0.0
    assert '--ul-cfo' in profiles.broker_arguments(profile, 'grc')
    with pytest.raises(profiles.ProfileError, match='grc backend'):
        profiles.broker_arguments(profile, 'c')
    with pytest.raises(schedules.ScheduleError):
        schedules.parse_wire(raw.replace(b'RADIO_SCHEDULE_GRC_CFO_WIRE_V1', b'RADIO_SCHEDULE_WIRE_V1'))


def test_actual_native_c_parser_rejects_cfo_wire_before_transport(tmp_path):
    program = tmp_path / 'c-broker'
    subprocess.run(['clang-18', '-std=c17', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-Werror',
                    str(ROOT / 'scripts/zmq_channel_broker.c'), '-o', str(program),
                    '-lzmq', '-lm', '-pthread'], check=True, capture_output=True, timeout=30)
    private = tmp_path / 'control'
    private.mkdir(mode=0o700)
    profile, schedule = inputs()
    (private / 'plan.wire').write_bytes(schedules.compile_plan(profile, schedule))
    (private / 'plan.wire').chmod(0o600)
    (private / 'control.token').write_text('a' * 64)
    (private / 'control.token').chmod(0o600)
    original = profiles.load_profile(ROOT / 'config/radio_broker/fixed_reference.fixture.json')[0]
    result = subprocess.run([str(program), *profiles.broker_arguments(original, 'c'),
                             '--radio-plan-file', str(private / 'plan.wire'),
                             '--radio-control-dir', str(private), '--validate-config-only'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'RADIO_CONFIG_VALIDATED:' not in result.stdout
    assert {path.name for path in private.iterdir()} == {'plan.wire', 'control.token'}


@pytest.mark.parametrize('offset', [True, float('nan'), float('inf'), -501, 501, 0.001, -0.001, '500'])
def test_invalid_cfo_fails_at_profile_schedule_and_core(offset):
    profile, schedule = inputs()
    profile['directions']['UL']['cfo_hz'] = offset
    with pytest.raises(profiles.ProfileError):
        profiles.validate_profile(profile)
    profile, schedule = inputs()
    schedule['directions']['UL']['events'][1]['settings']['cfo_hz'] = offset
    with pytest.raises(schedules.ScheduleError):
        schedules.compile_plan(profile, schedule)
    with pytest.raises(ValueError):
        broker.CfoReferenceChannel('UL', 23_040_000, 41, ref_power=1, cfo_hz=offset)


@pytest.mark.parametrize('change', [dict(gain=0.5), dict(noise_enabled=True), dict(cw_enabled=True),
                                  dict(cw_freq_hz=7), dict(mode='identity', cfo_hz=500)])
def test_other_impairments_and_identity_cfo_fail_before_processing(change):
    with pytest.raises(ValueError):
        broker.CfoReferenceChannel('UL', 23_040_000, 41, ref_power=1, **change)


def test_coefficient_validation_is_atomic_and_zero_cfo_is_byte_identity():
    core = broker.CfoReferenceChannel('UL', 23_040_000, 41, ref_power=1)
    samples = np.array([0x80000000, 0, 0, 0x80000000, 0x3f800000, 0xbf800000], np.uint32).view(np.complex64)
    before = core.record('started')
    assert core.process(samples).tobytes() == samples.tobytes()
    settings = schedules.mutable_settings(core.config)
    for invalid in (dict(settings, noise_enabled=True), dict(settings, cfo_hz=0.001),
                    dict(settings, cfo_hz=True), dict(settings, gain=0.25)):
        config = dict(core.config)
        with pytest.raises(ValueError):
            core.update_settings(invalid)
        assert dict(core.config) == config
    after = core.record('final')
    assert after['sample_clock'] == 3 and after['cfo_applied_samples'] == 0
    assert after['awgn_state_hex'] == before['awgn_state_hex']
    assert after['rng_algorithm'] == 'none_cfo_only'
    assert after['awgn_complex_draws'] == after['awgn_normal_draws'] == 0


@pytest.mark.parametrize('counter', ['sample_clock', 'cfo_applied_samples'])
def test_cfo_counter_overflow_fails_without_mutating_phase_or_state(counter):
    core = broker.CfoReferenceChannel('UL', 23_040_000, 41, ref_power=1, cfo_hz=500)
    setattr(core, counter, 2 ** 64 - 1)
    before = core.record('started')
    with pytest.raises(OverflowError):
        core.process(np.ones(1, np.complex64))
    after = core.record('started')
    assert after == before


def test_incomplete_cfo_run_retains_actual_active_setting_and_exposure(tmp_path):
    profile, schedule = inputs()
    plan = schedules.parse_wire(schedules.compile_plan(profile, schedule))
    channels = {name: broker.CfoReferenceChannel(name, plan.sample_rate_hz, plan.master_seed,
                                               **dict(plan.directions[name].base)) for name in ('DL', 'UL')}
    engine = runtime.ScheduleRuntime((plan, tmp_path, b'0' * 64), channels,
                                     threading.Event(), collections.deque(maxlen=1))
    engine.writer = MemoryTruth()
    engine.ready = True
    packet = f'RBCTRL1 1 ARM {engine.instance_id} {plan.sha256} {"0" * 64}\n'.encode()
    assert json.loads(engine.control_packet(packet, os.geteuid()))['ok']
    ledger = broker.RelayAccounting('UL', False)
    ledger.record_input(18)
    engine.process('UL', np.ones(18, np.complex64))
    engine.relay_final('UL', ledger)
    assert not engine.finish()
    final = next(r['details'] for r in engine.writer.records
                 if r['event_type'] == 'final' and r['direction'] == 'UL')
    assert final['status'] == 'incomplete' and not final['schedule_complete']
    assert not final['restoration_observed']
    assert final['state_at_finish']['cfo_hz'] == 500
    assert final['state_at_finish']['cfo_applied_samples'] == 1
    assert final['processed_samples'] == 18 and final['forwarded_samples'] == 0


@pytest.mark.parametrize('frequency', [-500.0, -0.01, 0.01, 500.0])
def test_existing_cfo_core_matches_signed_absolute_phase_oracle_across_chunks(frequency):
    core = broker.CfoReferenceChannel('UL', 23_040_000, 41, ref_power=1, cfo_hz=frequency)
    n = 100_003
    sample = (np.arange(n) % 13 - 6 + 1j * (np.arange(n) % 7 - 3)).astype(np.complex64)
    expected = sample.astype(np.complex128) * np.exp(2j * np.pi * frequency * np.arange(n) / 23_040_000)
    chunks, start, index = [], 0, 0
    pattern = (1, 31, 257, 23040)
    while start < n:
        stop = min(n, start + pattern[index % len(pattern)])
        chunks.append(core.process(sample[start:stop]))
        assert len(core.process(np.empty(0, np.complex64))) == 0
        start, index = stop, index + 1
    output = np.concatenate(chunks)
    np.testing.assert_allclose(output, expected, rtol=2e-7, atol=1e-6)
    assert core.cfo_applied_samples == core.sample_clock == n
    angle_error = (core.cfo_phase[0] - 2 * math.pi * frequency * n / 23_040_000 + math.pi) % (2 * math.pi) - math.pi
    assert abs(angle_error) < 1e-12
    assert math.isclose(core.energies['output'], core.energies['input'], rel_tol=2e-7)
    assert core.energies['noise'] == core.energies['cw'] == 0


class MemoryTruth:
    def __init__(self):
        self.records = []
        self.failed = threading.Event()
        self.logging_errors = 0

    def enqueue(self, value):
        assert len(json.dumps(value, allow_nan=False).encode()) <= runtime.MAX_RECORD_BYTES
        self.records.append(value)
        return True

    def finish(self):
        return True


@pytest.mark.parametrize('offset', [0.0, 500.0])
@pytest.mark.parametrize('pattern', [(5003,), (1, 31, 257, 801)])
def test_authenticated_segment_schedule_exact_support_restoration_and_final_truth(tmp_path, offset, pattern):
    profile, schedule = inputs(offset)
    plan = schedules.parse_wire(schedules.compile_plan(profile, schedule))
    channels = {name: broker.CfoReferenceChannel(name, plan.sample_rate_hz, plan.master_seed,
                                               **dict(plan.directions[name].base)) for name in ('DL', 'UL')}
    engine = runtime.ScheduleRuntime((plan, tmp_path, b'0' * 64), channels,
                                     threading.Event(), collections.deque(maxlen=1))
    engine.writer = MemoryTruth()
    engine.ready = True
    ledgers = {name: broker.RelayAccounting(name, name == 'DL') for name in channels}

    def exchange(name, samples):
        ledger = ledgers[name]
        ledger.record_input(len(samples))
        engine.input_received(name, ledger)
        result = engine.process(name, samples)
        ledger.record_output(len(samples))
        engine.forwarded(name, ledger)
        return result

    for name, n in [('DL', 11), ('UL', 19)]:
        warmup = np.ones(n, np.complex64)
        assert exchange(name, warmup).tobytes() == warmup.tobytes()
    packet = f'RBCTRL1 1 ARM {engine.instance_id} {plan.sha256} {"0" * 64}\n'.encode()
    assert json.loads(engine.control_packet(packet, os.geteuid()))['ok']
    iq = (np.arange(5003) % 7 - 3 + 1j * (np.arange(5003) % 11 - 5)).astype(np.complex64)
    for name in ('DL', 'UL'):
        outputs, start, index = [], 0, 0
        while start < len(iq):
            stop = min(len(iq), start + pattern[index % len(pattern)])
            outputs.append(exchange(name, iq[start:stop]))
            start, index = stop, index + 1
        actual = np.concatenate(outputs)
        expected = iq.astype(np.complex128)
        if name == 'UL' and offset:
            # Literal fixture intervals and mathematical oracle, independent of parsed plan/core state.
            expected[17:2048] *= np.exp(2j * np.pi * 500 * np.arange(2031) / 23_040_000)
            assert actual[:17].tobytes() == iq[:17].tobytes()
            assert actual[2048:].tobytes() == iq[2048:].tobytes()
        else:
            assert actual.tobytes() == iq.tobytes()
        np.testing.assert_allclose(actual, expected, rtol=2e-7, atol=1e-6)
        engine.relay_final(name, ledgers[name])
    assert engine.finish()
    for name, warmup in [('DL', 11), ('UL', 19)]:
        records = [r for r in engine.writer.records if r['direction'] == name]
        armed = next(r['details']['state_at_arm'] for r in records if r['event_type'] == 'armed')
        assert armed['sample_clock'] == warmup
        assert armed['cfo_hz'] == armed['cfo_phase_rad'] == armed['cfo_applied_samples'] == 0
        transitions = [r for r in records if r['event_type'].startswith('condition_')]
        assert [r['sample_start'] - warmup for r in transitions] == [0, 17, 2048]
        assert transitions[-1]['details']['settings']['cfo_hz'] == 0
        final = records[-1]['details']
        state = final['state_at_finish']
        assert final['status'] == 'complete' and final['schedule_complete']
        assert state['cfo_applied_samples'] == (2031 if name == 'UL' and offset else 0)
        assert state['sample_clock'] == 5003 + warmup
        assert state['cfo_hz'] == 0
        assert state['awgn_complex_draws'] == state['awgn_normal_draws'] == 0
        assert state['masked_samples'] == state['attenuated_samples'] == state['phase_u64'] == 0


def test_cfo_cli_validates_headless_without_outputs_and_refuses_other_components(tmp_path):
    profile, _ = inputs()
    args = profiles.broker_arguments(profile, 'grc') + ['--no-gui', '--validate-config-only']
    command = [sys.executable, '-B', str(ROOT / 'scripts/ocudu_channel_broker.py')]
    result = subprocess.run(command + args, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout.split('RADIO_CONFIG_VALIDATED: ', 1)[1])
    assert config['channel_semantics_version'] == 'grc_cfo_v1'
    assert config['directions']['UL']['cfo_hz'] == 0
    for extra in (['--ul-cw'], ['--cfo', '500']):
        failed = subprocess.run(command + args + extra, capture_output=True, text=True, timeout=10)
        assert failed.returncode != 0
        assert 'RADIO_CONFIG_VALIDATED:' not in failed.stdout
    args[args.index('grc_cfo_v1')] = 'fixed_reference_v1'
    failed = subprocess.run(command + args, capture_output=True, text=True, timeout=10)
    assert failed.returncode != 0
    assert 'directional CFO flags require' in failed.stderr
