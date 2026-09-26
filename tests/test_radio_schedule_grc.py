"""L0/L1 production schedule/control state tests; no radio or network peers."""

import collections
import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import threading
import tempfile
import time
import types

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    return (importlib.import_module('ocudu_channel_broker'),
            importlib.import_module('radio_schedule_runtime'),
            importlib.import_module('radio_broker_schedule'),
            importlib.import_module('radio_broker_profile'))


class MemoryTruth:
    """Declared L1 in-memory substitute for file I/O; actual DSP/control runs."""
    def __init__(self):
        self.records = []
        self.failed = threading.Event()
        self.logging_errors = 0

    def enqueue(self, value):
        encoded = json.dumps(value, allow_nan=False).encode()
        assert len(encoded) <= 4096
        self.records.append(value)
        return True

    def finish(self):
        return True


def fixture(modules, tmp_path, *, identity=False):
    broker, runtime, plans, profiles = modules
    profile = json.loads((ROOT / 'config/radio_broker/fixed_reference.fixture.json').read_text())
    schedule = json.loads((ROOT / 'config/radio_broker/finite_schedule.fixture.json').read_text())
    if identity:
        for name in ('DL', 'UL'):
            profile['directions'][name]['mode'] = 'identity'
            baseline = plans.mutable_settings(plans.profile_base(profile['directions'][name]))
            for event in schedule['directions'][name]['events']:
                event['settings'] = dict(baseline)
        schedule['profile_sha256'] = hashlib.sha256(profiles.canonical_bytes(profiles.validate_profile(profile))).hexdigest()
    plan = plans.parse_wire(plans.compile_plan(profile, schedule))
    channels = {name: broker.FixedReferenceChannel(name, plan.sample_rate_hz, plan.master_seed,
                                                  **dict(plan.directions[name].base)) for name in ('DL', 'UL')}
    engine = runtime.ScheduleRuntime((plan, tmp_path, b'0' * 64), channels,
                                     threading.Event(), collections.deque(maxlen=1))
    engine.writer = MemoryTruth()
    engine.ready = True
    accounting = {name: broker.RelayAccounting(name, identity) for name in channels}
    return engine, channels, accounting


def request(engine, sequence=1, operation='ARM', token=None):
    return (f'RBCTRL1 {sequence} {operation} {engine.instance_id} {engine.plan.sha256} '
            f'{token or "0" * 64}\n').encode('ascii')


def arm(engine):
    packet = request(engine)
    response = json.loads(engine.control_packet(packet, os.geteuid()))
    assert response['ok'] and response['state'] == 'arm_pending'
    return packet


def exchange(engine, accounting, name, samples):
    ledger = accounting[name]
    ledger.record_input(len(samples))
    engine.input_received(name, ledger)
    result = engine.process(name, samples)
    ledger.record_output(len(samples))
    engine.forwarded(name, ledger)
    return result


def test_arm_preserves_warmup_rng_phase_and_defers_empty_messages(modules, tmp_path):
    engine, channels, accounting = fixture(modules, tmp_path)
    for name, count in (('DL', 37), ('UL', 53)):
        exchange(engine, accounting, name, np.ones(count, np.complex64))
    before = {name: copy.deepcopy(channel.rng.bit_generator.state) for name, channel in channels.items()}
    phases = {name: channel.phase_u64 for name, channel in channels.items()}
    packet = arm(engine)
    cached = engine.control_packet(packet, os.geteuid())
    exchange(engine, accounting, 'DL', np.empty(0, np.complex64))
    assert all(state.arm_sample is None for state in engine.directions.values())
    assert cached == engine.control_packet(packet, os.geteuid())
    for name, channel in channels.items():
        assert channel.rng.bit_generator.state == before[name]
        assert channel.phase_u64 == phases[name]
    exchange(engine, accounting, 'DL', np.ones(17, np.complex64))
    assert engine.directions['DL'].arm_sample == 37
    assert engine.directions['UL'].arm_sample is None
    # Event at the message end must not yet be reported as applied.
    transitions = [r for r in engine.writer.records if r['event_type'].startswith('condition_')]
    assert [r['details']['event_id'] for r in transitions] == ['baseline']
    exchange(engine, accounting, 'DL', np.empty(0, np.complex64))
    assert engine.directions['DL'].next_event == 1
    exchange(engine, accounting, 'DL', np.ones(1, np.complex64))
    transition = engine.writer.records[-2]
    assert transition['event_type'] == 'condition_applied'
    assert transition['sample_start'] == 54 and transition['sample_end'] == 55
    armed = next(r for r in engine.writer.records if r['event_type'] == 'armed')
    assert armed['details']['state_at_arm']['sample_clock'] == 37
    assert armed['details']['state_at_arm']['phase_u64'] == phases['DL']


@pytest.mark.parametrize('pattern', [(5003,), (257,), (1024,), (1, 2, 7, 31, 257, 801, 4093)])
def test_scheduled_full_chain_partition_replay_and_independent_piecewise_oracle(modules, tmp_path, pattern):
    broker, _, _, _ = modules
    engine, channels, accounting = fixture(modules, tmp_path)
    warmup = 37
    for name in channels:
        exchange(engine, accounting, name, np.ones(warmup, np.complex64))
    arm(engine)
    n = 5003
    samples = np.ones(n, np.complex64)
    samples[901:1203] = 0
    actual = []
    cursor = index = 0
    while cursor < n:
        end = min(n, cursor + pattern[index % len(pattern)])
        actual.append(exchange(engine, accounting, 'DL', samples[cursor:end]))
        cursor, index = end, index + 1
    actual = np.concatenate(actual)
    # Independent array oracle: draw all innovations including warmup, then
    # apply literal fixture intervals and an absolute analytical tone.
    rng = np.random.Generator(np.random.PCG64(broker.radio_component_seed(41, 'DL', 'awgn')))
    innovations = rng.standard_normal(2 * (warmup + n), dtype=np.float32).view(np.complex64)[warmup:]
    noise = np.empty(n, np.complex64)
    noise.real = innovations.real.astype(np.float64) * math.sqrt(0.01 / 2)
    noise.imag = innovations.imag.astype(np.float64) * math.sqrt(0.01 / 2)
    noise[257:2048] = 0
    desired = samples.copy()
    desired[17:257] *= 0.25
    desired[1024:2048] = 0
    tone = np.zeros(n, np.complex64)
    angles = 2 * np.pi * 1440000.0 * (warmup + np.arange(257, 2048)) / 23040000.0
    tone[257:2048] = (math.sqrt(0.1) * np.exp(1j * angles)).astype(np.complex64)
    expected = (desired.astype(np.complex128) + tone + noise).astype(np.complex64)
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)
    baseline, _, baseline_accounting = fixture(modules, tmp_path)
    for name in channels:
        exchange(baseline, baseline_accounting, name, np.ones(warmup, np.complex64))
    arm(baseline)
    np.testing.assert_array_equal(actual, exchange(baseline, baseline_accounting, 'DL', samples))
    assert channels['DL'].sample_clock == channels['DL'].awgn_complex_draws == warmup + n
    assert channels['DL'].masked_samples == 1024
    assert channels['DL'].attenuated_samples == 240
    assert engine.directions['DL'].schedule_complete
    assert not engine.directions['UL'].schedule_complete
    transitions = [r for r in engine.writer.records if r['event_type'].startswith('condition_')]
    assert [r['sample_start'] - warmup for r in transitions] == [0, 17, 257, 1024, 1537, 2048]
    assert [r['details']['changed'] for r in transitions] == [False, True, True, True, False, True]


def test_forwarded_completion_and_final_require_both_reconciled_directions(modules, tmp_path):
    engine, _, accounting = fixture(modules, tmp_path)
    arm(engine)
    for name in ('DL', 'UL'):
        ledger = accounting[name]
        samples = np.ones(5000, np.complex64)
        ledger.record_input(len(samples)); engine.input_received(name, ledger)
        engine.process(name, samples)
        assert not engine.directions[name].schedule_complete
        ledger.record_output(len(samples)); engine.forwarded(name, ledger)
        engine.relay_final(name, ledger)
    assert engine.finish()
    finals = engine.writer.records[-2:]
    assert all(r['event_type'] == 'final' and r['details']['status'] == 'complete' for r in finals)
    assert all(r['scope'] == 'successfully_forwarded_samples'
               for r in engine.writer.records if r['event_type'] == 'progress')
    assert [r['event_sequence'] for r in engine.writer.records] == list(range(1, len(engine.writer.records) + 1))


def test_unsent_partial_processing_never_claims_forwarded_completion(modules, tmp_path, monkeypatch):
    engine, channels, accounting = fixture(modules, tmp_path)
    arm(engine)
    original = channels['DL'].process
    calls = []
    def fail_second(samples):
        calls.append(len(samples))
        if len(calls) == 2:
            raise ValueError('injected L1 processing failure')
        return original(samples)
    monkeypatch.setattr(channels['DL'], 'process', fail_second)
    accounting['DL'].record_input(100)
    engine.input_received('DL', accounting['DL'])
    with pytest.raises(ValueError):
        engine.process('DL', np.ones(100, np.complex64))
    engine.fail('relay_processing_or_transport_failed')
    engine.relay_final('DL', accounting['DL'])
    assert engine.directions['DL'].processed_samples == 17
    assert engine.directions['DL'].forwarded_samples == 0
    assert not engine.finish()
    assert all(r['details']['status'] == 'error' for r in engine.writer.records if r['event_type'] == 'final')


@pytest.mark.parametrize('case', ['wrong_token', 'wrong_uid', 'gap', 'modified_retry', 'duplicate_arm', 'truncated', 'malformed'])
def test_control_errors_are_fatal_and_never_echo_token(modules, tmp_path, case):
    engine, _, _ = fixture(modules, tmp_path)
    packet = request(engine)
    uid, truncated = os.geteuid(), False
    if case == 'wrong_token': packet = request(engine, token='a' * 64)
    elif case == 'wrong_uid': uid += 1
    elif case == 'gap': packet = request(engine, sequence=2)
    elif case == 'modified_retry':
        arm(engine); packet = request(engine, operation='STATUS')
    elif case == 'duplicate_arm':
        arm(engine); packet = request(engine, sequence=2)
    elif case == 'truncated': truncated = True
    elif case == 'malformed': packet = b'bad-control-packet'
    response = engine.control_packet(packet, uid, truncated)
    assert not json.loads(response)['ok']
    assert engine.stop_event.is_set() and engine.reason
    assert b'a' * 64 not in response and b'0' * 64 not in response
    assert 'TOKEN' not in json.dumps(engine.writer.records)


def test_status_budget_is_finite_without_per_request_truth_records(modules, tmp_path):
    engine, _, _ = fixture(modules, tmp_path)
    for sequence in range(1, 4097):
        response = engine.control_packet(request(engine, sequence, 'STATUS'), os.geteuid())
        assert json.loads(response)['ok']
    assert engine.writer.records == []
    response = engine.control_packet(request(engine, 4097, 'STATUS'), os.geteuid())
    assert not json.loads(response)['ok'] and engine.reason == 'control_request_budget'


def test_broker_stop_marks_surviving_relay_as_failure(modules):
    broker = modules[0]
    source = broker.channel_broker_source.__new__(broker.channel_broker_source)
    source._stop = threading.Event()
    source._fatal_errors = collections.deque(maxlen=1)
    source.schedule_runtime = None
    class Survivor:
        def join(self, timeout):
            assert timeout == 2.0
        def is_alive(self):
            return True
    source._dl_thread = Survivor()
    assert not source.stop()
    assert source._stop.is_set() and source.fatal_error == 'relay shutdown timed out'


@pytest.mark.parametrize('failed_cleanup', ['req', 'rep', 'context'])
def test_actual_relay_cleanup_failure_attempts_every_resource_and_invalidates_final(
        modules, tmp_path, monkeypatch, capsys, failed_cleanup):
    broker = modules[0]
    engine, channels, accounting = fixture(modules, tmp_path)
    arm(engine)
    exchange(engine, accounting, 'UL', np.ones(5000, np.complex64))
    engine.relay_final('UL', accounting['UL'])
    payload = np.ones(5000, np.complex64).tobytes()
    cleanup = []
    class SocketIO:
        def __init__(self, name): self.name = name
        def setsockopt(self, *_): pass
        def getsockopt(self, *_): return 0
        def connect(self, *_): pass
        def bind(self, *_): pass
        def recv(self, **_kwargs): return payload if self.name == 'req' else b'request'
        def send(self, value):
            if self.name == 'rep':
                assert len(value) == len(payload)
                engine.stop_event.set()
        def close(self):
            cleanup.append(self.name)
            if self.name == failed_cleanup: raise OSError('injected close failure')
    class ContextIO:
        def socket(self, kind): return SocketIO('req' if kind == broker._zmq.REQ else 'rep')
        def term(self):
            cleanup.append('context')
            if failed_cleanup == 'context': raise OSError('injected term failure')
    monkeypatch.setattr(broker._zmq, 'Context', ContextIO)
    impairments = {'lock': threading.RLock(), 'identity': [False],
                   'fading': [types.SimpleNamespace(samp_rate=23040000.0)],
                   'fixed_channel': channels['DL'], 'schedule_runtime': engine}
    before = time.monotonic()
    broker.relay_thread('DL', 'substitute://source', 'substitute://receiver', impairments,
                        np.random.default_rng(1), engine.stop_event, None, [0],
                        threading.Event(), engine.fatal_errors)
    assert time.monotonic() - before < 1
    assert cleanup == ['req', 'rep', 'context']
    assert engine.directions['DL'].schedule_complete
    assert engine.directions['DL'].relay_finished
    assert not engine.finish()
    finals = [r for r in engine.writer.records if r['event_type'] == 'final']
    assert len(finals) == 2 and all(r['details']['status'] == 'error' for r in finals)
    lines = capsys.readouterr().out.splitlines()
    raw = next(line.removeprefix('GRC_RELAY_ACCOUNTING: ') for line in lines
               if line.startswith('GRC_RELAY_ACCOUNTING: '))
    ledger = json.loads(raw)
    assert ledger['status'] == 'error' and ledger['error_count'] == 1
    assert ledger['input_samples'] == ledger['output_samples'] == 5000


@pytest.mark.parametrize('operation', ['request_send', 'upstream_recv', 'reply_send', 'polling'])
def test_actual_transport_error_racing_stop_is_fatal_but_receive_polling_is_normal(
        modules, tmp_path, monkeypatch, capsys, operation):
    broker = modules[0]
    engine, channels, _ = fixture(modules, tmp_path)
    arm(engine)
    payload = np.ones(5000, np.complex64).tobytes()
    cleanup, polls = [], collections.Counter()
    def transport_error():
        engine.stop_event.set()
        raise broker._zmq.ZMQError(broker._zmq.ENOTSOCK)
    class SocketIO:
        def __init__(self, name): self.name = name
        def setsockopt(self, *_): pass
        def getsockopt(self, *_): return 0
        def connect(self, *_): pass
        def bind(self, *_): pass
        def recv(self, **_kwargs):
            polls[self.name] += 1
            if operation == 'polling' and polls[self.name] == 1:
                raise broker._zmq.Again()
            if self.name == 'req' and operation == 'upstream_recv': transport_error()
            return payload if self.name == 'req' else b'request'
        def send(self, _value):
            if self.name == 'req' and operation == 'request_send': transport_error()
            if self.name == 'rep':
                if operation == 'reply_send': transport_error()
                engine.stop_event.set()
        def close(self): cleanup.append(self.name)
    class ContextIO:
        def socket(self, kind): return SocketIO('req' if kind == broker._zmq.REQ else 'rep')
        def term(self): cleanup.append('context')
    monkeypatch.setattr(broker._zmq, 'Context', ContextIO)
    impairments = {'lock': threading.RLock(), 'identity': [False],
                   'fading': [types.SimpleNamespace(samp_rate=23040000.0)],
                   'fixed_channel': channels['DL'], 'schedule_runtime': engine}
    before = time.monotonic()
    broker.relay_thread('DL', 'substitute://source', 'substitute://receiver', impairments,
                        np.random.default_rng(1), engine.stop_event, None, [0],
                        threading.Event(), engine.fatal_errors)
    assert time.monotonic() - before < 1
    assert cleanup == ['req', 'rep', 'context']
    assert not engine.finish()  # UL never ran, so even the benign case is incomplete.
    raw = next(line.removeprefix('GRC_RELAY_ACCOUNTING: ') for line in capsys.readouterr().out.splitlines()
               if line.startswith('GRC_RELAY_ACCOUNTING: '))
    ledger = json.loads(raw)
    finals = [r for r in engine.writer.records if r['event_type'] == 'final']
    assert len(finals) == 2
    if operation == 'polling':
        assert not engine.fatal_errors and engine.reason is None
        assert polls == {'req': 2, 'rep': 2}
        assert ledger['error_count'] == 0 and ledger['input_samples'] == ledger['output_samples'] == 5000
        assert all(row['details']['status'] == 'incomplete' for row in finals)
    else:
        assert engine.fatal_errors and engine.reason == 'relay_processing_or_transport_failed'
        assert ledger['status'] == 'error' and ledger['error_count'] == 1
        assert ledger['input_samples'] == (5000 if operation == 'reply_send' else 0)
        assert ledger['output_samples'] == 0
        assert all(row['details']['status'] == 'error' for row in finals)


def test_second_thread_start_failure_joins_first_and_finalizes_both_directions(
        modules, tmp_path, monkeypatch):
    broker = modules[0]
    engine, _, _ = fixture(modules, tmp_path)
    source = broker.channel_broker_source(channel_semantics='fixed_reference_v1',
        dl_profile=dict(engine.plan.directions['DL'].base), ul_profile=dict(engine.plan.directions['UL'].base),
        seed=41, samp_rate=23040000.0)
    engine.stop_event, engine.fatal_errors = source._stop, source._fatal_errors
    source.schedule_runtime = engine
    monkeypatch.setattr(engine, 'start', lambda: None)
    exited = threading.Event()
    def held_relay(*args):
        assert args[5].wait(1)
        engine.relay_final('DL', broker.RelayAccounting('DL', False))
        exited.set()
    monkeypatch.setattr(broker, 'relay_thread', held_relay)
    original_thread = threading.Thread
    created = []
    class FailingThread:
        def start(self): raise RuntimeError('injected second thread start failure')
        def join(self, *_args, **_kwargs): pytest.fail('unstarted thread was joined')
    def factory(*args, **kwargs):
        thread = original_thread(*args, **kwargs) if not created else FailingThread()
        created.append(thread)
        return thread
    monkeypatch.setattr(broker.threading, 'Thread', factory)
    before = time.monotonic()
    assert not source.start()
    assert time.monotonic() - before < 1
    assert exited.is_set() and not created[0].is_alive()
    assert source.fatal_error and engine.reason == 'relay_thread_start_failed'
    finals = [r for r in engine.writer.records if r['event_type'] == 'final']
    assert len(finals) == 2 and all(r['details']['status'] == 'error' for r in finals)


def test_truth_thread_start_failure_closes_its_output_descriptor(modules, tmp_path, monkeypatch):
    runtime = modules[1]
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_open = os.open
    opened = []
    def recording_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd
    class FailingThread:
        def start(self): raise RuntimeError('injected truth start failure')
    monkeypatch.setattr(runtime.os, 'open', recording_open)
    monkeypatch.setattr(runtime.threading, 'Thread', lambda **_kwargs: FailingThread())
    try:
        with pytest.raises(RuntimeError, match='injected truth start'):
            runtime.TruthWriter(directory_fd)
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
        assert (tmp_path / 'broker_events.jsonl').read_bytes() == b''
    finally:
        os.close(directory_fd)


def test_control_thread_start_failure_cleans_private_socket_and_finalizes(modules, monkeypatch):
    runtime = modules[1]
    # Real private local socket setup only; no peers, relay, radio or writer
    # thread is launched. A short path stays within sockaddr_un's path bound.
    with tempfile.TemporaryDirectory(prefix='rbs-') as directory:
        engine, _, _ = fixture(modules, Path(directory))
        writer = engine.writer
        monkeypatch.setattr(runtime, 'TruthWriter', lambda _fd: writer)
        class FailingThread:
            def start(self): raise RuntimeError('injected control start failure')
            def join(self, *_args): pytest.fail('unstarted control thread was joined')
        monkeypatch.setattr(runtime.threading, 'Thread', lambda **_kwargs: FailingThread())
        with pytest.raises(RuntimeError, match='injected control start'):
            engine.start()
        assert not engine.finish()
        assert engine.reason == 'runtime_start_failed'
        assert engine.server.fileno() == -1 and engine.directory_fd is None
        assert not (Path(directory) / 'rb.sock').exists()
        finals = [row for row in writer.records if row['event_type'] == 'final']
        assert len(finals) == 2 and all(row['details']['status'] == 'error' for row in finals)


def test_relay_join_failure_still_joins_other_thread_and_finalizes(modules, tmp_path):
    broker = modules[0]
    engine, _, _ = fixture(modules, tmp_path)
    source = object.__new__(broker.channel_broker_source)
    source._stop, source._fatal_errors = engine.stop_event, engine.fatal_errors
    source.schedule_runtime = engine
    joined = []
    class ThreadIO:
        def __init__(self, fail): self.fail = fail
        def join(self, **_kwargs):
            joined.append(self.fail)
            if self.fail: raise RuntimeError('injected join failure')
        def is_alive(self): return False
    source._started_relay_threads = [ThreadIO(True), ThreadIO(False)]
    assert not source.stop()
    assert joined == [True, False]
    finals = [row for row in engine.writer.records if row['event_type'] == 'final']
    assert len(finals) == 2 and all(row['details']['status'] == 'error' for row in finals)


def test_control_io_failure_after_stop_remains_fatal(modules, tmp_path, monkeypatch):
    runtime = modules[1]
    engine, _, _ = fixture(modules, tmp_path)
    def failed_select(*_args):
        engine.control_stop.set()
        raise OSError('injected failure after stop')
    monkeypatch.setattr(runtime.select, 'select', failed_select)
    engine._control_loop()
    assert engine.reason == 'control_io_failed' and engine.fatal_errors


def test_identity_schedule_retains_signed_zero_and_uint32_payloads(modules, tmp_path):
    engine, channels, accounting = fixture(modules, tmp_path, identity=True)
    arm(engine)
    words = np.array([0, 0x80000000, 1, 0x80000001, 0x7f7fffff, 0xff7fffff], dtype=np.uint32)
    samples = np.tile(words, 2000).view(np.complex64)
    for name in channels:
        result = exchange(engine, accounting, name, samples)
        assert result.tobytes() == samples.tobytes()
        assert channels[name].awgn_complex_draws == channels[name].phase_u64 == 0


def test_parameter_update_noop_and_rejection_preserve_state(modules, tmp_path):
    engine, channels, _ = fixture(modules, tmp_path)
    channel = channels['DL']
    channel.process(np.ones(101, np.complex64))
    before = channel.record('final')
    settings = {key: channel.config[key] for key in modules[1].MUTABLE_KEYS}
    assert not channel.update_settings(settings)
    assert channel.record('final') == before
    for changes in ({'noise_enabled': 1}, {'gain': float('nan')}, {'cw_freq_hz': 1e9}):
        with pytest.raises(ValueError):
            channel.update_settings({**settings, **changes})
        assert channel.record('final') == before


def test_frequency_update_preserves_integrated_phase_across_sign_change(modules):
    broker = modules[0]
    channel = broker.FixedReferenceChannel('DL', 23040000.0, 41, ref_power=1.0,
                                           noise_enabled=False, cw_enabled=True,
                                           cw_sir_db=0.0, cw_freq_hz=1440000.0)
    first_count, second_count = 37, 5003
    first = channel.process(np.zeros(first_count, np.complex64))
    before_phase = channel.phase_u64
    before_rng = copy.deepcopy(channel.rng.bit_generator.state)
    settings = {key: channel.config[key] for key in modules[1].MUTABLE_KEYS}
    assert channel.update_settings({**settings, 'cw_freq_hz': -2880000.0})
    assert channel.phase_u64 == before_phase and channel.rng.bit_generator.state == before_rng
    second = channel.process(np.zeros(second_count, np.complex64))
    expected_first = np.exp(2j * np.pi * 1440000.0 * np.arange(first_count) / 23040000.0)
    expected_second = np.exp(2j * np.pi * (1440000.0 * first_count
                             - 2880000.0 * np.arange(second_count)) / 23040000.0)
    np.testing.assert_allclose(first, expected_first, rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(second, expected_second, rtol=1e-6, atol=1e-6)
    assert channel.awgn_complex_draws == channel.sample_clock == first_count + second_count


def test_checked_arm_duration_overflow_prevents_armed_record(modules, tmp_path):
    engine, channels, _ = fixture(modules, tmp_path)
    channels['DL'].sample_clock = (1 << 64) - 100
    arm(engine)
    with pytest.raises(ValueError, match='overflow'):
        engine.process('DL', np.ones(1, np.complex64))
    assert not any(r['event_type'] == 'armed' for r in engine.writer.records)


def test_writer_bounds_and_actual_jsonl_finalization(modules, tmp_path):
    runtime = modules[1]
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = runtime.TruthWriter(fd)
        assert writer.enqueue({'value': 17})
        assert writer.finish()
        assert (tmp_path / 'broker_events.jsonl').read_bytes() == b'{"value":17}\n'
        assert (tmp_path / 'broker_events.jsonl').stat().st_mode & 0o777 == 0o600
    finally:
        os.close(fd)


@pytest.mark.parametrize('failure', ['record', 'total', 'nonfinite'])
def test_truth_budget_errors_prevent_success(modules, tmp_path, failure):
    runtime = modules[1]
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = runtime.TruthWriter(fd)
        if failure == 'total': writer.total_bytes = runtime.MAX_TRUTH_BYTES
        value = {'value': 'x' * 4096} if failure == 'record' else {'value': float('nan') if failure == 'nonfinite' else 1}
        assert not writer.enqueue(value)
        assert not writer.finish()
        assert writer.logging_errors >= 1
    finally:
        os.close(fd)


@pytest.mark.parametrize('metrics_every', [None, '4'])
def test_validate_only_private_plan_creates_no_runtime_artifacts(modules, tmp_path, monkeypatch, capsys, metrics_every):
    broker, runtime, plans, profiles = modules
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    plans.prepare(ROOT / 'config/radio_broker/fixed_reference.fixture.json',
                  ROOT / 'config/radio_broker/finite_schedule.fixture.json', directory)
    profile, _ = profiles.load_profile(ROOT / 'config/radio_broker/fixed_reference.fixture.json')
    argv = profiles.broker_arguments(profile, 'grc') + ['--no-gui', '--validate-config-only',
            '--radio-plan-file', str(directory / 'plan.wire'), '--radio-control-dir', str(directory)]
    if metrics_every is not None:
        argv += ['--radio-metrics-every-messages', metrics_every]
    before = sorted(p.name for p in directory.iterdir())
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('validation opened ZMQ'))
    monkeypatch.setattr(runtime.socket, 'socket', lambda *a, **k: pytest.fail('validation opened control socket'))
    assert broker.main(options=argv) == 0
    assert sorted(p.name for p in directory.iterdir()) == before
    assert 'RADIO_CONFIG_VALIDATED:' in capsys.readouterr().out
