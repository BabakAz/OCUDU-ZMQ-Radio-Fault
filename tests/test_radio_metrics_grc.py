"""Deterministic RAD-08 L0/L1 accounting and explicit transport substitutes."""
import collections
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import threading
import types

import numpy as np
import pytest

from test_radio_schedule_grc import fixture as schedule_fixture, modules

ROOT = Path(__file__).resolve().parents[1]


class ManualClock:
    def __init__(self, now=100): self.now = now
    def __call__(self): return self.now
    def advance(self, amount): self.now += amount


class Collector:
    def __init__(self): self.records = []
    def emit(self, direction, kind, start, end, details):
        self.records.append({'direction': direction, 'event_type': kind,
                             'sample_start': start, 'sample_end': end,
                             'details': copy.deepcopy(details)})


def setup_metric(modules, every=1):
    library = importlib.import_module('radio_broker_metrics')
    channel = modules[0].FixedReferenceChannel('DL', 1000.0, 41, ref_power=1.0,
                                             mode='identity', noise_enabled=False)
    clock, collector = ManualClock(), Collector()
    metric = library.DirectionMetrics(collector, 'DL', channel, every, clock)
    metric.start()
    return library, metric, channel, clock, collector


def exchange(metric, channel, clock, count, *, send=True):
    metric.begin_exchange()
    clock.advance(10); metric.next_phase('request_send')
    clock.advance(20); metric.next_phase('upstream_receive')
    clock.advance(30); metric.next_phase('processing')
    metric.received(count)
    clock.advance(40); metric.next_part('channel_chain')
    def core(samples):
        clock.advance(50)
        return channel.process(samples)
    metric.core_call(core, np.ones(count, np.complex64))
    clock.advance(10); metric.next_part('output_prepare')
    clock.advance(70); metric.next_phase('downstream_send')
    clock.advance(80)
    if send: metric.sent(count, clock())


def assert_conservation(details):
    phases = details['phases']
    assert sum(row[1] for stage in phases.values() for row in stage.values()) + details['loop_overhead_ns'] == details['wall_ns']
    for outcome in ('completed', 'stopped', 'error'):
        assert sum(stage[outcome][1] for stage in details['processing_parts'].values()) == phases['processing'][outcome][1]
    assert sum(row[1] for row in details['dsp_core'].values()) <= sum(row[1] for row in details['processing_parts']['channel_chain'].values())


def test_first_full_window_final_idle_partial_and_integer_conservation(modules):
    _, metric, channel, clock, collector = setup_metric(modules)
    exchange(metric, channel, clock, 2)
    first = collector.records[0]['details']
    assert first['start_ns'] == 100 and first['end_ns'] == 410 and first['wall_ns'] == 310
    assert first['input_samples'] == first['output_samples'] == first['completed_processing_samples'] == 2
    assert first['processing_ns_histogram'] == [[8, 1]]  # 170ns in [128,255].
    assert first['message_samples_histogram'] == [[2, 1]]
    assert first['phases']['processing']['completed'] == [1, 170, 170]
    assert first['dsp_core']['completed'] == [1, 50, 50]
    assert first['energies'] == {'input': 2., 'desired': 2., 'noise': 0., 'cw': 0., 'output': 2.}
    assert_conservation(first)
    clock.advance(90)
    metric.finish_window()
    metric.finish_window()
    final = collector.records[1]['details']
    assert len(collector.records) == 2 and final['final_partial']
    assert final['start_ns'] == first['end_ns'] and final['wall_ns'] == 90
    assert final['output_messages'] == final['input_messages'] == 0
    assert final['loop_overhead_ns'] == 90 and final['energies'] == dict.fromkeys(first['energies'], 0.)
    assert metric.final(False, 0)['status'] == 'complete'
    assert_conservation(final)


def test_empty_frames_count_as_messages_and_execute_core_without_waveform_time(modules):
    _, metric, channel, clock, collector = setup_metric(modules, every=2)
    exchange(metric, channel, clock, 0)
    exchange(metric, channel, clock, 0)
    details = collector.records[0]['details']
    assert details['input_messages'] == details['output_messages'] == 2
    assert details['input_samples'] == details['output_samples'] == details['completed_processing_samples'] == 0
    assert details['message_samples_histogram'] == [[0, 2]]
    assert details['dsp_core']['completed'] == [2, 100, 50]
    assert details['energies'] == dict.fromkeys(('input', 'desired', 'noise', 'cw', 'output'), 0.)
    assert_conservation(details)


@pytest.mark.parametrize('wall_delay,expected_above_one', [(0, True), (2_000_000_000, False)])
def test_forwarded_sample_rtf_is_never_clipped_or_paced(modules, wall_delay, expected_above_one):
    _, metric, channel, clock, collector = setup_metric(modules)
    clock.advance(wall_delay)
    exchange(metric, channel, clock, 1000)
    detail = collector.records[0]['details']
    ratio = detail['output_samples'] * 1e9 / (channel.samp_rate * detail['wall_ns'])
    assert (ratio > 1) is expected_above_one


@pytest.mark.parametrize('outcome', ['stopped', 'error'])
def test_unsent_completed_processing_is_retained_in_partial_window(modules, outcome):
    _, metric, channel, clock, collector = setup_metric(modules)
    exchange(metric, channel, clock, 5, send=False)
    metric.finish_window(outcome)
    detail = collector.records[0]['details']
    assert detail['input_samples'] == detail['completed_processing_samples'] == detail['processed_sample_end'] == 5
    assert detail['output_samples'] == detail['output_messages'] == 0
    assert detail['phases']['downstream_send'][outcome] == [1, 80, 80]
    assert detail['processing_ns_histogram'] == [[8, 1]]
    assert metric.final(outcome == 'error', 0)['status'] == ('error' if outcome == 'error' else 'incomplete')
    assert_conservation(detail)


def test_partial_core_failure_relabels_all_processing_parts_and_keeps_core_outcomes(modules):
    _, metric, channel, clock, collector = setup_metric(modules)
    clock.advance(10); metric.next_phase('request_send')
    clock.advance(20); metric.next_phase('upstream_receive')
    clock.advance(30); metric.next_phase('processing')
    metric.received(5)
    clock.advance(40); metric.next_part('channel_chain')
    def first(samples): clock.advance(50); return channel.process(samples)
    metric.core_call(first, np.ones(3, np.complex64))
    def failed(_samples):
        clock.advance(20)
        raise ValueError('injected core failure')
    with pytest.raises(ValueError, match='injected'):
        metric.core_call(failed, np.ones(2, np.complex64))
    metric.finish_window('error')
    detail = collector.records[0]['details']
    assert detail['processed_sample_end'] == 3 and detail['completed_processing_samples'] == 0
    assert detail['processing_ns_histogram'] == []
    assert detail['processing_parts']['input_prepare']['error'] == [1, 40, 40]
    assert detail['processing_parts']['channel_chain']['error'] == [1, 70, 70]
    assert detail['dsp_core']['completed'] == [1, 50, 50]
    assert detail['dsp_core']['error'] == [1, 20, 20]
    assert_conservation(detail)


def test_stalled_upstream_receive_is_stopped_and_clean_with_no_valid_input(modules):
    _, metric, _, clock, collector = setup_metric(modules)
    clock.advance(10); metric.next_phase('request_send')
    clock.advance(20); metric.next_phase('upstream_receive')
    clock.advance(1000); metric.finish_window('stopped')
    detail = collector.records[0]['details']
    assert detail['phases']['upstream_receive']['stopped'] == [1, 1000, 1000]
    assert metric.final(False, 0)['status'] == 'complete'
    assert_conservation(detail)


@pytest.mark.parametrize('value,bucket', [(0, 0), (1, 1), (2, 2), (3, 2), (4, 3), ((1 << 63), 64), ((1 << 64) - 1, 64)])
def test_histogram_uses_declared_integer_bounds(modules, value, bucket):
    library = importlib.import_module('radio_broker_metrics')
    hist = [0] * 65
    library.histogram_add(hist, value)
    assert library.sparse(hist) == [[bucket, 1]]


@pytest.mark.parametrize('failure', ['negative', 'bool', 'overflow', 'zero_window', 'window_budget', 'energy', 'nested_clock', 'sent_clock'])
def test_invalid_accounting_and_cross_boundary_clocks_fail(modules, failure):
    library, metric, channel, clock, _ = setup_metric(modules)
    with pytest.raises(library.MetricsError):
        if failure in ('negative', 'bool', 'overflow'):
            library.uint({'negative': -1, 'bool': True, 'overflow': 1 << 64}[failure])
        elif failure == 'zero_window': metric.finish_window()
        elif failure == 'window_budget':
            metric.window_count = 256; clock.advance(1); metric.finish_window()
        elif failure == 'energy':
            channel.energies['input'] = float('nan'); clock.advance(1); metric.finish_window()
        elif failure == 'nested_clock':
            clock.advance(10); metric.next_phase('request_send')
            clock.advance(10); metric.next_phase('upstream_receive')
            clock.advance(10); metric.next_phase('processing'); metric.received(1)
            clock.advance(10); metric.next_part('channel_chain')
            clock.advance(-5)  # Core's local duration could otherwise be positive.
            metric.core_call(channel.process, np.ones(1, np.complex64))
        elif failure == 'sent_clock':
            exchange(metric, channel, clock, 1, send=False)
            metric.sent(1, metric.last_clock - 1)


class MemoryMux:
    def __init__(self):
        self.records, self.metrics_records = [], []
        self.failed, self.logging_errors = threading.Event(), 0
    def enqueue(self, record, *, metrics=False):
        assert len(json.dumps(record, allow_nan=False).encode()) < (16384 if metrics else 4096)
        (self.metrics_records if metrics else self.records).append(copy.deepcopy(record))
        return True
    def finish(self): return True


def run_relay(modules, tmp_path, monkeypatch, enabled, *, blocked_send=False, malformed=False, retry_sends=False, cleanup_error=False, send_clock_error=False):
    broker = modules[0]
    engine, channels, _ = schedule_fixture(modules, tmp_path)
    engine.writer = MemoryMux()
    if enabled:
        module = importlib.import_module('radio_broker_metrics')
        engine.metrics = module.MetricsRuntime(engine, 2)
        engine.metrics.start()
    from test_radio_schedule_grc import arm
    arm(engine)
    payloads = [np.ones(count, np.complex64).tobytes() for count in (0, 1, 17, 31, 4099)]
    if malformed: payloads = [b'x' * 7]
    if blocked_send or send_clock_error: payloads = [np.ones(17, np.complex64).tobytes()]
    frames, output = iter(payloads), []
    calls = []
    class SocketIO:
        def __init__(self, name): self.name, self.first_send = name, True
        def setsockopt(self, *_): pass
        def getsockopt(self, *_): return 0
        def connect(self, *_): pass
        def bind(self, *_): pass
        def recv(self, **kwargs):
            if self.name == 'req':
                assert kwargs == {'copy': False}
                calls.append('noncopy_receive')
                return broker._zmq.Frame(next(frames))
            return b'request'
        def send(self, value):
            if retry_sends and self.first_send:
                self.first_send = False
                raise broker._zmq.ZMQError(broker._zmq.EAGAIN)
            if self.name == 'rep':
                if blocked_send:
                    engine.stop_event.set()
                    raise broker._zmq.Again()
                output.append(value)
                if send_clock_error:
                    metric = engine.metrics.directions['DL']
                    original_clock = metric.clock
                    def broken_clock():
                        metric.clock = original_clock
                        raise OSError('injected clock read failure after send')
                    metric.clock = broken_clock
                if len(output) == len(payloads): engine.stop_event.set()
        def close(self): pass
    class ContextIO:
        def socket(self, kind): return SocketIO('req' if kind == broker._zmq.REQ else 'rep')
        def term(self):
            if cleanup_error: raise OSError('injected context termination failure')
    monkeypatch.setattr(broker._zmq, 'Context', ContextIO)
    impairment = {'lock': threading.RLock(), 'identity': [False],
                  'fading': [types.SimpleNamespace(samp_rate=23040000.0)],
                  'fixed_channel': channels['DL'], 'schedule_runtime': engine}
    broker.relay_thread('DL', 'substitute://source', 'substitute://receiver', impairment,
                       np.random.default_rng(1), engine.stop_event, None, [0], threading.Event(), engine.fatal_errors)
    engine.finish()
    return engine, channels['DL'], output, calls


def test_actual_relay_on_off_outputs_and_terminal_state_match_without_legacy_metrics(modules, tmp_path, monkeypatch, capsys):
    off, off_channel, off_output, off_calls = run_relay(modules, tmp_path, monkeypatch, False)
    on, on_channel, on_output, on_calls = run_relay(modules, tmp_path, monkeypatch, True)
    assert not off.fatal_errors and not on.fatal_errors
    assert off_output == on_output and off_calls == on_calls == ['noncopy_receive'] * 5
    assert off_channel.record('final') == on_channel.record('final')
    assert off.writer.metrics_records == []
    windows = [r for r in on.writer.metrics_records if r['event_type'] == 'window']
    assert [r['details']['output_messages'] for r in windows] == [2, 2, 1]
    assert all(r['scope'] == 'direction' for r in windows)
    assert sum(r['details']['intentional_mask_samples'] for r in windows) == on_channel.masked_samples
    assert sum(r['details']['input_samples'] for r in windows) == 4148
    assert all(r['details']['rejected_messages'] == 0 for r in windows)
    for record in windows: assert_conservation(record['details'])
    assert [r['event_sequence'] for r in on.writer.metrics_records] == list(range(1, len(on.writer.metrics_records) + 1))
    assert not any(prefix in capsys.readouterr().out for prefix in ('GRC_RELAY_PROGRESS', 'GRC_RELAY_PARTIAL', 'GRC_RELAY_SUMMARY'))


def test_actual_relay_stopped_send_is_incomplete_without_fatal_error(modules, tmp_path, monkeypatch):
    engine, _, output, _ = run_relay(modules, tmp_path, monkeypatch, True, blocked_send=True)
    assert not engine.fatal_errors and output == []
    window = next(r['details'] for r in engine.writer.metrics_records if r['event_type'] == 'window')
    assert window['input_messages'] == 1 and window['output_messages'] == 0
    assert window['completed_processing_samples'] == window['processed_sample_end'] == 17
    assert window['phases']['processing']['completed'][0] == 1
    assert window['phases']['downstream_send']['stopped'][0] == 1
    final = next(r['details'] for r in engine.writer.metrics_records if r['event_type'] == 'final' and r['direction'] == 'DL')
    assert final['status'] == 'incomplete'
    assert_conservation(window)


def test_actual_relay_transient_send_retries_preserve_whole_exchange(modules, tmp_path, monkeypatch):
    engine, channel, output, _ = run_relay(modules, tmp_path, monkeypatch, True, retry_sends=True)
    assert not engine.fatal_errors and len(output) == 5 and channel.sample_clock == 4148
    windows = [r['details'] for r in engine.writer.metrics_records if r['event_type'] == 'window']
    assert sum(r['phases']['request_send']['completed'][0] for r in windows) == 5
    assert sum(r['phases']['downstream_send']['completed'][0] for r in windows) == 5
    for window in windows: assert_conservation(window)


def test_actual_relay_rejected_iq_is_visible_in_error_window(modules, tmp_path, monkeypatch):
    engine, _, output, _ = run_relay(modules, tmp_path, monkeypatch, True, malformed=True)
    assert engine.fatal_errors and output == []
    window = next(r['details'] for r in engine.writer.metrics_records if r['event_type'] == 'window')
    assert window['rejected_messages'] == 1 and window['input_messages'] == 0
    assert window['phases']['processing']['error'][0] == 1
    assert window['processing_parts']['input_prepare']['error'][0] == 1
    assert_conservation(window)


def test_actual_relay_metrics_final_reflects_later_checked_cleanup_failure(modules, tmp_path, monkeypatch):
    engine, _, output, _ = run_relay(modules, tmp_path, monkeypatch, True, cleanup_error=True)
    assert engine.fatal_errors and len(output) == 5
    finals = [r['details'] for r in engine.writer.metrics_records if r['event_type'] == 'final']
    assert len(finals) == 2 and all(row['status'] == 'error' for row in finals)


def test_successful_send_survives_timing_failure_in_both_final_ledgers(modules, tmp_path, monkeypatch):
    engine, _, output, _ = run_relay(modules, tmp_path, monkeypatch, True, send_clock_error=True)
    assert engine.fatal_errors and len(output) == 1 and len(output[0]) == 17 * 8
    metric = next(r['details'] for r in engine.writer.metrics_records if r['event_type'] == 'final' and r['direction'] == 'DL')
    truth = next(r['details'] for r in engine.writer.records if r['event_type'] == 'final' and r['direction'] == 'DL')
    assert metric['output_messages'] == truth['output_messages'] == 1
    assert metric['output_samples'] == truth['output_samples'] == 17
    assert metric['status'] == truth['status'] == 'error'


@pytest.mark.parametrize('failure', ['negative_monotonic', 'overflow_wall', 'monotonic_regression'])
def test_invalid_envelope_clocks_fail_runtime_before_serialization(modules, tmp_path, monkeypatch, failure):
    engine, _, _ = schedule_fixture(modules, tmp_path)
    engine.writer = MemoryMux()
    runtime = importlib.import_module('radio_broker_metrics').MetricsRuntime(engine, 2)
    mono = -1 if failure == 'negative_monotonic' else 100
    wall = (1 << 64) if failure == 'overflow_wall' else 1000
    if failure == 'monotonic_regression': engine.metrics_last_monotonic = 101
    monkeypatch.setattr(modules[1].time, 'monotonic_ns', lambda: mono)
    monkeypatch.setattr(modules[1].time, 'time_ns', lambda: wall)
    with pytest.raises(ValueError): runtime.start()
    assert engine.reason == 'metrics_clock_failure' and not engine.writer.metrics_records


def test_backward_wall_adjustment_is_valid_with_monotonic_envelope_order(modules, tmp_path, monkeypatch):
    engine, _, _ = schedule_fixture(modules, tmp_path)
    engine.writer = MemoryMux()
    runtime = importlib.import_module('radio_broker_metrics').MetricsRuntime(engine, 2)
    clocks = iter([100, 101]); walls = iter([1000, 999])
    monkeypatch.setattr(modules[1].time, 'monotonic_ns', lambda: next(clocks))
    monkeypatch.setattr(modules[1].time, 'time_ns', lambda: next(walls))
    runtime.start()
    runtime.emit('DL', 'final', 0, 0, {'fixture': True})
    assert not engine.fatal_errors
    assert [r['wall_ns'] for r in engine.writer.metrics_records] == [1000, 999]


def test_shared_writer_preserves_separate_streams_and_private_file_modes(modules, tmp_path):
    runtime = modules[1]
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = runtime.TruthWriter(fd, metrics_enabled=True)
        assert writer.enqueue({'truth': 1}) and writer.enqueue({'metric': 'x' * 5000}, metrics=True)
        assert writer.enqueue({'truth': 2}) and writer.finish()
        assert (tmp_path / 'broker_events.jsonl').read_text() == '{"truth":1}\n{"truth":2}\n'
        assert len((tmp_path / 'broker_metrics.jsonl').read_text()) > 5000
        assert writer.thread.is_alive() is False
        assert all((tmp_path / name).stat().st_mode & 0o777 == 0o600
                   for name in ('broker_events.jsonl', 'broker_metrics.jsonl'))
    finally: os.close(fd)


def test_shared_writer_queue_exhaustion_never_silently_discards_records(modules, tmp_path, monkeypatch):
    runtime = modules[1]
    held, release = threading.Event(), threading.Event()
    original_run = runtime.TruthWriter._run
    def delayed_run(writer):
        held.set()
        assert release.wait(2)
        original_run(writer)
    monkeypatch.setattr(runtime.TruthWriter, '_run', delayed_run)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = runtime.TruthWriter(fd, metrics_enabled=True)
        assert held.wait(1)
        for number in range(256):
            assert writer.enqueue({'record': number}, metrics=bool(number % 2))
        assert not writer.enqueue({'excess': True}, metrics=True)
        release.set()
        assert not writer.finish() and writer.logging_errors >= 1
        assert len((tmp_path / 'broker_events.jsonl').read_text().splitlines()) == 128
        assert len((tmp_path / 'broker_metrics.jsonl').read_text().splitlines()) == 128
    finally:
        release.set()
        os.close(fd)


@pytest.mark.parametrize('failure', ['record', 'total', 'nonfinite', 'close'])
def test_metric_writer_failure_invalidates_shared_writer(modules, tmp_path, monkeypatch, failure):
    runtime = modules[1]
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        writer = runtime.TruthWriter(fd, metrics_enabled=True)
        if failure == 'close':
            real_close = os.close
            def close(descriptor):
                real_close(descriptor)
                if descriptor == writer.metrics_fd: raise OSError('injected metrics close failure')
            monkeypatch.setattr(runtime.os, 'close', close)
        else:
            if failure == 'total': writer.metrics_total_bytes = runtime.MAX_METRICS_BYTES
            record = {'metric': 'x' * 16384} if failure == 'record' else {'metric': float('nan') if failure == 'nonfinite' else 1}
            assert not writer.enqueue(record, metrics=True)
        assert not writer.finish() and writer.logging_errors > 0
    finally: os.close(fd)


def test_legacy_console_metrics_include_empty_first_and_last_intervals(modules):
    broker = modules[0]
    metric = broker.RelayMetrics('DL', 1000.0, progress_messages=2)
    never = json.loads(metric.summary(now_ns=0).split(': ', 1)[1])
    assert never['message_rate_hz'] is None and never['undefined_reasons']['message_rate_hz'] == 'not_started'
    metric.start(now_ns=100)
    assert metric.observe(0, 0, 10, now_ns=200) is None
    full = json.loads(metric.observe(1, 8, 20, now_ns=300).split(': ', 1)[1])
    assert full['interval_messages'] == 2 and full['elapsed_s'] == 2e-7
    assert full['elapsed_ns'] == 200 and full['message_rate_hz'] == 10000000.0
    partial = json.loads(metric.partial(now_ns=500).split(': ', 1)[1])
    assert partial['final_partial'] and partial['interval_messages'] == 0
    assert partial['processing_mean_us'] is None and partial['undefined_reasons']['processing_mean_us'] == 'no_messages'
    assert metric.partial(now_ns=600) is None
    summary = json.loads(metric.summary(now_ns=500).split(': ', 1)[1])
    assert summary['total_messages'] == 2 and summary['total_samples'] == 1


@pytest.mark.parametrize('value', ['0', '-1', '01', '1.0', '1000001', 'nan', '１２'])
def test_metrics_cli_rejects_invalid_intervals_without_startup(modules, value):
    with pytest.raises(SystemExit) as error:
        modules[0].main(['--no-gui', '--validate-config-only', '--radio-metrics-every-messages', value])
    assert error.value.code == 2


def test_build_identity_includes_exact_sorted_six_sources(modules):
    names = ('ocudu_channel_broker.py', 'radio_broker_metrics.py', 'radio_broker_profile.py',
             'radio_broker_schedule.py', 'radio_schedule_runtime.py', 'radio_static_tdl.py')
    payload = ''.join(name + ' ' + hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest() + '\n' for name in names)
    assert modules[1].source_build_sha256() == hashlib.sha256(payload.encode()).hexdigest()
