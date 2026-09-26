"""Bounded, sample-neutral RAD-08 accounting; no sockets or background threads.

All phases use monotonic integer timestamps. Processing parts inherit their
parent outcome; core calls retain their actual outcome and are nested only.
"""
from __future__ import annotations

import hashlib
import math
import os
import time

UINT64_MAX = (1 << 64) - 1
MAX_WINDOWS = 256
PHASES = ('request_receive', 'request_send', 'upstream_receive', 'processing', 'downstream_send')
PARTS = ('input_prepare', 'channel_chain', 'output_prepare')
OUTCOMES = ('completed', 'stopped', 'error')
ENERGIES = ('input', 'desired', 'noise', 'cw', 'output')


class MetricsError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise MetricsError(reason)


def uint(value):
    require(type(value) is int and 0 <= value <= UINT64_MAX, 'metrics_uint64')
    return value


def add(left, right):
    return uint(uint(left) + uint(right))


def interval(start, end):
    uint(start); uint(end)
    require(end >= start, 'metrics_clock_regression')
    return end - start


def triplets():
    return {outcome: [0, 0, 0] for outcome in OUTCOMES}


def observe(table, outcome, duration):
    require(outcome in OUTCOMES, 'metrics_outcome')
    uint(duration)
    old = table[outcome]
    table[outcome] = [add(old[0], 1), add(old[1], duration), max(old[2], duration)]


def histogram_add(histogram, value):
    bucket = uint(value).bit_length()
    histogram[bucket] = add(histogram[bucket], 1)


def sparse(histogram):
    return [[index, count] for index, count in enumerate(histogram) if count]


def validate_every(value):
    require(type(value) is int and 1 <= value <= 1000000, 'metrics_message_interval')
    return value


class DirectionMetrics:
    """Owned by one relay. The clock is injectable for deterministic fixtures."""

    def __init__(self, owner, direction, channel, every_messages, clock=None):
        self.owner, self.direction, self.channel = owner, direction, channel
        self.every_messages = validate_every(every_messages)
        self.clock = clock or time.monotonic_ns
        self.last_clock = None
        self.window_start = None
        self.window_count = 0
        self.ended = False
        self.phase = self.part = None
        self.phase_start = self.part_start = None
        self.part_durations = {}
        self.input_messages = self.output_messages = 0
        self.input_samples = self.output_samples = 0
        self.completed_processing_samples = 0
        self.rejected_messages = 0
        self.message_samples = None
        self.message_forwarded = False
        self._reset_window()

    def _reset_window(self):
        self.starts = {'input_messages': self.input_messages, 'output_messages': self.output_messages,
                       'input_samples': self.input_samples, 'output_samples': self.output_samples,
                       'completed_processing_samples': self.completed_processing_samples}
        self.starts['rejected_messages'] = self.rejected_messages
        self.processed_start = uint(self.channel.sample_clock)
        self.mask_start = uint(self.channel.masked_samples)
        self.energy_start = dict(self.channel.energies)
        self.phases = {name: triplets() for name in PHASES}
        self.parts = {name: triplets() for name in PARTS}
        self.core = triplets()
        self.message_hist = [0] * 65
        self.processing_hist = [0] * 65

    def _at(self, value=None):
        now = uint(self.clock() if value is None else value)
        if self.last_clock is not None:
            interval(self.last_clock, now)
        self.last_clock = now
        return now

    def start(self):
        require(self.window_start is None and not self.ended, 'metrics_already_started')
        self.window_start = self._at()
        self.phase = 'request_receive'
        self.phase_start = self.window_start

    def begin_exchange(self):
        require(not self.ended and self.window_start is not None, 'metrics_not_active')
        if self.phase is None:
            self.phase = 'request_receive'
            self.phase_start = self._at()
            interval(self.window_start, self.phase_start)
        else:
            require(self.phase == 'request_receive', 'metrics_exchange_phase')

    def _finish_phase(self, outcome, end):
        require(self.phase in PHASES, 'metrics_missing_phase')
        duration = interval(self.phase_start, end)
        if self.phase == 'processing':
            require(self.part in PARTS, 'metrics_missing_processing_part')
            self.part_durations[self.part] = interval(self.part_start, end)
            require(sum(self.part_durations.values()) == duration, 'metrics_parts_do_not_partition')
            for name, span in self.part_durations.items():
                observe(self.parts[name], outcome, span)
            if outcome == 'completed':
                require(self.part == 'output_prepare' and self.message_samples is not None,
                        'metrics_processing_incomplete')
                self.completed_processing_samples = add(self.completed_processing_samples, self.message_samples)
                histogram_add(self.processing_hist, duration)
            self.part = self.part_start = None
            self.part_durations = {}
        observe(self.phases[self.phase], outcome, duration)
        self.phase = self.phase_start = None

    def next_phase(self, name):
        require(name in PHASES and self.phase in PHASES
                and PHASES.index(name) == PHASES.index(self.phase) + 1, 'metrics_phase_order')
        now = self._at()
        self._finish_phase('completed', now)
        self.phase, self.phase_start = name, now
        if name == 'processing':
            self.part, self.part_start = 'input_prepare', now
            self.message_samples = None
            self.message_forwarded = False

    def next_part(self, name):
        require(self.phase == 'processing' and name in PARTS and self.part in PARTS
                and PARTS.index(name) == PARTS.index(self.part) + 1, 'metrics_part_order')
        now = self._at()
        self.part_durations[self.part] = interval(self.part_start, now)
        self.part, self.part_start = name, now

    def received(self, samples):
        require(self.phase == 'processing' and self.part == 'input_prepare'
                and self.message_samples is None, 'metrics_input_phase')
        self.input_messages = add(self.input_messages, 1)
        self.input_samples = add(self.input_samples, samples)
        uint(self.input_samples * 8)
        self.message_samples = uint(samples)
        histogram_add(self.message_hist, samples)

    def rejected(self):
        self.rejected_messages = add(self.rejected_messages, 1)

    def core_call(self, callback, samples):
        require(self.phase == 'processing' and self.part == 'channel_chain', 'metrics_core_phase')
        start = self._at()
        outcome = 'error'
        try:
            result = callback(samples)
            outcome = 'completed'
            return result
        finally:
            observe(self.core, outcome, interval(start, self._at()))

    def forwarded(self, samples):
        require(self.phase == 'downstream_send' and samples == self.message_samples
                and not self.message_forwarded, 'metrics_output_phase')
        self.output_messages = add(self.output_messages, 1)
        self.output_samples = add(self.output_samples, samples)
        uint(self.output_samples * 8)
        self.message_forwarded = True

    def sent(self, samples, end_ns):
        require(self.phase == 'downstream_send' and samples == self.message_samples, 'metrics_output_phase')
        if not self.message_forwarded:
            self.forwarded(samples)
        self._finish_phase('completed', self._at(end_ns))
        if self.output_messages - self.starts['output_messages'] == self.every_messages:
            self._flush(end_ns, False)

    def _flush(self, end, final):
        require(self.phase is None and self.window_start is not None, 'metrics_window_active_phase')
        elapsed = interval(self.window_start, end)
        require(elapsed > 0, 'metrics_zero_window')
        require(self.window_count < MAX_WINDOWS, 'metrics_window_budget')
        phase_sum = sum(table[outcome][1] for table in self.phases.values() for outcome in OUTCOMES)
        require(phase_sum <= elapsed, 'metrics_phase_exceeds_window')
        for outcome in OUTCOMES:
            require(sum(table[outcome][1] for table in self.parts.values())
                    == self.phases['processing'][outcome][1], 'metrics_part_conservation')
        require(sum(row[1] for row in self.core.values())
                <= sum(row[1] for row in self.parts['channel_chain'].values()), 'metrics_core_exceeds_chain')
        processed_end = uint(self.channel.sample_clock)
        require(processed_end >= self.processed_start, 'metrics_sample_regression')
        energies = {key: self.channel.energies[key] - self.energy_start[key] for key in ENERGIES}
        require(all(type(value) in (float, int) and math.isfinite(value) and value >= 0
                    for value in energies.values()), 'metrics_invalid_energy')
        self.window_count += 1
        details = {'window_id': self.window_count, 'final_partial': final,
                   'start_ns': self.window_start, 'end_ns': end, 'wall_ns': elapsed,
                   'loop_overhead_ns': elapsed - phase_sum,
                   **{key: getattr(self, key) - initial for key, initial in self.starts.items()},
                   'input_sample_start': self.starts['input_samples'], 'input_sample_end': self.input_samples,
                   'output_sample_start': self.starts['output_samples'], 'output_sample_end': self.output_samples,
                   'processed_sample_start': self.processed_start, 'processed_sample_end': processed_end,
                   'intentional_mask_samples': uint(self.channel.masked_samples - self.mask_start),
                   'energies': energies, 'phases': self.phases, 'processing_parts': self.parts,
                   'dsp_core': self.core, 'message_samples_histogram': sparse(self.message_hist),
                   'processing_ns_histogram': sparse(self.processing_hist)}
        details['input_bytes'] = uint(details['input_samples'] * 8)
        details['output_bytes'] = uint(details['output_samples'] * 8)
        self.owner.emit(self.direction, 'window', self.processed_start, processed_end, details)
        self.window_start = end
        self._reset_window()

    def finish_window(self, outcome='stopped'):
        if self.ended:
            return
        self.ended = True
        if self.window_start is not None:
            end = self._at()
            if self.phase is not None:
                self._finish_phase(outcome, end)
            self._flush(end, True)

    def final(self, failed, logging_errors):
        processed = uint(self.channel.sample_clock)
        complete = (self.ended and self.input_messages == self.output_messages
                    and self.input_samples == self.output_samples == processed
                    and self.completed_processing_samples == processed)
        return {'window_count': self.window_count, 'input_messages': self.input_messages,
                'output_messages': self.output_messages, 'input_samples': self.input_samples,
                'output_samples': self.output_samples, 'processed_samples': processed,
                'completed_processing_samples': self.completed_processing_samples,
                'status': 'error' if failed else ('complete' if complete else 'incomplete'),
                'logging_errors': uint(logging_errors)}


class MetricsRuntime:
    def __init__(self, schedule, every_messages):
        self.schedule = schedule
        self.every_messages = validate_every(every_messages)
        self.config_sha256 = hashlib.sha256(
            f'radio_broker_metrics_v1\n{every_messages}\n'.encode('ascii')).hexdigest()
        self.sequence = 0
        self.directions = {name: DirectionMetrics(self, name, state.channel, every_messages)
                           for name, state in schedule.directions.items()}

    def emit(self, direction, event_type, start=None, end=None, details=None):
        with self.schedule.lock:
            self.sequence = add(self.sequence, 1)
            self.schedule.emit_metrics(self.sequence, self.config_sha256, direction,
                                       event_type, start, end, details or {})

    def start(self):
        self.emit('control', 'started', details={'every_messages': self.every_messages,
                  'max_windows': MAX_WINDOWS, 'histogram': 'uint64_bit_length',
                  'sample_rate_hz': self.schedule.plan.sample_rate_hz, 'pid': os.getpid()})

    def final(self):
        failed = bool(self.schedule.reason or self.schedule.fatal_errors or self.schedule.writer.failed.is_set())
        for name, state in self.directions.items():
            self.emit(name, 'final', 0, state.channel.sample_clock,
                      state.final(failed, self.schedule.writer.logging_errors))
