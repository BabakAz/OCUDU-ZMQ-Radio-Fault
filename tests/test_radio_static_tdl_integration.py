"""Scheduled TDL transport, strict scope and retained evidence checks."""
import collections
import copy
import json
import os
import subprocess
import sys
import threading

import numpy as np
import pytest

from test_radio_broker_grc_cfo import MemoryTruth, broker, profiles, schedules, runtime, ROOT
from test_radio_fault_grc_cfo import cfo_fixture, IDS, STUDY_IDS, fault, write_evidence
from radio_static_tdl import TdlReferenceChannel
from test_radio_static_tdl import independent_kernel, engine as numerical_engine


def inputs(kind='grc_ul_tdl_a_500ms'):
    spec = fault.specification(kind, traffic_bitrate='2M')
    profile = fault.radio_profile(spec)
    schedule = fault.schedule(spec, **IDS)
    for leg in schedule['directions'].values():
        leg['duration_samples'] = 4097
        for row, offset in zip(leg['events'], (0, 17, 2048)):
            row['sample_offset'] = offset
    return profile, schedule


def test_tdl_wire_cli_scope_and_c_rejection(tmp_path):
    profile, schedule = inputs()
    raw = schedules.compile_plan(profile, schedule)
    plan = schedules.parse_wire(raw)
    assert plan.channel_semantics_version == profiles.TDL_SEMANTICS
    assert plan.directions['UL'].events[1].settings['tdl_enabled'] is True
    with pytest.raises(ValueError):
        profiles.broker_arguments(profile, 'c')
    base = [sys.executable, str(ROOT / 'scripts/ocudu_channel_broker.py'), '--no-gui',
            *profiles.broker_arguments(profile, 'grc'), '--validate-config-only']
    result = subprocess.run(base, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    validated = json.loads(result.stdout.removeprefix('RADIO_CONFIG_VALIDATED: '))
    assert validated['directions']['UL']['realization']['common_delay_samples'] == 15
    for flags in (['--doppler', '1'], ['--ul-cfo', '1'], ['--profile', 'epa'],
                  ['--radio-metrics-every-messages', '1'], ['--ul-tdl-enabled', '--ul-tdl-enabled']):
        failed = subprocess.run(base + flags, capture_output=True, text=True, timeout=10)
        assert failed.returncode != 0 and 'RADIO_CONFIG_VALIDATED:' not in failed.stdout
    for change in (dict(tdl_enabled=1), dict(noise_enabled=True), dict(gain=.5), dict(cfo_hz=1)):
        state = dict(plan.directions['UL'].base, **change)
        with pytest.raises((ValueError, TypeError)):
            TdlReferenceChannel('UL', 23_040_000, 41, **state)
    for changed in (raw.replace(b'RADIO_SCHEDULE_GRC_STATIC_TDL_A_WIRE_V1', b'RADIO_SCHEDULE_WIRE_V1'),
                    raw.replace(b'0x1.5f90000000000p+24', b'0x1.5f90000000000p+23')):
        with pytest.raises(ValueError):
            schedules.parse_wire(changed)
    private = tmp_path / 'radio'; private.mkdir(mode=0o700)
    (private / 'plan.wire').write_bytes(raw); (private / 'plan.wire').chmod(0o600)
    (private / 'control.token').write_bytes(b'a' * 64); (private / 'control.token').chmod(0o600)
    program = tmp_path / 'c-broker'
    subprocess.run(['clang-18', '-std=c17', '-O2', '-Wall', '-Wextra', '-Wpedantic', '-Werror',
                    str(ROOT / 'scripts/zmq_channel_broker.c'), '-o', str(program),
                    '-lzmq', '-lm', '-pthread'], check=True, capture_output=True, timeout=30)
    original = profiles.load_profile(ROOT / 'config/radio_broker/fixed_reference.fixture.json')[0]
    result = subprocess.run([str(program), *profiles.broker_arguments(original, 'c'),
        '--radio-plan-file', str(private / 'plan.wire'), '--radio-control-dir', str(private),
        '--validate-config-only'], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and 'RADIO_CONFIG_VALIDATED:' not in result.stdout
    channels = {name: fault.tdl_channel_type(plan.channel_semantics_version)(name, plan.sample_rate_hz, 41, **dict(leg.base))
                for name, leg in plan.directions.items()}
    with pytest.raises(ValueError, match='metrics_unsupported'):
        runtime.validate_inputs(private / 'plan.wire', private, channels, metrics_every=1)
    assert sorted(p.name for p in private.iterdir()) == ['control.token', 'plan.wire']


@pytest.mark.parametrize('kind', ['grc_ul_tdl_a_500ms', 'grc_ul_tdl_c_500ms'])
@pytest.mark.parametrize('pattern', [(5003,), (1, 31, 257, 801)])
@pytest.mark.parametrize('send_failure', [False, True])
def test_scheduled_tdl_history_boundaries_and_forwarding(tmp_path, pattern, send_failure, kind):
    profile, schedule = inputs(kind)
    plan = schedules.parse_wire(schedules.compile_plan(profile, schedule))
    channels = {name: fault.tdl_channel_type(plan.channel_semantics_version)(name, plan.sample_rate_hz, 41, **dict(leg.base))
                for name, leg in plan.directions.items()}
    engine = runtime.ScheduleRuntime((plan, tmp_path, b'0' * 64), channels,
                                     threading.Event(), collections.deque(maxlen=1))
    engine.writer = MemoryTruth(); engine.ready = True
    ledgers = {name: broker.RelayAccounting(name, name == 'DL') for name in channels}
    def exchange(name, samples, forward=True):
        ledger = ledgers[name]; ledger.record_input(len(samples)); engine.input_received(name, ledger)
        output = engine.process(name, samples)
        if forward:
            ledger.record_output(len(samples)); engine.forwarded(name, ledger)
        return output
    for name in channels:
        exchange(name, np.ones(19, np.complex64))
    packet = f'RBCTRL1 1 ARM {engine.instance_id} {plan.sha256} {"0" * 64}\n'.encode()
    assert json.loads(engine.control_packet(packet, os.geteuid()))['ok']
    count = 18 if send_failure else 5003
    iq = (np.arange(count) % 7 - 3 + 1j * (np.arange(count) % 11 - 5)).astype(np.complex64)
    source = np.concatenate((np.ones(19, np.complex64), iq))
    if kind == 'grc_ul_tdl_c_500ms':
        from test_radio_static_tdl_c import independent_kernel as c_kernel, engine as c_engine
        kernel = c_kernel(c_engine())
    else:
        kernel = independent_kernel(numerical_engine())
    full = np.convolve(source.astype(np.complex128), kernel.astype(np.complex128))[:len(source)].astype(np.complex64)
    for name in channels:
        outputs, start, index = [], 0, 0
        while start < len(iq):
            end = min(len(iq), start + pattern[index % len(pattern)])
            outputs.append(exchange(name, iq[start:end], forward=not send_failure))
            start, index = end, index + 1
        actual = np.concatenate(outputs)
        expected = iq.copy() if name == 'DL' else source[4:4+len(iq)].copy()
        if name == 'UL':
            expected[17:2048] = full[19+17:19+min(2048, count)]
        np.testing.assert_allclose(actual, expected, rtol=3e-7, atol=2e-6)
        assert channels[name].realization_sha256 == channels[name].record('final')['realization_sha256']
        engine.relay_final(name, ledgers[name])
    assert engine.finish() is (not send_failure)
    final = next(r['details'] for r in engine.writer.records if r['direction'] == 'UL' and r['event_type'] == 'final')
    assert final['state_at_finish']['tdl_applied_samples'] == (1 if send_failure else 2031)
    assert final['state_at_finish']['tdl_enabled'] is send_failure
    assert final['restoration_observed'] is (not send_failure)
    assert final['forwarded_samples'] == (19 if send_failure else 5022)


def tdl_fixture(kind):
    _, _, ready, receipts, execution, records, identity = cfo_fixture('ul_cfo_500ms')
    spec = fault.specification(kind, traffic_bitrate='2M')
    profile = fault.radio_profile(spec)
    plan = schedules.parse_wire(schedules.compile_plan(profile, fault.schedule(spec, **IDS)))
    for row in [ready, execution, *records]:
        row['plan_sha256'] = plan.sha256
        if 'config_sha256' in row: row['config_sha256'] = plan.profile_sha256
    for receipt in receipts: receipt['response']['plan_sha256'] = plan.sha256
    for row in records:
        name, detail = row['direction'], row['details']
        if name == 'control': continue
        channel = fault.tdl_channel_type(plan.channel_semantics_version)(name, plan.sample_rate_hz, 41, **dict(plan.directions[name].base))
        if row['event_type'] == 'armed':
            state = channel.record('started'); state['sample_clock'] = detail['arm_sample']
            detail['state_at_arm'] = state
        if row['event_type'].startswith('condition_'):
            event = next(e for e in plan.directions[name].events if e.event_id == detail['event_id'])
            detail['settings'] = dict(event.settings)
            detail['changed'] = spec['tdl_enabled'] and name == 'UL' and event.event_id != 'baseline'
        if row['event_type'] == 'final':
            state = channel.record('final'); state['sample_clock'] = detail['processed_samples']
            state['tdl_applied_samples'] = 11_520_000 if spec['tdl_enabled'] and name == 'UL' else 0
            detail['state_at_finish'] = state
    return spec, plan, ready, receipts, execution, records, identity, fault.tdl_realizations(profile)


@pytest.mark.parametrize('kind', fault.GRC_TDL_KINDS)
@pytest.mark.parametrize('tamper', [None, 'missing', 'exposure', 'history', 'rng', 'realization', 'restoration', 'forwarded', 'wrong_profile'])
def test_tdl_truth_and_preparation_binding(tmp_path, kind, tamper):
    spec, plan, ready, receipts, execution, records, identity, realizations = tdl_fixture(kind)
    final = next(r['details'] for r in records if r['direction'] == 'UL' and r['event_type'] == 'final')
    if tamper == 'missing': final.pop('state_at_finish')
    if tamper == 'exposure': final['state_at_finish']['tdl_applied_samples'] += 1
    if tamper == 'history': final['state_at_finish'].pop('history_sha256')
    if tamper == 'rng': final['state_at_finish']['rng_state_sha256'] = 'f' * 64
    if tamper == 'realization': realizations['UL']['numerical_recipe']['coefficient_cf32_le_hex'] = '00'
    if tamper == 'wrong_profile':
        other = 'grc_tdl_a_normal' if kind in fault.GRC_TDL_C_KINDS else 'grc_tdl_c_normal'
        realizations = fault.tdl_realizations(fault.radio_profile(fault.specification(other)))
    if tamper == 'restoration': final['state_at_finish']['tdl_enabled'] = True
    if tamper == 'forwarded': final['forwarded_samples'] -= 1
    evidence = tmp_path / 'evidence'; evidence.mkdir(mode=0o700)
    write_evidence(evidence, spec, ready, receipts, execution, records, identity=identity,
                   realizations=realizations)
    result = fault.verify(evidence, expected_pid=123, expected_build_sha256=identity['build_sha256'])
    assert result['qualified'] is (tamper is None), result['errors']
    if tamper is None:
        assert result['programmed_ul_tdl_samples'] == (11_520_000 if spec['tdl_enabled'] else 0)
