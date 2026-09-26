"""Finite sample scheduling and private control for the Python CPU broker.

Build identity is SHA256 of UTF-8 lines ``name + ' ' + file_sha256 + '\n'``
for ocudu_channel_broker.py, radio_broker_metrics.py, radio_broker_profile.py,
radio_broker_schedule.py, radio_schedule_runtime.py
in that lexicographic order. The authoritative truth sink is a bounded file
writer; stdout is never its replacement. This module launches no radio stack.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import select
import socket
import stat
import struct
import threading
import time

import numpy as np

UINT64_MAX = (1 << 64) - 1
MUTABLE_KEYS = ('gain', 'noise_enabled', 'noise_snr_db', 'cw_enabled', 'cw_sir_db', 'cw_freq_hz')
MAX_RECORD_BYTES = 4096
MAX_TRUTH_BYTES = 1024 * 1024
MAX_METRICS_RECORD_BYTES = 16384
MAX_METRICS_BYTES = 16 * 1024 * 1024
MAX_PENDING_RECORDS = 256
MAX_CONTROL_REQUESTS = 4096


class RuntimeErrorContract(ValueError):
    """Invalid private inputs or failed finite runtime contract."""


def _check(condition, reason):
    if not condition:
        raise RuntimeErrorContract(reason)


def _private_file(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        _check(stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600
               and metadata.st_uid == os.geteuid() and metadata.st_nlink == 1,
               'unsafe_private_input')
        data = os.read(fd, maximum + 1)
        _check(len(data) <= maximum and os.read(fd, 1) == b'', 'private_input_too_large')
        return data
    finally:
        os.close(fd)


def validate_inputs(plan_file, directory, channels, metrics_every=None):
    """Read immutable inputs only; no sockets, outputs, threads or ZMQ context."""
    from radio_broker_schedule import load_wire

    if metrics_every is not None:
        from radio_broker_metrics import validate_every
        validate_every(metrics_every)

    directory = Path(directory)
    _check(directory.is_absolute() and str(directory.resolve(strict=True)) == str(directory),
           'control_directory_not_canonical')
    metadata = directory.lstat()
    _check(stat.S_ISDIR(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o700
           and metadata.st_uid == os.geteuid(), 'unsafe_control_directory')
    _check(len(os.fsencode(directory / 'rb.sock')) <= 107, 'control_socket_path_too_long')
    raw = _private_file(Path(plan_file), 65536)
    plan = load_wire(Path(plan_file))
    tdl = plan.channel_semantics_version in ('grc_static_tdl_a_v1', 'grc_static_tdl_c_v1')
    _check(metrics_every is None or not tdl,
           'static_tdl_metrics_unsupported')
    _check(hashlib.sha256(raw).hexdigest() == plan.sha256, 'plan_changed_during_read')
    token = _private_file(directory / 'control.token', 64)
    _check(re.fullmatch(b'[0-9a-f]{64}', token) is not None, 'invalid_control_token')
    for direction, channel in channels.items():
        if tdl:
            realization = getattr(channel, 'realization', None)
            _check(callable(realization)
                   and realization().get('channel_semantics_version') == plan.channel_semantics_version,
                   'initial_channel_semantics_mismatch')
        _check(channel.master_seed == plan.master_seed
               and channel.samp_rate == plan.sample_rate_hz
               and dict(channel.config) == dict(plan.directions[direction].base),
               'initial_profile_mismatch')
    outputs = ('rb.sock', 'broker_ready.json', 'broker_events.jsonl')
    if metrics_every is not None:
        outputs += ('broker_metrics.jsonl',)
    for filename in outputs:
        _check(not os.path.lexists(directory / filename), 'existing_runtime_artifact')
    return plan, directory, token


def source_build_sha256():
    directory = Path(__file__).resolve().parent
    lines = ''.join(name + ' ' + hashlib.sha256((directory / name).read_bytes()).hexdigest() + '\n'
                    for name in ('ocudu_channel_broker.py', 'radio_broker_metrics.py', 'radio_broker_profile.py', 'radio_broker_schedule.py',
                                 'radio_schedule_runtime.py', 'radio_static_tdl.py'))
    return hashlib.sha256(lines.encode('utf-8')).hexdigest()


class TruthWriter:
    """Bounded nonblocking producer; one thread owns writes and file close."""

    def __init__(self, directory_fd, metrics_enabled=False):
        self.fd = os.open('broker_events.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        try:
            os.fchmod(self.fd, 0o600)
            self.metrics_fd = None
            if metrics_enabled:
                self.metrics_fd = os.open('broker_metrics.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o600, dir_fd=directory_fd)
                os.fchmod(self.metrics_fd, 0o600)
            self.queue = queue.Queue(MAX_PENDING_RECORDS)
            self.lock = threading.Lock()
            self.total_bytes = 0
            self.metrics_total_bytes = 0
            self.logging_errors = 0
            self.failed = threading.Event()
            self.closed = False
            self.thread = threading.Thread(target=self._run, name='radio-truth', daemon=True)
            self.thread.start()
        except Exception:
            try:
                os.close(self.fd)
            finally:
                if getattr(self, 'metrics_fd', None) is not None:
                    os.close(self.metrics_fd)
            raise

    def _error(self):
        with self.lock:
            self.logging_errors += 1
            self.failed.set()

    def enqueue(self, record, *, metrics=False):
        try:
            payload = (json.dumps(record, sort_keys=True, separators=(',', ':'),
                                  allow_nan=False) + '\n').encode('ascii')
            with self.lock:
                record_limit = MAX_METRICS_RECORD_BYTES if metrics else MAX_RECORD_BYTES
                file_limit = MAX_METRICS_BYTES if metrics else MAX_TRUTH_BYTES
                total = self.metrics_total_bytes if metrics else self.total_bytes
                descriptor = self.metrics_fd if metrics else self.fd
                if (self.closed or self.failed.is_set() or descriptor is None or len(payload) > record_limit
                        or total + len(payload) > file_limit):
                    raise RuntimeErrorContract('truth_budget_or_state')
                self.queue.put_nowait((descriptor, payload))
                if metrics:
                    self.metrics_total_bytes += len(payload)
                else:
                    self.total_bytes += len(payload)
            return True
        except (ValueError, TypeError, OverflowError, MemoryError, queue.Full):
            self._error()
            return False

    def _run(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                descriptor, payload = item
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError('zero_length_truth_write')
                    view = view[written:]
        except (OSError, MemoryError):
            self._error()
        finally:
            for descriptor in (self.fd, self.metrics_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        self._error()

    def finish(self, timeout=2.0):
        with self.lock:
            self.closed = True
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            self._error()
        self.thread.join(timeout)
        if self.thread.is_alive():
            self._error()
        return not self.failed.is_set() and not self.thread.is_alive()


class DirectionState:
    def __init__(self, plan, channel):
        self.plan = plan
        self.channel = channel
        self.arm_sample = None
        self.next_event = 0
        self.restoration_observed = False
        self.schedule_complete = False
        self.pending_transitions = 0
        self.processed_samples = channel.sample_clock
        self.forwarded_samples = 0
        self.input_messages = self.output_messages = 0
        self.input_samples = self.output_samples = 0
        self.relay_finished = False

    def snapshot(self):
        return {'armed': self.arm_sample is not None, 'arm_sample': self.arm_sample,
                'processed_samples': self.processed_samples, 'forwarded_samples': self.forwarded_samples,
                'duration_samples': self.plan.duration_samples, 'schedule_complete': self.schedule_complete,
                'next_event': self.next_event}


class ScheduleRuntime:
    """Shared small control ledger; DSP ownership stays with each relay thread."""

    def __init__(self, inputs, channels, stop_event, fatal_errors, metrics_every=None):
        self.plan, self.directory, self._token = inputs
        metadata = self.directory.lstat()
        self.directory_identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid)
        self.stop_event = stop_event
        self.fatal_errors = fatal_errors
        self.lock = threading.RLock()
        self.instance_id = secrets.token_hex(16)
        self.build_sha256 = source_build_sha256()
        self.directions = {name: DirectionState(self.plan.directions[name], channel)
                           for name, channel in channels.items()}
        self.event_sequence = 0
        self.request_sequence = 0
        self.arm_request = None
        self.last_packet = self.last_response = None
        self.reason = None
        self.ready_directions = set()
        self.ready = False
        self.control_stop = threading.Event()
        self.server = self.writer = self.control_thread = None
        self.control_thread_started = False
        self.directory_fd = None
        self.socket_identity = None
        self.finished = False
        self.metrics_last_monotonic = None
        self.metrics = None
        if metrics_every is not None:
            from radio_broker_metrics import MetricsRuntime
            self.metrics = MetricsRuntime(self, metrics_every)

    def emit_metrics(self, sequence, config_hash, direction, event_type, start, end, details):
        from radio_broker_metrics import uint
        # MetricsRuntime.emit holds this same RLock across sequence allocation,
        # clock checks and enqueue, so each global envelope observation is ordered.
        with self.lock:
            try:
                mono, wall = uint(time.monotonic_ns()), uint(time.time_ns())
                _check(self.metrics_last_monotonic is None or mono >= self.metrics_last_monotonic,
                       'metrics_envelope_clock_regression')
            except Exception:
                self.fail('metrics_clock_failure', emit=False)
                raise
            self.metrics_last_monotonic = mono
        record = {'schema_version': 'radio_broker_metrics_v1',
                  **{name: getattr(self.plan, name) for name in
                     ('study_id', 'protocol_id', 'trial_id', 'pipeline_id')},
                  'instance_id': self.instance_id, 'backend': 'grc', 'build_sha256': self.build_sha256,
                  'config_sha256': self.plan.profile_sha256, 'plan_sha256': self.plan.sha256,
                  'metrics_config_sha256': config_hash, 'direction': direction,
                  'event_sequence': sequence, 'event_type': event_type,
                  'monotonic_ns': mono, 'wall_ns': wall,
                  'sample_start': start, 'sample_end': end,
                  'scope': 'control' if direction == 'control' else 'direction', 'details': details}
        if self.writer is None or not self.writer.enqueue(record, metrics=True):
            self.fail('metrics_logging_failure', emit=False)
            raise RuntimeErrorContract('metrics_logging_failure')

    def emit(self, direction, event_type, sample_start=None, sample_end=None,
             scope='control', details=None, clocks=None):
        with self.lock:
            mono, wall = clocks or (time.monotonic_ns(), time.time_ns())
            self.event_sequence += 1
            record = {'schema_version': 'radio_broker_truth_v1',
                      **{name: getattr(self.plan, name) for name in
                         ('study_id', 'protocol_id', 'trial_id', 'pipeline_id')},
                      'instance_id': self.instance_id, 'backend': 'grc', 'build_sha256': self.build_sha256,
                      'config_sha256': self.plan.profile_sha256, 'plan_sha256': self.plan.sha256,
                      'direction': direction, 'event_sequence': self.event_sequence, 'event_type': event_type,
                      'monotonic_ns': mono, 'wall_ns': wall, 'sample_start': sample_start,
                      'sample_end': sample_end, 'scope': scope, 'details': details or {}}
            if self.writer is None or not self.writer.enqueue(record):
                self.fail('truth_logging_failure', emit=False)
                raise RuntimeErrorContract('truth_logging_failure')

    def fail(self, reason, *, emit=True):
        with self.lock:
            if self.reason is None:
                self.reason = reason
                self.fatal_errors.append('scheduled broker failure: ' + reason)
                self.stop_event.set()
                if emit and self.writer is not None and not self.writer.failed.is_set():
                    try:
                        self.emit('control', 'error', details={'reason': reason})
                    except RuntimeErrorContract:
                        pass

    def start(self):
        try:
            self.directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(self.directory_fd)
            _check(stat.S_IMODE(metadata.st_mode) == 0o700
                   and (metadata.st_dev, metadata.st_ino, metadata.st_uid) == self.directory_identity,
                   'control_directory_identity_changed')
            self.writer = (TruthWriter(self.directory_fd, metrics_enabled=True)
                           if self.metrics is not None else TruthWriter(self.directory_fd))
            self.emit('control', 'started', details={'pid': os.getpid()})
            if self.metrics is not None:
                self.metrics.start()
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.server.setblocking(False)
            self.server.bind(str(self.directory / 'rb.sock'))
            metadata = (self.directory / 'rb.sock').lstat()
            self.socket_identity = (metadata.st_dev, metadata.st_ino, metadata.st_uid)
            os.chmod(self.directory / 'rb.sock', 0o600)
            self.server.listen(8)
            self.control_thread = threading.Thread(target=self._control_loop, name='radio-control', daemon=True)
            self.control_thread.start()
            self.control_thread_started = True
        except Exception:
            self.fail('runtime_start_failed')
            raise

    def relay_ready(self, direction):
        with self.lock:
            self.ready_directions.add(direction)
            if self.ready_directions == {'DL', 'UL'} and self.reason is None:
                ready = {'schema_version': 'radio_broker_ready_v1', 'instance_id': self.instance_id,
                         'pid': os.getpid(), 'plan_sha256': self.plan.sha256,
                         'config_sha256': self.plan.profile_sha256, 'backend': 'grc',
                         'build_sha256': self.build_sha256,
                         'control_socket': str(self.directory / 'rb.sock')}
                payload = (json.dumps(ready, sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')
                fd = os.open('broker_ready.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.directory_fd)
                try:
                    os.fchmod(fd, 0o600)
                    _check(os.write(fd, payload) == len(payload), 'ready_write_failed')
                finally:
                    os.close(fd)
                self.emit('control', 'ready', details={'readiness': 'control_and_relay_sockets_bound'})
                self.ready = True

    def _state(self):
        if self.reason is not None:
            return 'error'
        if all(state.schedule_complete for state in self.directions.values()):
            return 'completed'
        if self.arm_request is not None:
            return 'armed' if all(s.arm_sample is not None for s in self.directions.values()) else 'arm_pending'
        return 'ready'

    def _response(self, sequence, operation, *, ok=True, reason=None):
        value = {'schema_version': 'radio_broker_control_v1', 'ok': ok, 'request_sequence': sequence,
                 'operation': operation, 'instance_id': self.instance_id, 'plan_sha256': self.plan.sha256,
                 'state': self._state(), 'directions': {name: state.snapshot()
                                                       for name, state in self.directions.items()}}
        if reason is not None:
            value['reason'] = reason
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        _check(len(data) <= 4096, 'control_response_too_large')
        return data

    def control_packet(self, packet, uid, truncated=False):
        with self.lock:
            sequence, operation = 0, 'STATUS'
            try:
                _check(uid == os.geteuid(), 'unauthorized_control_peer')
                _check(not truncated and len(packet) <= 512, 'oversized_control_packet')
                match = re.fullmatch(rb'RBCTRL1 ([1-9][0-9]*) (STATUS|ARM) ([0-9a-f]{32}) ([0-9a-f]{64}) ([0-9a-f]{64})\n', packet)
                _check(match is not None, 'malformed_control_packet')
                number, op, instance, plan_hash, token = match.groups()
                sequence, operation = int(number), op.decode('ascii')
                _check(hmac.compare_digest(token, self._token), 'unauthorized_control_token')
                _check(instance.decode('ascii') == self.instance_id, 'stale_control_instance')
                _check(plan_hash.decode('ascii') == self.plan.sha256, 'stale_control_plan')
                if sequence == self.request_sequence and packet == self.last_packet:
                    return self.last_response
                _check(sequence == self.request_sequence + 1, 'control_sequence_mismatch')
                _check(sequence <= MAX_CONTROL_REQUESTS, 'control_request_budget')
                _check(self.reason is None and self.ready, 'control_not_ready')
                if operation == 'ARM':
                    _check(self.arm_request is None, 'duplicate_arm')
                    mono, wall = time.monotonic_ns(), time.time_ns()
                    self.arm_request = {'request_sequence': sequence, 'request_monotonic_ns': mono,
                                        'request_wall_ns': wall}
                    self.emit('control', 'arm_requested', details=dict(self.arm_request), clocks=(mono, wall))
                self.request_sequence = sequence
                response = self._response(sequence, operation)
                self.last_packet, self.last_response = packet, response
                return response
            except (RuntimeErrorContract, ValueError, MemoryError) as exc:
                reason = str(exc) if isinstance(exc, RuntimeErrorContract) else 'invalid_control_value'
                self.fail(reason)
                return self._response(sequence, operation, ok=False, reason=reason)

    def _control_loop(self):
        try:
            while not self.control_stop.is_set():
                if self.writer.failed.is_set():
                    self.fail('truth_write_failed', emit=False)
                readable, _, _ = select.select([self.server], [], [], 0.05)
                if not readable:
                    continue
                connection, _ = self.server.accept()
                with connection:
                    connection.setblocking(False)
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                    _, uid, _ = struct.unpack('3i', credentials)
                    readable, _, _ = select.select([connection], [], [], 0.5)
                    if not readable:
                        self.fail('control_receive_timeout')
                        continue
                    packet, _, flags, _ = connection.recvmsg(512)
                    response = self.control_packet(packet, uid, bool(flags & socket.MSG_TRUNC))
                    _, writable, _ = select.select([], [connection], [], 0.5)
                    if not writable or connection.send(response) != len(response):
                        self.fail('control_send_failed')
        except Exception:
            self.fail('control_io_failed')

    def input_received(self, direction, accounting):
        with self.lock:
            state = self.directions[direction]
            for name in ('input_messages', 'input_samples', 'output_messages', 'output_samples'):
                setattr(state, name, getattr(accounting, name))

    def process(self, direction, iq, core_call=None):
        state = self.directions[direction]
        channel = state.channel
        process_core = channel.process if core_call is None else lambda samples: core_call(channel.process, samples)
        if not len(iq):
            return process_core(iq)
        with self.lock:
            if state.arm_sample is None and self.arm_request is not None:
                _check(channel.sample_clock <= UINT64_MAX - state.plan.duration_samples, 'schedule_sample_overflow')
                state.arm_sample = channel.sample_clock
                self.emit(direction, 'armed', state.arm_sample, state.arm_sample, 'processed_samples',
                          {**self.arm_request, 'arm_sample': state.arm_sample,
                           'duration_samples': state.plan.duration_samples,
                           'state_at_arm': channel.record('started')})
        if state.arm_sample is None:
            result = process_core(iq)
            with self.lock:
                state.processed_samples = channel.sample_clock
            return result
        output = np.empty_like(iq)
        cursor = 0
        while cursor < len(iq):
            event = None
            changed = False
            boundary = channel.sample_clock
            clocks = time.monotonic_ns(), time.time_ns()
            if state.next_event < len(state.plan.events):
                candidate = state.plan.events[state.next_event]
                if state.arm_sample + candidate.sample_offset == boundary:
                    event = candidate
                    changed = channel.update_settings(dict(event.settings))
            upcoming = state.next_event + (event is not None)
            length = len(iq) - cursor
            if upcoming < len(state.plan.events):
                length = min(length, state.arm_sample + state.plan.events[upcoming].sample_offset - boundary)
            _check(length > 0, 'schedule_boundary_order')
            output[cursor:cursor + length] = process_core(iq[cursor:cursor + length])
            cursor += length
            with self.lock:
                state.processed_samples = channel.sample_clock
                if event is not None:
                    state.next_event += 1
                    state.pending_transitions += 1
                    state.restoration_observed |= event.kind == 'restore'
                    self.emit(direction, 'condition_restored' if event.kind == 'restore' else 'condition_applied',
                              boundary, channel.sample_clock, 'processed_samples',
                              {'event_id': event.event_id, 'kind': event.kind, 'sample_offset': event.sample_offset,
                               'changed': changed, 'settings': dict(event.settings),
                               'processed_samples': channel.sample_clock}, clocks)
        return output

    def forwarded(self, direction, accounting):
        with self.lock:
            self.input_received(direction, accounting)
            state = self.directions[direction]
            state.forwarded_samples = accounting.output_samples
            before = state.schedule_complete
            if state.arm_sample is not None:
                state.schedule_complete = (state.forwarded_samples >= state.arm_sample + state.plan.duration_samples
                                           and state.next_event == len(state.plan.events)
                                           and state.restoration_observed)
            if state.pending_transitions or (state.schedule_complete and not before):
                self.emit(direction, 'progress', state.arm_sample,
                          state.forwarded_samples, 'successfully_forwarded_samples',
                          {'input_messages': state.input_messages, 'output_messages': state.output_messages,
                           'input_samples': state.input_samples, 'output_samples': state.output_samples,
                           'scheduled_end_sample': state.arm_sample + state.plan.duration_samples,
                           'schedule_complete': state.schedule_complete})
                state.pending_transitions = 0

    def relay_final(self, direction, accounting):
        with self.lock:
            self.input_received(direction, accounting)
            state = self.directions[direction]
            state.processed_samples = state.channel.sample_clock
            state.forwarded_samples = accounting.output_samples
            state.relay_finished = True

    def finish(self):
        if self.finished:
            return self.reason is None
        self.finished = True
        self.control_stop.set()
        if self.control_thread_started:
            try:
                self.control_thread.join(1.5)
                if self.control_thread.is_alive():
                    self.fail('control_join_timeout')
            except Exception:
                self.fail('control_join_failed')
        # Complete identity-checked local resource cleanup before publishing
        # final status, so a replacement socket cannot receive a clean final.
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                self.fail('control_socket_close_failed')
        if self.socket_identity is not None:
            try:
                metadata = os.stat('rb.sock', dir_fd=self.directory_fd, follow_symlinks=False)
                _check(stat.S_ISSOCK(metadata.st_mode)
                       and (metadata.st_dev, metadata.st_ino, metadata.st_uid) == self.socket_identity,
                       'control_socket_identity_changed')
                os.unlink('rb.sock', dir_fd=self.directory_fd)
            except (OSError, RuntimeErrorContract):
                self.fail('control_socket_cleanup_failed')
        if self.directory_fd is not None:
            try:
                os.close(self.directory_fd)
            except OSError:
                self.fail('control_directory_close_failed')
            self.directory_fd = None
        with self.lock:
            complete = all(state.schedule_complete and state.relay_finished
                           and state.input_messages == state.output_messages
                           and state.input_samples == state.output_samples
                           and state.processed_samples == state.forwarded_samples
                           for state in self.directions.values())
            if self.writer is not None and self.writer.failed.is_set():
                self.fail('truth_write_failed', emit=False)
            status = 'error' if self.reason or self.fatal_errors else ('complete' if complete else 'incomplete')
            for direction, state in self.directions.items():
                try:
                    self.emit(direction, 'final', 0, state.processed_samples, 'processed_samples',
                              {'status': status, 'reason': self.reason or ('none' if complete else 'schedule_incomplete'),
                               'armed_sample': state.arm_sample,
                               'scheduled_end_sample': None if state.arm_sample is None else state.arm_sample + state.plan.duration_samples,
                               'processed_samples': state.processed_samples, 'forwarded_samples': state.forwarded_samples,
                               'input_messages': state.input_messages, 'output_messages': state.output_messages,
                               'input_samples': state.input_samples, 'output_samples': state.output_samples,
                               'logging_errors': 0 if self.writer is None else self.writer.logging_errors,
                               'all_events_processed': state.next_event == len(state.plan.events),
                               'restoration_observed': state.restoration_observed,
                               'schedule_complete': state.schedule_complete,
                               **({'state_at_finish': state.channel.record('final')}
                                  if self.plan.channel_semantics_version in ('grc_cfo_v1', 'grc_static_tdl_a_v1', 'grc_static_tdl_c_v1') else {})})
                except RuntimeErrorContract:
                    status = 'error'
        if self.metrics is not None and self.writer is not None:
            try:
                self.metrics.final()
            except Exception:
                self.fail('metrics_finalization_failed', emit=False)
        if self.writer is not None and not self.writer.finish():
            self.fail('truth_finalization_failed', emit=False)
        return status == 'complete' and self.reason is None
