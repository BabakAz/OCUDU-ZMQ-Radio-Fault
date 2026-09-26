#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: OCUDU ZMQ Legacy LTE-profile Channel Broker
#
# Description:
#   Exploratory ZMQ broker for OCUDU waveforms using legacy LTE profiles.
#
#   Capabilities BEYOND the C broker (zmq_channel_broker):
#     1. Frequency-selective fading (3GPP EPA/EVA/ETU multi-tap FIR)
#        — causes ISI that stresses the equalizer
#     2. Carrier Frequency Offset (CFO) injection
#        — phase drift that stresses synchronization tracking
#     3. Whole-message zeroing (legacy optional mode)
#        — abstract blanking; channel clocks currently freeze on zeroed messages
#     4. Time-varying scenarios (Drive-by, Urban Walk, Edge-of-cell)
#        — automatically varies parameters for diverse telemetry
#     5. Live QT GUI with interactive sliders and real-time visualization

import sys
import builtins
import signal
import math
import collections
import json
import threading
import time as _time
import os
import errno
from types import MappingProxyType

import numpy as np

import zmq as _zmq
from scipy.special import j0 as bessel_j0


def study_channel_type(semantics):
    # File-based imports of the standalone legacy broker need no study modules.
    if semantics == 'grc_static_tdl_a_v1':
        from radio_static_tdl import TdlReferenceChannel
        return TdlReferenceChannel
    if semantics == 'grc_static_tdl_c_v1':
        from radio_static_tdl import TdlCReferenceChannel
        return TdlCReferenceChannel
    return CfoReferenceChannel if semantics == 'grc_cfo_v1' else FixedReferenceChannel


# Serialize this module's complete print calls across relay/control threads.
# Builtin print writes the text and newline separately, even with flush=True.
# This local wrapper never changes builtins.print for other modules. RLock also
# permits the main-thread signal handler to report while it owns the log lock.
_LOG_PRINT_LOCK = threading.RLock()


def print(*args, **kwargs):
    with _LOG_PRINT_LOCK:
        return builtins.print(*args, **kwargs)


# ── 3GPP Delay Profiles (TS 36.104 Table B.2) ───────────────────────────────

DELAY_PROFILES = {
    'epa': {  # Extended Pedestrian A — 7 taps, max delay 410 ns
        'delays_ns': [0, 30, 70, 90, 110, 190, 410],
        'powers_db': [0.0, -1.0, -2.0, -3.0, -8.0, -17.2, -20.8],
        'default_doppler': 5.0,
    },
    'eva': {  # Extended Vehicular A — 9 taps, max delay 2510 ns
        'delays_ns': [0, 30, 150, 310, 370, 710, 1090, 1730, 2510],
        'powers_db': [0.0, -1.5, -1.4, -3.6, -0.6, -9.1, -7.0, -12.0, -16.9],
        'default_doppler': 70.0,
    },
    'etu': {  # Extended Typical Urban — 9 taps, max delay 5000 ns
        'delays_ns': [0, 50, 120, 200, 230, 500, 1600, 2300, 5000],
        'powers_db': [-1.0, -1.0, -1.0, 0.0, 0.0, 0.0, -3.0, -5.0, -7.0],
        'default_doppler': 300.0,
    },
}

FADING_MODES = {
    0: "Off (AWGN only)",
    1: "Flat Rician",
    2: "Flat Rayleigh",
    3: "EPA (7-tap, 410 ns)",
    4: "EVA (9-tap, 2510 ns)",
    5: "ETU (9-tap, 5000 ns)",
}

INT_TYPES = {
    0: "None",
    1: "CW Tone",
    2: "Narrowband (1 PRB)",
}
INT_TYPE_NAMES = {0: "none", 1: "cw", 2: "narrowband"}

FADING_MODE_PROFILES = {3: 'epa', 4: 'eva', 5: 'etu'}
MAX_ZMQ_MESSAGE_BYTES = 64 * 1024 * 1024
UINT64_MAX = (1 << 64) - 1
RELAY_PROGRESS_MESSAGES = 10_000
FADING_SOS_TERMS = 16
FADING_MIN_INTERPOLATION_RATE_HZ = 2_500.0
FADING_DOPPLER_OVERSAMPLE = 8.0

SCENARIO_NAMES = {
    0: "Manual",
    1: "Drive-by (30s cycle)",
    2: "Urban Walk (random)",
    3: "Edge of Cell (60s ramp)",
}


RADIO_COMPONENT_TAGS = {
    'awgn': 0x4157474E, 'flat': 0x464C4154, 'taps': 0x54415053,
    'erasure': 0x4D41534B, 'scenario': 0x5343454E,
}


def radio_component_seed(master_seed, direction, component):
    """Stable uint32 domain separation; non-AWGN components are reserved only."""
    if (not isinstance(master_seed, int) or isinstance(master_seed, bool)
            or not 0 <= master_seed <= 0xFFFFFFFF):
        raise ValueError("master seed must be uint32")
    if direction not in ('DL', 'UL') or component not in RADIO_COMPONENT_TAGS:
        raise ValueError("unknown radio RNG direction or component")
    tag = 0x0D1A5EED if direction == 'DL' else 0x00A17EED
    value = master_seed ^ tag ^ RADIO_COMPONENT_TAGS[component]
    value ^= value >> 16
    value = (value * 0x7FEB352D) & 0xFFFFFFFF
    value ^= value >> 15
    value = (value * 0x846CA68B) & 0xFFFFFFFF
    return value ^ (value >> 16)


class FixedReferenceChannel:
    """Versioned gain + fixed-reference AWGN + sample-clocked CW core.

    Mode/reference/rate/seeds are immutable; scheduled coefficients retain state.
    Components are rounded to cf32, summed in binary64 and rounded to cf32.
    Energies cover processed samples, which can exceed successful sends on abort.
    """

    def __init__(self, direction, sample_rate_hz, master_seed, *, ref_power,
                 mode='fixed', gain=1.0, noise_snr_db=28.0, noise_enabled=True,
                 cw_enabled=False, cw_sir_db=20.0, cw_freq_hz=0.0):
        def finite(name, value, lower, upper):
            if isinstance(value, bool):
                raise ValueError(f"{name} requires a finite number")
            value = float(value)
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{name} must be finite in [{lower}, {upper}]")
            return value

        sample_rate_hz = finite('sample rate', sample_rate_hz, 1000.0, 250e6)
        ref_power = finite('reference power', ref_power, 1e-20, 1e10)
        gain = finite('gain', gain, 0.0, 1.0)
        noise_snr_db = finite('noise SNR', noise_snr_db, -100.0, 100.0)
        cw_sir_db = finite('CW SIR', cw_sir_db, -100.0, 100.0)
        cw_freq_hz = finite('CW frequency', cw_freq_hz, -sample_rate_hz / 2, sample_rate_hz / 2)
        if mode not in ('identity', 'fixed'):
            raise ValueError("fixed-reference mode must be identity or fixed")
        if not isinstance(noise_enabled, bool) or not isinstance(cw_enabled, bool):
            raise ValueError("noise/CW enable flags must be bool")
        self.awgn_seed = radio_component_seed(master_seed, direction, 'awgn')
        self.master_seed = master_seed
        self.direction = direction
        self.samp_rate = sample_rate_hz
        self.mode = mode
        self.config = MappingProxyType({
            'mode': mode, 'ref_power': ref_power, 'gain': gain,
            'noise_enabled': noise_enabled and mode != 'identity',
            'noise_snr_db': noise_snr_db,
            'cw_enabled': cw_enabled and mode != 'identity',
            'cw_sir_db': cw_sir_db, 'cw_freq_hz': cw_freq_hz,
        })
        self.rng = np.random.Generator(np.random.PCG64(self.awgn_seed))
        # Binary64 operations are deliberate and match the C profile, including
        # rounding near a uint64 tick and the +/-Nyquist equivalence.
        step = math.floor(math.ldexp(abs(cw_freq_hz) / sample_rate_hz, 64) + 0.5)
        self.cw_step_u64 = (-step if cw_freq_hz < 0.0 else step) & UINT64_MAX
        self.phase_u64 = 0
        self.sample_clock = 0
        self.awgn_complex_draws = 0
        self.awgn_normal_draws = 0
        self.masked_samples = 0
        self.attenuated_samples = 0
        self.energies = dict.fromkeys(('input', 'desired', 'noise', 'cw', 'output'), 0.0)
        self.noise_std = math.sqrt(ref_power * 10.0 ** (-noise_snr_db / 10.0) / 2.0)
        self.cw_amplitude = math.sqrt(ref_power * 10.0 ** (-cw_sir_db / 10.0))

    def update_settings(self, settings):
        """Atomically validate coefficients without resetting any channel state."""
        keys = {'gain', 'noise_enabled', 'noise_snr_db', 'cw_enabled', 'cw_sir_db', 'cw_freq_hz'}
        if not isinstance(settings, dict) or set(settings) != keys:
            raise ValueError('scheduled settings require exactly six mutable coefficients')
        updated = dict(self.config)
        for key in ('noise_enabled', 'cw_enabled'):
            if type(settings[key]) is not bool:
                raise ValueError('scheduled enable flags must be bool')
            updated[key] = settings[key]
        for key, lower, upper in (('gain', 0.0, 1.0), ('noise_snr_db', -100.0, 100.0),
                                  ('cw_sir_db', -100.0, 100.0),
                                  ('cw_freq_hz', -self.samp_rate / 2, self.samp_rate / 2)):
            value = settings[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError('scheduled coefficient must be numeric')
            value = float(value)
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError('scheduled coefficient outside supported range')
            updated[key] = value
        changed = updated != dict(self.config)
        if self.mode == 'identity' and changed:
            raise ValueError('identity schedule accepts only no-op coefficients')
        frequency = updated['cw_freq_hz']
        step = math.floor(math.ldexp(abs(frequency) / self.samp_rate, 64) + 0.5)
        noise_std = math.sqrt(updated['ref_power'] * 10.0 ** (-updated['noise_snr_db'] / 10.0) / 2.0)
        amplitude = math.sqrt(updated['ref_power'] * 10.0 ** (-updated['cw_sir_db'] / 10.0))
        self.config = MappingProxyType(updated)
        self.noise_std, self.cw_amplitude = noise_std, amplitude
        self.cw_step_u64 = (-step if frequency < 0 else step) & UINT64_MAX
        return changed

    @staticmethod
    def _energy(iq):
        real = iq.real.astype(np.float64)
        imag = iq.imag.astype(np.float64)
        return float(np.sum(real * real + imag * imag, dtype=np.float64))

    def process(self, iq):
        if (not isinstance(iq, np.ndarray) or iq.dtype != np.complex64
                or iq.ndim != 1 or not np.isfinite(iq).all()):
            raise ValueError("fixed-reference input must be finite one-dimensional cf32")
        n = len(iq)
        fixed = self.mode == 'fixed'
        masked = n if fixed and self.config['gain'] == 0.0 else 0
        attenuated = n if fixed and 0.0 < self.config['gain'] < 1.0 else 0
        increments = {
            'sample_clock': n, 'awgn_complex_draws': n if fixed else 0,
            'awgn_normal_draws': 2 * n if fixed else 0,
            'masked_samples': masked, 'attenuated_samples': attenuated,
        }
        for name, increment in increments.items():
            if getattr(self, name) > UINT64_MAX - increment:
                raise OverflowError(f"fixed-reference {name} exceeds uint64")
        if not fixed:
            desired = output = iq
            energies = {'input': self._energy(iq), 'noise': 0.0, 'cw': 0.0}
            energies['desired'] = energies['output'] = energies['input']
        else:
            desired = np.empty(n, dtype=np.complex64)
            np.multiply(iq.real, self.config['gain'], out=desired.real, dtype=np.float64)
            np.multiply(iq.imag, self.config['gain'], out=desired.imag, dtype=np.float64)
            # Consume the same two float32 normal variates per complex sample,
            # even with noise disabled, a zero input, or a desired-signal mask.
            noise_reals = self.rng.standard_normal(2 * n, dtype=np.float32)
            if self.config['noise_enabled']:
                np.multiply(noise_reals, self.noise_std, out=noise_reals, dtype=np.float64)
            else:
                noise_reals.fill(0.0)
            noise = noise_reals.view(np.complex64)
            cw = np.zeros(n, dtype=np.complex64)
            if self.config['cw_enabled']:
                phases = np.arange(n, dtype=np.uint64)
                phases *= np.uint64(self.cw_step_u64)
                phases += np.uint64(self.phase_u64)
                angles = np.ldexp(phases.astype(np.float64), -64) * (2.0 * math.pi)
                np.multiply(np.cos(angles), self.cw_amplitude, out=cw.real)
                np.multiply(np.sin(angles), self.cw_amplitude, out=cw.imag)
            output = np.empty(n, dtype=np.complex64)
            output.real = desired.real.astype(np.float64) + cw.real + noise.real
            output.imag = desired.imag.astype(np.float64) + cw.imag + noise.imag
            if not np.isfinite(output).all():
                raise ValueError("fixed-reference processing produced nonfinite output")
            # Reuse only mathematically identical energy sums. Keep the output
            # arithmetic and every RNG draw above, including signed-zero effects.
            input_energy = self._energy(iq)
            gain = self.config['gain']
            desired_energy = (input_energy if gain == 1.0 else 0.0 if gain == 0.0
                              else self._energy(desired))
            additions = self.config['noise_enabled'] or self.config['cw_enabled']
            energies = {
                'input': input_energy, 'desired': desired_energy,
                'noise': self._energy(noise) if self.config['noise_enabled'] else 0.0,
                'cw': self._energy(cw) if self.config['cw_enabled'] else 0.0,
                'output': self._energy(output) if additions else desired_energy,
            }
        for name, energy in energies.items():
            if not math.isfinite(self.energies[name] + energy):
                raise OverflowError("fixed-reference cumulative energy exceeds binary64")
        for name, increment in increments.items():
            setattr(self, name, getattr(self, name) + increment)
        for name, energy in energies.items():
            self.energies[name] += energy
        if fixed:
            self.phase_u64 = (self.phase_u64 + n * self.cw_step_u64) & UINT64_MAX
        return output

    def record(self, record_type):
        if record_type not in ('started', 'final'):
            raise ValueError("unsupported fixed-reference record type")
        rng_state = self.rng.bit_generator.state
        return {
            'schema_version': 'radio_fixed_profile_v1', 'record_type': record_type,
            'backend': 'grc', 'direction': self.direction,
            'channel_semantics_version': 'fixed_reference_v1',
            'rng_version': 'component_streams_v1',
            'rng_algorithm': 'numpy.PCG64+standard_normal_float32',
            'rng_draw_definition': 'two real float32 normal variates per complex draw',
            'rng_library_version': np.__version__,
            'awgn_state_hex': format(rng_state['state']['state'], '032x'),
            'awgn_increment_hex': format(rng_state['state']['inc'], '032x'),
            'awgn_has_uint32': bool(rng_state['has_uint32']),
            'awgn_cached_uint32': int(rng_state['uinteger']),
            'master_seed': self.master_seed, 'awgn_seed': self.awgn_seed,
            'sample_rate_hz': self.samp_rate, **self.config,
            'cw_step_u64': self.cw_step_u64, 'sample_clock': self.sample_clock,
            'awgn_complex_draws': self.awgn_complex_draws,
            'awgn_normal_draws': self.awgn_normal_draws, 'phase_u64': self.phase_u64,
            'masked_samples': self.masked_samples, 'attenuated_samples': self.attenuated_samples,
            **{name + '_energy': energy for name, energy in self.energies.items()},
            'units': 'relative_digital_complex_power',
            'energy_definition': 'sum(I_squared + Q_squared), not a sample mean',
            'scope': 'cumulative_processed_samples',
        }


class CfoReferenceChannel(FixedReferenceChannel):
    """GRC-only CFO rotation, with exact zero-CFO pass-through and no RNG draws.

    This separate semantic uses the existing apply_cfo DSP. Its phase advances
    only while CFO is nonzero and survives coefficient changes. All other
    impairment components are forbidden, including desired-signal scaling.
    """

    def __init__(self, *args, cfo_hz=0.0, noise_enabled=False, **kwargs):
        super().__init__(*args, noise_enabled=noise_enabled, **kwargs)
        # Validate the supplied additions before identity mode could mask them.
        if noise_enabled or kwargs.get('cw_enabled', False):
            raise ValueError('grc_cfo_v1 requires disabled additions')
        from radio_broker_schedule import validate_settings, mutable_settings
        settings = validate_settings({**mutable_settings(self.config), 'cfo_hz': cfo_hz},
                                     self.samp_rate, cfo=True)
        if self.mode == 'identity' and settings['cfo_hz'] != 0:
            raise ValueError('identity requires zero CFO')
        self.config = MappingProxyType({**self.config, **settings})
        self.cfo_phase = [0.0]
        self.cfo_applied_samples = 0

    def update_settings(self, settings):
        from radio_broker_schedule import validate_settings
        updated = {**self.config, **validate_settings(settings, self.samp_rate, cfo=True)}
        changed = updated != dict(self.config)
        if self.mode == 'identity' and changed:
            raise ValueError('identity schedule accepts only no-op coefficients')
        self.config = MappingProxyType(updated)
        return changed

    def process(self, iq):
        if (not isinstance(iq, np.ndarray) or iq.dtype != np.complex64
                or iq.ndim != 1 or not np.isfinite(iq).all()):
            raise ValueError('CFO input must be finite one-dimensional cf32')
        n = len(iq)
        affected = n if self.config['cfo_hz'] != 0 else 0
        if self.sample_clock > UINT64_MAX - n or self.cfo_applied_samples > UINT64_MAX - affected:
            raise OverflowError('CFO sample counter exceeds uint64')
        phase = list(self.cfo_phase)
        output = apply_cfo(iq, self.config['cfo_hz'], self.samp_rate, phase) if affected else iq
        if not np.isfinite(output).all():
            raise ValueError('CFO processing produced nonfinite output')
        input_energy = self._energy(iq)
        energies = {'input': input_energy, 'desired': input_energy,
                    'noise': 0.0, 'cw': 0.0, 'output': self._energy(output)}
        if any(not math.isfinite(self.energies[name] + energy) for name, energy in energies.items()):
            raise OverflowError('CFO cumulative energy exceeds binary64')
        self.sample_clock += n
        self.cfo_applied_samples += affected
        self.cfo_phase[:] = phase
        for name, energy in energies.items():
            self.energies[name] += energy
        return output

    def record(self, record_type):
        result = super().record(record_type)
        result.update(schema_version='radio_grc_cfo_profile_v1',
                      channel_semantics_version='grc_cfo_v1', rng_algorithm='none_cfo_only',
                      rng_draw_definition='no random draws; deterministic CFO rotation only',
                      cfo_phase_rad=self.cfo_phase[0], cfo_applied_samples=self.cfo_applied_samples)
        return result


def validate_cf32_payload(raw, description):
    """Validate one complete, single-frame cf32 payload without changing bytes.

    A zero-length single frame is legal and advances the message count only.
    The native-endian complex64 layout is the existing local radio contract.
    """
    if len(raw) > MAX_ZMQ_MESSAGE_BYTES:
        raise ValueError(
            f"{description} payload length {len(raw)} exceeds "
            f"{MAX_ZMQ_MESSAGE_BYTES} bytes"
        )
    if len(raw) % np.dtype(np.complex64).itemsize != 0:
        raise ValueError(
            f"malformed IQ payload length {len(raw)} is not a multiple of 8"
        )
    iq = np.frombuffer(raw, dtype=np.complex64)
    if not np.isfinite(iq).all():
        raise ValueError(f"{description} contains nonfinite cf32 samples")
    return iq


def validate_local_endpoint(value):
    """Validate an explicit loopback TCP or filesystem IPC address.

    The caller owns endpoint lifecycle. In particular, an IPC pathname alone
    does not authenticate its peer or establish private-directory ownership.
    """
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("endpoint must be a string without control characters")
    prefix = "tcp://127.0.0.1:"
    if value.startswith(prefix):
        port = value[len(prefix):]
        if (not port or not all('0' <= c <= '9' for c in port)
                or port.startswith('0') or len(port) > 5
                or not 1 <= int(port) <= 65535):
            raise ValueError("loopback TCP port must be canonical decimal in 1..65535")
        return value
    if value.startswith("ipc://"):
        path = value[len("ipc://"):]
        if (not path.startswith('/') or path == '/' or '*' in path
                or any(part in ('', '.', '..') for part in path.split('/')[1:])):
            raise ValueError("IPC requires an absolute, nonempty filesystem path without traversal")
        if len(os.fsencode(path)) > 107:
            raise ValueError("IPC filesystem path exceeds the 107-byte Linux limit")
        return value
    raise ValueError("endpoint must use tcp://127.0.0.1:port or ipc:///absolute/path")


class RelayAccounting:
    """Complete legal-input/successful-output counts, separate from legacy rates.

    These uint64-bounded counters include empty messages and every finite
    sample, including zero-valued/blanked samples. A send failure leaves the
    validated input visible with no invented successful output. This small
    final schema is not a campaign truth or sample-scheduling record.
    """

    def __init__(self, direction, identity):
        self.direction = direction
        self.identity = bool(identity)
        self.input_messages = 0
        self.input_samples = 0
        self.output_messages = 0
        self.output_samples = 0
        self.error_count = 0

    @staticmethod
    def _next_counts(messages, samples, added_samples):
        if not isinstance(added_samples, int) or isinstance(added_samples, bool) or added_samples < 0:
            raise ValueError("sample count must be a non-negative integer")
        if messages >= UINT64_MAX or samples > UINT64_MAX - added_samples:
            raise OverflowError("relay accounting exceeds uint64")
        return messages + 1, samples + added_samples

    def record_input(self, samples):
        self.input_messages, self.input_samples = self._next_counts(
            self.input_messages, self.input_samples, samples
        )

    def check_output(self, samples):
        self._next_counts(self.output_messages, self.output_samples, samples)

    def record_output(self, samples):
        self.output_messages, self.output_samples = self._next_counts(
            self.output_messages, self.output_samples, samples
        )

    def final_record(self, sibling_failed):
        if self.error_count or sibling_failed:
            status = "error"
        elif (self.input_messages != self.output_messages
              or self.input_samples != self.output_samples):
            status = "incomplete"
        else:
            # A clean externally stopped relay is not a completed campaign.
            status = "stopped"
        return {
            "schema_version": "radio_broker_accounting_v1",
            "record_type": "final", "backend": "grc",
            "direction": self.direction, "identity": self.identity,
            "input_messages": self.input_messages,
            "input_samples": self.input_samples,
            "output_messages": self.output_messages,
            "output_samples": self.output_samples,
            "error_count": self.error_count, "status": status,
        }


class RelayMetrics:
    """Bounded relay-rate and processing telemetry for one direction.

    Processing time covers IQ copy, impairments, and serialization. The relay
    rate additionally reflects peer pacing and ZMQ backpressure. Latency
    samples are retained only until the next progress record, so long-running
    brokers have bounded memory use.
    """

    def __init__(self, label, samp_rate, progress_messages=RELAY_PROGRESS_MESSAGES):
        self.label = label
        self.samp_rate = float(samp_rate)
        self.progress_messages = int(progress_messages)
        if not np.isfinite(self.samp_rate) or self.samp_rate <= 0.0 or self.progress_messages <= 0:
            raise ValueError("relay metrics require positive rate and interval")
        self.started_ns = None
        self.interval_started_ns = None
        self.messages = 0
        self.samples = 0
        self.payload_bytes = 0
        self.drops = 0
        self.processing_total_ns = 0
        self.processing_max_ns = 0
        self._interval_messages = 0
        self._interval_samples = 0
        self._interval_payload_bytes = 0
        self._interval_processing_ns = []
        self.finalized = False
        self.last_observed_ns = None

    @staticmethod
    def _elapsed_seconds(started_ns, now_ns):
        elapsed = int(now_ns) - int(started_ns)
        if elapsed <= 0:
            raise ValueError('relay metric interval must have positive elapsed time')
        return elapsed / 1e9

    def start(self, now_ns=None):
        if self.started_ns is not None:
            raise ValueError('relay metrics already started')
        self.started_ns = _time.monotonic_ns() if now_ns is None else int(now_ns)
        self.interval_started_ns = self.started_ns
        self.last_observed_ns = self.started_ns

    def observe(self, samples, payload_bytes, processing_ns, dropped=False, now_ns=None):
        now_ns = _time.monotonic_ns() if now_ns is None else int(now_ns)
        samples = int(samples)
        payload_bytes = int(payload_bytes)
        processing_ns = int(processing_ns)
        if processing_ns < 0:
            raise ValueError('relay processing duration must be nonnegative')
        if samples < 0 or payload_bytes != samples * 8:
            raise ValueError("relay metrics require valid cf32 message sizes")
        if self.started_ns is None:
            raise ValueError('relay metrics must start before measured work')
        if self.finalized or now_ns < self.last_observed_ns:
            raise ValueError('relay metrics finalized or monotonic clock regressed')
        self.last_observed_ns = now_ns

        self.messages += 1
        self.samples += samples
        self.payload_bytes += payload_bytes
        self.drops += int(bool(dropped))
        self.processing_total_ns += processing_ns
        self.processing_max_ns = max(self.processing_max_ns, processing_ns)
        self._interval_messages += 1
        self._interval_samples += samples
        self._interval_payload_bytes += payload_bytes
        self._interval_processing_ns.append(processing_ns)

        if self._interval_messages < self.progress_messages:
            return None
        payload = self._interval_payload(now_ns)
        self.interval_started_ns = now_ns
        self._interval_messages = 0
        self._interval_samples = 0
        self._interval_payload_bytes = 0
        self._interval_processing_ns.clear()
        return "GRC_RELAY_PROGRESS: " + json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )

    def _interval_payload(self, now_ns):
        elapsed_s = self._elapsed_seconds(self.interval_started_ns, now_ns)
        processing = np.asarray(self._interval_processing_ns, dtype=np.float64)
        waveform_ns = self._interval_samples / self.samp_rate * 1e9
        payload = {
            "component": "ocudu-grc-relay-metric",
            "direction": self.label,
            "elapsed_s": elapsed_s,
            "start_ns": self.interval_started_ns, "end_ns": now_ns,
            "elapsed_ns": now_ns - self.interval_started_ns,
            "interval_messages": self._interval_messages,
            "interval_payload_bytes": self._interval_payload_bytes,
            "interval_samples": self._interval_samples,
            "iq_sample_rate_msps": round(self._interval_samples / elapsed_s / 1e6, 6),
            "message_rate_hz": round(self._interval_messages / elapsed_s, 6),
            "processing_budget_mean_pct": (round(
                100.0 * float(processing.sum()) / waveform_ns, 6
            ) if waveform_ns else None),
            "processing_max_us": round(float(processing.max()) / 1e3, 6) if len(processing) else None,
            "processing_mean_us": round(float(processing.mean()) / 1e3, 6) if len(processing) else None,
            "processing_p95_us": round(float(np.percentile(processing, 95)) / 1e3, 6) if len(processing) else None,
            "raw_payload_gbps": round(
                self._interval_payload_bytes * 8.0 / elapsed_s / 1e9, 6
            ),
            "schema_version": 2,
            "scope": "interval",
            "total_messages": self.messages,
        }
        reasons = {key: ('no_samples' if key == 'processing_budget_mean_pct' else 'no_messages')
                   for key, value in payload.items() if value is None}
        if reasons:
            payload['undefined_reasons'] = reasons
        return payload

    def partial(self, now_ns=None):
        if self.finalized:
            return None
        self.finalized = True
        if self.started_ns is None:
            return None
        now_ns = _time.monotonic_ns() if now_ns is None else int(now_ns)
        if now_ns < self.last_observed_ns:
            raise ValueError('relay metric clock regressed')
        self.last_observed_ns = now_ns
        if now_ns == self.interval_started_ns:
            return None
        payload = self._interval_payload(now_ns)
        payload['final_partial'] = True
        self._interval_messages = self._interval_samples = self._interval_payload_bytes = 0
        self._interval_processing_ns.clear()
        self.interval_started_ns = now_ns
        return 'GRC_RELAY_PARTIAL: ' + json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def summary(self, now_ns=None):
        now_ns = _time.monotonic_ns() if now_ns is None else int(now_ns)
        if self.last_observed_ns is not None and now_ns < self.last_observed_ns:
            raise ValueError('relay metric clock regressed')
        if self.started_ns is None:
            elapsed_s = message_rate_hz = iq_sample_rate_msps = raw_payload_gbps = None
        else:
            elapsed_s = self._elapsed_seconds(self.started_ns, now_ns) if now_ns != self.started_ns else None
            message_rate_hz = self.messages / elapsed_s if elapsed_s else None
            iq_sample_rate_msps = self.samples / elapsed_s / 1e6 if elapsed_s else None
            raw_payload_gbps = self.payload_bytes * 8.0 / elapsed_s / 1e9 if elapsed_s else None
        processing_mean_us = self.processing_total_ns / self.messages / 1e3 if self.messages else None
        waveform_ns = self.samples / self.samp_rate * 1e9
        processing_budget_mean_pct = 100.0 * self.processing_total_ns / waveform_ns if waveform_ns else None
        payload = {
            "component": "ocudu-grc-relay-metric",
            "direction": self.label,
            "dropped_messages": self.drops,
            "elapsed_s": elapsed_s,
            "start_ns": self.started_ns, "end_ns": now_ns if self.started_ns is not None else None,
            "elapsed_ns": now_ns - self.started_ns if self.started_ns is not None else None,
            "iq_sample_rate_msps": round(iq_sample_rate_msps, 6) if iq_sample_rate_msps is not None else None,
            "message_rate_hz": round(message_rate_hz, 6) if message_rate_hz is not None else None,
            "processing_budget_mean_pct": (round(processing_budget_mean_pct, 6)
                                           if processing_budget_mean_pct is not None else None),
            "processing_max_us": round(self.processing_max_ns / 1e3, 6) if self.messages else None,
            "processing_mean_us": round(processing_mean_us, 6) if processing_mean_us is not None else None,
            "raw_payload_gbps": round(raw_payload_gbps, 6) if raw_payload_gbps is not None else None,
            "schema_version": 2,
            "scope": "run",
            "total_messages": self.messages,
            "total_payload_bytes": self.payload_bytes,
            "total_samples": self.samples,
        }
        reasons = {}
        for key, value in payload.items():
            if value is None:
                reasons[key] = ('no_samples' if key == 'processing_budget_mean_pct' else
                                'no_messages' if key.startswith('processing_') else
                                'not_started' if self.started_ns is None else 'zero_elapsed_time')
        if reasons:
            payload['undefined_reasons'] = reasons
        return "GRC_RELAY_SUMMARY: " + json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)


# ── Flat Fading (message-updated AR(1) approximation) ───────────────────────

class FadingState:
    """Rician/Rayleigh scatter with a message-duration J0 AR(1) coefficient.

    This matches one chosen correlation lag, not the full Jakes time law.
    Gaussian scatter also gives no deterministic positive Rician fade floor.
    """

    def __init__(self, enabled, doppler, samp_rate, k_db, rng):
        self.enabled = enabled
        self.doppler = doppler
        self.samp_rate = samp_rate
        self.rng = rng
        self._set_k(k_db)
        if enabled:
            self.h_I = rng.standard_normal() * math.sqrt(0.5)
            self.h_Q = rng.standard_normal() * math.sqrt(0.5)
        else:
            self.h_I = 1.0
            self.h_Q = 0.0

    def _set_k(self, k_db):
        k_lin = 10.0 ** (k_db / 10.0)
        kp1 = k_lin + 1.0
        self.los_amp = math.sqrt(k_lin / kp1)
        self.scatter_amp = math.sqrt(1.0 / kp1)

    def update_and_apply(self, iq):
        if not self.enabled:
            return iq
        n = len(iq)
        T = n / self.samp_rate
        alpha = float(bessel_j0(2.0 * math.pi * self.doppler * T))
        sigma = math.sqrt(max(0.0, (1.0 - alpha * alpha) * 0.5))
        n1, n2 = self.rng.standard_normal(2)
        self.h_I = alpha * self.h_I + sigma * n1
        self.h_Q = alpha * self.h_Q + sigma * n2
        h = complex(self.los_amp + self.scatter_amp * self.h_I,
                    self.scatter_amp * self.h_Q)
        return iq * np.complex64(h)

    def reconfigure(self, enabled, doppler, k_db=None):
        self.enabled = enabled
        self.doppler = doppler
        if k_db is not None:
            self._set_k(k_db)


# ── Frequency-Selective Fading (3GPP multi-tap) ─────────────────────────────

class FrequencySelectiveFading:
    """Multi-tap freq-selective fading with 3GPP EPA/EVA/ETU delay profiles.

    Each tap uses an independent deterministic sum-of-sinusoids approximation
    to Jake's spectrum, evaluated from an absolute IQ-sample clock. Complex
    coefficients are linearly interpolated on a fixed absolute-sample grid
    running at no less than 2.5 kHz and eight times the configured Doppler before
    a causal sparse FIR is applied. Consequently, the channel trajectory
    depends on sample time and seed, never on arbitrary ZMQ message boundaries.
    """

    def __init__(self, profile_name, samp_rate, doppler_hz, rng):
        prof = DELAY_PROFILES[profile_name]
        if (not math.isfinite(float(samp_rate)) or float(samp_rate) <= 0.0
                or not math.isfinite(float(doppler_hz))
                or float(doppler_hz) < 0.0):
            raise ValueError(
                "frequency-selective fading requires finite non-negative "
                "Doppler and a finite positive sample rate"
            )
        if FADING_DOPPLER_OVERSAMPLE * float(doppler_hz) > float(samp_rate):
            raise ValueError(
                "frequency-selective Doppler requires at least eight IQ "
                "samples per maximum-Doppler cycle"
            )
        self.profile_name = profile_name
        self.samp_rate = samp_rate
        self.doppler = doppler_hz
        self.rng = rng
        self.enabled = True
        self._sample_clock = 0
        interpolation_rate = min(
            float(samp_rate),
            max(
                FADING_MIN_INTERPOLATION_RATE_HZ,
                FADING_DOPPLER_OVERSAMPLE * float(doppler_hz),
            ),
        )
        self._grid_step_samples = max(
            1, int(float(samp_rate) / interpolation_rate)
        )

        sample_period_ns = 1e9 / samp_rate
        self.tap_indices = [int(round(d / sample_period_ns)) for d in prof['delays_ns']]
        self.ntaps = max(1, self.tap_indices[-1] + 1)

        powers_lin = [10.0 ** (p / 20.0) for p in prof['powers_db']]
        total_power = sum(p * p for p in powers_lin)
        norm = math.sqrt(total_power) if total_power > 0 else 1.0
        self.tap_amplitudes = [p / norm for p in powers_lin]

        self.num_path_taps = len(self.tap_indices)
        # Independent angle-of-arrival and phase sets give each physical path
        # a finite scattering approximation with unit ensemble mean power.
        # Its marginal distribution approximates Rayleigh scattering; the sum converges
        # to the J0 Doppler autocorrelation while remaining a pure function of
        # absolute sample time. Separate path phases are essential when two
        # physical delays quantize to the same IQ sample.
        self._oscillator_directions = rng.uniform(
            0.0, 2.0 * math.pi,
            size=(self.num_path_taps, FADING_SOS_TERMS),
        )
        self._doppler_factors = np.cos(self._oscillator_directions)
        self._phase_offsets = rng.uniform(
            0.0, 2.0 * math.pi,
            size=(self.num_path_taps, FADING_SOS_TERMS),
        )
        self._sos_scale = 1.0 / math.sqrt(FADING_SOS_TERMS)

        # Bounded channel-truth accumulators. These cover each unique channel
        # interpolation-grid point reached by the IQ sample clock, rather than
        # re-counting look-ahead points at every ZMQ message boundary.
        self._observed_grid_points = 0
        self._last_observed_grid_index = -1
        self._tap_grid_power_sum = np.zeros(self.num_path_taps, dtype=np.float64)
        self._tap_grid_power_min = np.full(
            self.num_path_taps, np.inf, dtype=np.float64
        )
        self._tap_grid_power_max = np.zeros(self.num_path_taps, dtype=np.float64)
        self._doppler_min_hz = float(doppler_hz)
        self._doppler_max_hz = float(doppler_hz)
        self._doppler_changes = 0
        # Preserve the prior input samples needed by the largest path delay.
        # EPA/EVA/ETU have at most a few hundred complex-sample delays at the
        # supported rates, so a direct sparse delay line avoids the much more
        # expensive generic scipy.signal.lfilter dispatch on every 1 ms IQ
        # message while retaining exact cross-message continuity.
        self._delay_line = np.zeros(self.ntaps - 1, dtype=np.complex64)

    def _grid_coefficients(self, grid_indices):
        """Return every physical tap coefficient at absolute grid indices."""
        grid_indices = np.asarray(grid_indices, dtype=np.int64)
        times = (
            grid_indices.astype(np.float64)
            * float(self._grid_step_samples)
            / float(self.samp_rate)
        )
        phase = (
            2.0
            * math.pi
            * float(self.doppler)
            * self._doppler_factors[:, :, np.newaxis]
            * times[np.newaxis, np.newaxis, :]
            + self._phase_offsets[:, :, np.newaxis]
        )
        return (
            np.exp(1j * phase).sum(axis=1) * self._sos_scale
        ).astype(np.complex64)

    def _exact_coefficients_at_sample(self, sample_index):
        """Return un-interpolated oscillator coefficients at one IQ sample."""
        time_s = float(sample_index) / float(self.samp_rate)
        phase = (
            2.0
            * math.pi
            * float(self.doppler)
            * self._doppler_factors
            * time_s
            + self._phase_offsets
        )
        return (
            np.exp(1j * phase).sum(axis=1) * self._sos_scale
        ).astype(np.complex64)

    def _observe_grid_coefficients(self, grid_indices, coefficients, last_sample):
        last_reached_grid = int(last_sample) // self._grid_step_samples
        first_new = max(
            self._last_observed_grid_index + 1, int(grid_indices[0])
        )
        if first_new > last_reached_grid:
            return
        start = first_new - int(grid_indices[0])
        stop = last_reached_grid - int(grid_indices[0]) + 1
        powers = np.abs(coefficients[:, start:stop].astype(np.complex128)) ** 2
        powers *= np.square(
            np.asarray(self.tap_amplitudes, dtype=np.float64)
        )[:, np.newaxis]
        if powers.shape[1] == 0:
            return
        self._tap_grid_power_sum += powers.sum(axis=1)
        self._tap_grid_power_min = np.minimum(
            self._tap_grid_power_min, powers.min(axis=1)
        )
        self._tap_grid_power_max = np.maximum(
            self._tap_grid_power_max, powers.max(axis=1)
        )
        self._observed_grid_points += int(powers.shape[1])
        self._last_observed_grid_index = last_reached_grid

    def _advance_delay_line(self, iq):
        history_len = len(self._delay_line)
        if history_len == 0 or len(iq) == 0:
            return
        if len(iq) >= history_len:
            self._delay_line[:] = iq[-history_len:]
        else:
            self._delay_line[:-len(iq)] = self._delay_line[len(iq):].copy()
            self._delay_line[-len(iq):] = iq

    def update_and_apply(self, iq):
        n = len(iq)
        if n == 0:
            return iq
        if not self.enabled:
            self._advance_delay_line(iq)
            self._sample_clock += n
            return iq

        first_sample = self._sample_clock
        last_sample = first_sample + n - 1
        first_grid = first_sample // self._grid_step_samples
        last_grid = last_sample // self._grid_step_samples
        grid_indices = np.arange(first_grid, last_grid + 2, dtype=np.int64)
        grid_coefficients = self._grid_coefficients(grid_indices)
        self._observe_grid_coefficients(
            grid_indices, grid_coefficients, last_sample
        )

        history_len = len(self._delay_line)
        extended = np.empty(history_len + n, dtype=np.complex64)
        if history_len:
            extended[:history_len] = self._delay_line
        extended[history_len:] = iq

        # Apply each physical path independently. This is the causal,
        # time-varying FIR y[n] = sum(h_path[n] * x[n-delay_path]); paths whose
        # delays quantize to the same sample are still independently faded and
        # superposed rather than collapsed into one stochastic process.
        y = np.zeros(n, dtype=np.complex64)
        position = 0
        while position < n:
            absolute_position = first_sample + position
            grid_offset = absolute_position // self._grid_step_samples - first_grid
            within_grid = absolute_position % self._grid_step_samples
            segment_length = min(
                n - position, self._grid_step_samples - within_grid
            )
            fraction = np.arange(
                within_grid,
                within_grid + segment_length,
                dtype=np.float32,
            ) / np.float32(self._grid_step_samples)
            destination = y[position:position + segment_length]
            for path, delay in enumerate(self.tap_indices):
                h0 = grid_coefficients[path, grid_offset]
                h1 = grid_coefficients[path, grid_offset + 1]
                coefficient = h0 + (h1 - h0) * fraction
                source_start = history_len - delay + position
                source = extended[source_start:source_start + segment_length]
                destination += (
                    np.float32(self.tap_amplitudes[path])
                    * coefficient
                    * source
                )
            position += segment_length

        self._advance_delay_line(iq)
        self._sample_clock += n
        return y

    def reconfigure(self, enabled, doppler, _k_db=None):
        new_doppler = float(doppler)
        grid_rate = float(self.samp_rate) / self._grid_step_samples
        if new_doppler < 0.0 or new_doppler * FADING_DOPPLER_OVERSAMPLE > grid_rate:
            raise ValueError(
                "frequency-selective Doppler exceeds the fixed interpolation "
                "grid established for this channel instance"
            )
        if new_doppler != float(self.doppler):
            # Preserve every underlying oscillator's phase at the exact next
            # IQ sample. The fixed interpolation grid then changes only its
            # forward slope instead of restarting the stochastic trajectory.
            now = self._sample_clock / float(self.samp_rate)
            self._phase_offsets += (
                2.0
                * math.pi
                * (float(self.doppler) - new_doppler)
                * self._doppler_factors
                * now
            )
            self._phase_offsets %= 2.0 * math.pi
            self._doppler_changes += 1
            self._doppler_min_hz = min(self._doppler_min_hz, new_doppler)
            self._doppler_max_hz = max(self._doppler_max_hz, new_doppler)
            self.doppler = new_doppler
        self.enabled = enabled

    def metrics_record(self, label, scope):
        points = self._observed_grid_points
        if points:
            means = self._tap_grid_power_sum / points
            minima = self._tap_grid_power_min
            maxima = self._tap_grid_power_max
        else:
            means = np.zeros(self.num_path_taps, dtype=np.float64)
            minima = np.zeros(self.num_path_taps, dtype=np.float64)
            maxima = np.zeros(self.num_path_taps, dtype=np.float64)
        target_powers = np.square(
            np.asarray(self.tap_amplitudes, dtype=np.float64)
        )
        return {
            "channel_model": "absolute-time-sum-of-sinusoids",
            "coefficient_grid_points": points,
            "coefficient_grid_rate_hz": round(
                float(self.samp_rate) / self._grid_step_samples, 6
            ),
            "component": "ocudu-grc-channel-metric",
            "direction": label,
            "doppler_changes": self._doppler_changes,
            "doppler_current_hz": round(float(self.doppler), 6),
            "doppler_max_hz": round(self._doppler_max_hz, 6),
            "doppler_min_hz": round(self._doppler_min_hz, 6),
            "grid_step_samples": self._grid_step_samples,
            "interpolation": "linear",
            "profile": self.profile_name,
            "sample_clock": self._sample_clock,
            "sample_rate_hz": round(float(self.samp_rate), 6),
            "schema_version": 1,
            "scope": scope,
            "sos_terms_per_tap": FADING_SOS_TERMS,
            "tap_delays_samples": self.tap_indices,
            "tap_grid_power_max": [round(float(value), 9) for value in maxima],
            "tap_grid_power_mean": [round(float(value), 9) for value in means],
            "tap_grid_power_min": [round(float(value), 9) for value in minima],
            "tap_target_power": [
                round(float(value), 9) for value in target_powers
            ],
        }


# ── Impairment functions ─────────────────────────────────────────────────────

def add_awgn(iq, snr_linear, rng, signal_power=None):
    """Add complex AWGN using desired-signal power as the SNR reference."""
    sig_power = (float(np.mean(np.real(iq)**2 + np.imag(iq)**2))
                 if signal_power is None else float(signal_power))
    if sig_power < 1e-20:
        return iq
    # I and Q each contribute variance noise_std**2, so divide the target
    # complex-noise power equally between the two components.
    noise_std = np.float32(np.sqrt(sig_power / (2.0 * snr_linear)))
    # Generate the target dtype directly. Converting a float64 allocation on
    # every 1 ms message needlessly consumes the headless relay's RT budget.
    buf = rng.standard_normal(2 * len(iq), dtype=np.float32)
    buf *= noise_std
    return iq + buf.view(np.complex64)


def apply_cfo(iq, cfo_hz, samp_rate, phase_state):
    """Carrier frequency offset — cumulative phase rotation across subframes."""
    if abs(cfo_hz) < 0.01:
        return iq
    n = len(iq)
    t = np.arange(n, dtype=np.float64) / samp_rate
    phase = 2.0 * np.pi * cfo_hz * t + phase_state[0]
    iq_out = iq * np.exp(1j * phase).astype(np.complex64)
    phase_state[0] = float(
        (phase_state[0] + 2.0 * np.pi * cfo_hz * n / samp_rate) % (2.0 * np.pi))
    return iq_out


def apply_interference(iq, int_type, freq_hz, sir_linear, samp_rate, int_phase, rng,
                       bw_hz=180e3):
    """Inject a CW or narrowband interferer into IQ samples (DL path only).

    int_type:   'cw'  — single continuous-wave tone at freq_hz
                'narrowband' — bandlimited noise (1 PRB = 180 kHz) centred at freq_hz
    freq_hz:    centre frequency offset from baseband DC (Hz); can be negative
    sir_linear: Signal-to-Interference Ratio (linear power ratio)
                Lower SIR = stronger interference; SIR < 1 = jammer-dominated
    int_phase:  mutable [float] list — cumulative CW phase for subframe continuity
    bw_hz:      narrowband bandwidth (default 1 PRB = 180 kHz at 15 kHz SCS)
    """
    n = len(iq)
    if n == 0:
        return iq
    sig_power = float(np.mean(np.real(iq) ** 2 + np.imag(iq) ** 2))
    if sig_power < 1e-20:
        if int_type == 'cw':
            # Correct the legacy clock gap without changing its message-local
            # amplitude reference: silence has zero tone amplitude, but consumes
            # elapsed samples before the next nonzero message resumes the tone.
            int_phase[0] = float((int_phase[0] +
                2.0 * math.pi * freq_hz * n / samp_rate) % (2.0 * math.pi))
        return iq

    int_amp = float(math.sqrt(sig_power / sir_linear))
    phase_inc = 2.0 * math.pi * freq_hz / samp_rate

    # Phase ramp — continuous across subframe boundaries via int_phase[0]
    phases = np.arange(n, dtype=np.float64) * phase_inc + int_phase[0]
    int_phase[0] = float((int_phase[0] + phase_inc * n) % (2.0 * math.pi))

    if int_type == 'cw':
        interferer = np.exp(1j * phases).astype(np.complex64) * np.float32(int_amp)

    elif int_type == 'narrowband':
        # Complex AWGN, FFT-bandlimited to bw_hz, then frequency-shifted to freq_hz
        noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
        spec = np.fft.fft(noise)
        freqs = np.fft.fftfreq(n, 1.0 / samp_rate)
        spec[np.abs(freqs) > bw_hz / 2.0] = 0.0
        noise_filt = np.fft.ifft(spec).astype(np.complex64)
        filt_power = float(np.mean(np.real(noise_filt) ** 2 + np.imag(noise_filt) ** 2))
        if filt_power > 1e-30:
            noise_filt *= np.float32(int_amp / math.sqrt(filt_power))
        interferer = noise_filt * np.exp(1j * phases).astype(np.complex64)

    else:
        return iq

    return (iq + interferer).astype(np.complex64)


# ── Scenario Runner ──────────────────────────────────────────────────────────

class ScenarioRunner:
    """Time-varying channel scenarios that auto-vary parameters."""

    def __init__(self, seed=1):
        self.scenario = 0
        self._t0 = _time.time()
        self._snr_walk = 28.0
        self._dop_walk = 5.0
        self._rng = np.random.default_rng(int(seed) ^ 0x5CE0A710)

    def set_scenario(self, idx):
        self.scenario = idx
        self._t0 = _time.time()
        self._snr_walk = 28.0
        self._dop_walk = 5.0

    def tick(self):
        """Returns dict of parameter updates, or None if manual mode."""
        if self.scenario == 0:
            return None
        t = _time.time() - self._t0

        if self.scenario == 1:  # Drive-by: 30s sinusoidal cycle
            phase = math.pi * (t % 30.0) / 30.0
            s = math.sin(phase)
            return {
                'snr_db': 30.0 - 15.0 * s,       # 30 → 15 → 30 dB
                'doppler_hz': 5.0 + 195.0 * s,    # 5 → 200 → 5 Hz
                'drop_prob': 0.02 * s,             # 0 → 2% → 0
            }

        elif self.scenario == 2:  # Urban walk: bounded random perturbations
            self._snr_walk += float(self._rng.uniform(-1.5, 1.5))
            self._snr_walk = max(12.0, min(35.0, self._snr_walk))
            self._dop_walk += float(self._rng.uniform(-2.0, 2.0))
            self._dop_walk = max(1.0, min(20.0, self._dop_walk))
            drop = 0.05 if float(self._rng.random()) < 0.15 else 0.0
            return {
                'snr_db': self._snr_walk,
                'doppler_hz': self._dop_walk,
                'drop_prob': drop,
            }

        elif self.scenario == 3:  # Edge of cell: 60s linear decline
            frac = min(1.0, t / 60.0)
            return {
                'snr_db': 30.0 - 22.0 * frac,     # 30 → 8 dB
                'doppler_hz': 5.0,
                'drop_prob': 0.10 * frac,           # 0 → 10%
            }

        return None


# ── ZMQ Relay Thread ────────────────────────────────────────────────────────

def send_until_stop(socket, payload, stop_event):
    """Retry transient send waits without masking real errors racing stop."""
    while not stop_event.is_set():
        try:
            socket.send(payload)
            return True
        except _zmq.ZMQError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EINTR):
                raise
    return False

def relay_thread(label, src_addr, dst_addr, imp, rng,
                 stop_ev, viz_buf, msg_counter, ready_ev, fatal_errors):
    """Relay ZMQ REQ/REP with full impairment chain.

    imp: dict with mutable [val] entries:
        snr_linear, fading, cfo_hz, cfo_phase, drop_prob
    """
    ctx = None
    req = None
    rep = None
    count = 0
    drops = 0
    accounting = RelayAccounting(label, imp.get('identity', [False])[0])
    fixed_channel = imp.get('fixed_channel')
    schedule = imp.get('schedule_runtime')
    metrics = RelayMetrics(label, imp['fading'][0].samp_rate) if schedule is None else None
    stage_metrics = (schedule.metrics.directions[label]
                     if schedule is not None and schedule.metrics is not None else None)
    relay_outcome = 'stopped'
    try:
        if fixed_channel is not None:
            print("RADIO_FIXED_PROFILE: " + json.dumps({
                **fixed_channel.record('started'),
                'monotonic_ns': _time.monotonic_ns(), 'wall_ns': _time.time_ns(),
            }, sort_keys=True, separators=(",", ":"), allow_nan=False), flush=True)
        ctx = _zmq.Context()
        tmo = 500
        req = ctx.socket(_zmq.REQ)
        req.setsockopt(_zmq.LINGER, 0)
        req.setsockopt(_zmq.RCVTIMEO, tmo)
        req.setsockopt(_zmq.SNDTIMEO, tmo)
        req.setsockopt(_zmq.MAXMSGSIZE, MAX_ZMQ_MESSAGE_BYTES)
        req.connect(src_addr)

        rep = ctx.socket(_zmq.REP)
        rep.setsockopt(_zmq.LINGER, 0)
        rep.setsockopt(_zmq.RCVTIMEO, tmo)
        rep.setsockopt(_zmq.SNDTIMEO, tmo)
        rep.setsockopt(_zmq.MAXMSGSIZE, MAX_ZMQ_MESSAGE_BYTES)
        rep.bind(dst_addr)
        if schedule is not None:
            schedule.relay_ready(label)
        ready_ev.set()

        if metrics is not None:
            metrics.start()
        if stage_metrics is not None:
            stage_metrics.start()

        while not stop_ev.is_set():
            try:
                if stage_metrics is not None:
                    stage_metrics.begin_exchange()
                try:
                    downstream_req = rep.recv()
                except _zmq.Again:
                    continue

                if rep.getsockopt(_zmq.RCVMORE):
                    raise ValueError("multipart downstream requests are unsupported")
                if stage_metrics is not None:
                    stage_metrics.next_phase('request_send')
                if not send_until_stop(req, downstream_req, stop_ev):
                    break
                if stage_metrics is not None:
                    stage_metrics.next_phase('upstream_receive')

                while not stop_ev.is_set():
                    try:
                        frame = req.recv(copy=False)
                        if stage_metrics is not None:
                            stage_metrics.next_phase('processing')
                        break
                    except _zmq.Again:
                        continue
                if stop_ev.is_set():
                    break

                processing_started_ns = _time.monotonic_ns() if metrics is not None else None
                # Explicit receive materialization is identical with metrics on
                # or off, and occurs after the complete-frame receipt boundary.
                raw = bytes(memoryview(frame))
                frame = None
                try:
                    if req.getsockopt(_zmq.RCVMORE):
                        raise ValueError("multipart upstream IQ replies are unsupported")
                    iq = validate_cf32_payload(raw, "input IQ")
                except ValueError:
                    if stage_metrics is not None:
                        stage_metrics.rejected()
                    raise
                accounting.record_input(len(iq))
                if stage_metrics is not None:
                    stage_metrics.received(len(iq))
                if schedule is not None:
                    schedule.input_received(label, accounting)

                # Parameter changes and stateful channel updates are serialized
                # at a whole-message boundary. This prevents one IQ message
                # from mixing pre/post-update fader, SNR, CFO, or interference
                # state while the GUI/scenario thread changes controls.
                dropped_message = False
                if stage_metrics is not None:
                    stage_metrics.next_part('channel_chain')
                with imp['lock']:
                    # ── Impairment chain ──
                    # 1) Burst drop (zero entire subframe)
                    if fixed_channel is not None:
                        if stage_metrics is not None:
                            iq = schedule.process(label, iq, core_call=stage_metrics.core_call)
                            stage_metrics.next_part('output_prepare')
                        else:
                            iq = (schedule.process(label, iq) if schedule is not None else fixed_channel.process(iq))
                        payload = raw if accounting.identity else iq.tobytes()
                    elif accounting.identity or len(iq) == 0:
                        # Preserve signed zeros and every original finite cf32
                        # bit. Empty frames need no DSP/state advancement.
                        payload = raw
                    elif (imp['drop_prob'][0] > 0.0
                            and rng.random() < imp['drop_prob'][0]):
                        iq = np.zeros_like(iq)
                        drops += 1
                        dropped_message = True
                        payload = iq.tobytes()
                    else:
                        iq = iq.copy()
                        fader = imp['fading'][0]
                        # 2) Fading (flat or frequency-selective)
                        iq = fader.update_and_apply(iq)
                        # 3) CFO (cumulative phase rotation)
                        iq = apply_cfo(iq, imp['cfo_hz'][0],
                                       fader.samp_rate, imp['cfo_phase'])
                        desired_power = float(np.mean(
                            np.real(iq) ** 2 + np.imag(iq) ** 2))
                        # 4) Superpose synthetic interference before receiver
                        #    noise. SIR is referenced only to the desired signal.
                        if imp.get('int_enabled', [False])[0]:
                            iq = apply_interference(
                                iq, imp['int_type'][0], imp['int_freq'][0],
                                imp['sir_linear'][0], fader.samp_rate,
                                imp['int_phase'], rng)
                        # 5) AWGN is independently referenced to that same desired
                        #    signal, rather than desired-plus-interference power.
                        iq = add_awgn(iq, imp['snr_linear'][0], rng,
                                      signal_power=desired_power)
                        payload = iq.tobytes()

                output_iq = validate_cf32_payload(payload, "output IQ")
                if len(payload) != len(raw):
                    raise ValueError("IQ processing changed payload length")
                accounting.check_output(len(output_iq))
                processing_ns = (_time.monotonic_ns() - processing_started_ns if metrics is not None else None)
                if stage_metrics is not None:
                    stage_metrics.next_phase('downstream_send')
                if not send_until_stop(rep, payload, stop_ev):
                    break
                if stage_metrics is not None:
                    # An unavailable/regressed timing observation cannot erase
                    # a reply already accepted by the transport send call.
                    try:
                        sent_ns = stage_metrics.clock()
                    finally:
                        accounting.record_output(len(output_iq))
                        stage_metrics.forwarded(len(output_iq))
                    stage_metrics.sent(len(output_iq), sent_ns)
                else:
                    accounting.record_output(len(output_iq))
                if schedule is not None:
                    schedule.forwarded(label, accounting)

                count += 1
                if label == "DL" and viz_buf is not None:
                    viz_buf.append(iq)
                if count == 1:
                    print(
                        f"[GRC] {label}: first msg relayed ({len(iq)} samples)",
                        flush=True,
                    )
                # Unscheduled console metrics include empty messages; scheduled
                # runs use the independently checked optional metrics stream.
                progress = (metrics.observe(
                    len(iq), len(payload), processing_ns,
                    dropped=dropped_message,
                ) if metrics is not None else None)
                if progress is not None:
                    print(progress, flush=True)
                    with imp['lock']:
                        channel_record = getattr(
                            imp['fading'][0], 'metrics_record', None
                        )
                        if channel_record is not None:
                            print(
                                "GRC_CHANNEL_PROGRESS: " + json.dumps(
                                    channel_record(label, "cumulative"),
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                                flush=True,
                            )
                msg_counter[0] = count

            except _zmq.ZMQError:
                # Receive polling handles Again at the operation above. Any
                # other transport failure remains fatal when stop races it.
                raise
    except Exception as exc:
        relay_outcome = 'error'
        accounting.error_count += 1
        fatal_errors.append(f"{label} relay failed: {type(exc).__name__}: {exc}")
        if schedule is not None:
            schedule.fail('relay_processing_or_transport_failed')
        stop_ev.set()
        try:
            print(f"[GRC] {fatal_errors[-1]}", file=sys.stderr)
        except Exception:
            pass  # The failure is already latched independently of stdout.
    finally:
        relay_finished_ns = _time.monotonic_ns() if metrics is not None else None
        ready_ev.set()
        def cleanup_failure(reason):
            accounting.error_count += 1
            fatal_errors.append(f'{label} relay cleanup failed: {reason}')
            stop_ev.set()
            if schedule is not None:
                try:
                    schedule.fail(reason)
                except Exception:
                    fatal_errors.append(f'{label} schedule cleanup reporting failed')

        if stage_metrics is not None:
            try:
                stage_metrics.finish_window(relay_outcome)
            except Exception:
                cleanup_failure('metrics_window_final_failed')

        # Every owned transport resource gets its cleanup attempt even when
        # another close fails. Such failures must reach the process/final gate.
        for resource, method, reason in ((req, 'close', 'request_socket_close_failed'),
                                          (rep, 'close', 'reply_socket_close_failed'),
                                          (ctx, 'term', 'relay_context_term_failed')):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    cleanup_failure(reason)
        try:
            with imp['lock']:
                channel_record = getattr(imp['fading'][0], 'metrics_record', None)
                if channel_record is not None:
                    print('GRC_CHANNEL_SUMMARY: ' + json.dumps(channel_record(label, 'run'),
                          sort_keys=True, separators=(',', ':'), allow_nan=False), flush=True)
            if metrics is not None:
                partial = metrics.partial(now_ns=relay_finished_ns)
                if partial is not None:
                    print(partial, flush=True)
                print(metrics.summary(now_ns=relay_finished_ns), flush=True)
        except Exception:
            cleanup_failure('relay_summary_failed')
        try:
            if fixed_channel is not None:
                print('RADIO_FIXED_PROFILE: ' + json.dumps({
                    **fixed_channel.record('final'), 'monotonic_ns': _time.monotonic_ns(),
                    'wall_ns': _time.time_ns(), 'status': accounting.final_record(bool(fatal_errors))['status'],
                }, sort_keys=True, separators=(',', ':'), allow_nan=False), flush=True)
        except Exception:
            cleanup_failure('fixed_profile_final_failed')
        try:
            print('GRC_RELAY_ACCOUNTING: ' + json.dumps(accounting.final_record(bool(fatal_errors)),
                  sort_keys=True, separators=(',', ':'), allow_nan=False), flush=True)
            drop_str = f' ({100*drops/count:.1f}% dropped)' if count > 0 and drops > 0 else ''
            print(f'[GRC] {label}: exiting (relayed {count} msgs{drop_str})', flush=True)
        except Exception:
            cleanup_failure('relay_accounting_final_failed')
        if schedule is not None:
            try:
                schedule.relay_final(label, accounting)
            except Exception:
                cleanup_failure('scheduled_relay_final_failed')


# ── Plain Python ZMQ Relay Engine ──────────────────────────────────────────

class channel_broker_source:
    """Custom Python/NumPy relay engine; optional DL visualization buffer.

    Actual GNU Radio classes are loaded only by the explicit GUI factory.
    """

    def __init__(self, snr_db=28.0, k_factor_db=3.0, doppler_hz=5.0,
                 fading_mode=1, samp_rate=23.04e6, cfo_hz=0.0, drop_prob=0.0,
                 gnb_tx_port=4000, gnb_rx_port=4001,
                 ue_rx_port=2000, ue_tx_port=2001,
                 int_type='none', int_freq_hz=1.0e6, sir_db=20.0, seed=1,
                 identity=False, dl_bind=None, dl_connect=None,
                 ul_bind=None, ul_connect=None,
                 channel_semantics='legacy_message_local_v1',
                 dl_profile=None, ul_profile=None, schedule_inputs=None, metrics_every=None):
        if channel_semantics not in ('legacy_message_local_v1', 'fixed_reference_v1', 'grc_cfo_v1', 'grc_static_tdl_a_v1', 'grc_static_tdl_c_v1'):
            raise ValueError("unknown channel semantics version")
        self.channel_semantics = channel_semantics
        fixed_selected = channel_semantics in ('fixed_reference_v1', 'grc_cfo_v1', 'grc_static_tdl_a_v1', 'grc_static_tdl_c_v1')
        if fixed_selected:
            if identity or dl_profile is None or ul_profile is None:
                raise ValueError("fixed-reference requires both profiles and excludes legacy identity")
            # Existing constructor defaults may be omitted by callers. Explicit
            # nondefault legacy settings must not disappear behind a new mode.
            if (snr_db != 28.0 or k_factor_db != 3.0 or doppler_hz != 5.0
                    or fading_mode not in (0, 1) or cfo_hz != 0.0 or drop_prob != 0.0
                    or int_type != 'none' or int_freq_hz != 1e6 or sir_db != 20.0):
                raise ValueError("fixed-reference excludes nondefault legacy constructor parameters")
            if channel_semantics in ('grc_static_tdl_a_v1', 'grc_static_tdl_c_v1') and metrics_every is not None:
                raise ValueError('static TDL optional metrics are unsupported')
            channel_type = study_channel_type(channel_semantics)
            dl_channel = channel_type('DL', samp_rate, seed, **dl_profile)
            ul_channel = channel_type('UL', samp_rate, seed, **ul_profile)
            # The legacy placeholder supplies only its sample-rate attribute;
            # no fader/CFO/erasure or legacy RNG enters the selected new core.
            fading_mode = 0
        elif dl_profile is not None or ul_profile is not None:
            raise ValueError("directional profiles require fixed_reference_v1")
        self.samp_rate = samp_rate
        self.snr_db = snr_db
        self.k_factor_db = k_factor_db
        self.doppler_hz = doppler_hz
        self.fading_mode = fading_mode
        self.int_type = int_type
        self.int_freq_hz = int_freq_hz
        self.sir_db = sir_db
        self.gnb_tx_port = gnb_tx_port
        self.gnb_rx_port = gnb_rx_port
        self.ue_rx_port = ue_rx_port
        self.ue_tx_port = ue_tx_port
        self.dl_bind = validate_local_endpoint(
            dl_bind if dl_bind is not None else f"tcp://127.0.0.1:{ue_rx_port}")
        self.dl_connect = validate_local_endpoint(
            dl_connect if dl_connect is not None else f"tcp://127.0.0.1:{gnb_tx_port}")
        self.ul_bind = validate_local_endpoint(
            ul_bind if ul_bind is not None else f"tcp://127.0.0.1:{gnb_rx_port}")
        self.ul_connect = validate_local_endpoint(
            ul_connect if ul_connect is not None else f"tcp://127.0.0.1:{ue_tx_port}")
        if len({self.dl_bind, self.dl_connect, self.ul_bind, self.ul_connect}) != 4:
            raise ValueError("all four broker endpoints must be distinct")
        self._stop = threading.Event()
        self._viz_buf = collections.deque(maxlen=4)
        self.seed = int(seed)
        self._dl_rng = np.random.default_rng(self.seed ^ 0x0D1A5EED)
        self._ul_rng = np.random.default_rng(self.seed ^ 0x00A17EED)
        self._dl_lock = threading.RLock()
        self._ul_lock = threading.RLock()
        self._dl_msg_count = [0]
        self._ul_msg_count = [0]
        self._fatal_errors = collections.deque(maxlen=1)
        self.schedule_runtime = None
        if metrics_every is not None and schedule_inputs is None:
            raise ValueError('radio metrics require a finite schedule')
        if schedule_inputs is not None:
            if not fixed_selected:
                raise ValueError('sample schedules require fixed_reference_v1')
            from radio_schedule_runtime import ScheduleRuntime
            self.schedule_runtime = ScheduleRuntime(schedule_inputs,
                                                    {'DL': dl_channel, 'UL': ul_channel},
                                                    self._stop, self._fatal_errors, metrics_every=metrics_every)

        snr_lin = pow(10.0, snr_db / 10.0)
        sir_lin = pow(10.0, sir_db / 10.0) if sir_db < 100.0 else float('inf')
        self._dl_imp = self._make_impairments(
            snr_lin, fading_mode, doppler_hz, k_factor_db, cfo_hz, drop_prob, self._dl_rng,
            int_enabled=(int_type != 'none'), int_type=int_type,
            int_freq_hz=int_freq_hz, sir_linear=sir_lin)
        # Apply the selected legacy profile independently in both directions.
        # Replacing an UL multipath model with flat fading would invalidate
        # directional comparisons.
        ul_mode = fading_mode
        self._ul_imp = self._make_impairments(
            snr_lin, ul_mode, doppler_hz, k_factor_db, 0.0, 0.0, self._ul_rng,
            int_enabled=False, int_type='none', int_freq_hz=0.0, sir_linear=float('inf'))
        if fixed_selected:
            self._dl_imp['fixed_channel'] = dl_channel
            self._ul_imp['fixed_channel'] = ul_channel
            self._dl_imp['identity'] = [dl_channel.mode == 'identity']
            self._ul_imp['identity'] = [ul_channel.mode == 'identity']
            if self.schedule_runtime is not None:
                self._dl_imp['schedule_runtime'] = self.schedule_runtime
                self._ul_imp['schedule_runtime'] = self.schedule_runtime
        else:
            self.set_identity_mode(identity)

    def _make_fading(self, mode, rng):
        if mode in FADING_MODE_PROFILES:
            return FrequencySelectiveFading(
                FADING_MODE_PROFILES[mode], self.samp_rate, self.doppler_hz, rng)
        elif mode == 2:
            return FadingState(True, self.doppler_hz, self.samp_rate, -100.0, rng)
        elif mode == 1:
            return FadingState(True, self.doppler_hz, self.samp_rate,
                               self.k_factor_db, rng)
        else:
            return FadingState(False, self.doppler_hz, self.samp_rate,
                               self.k_factor_db, rng)

    def _make_impairments(self, snr_linear, mode, doppler, k_db, cfo, drop, rng,
                          int_enabled=False, int_type='none',
                          int_freq_hz=0.0, sir_linear=float('inf')):
        fading = self._make_fading(mode, rng)
        lock = self._dl_lock if rng is self._dl_rng else self._ul_lock
        return {
            'lock':        lock,
            'snr_linear':  [snr_linear],
            'fading':      [fading],
            'cfo_hz':      [cfo],
            'cfo_phase':   [0.0],
            'drop_prob':   [drop],
            'int_enabled': [int_enabled],
            'int_type':    [int_type],
            'int_freq':    [int_freq_hz],
            'sir_linear':  [sir_linear],
            'int_phase':   [0.0],
        }

    def start(self):
        self._stop.clear()
        self._fatal_errors.clear()
        self._started_relay_threads = []
        if self.schedule_runtime is not None:
            try:
                self.schedule_runtime.start()
            except Exception:
                self.stop()
                return False
        self._dl_ready = threading.Event()
        self._ul_ready = threading.Event()
        try:
            self._dl_thread = threading.Thread(
                target=relay_thread,
                args=("DL", self.dl_connect, self.dl_bind,
                      self._dl_imp, self._dl_rng, self._stop, self._viz_buf,
                      self._dl_msg_count, self._dl_ready, self._fatal_errors), daemon=True)
            self._ul_thread = threading.Thread(
                target=relay_thread,
                args=("UL", self.ul_connect, self.ul_bind,
                      self._ul_imp, self._ul_rng, self._stop, None,
                      self._ul_msg_count, self._ul_ready, self._fatal_errors), daemon=True)
            for thread in (self._dl_thread, self._ul_thread):
                thread.start()
                self._started_relay_threads.append(thread)
        except Exception:
            self._fatal_errors.append('relay thread startup failed')
            self._stop.set()
            if self.schedule_runtime is not None:
                self.schedule_runtime.fail('relay_thread_start_failed')
            self.stop()
            return False
        if not self._dl_ready.wait(2.0) or not self._ul_ready.wait(2.0):
            self._fatal_errors.append("relay startup timed out")
            self._stop.set()
        if self._fatal_errors:
            self.stop()
            return False
        print("[GRC] Relay threads started")
        return True

    def stop(self):
        self._stop.set()
        for thread in getattr(self, '_started_relay_threads',
                              (getattr(self, '_dl_thread', None), getattr(self, '_ul_thread', None))):
            if thread is not None and thread is not threading.current_thread():
                try:
                    thread.join(timeout=2.0)
                    if thread.is_alive():
                        self._fatal_errors.append('relay shutdown timed out')
                        if self.schedule_runtime is not None:
                            self.schedule_runtime.fail('relay_join_timeout')
                except Exception:
                    self._fatal_errors.append('relay shutdown join failed')
                    if self.schedule_runtime is not None:
                        self.schedule_runtime.fail('relay_join_failed')
        if self.schedule_runtime is not None:
            self.schedule_runtime.finish()
        print("[GRC] Stopping relay threads...")
        return not bool(self._fatal_errors)

    @property
    def fatal_error(self):
        return self._fatal_errors[0] if self._fatal_errors else None

    def work(self, input_items, output_items):
        if self.fatal_error is not None:
            return -1
        out = output_items[0]
        n = len(out)
        if self._viz_buf:
            iq = self._viz_buf[-1]
            use = min(n, len(iq))
            out[:use] = iq[:use]
            if use < n:
                out[use:] = 0
        else:
            out[:] = 0
        return n

    # ── Callbacks for GUI / scenario parameter changes ──

    def _require_legacy_controls(self):
        if self.channel_semantics != 'legacy_message_local_v1':
            raise RuntimeError("legacy controls cannot modify fixed_reference_v1")

    def set_identity_mode(self, enabled):
        """Select identity before starting either direction; no live transitions."""
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            for name in ('_dl_thread', '_ul_thread'):
                thread = getattr(self, name, None)
                if thread is not None and thread.is_alive():
                    raise RuntimeError("identity mode cannot change while relays are running")
            self._dl_imp['identity'] = [bool(enabled)]
            self._ul_imp['identity'] = [bool(enabled)]

    def set_snr_db(self, val):
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            self.snr_db = val
            snr_lin = pow(10.0, val / 10.0)
            self._dl_imp['snr_linear'][0] = snr_lin
            self._ul_imp['snr_linear'][0] = snr_lin

    def set_k_factor_db(self, val):
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            self.k_factor_db = val
            if self.fading_mode <= 2:
                k = -100.0 if self.fading_mode == 2 else val
                self._dl_imp['fading'][0].reconfigure(
                    self.fading_mode >= 1, self.doppler_hz, k)
                self._ul_imp['fading'][0].reconfigure(
                    self.fading_mode >= 1, self.doppler_hz, k)

    def set_doppler_hz(self, val):
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            self.doppler_hz = val
            if self.fading_mode <= 2:
                k = -100.0 if self.fading_mode == 2 else self.k_factor_db
                self._dl_imp['fading'][0].reconfigure(
                    self.fading_mode >= 1, val, k)
                self._ul_imp['fading'][0].reconfigure(
                    self.fading_mode >= 1, val, k)
            else:
                self._dl_imp['fading'][0].reconfigure(True, val)
                self._ul_imp['fading'][0].reconfigure(True, val)

    def set_fading_mode(self, val):
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            self.fading_mode = val
            self._dl_imp['fading'][0] = self._make_fading(val, self._dl_rng)
            self._ul_imp['fading'][0] = self._make_fading(val, self._ul_rng)

    def set_cfo_hz(self, val):
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            self._dl_imp['cfo_hz'][0] = val
            self._ul_imp['cfo_hz'][0] = val

    def set_drop_prob(self, val):
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            self._dl_imp['drop_prob'][0] = val
            self._ul_imp['drop_prob'][0] = val

    def set_sir_db(self, val):
        with self._dl_lock:
            self._require_legacy_controls()
            self.sir_db = val
            self._dl_imp['sir_linear'][0] = (
                pow(10.0, val / 10.0) if val < 100.0 else float('inf'))

    def set_int_type(self, int_type):
        with self._dl_lock:
            self._require_legacy_controls()
            self.int_type = int_type
            self._dl_imp['int_type'][0] = int_type
            self._dl_imp['int_enabled'][0] = (int_type != 'none')

    def set_int_freq_hz(self, val):
        with self._dl_lock:
            self._require_legacy_controls()
            self.int_freq_hz = val
            self._dl_imp['int_freq'][0] = val

    def apply_scenario_updates(self, updates):
        """Apply one scenario tick atomically across both relay directions."""
        with self._dl_lock, self._ul_lock:
            self._require_legacy_controls()
            if 'snr_db' in updates:
                self.set_snr_db(updates['snr_db'])
            if 'doppler_hz' in updates:
                self.set_doppler_hz(updates['doppler_hz'])
            if 'drop_prob' in updates:
                self.set_drop_prob(updates['drop_prob'])


# ── QT GUI Flow Graph ──────────────────────────────────────────────────────

def load_gui_top_block_class():
    """Import the actual optional GNU Radio/Qt GUI only on explicit request."""
    from gnuradio import gr, blocks, qtgui
    from gnuradio.fft import window
    import sip
    from PyQt5 import Qt, QtCore

    class gui_channel_broker_source(gr.sync_block, channel_broker_source):
        """Real GNU Radio adapter around the same plain Python relay engine."""

        def __init__(self, **kwargs):
            gr.sync_block.__init__(self, name="OCUDU ZMQ Channel Broker",
                                   in_sig=[], out_sig=[np.complex64])
            channel_broker_source.__init__(self, **kwargs)

        def start(self):
            return channel_broker_source.start(self)

        def stop(self):
            return channel_broker_source.stop(self)

        def work(self, input_items, output_items):
            return channel_broker_source.work(self, input_items, output_items)

    class ocudu_channel_broker(gr.top_block, Qt.QWidget):

        def __init__(self, samp_rate=23.04e6, seed=1):
            gr.top_block.__init__(self, "OCUDU ZMQ Legacy Channel Broker",
                                  catch_exceptions=True)
            Qt.QWidget.__init__(self)
            self.setWindowTitle("OCUDU ZMQ Legacy Channel Broker")
            qtgui.util.check_set_qss()
            try:
                self.setWindowIcon(Qt.QIcon.fromTheme("gnuradio-grc"))
            except:
                pass
            self.top_scroll_layout = Qt.QVBoxLayout()
            self.setLayout(self.top_scroll_layout)
            self.top_scroll = Qt.QScrollArea()
            self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
            self.top_scroll_layout.addWidget(self.top_scroll)
            self.top_scroll.setWidgetResizable(True)
            self.top_widget = Qt.QWidget()
            self.top_scroll.setWidget(self.top_widget)
            self.top_layout = Qt.QVBoxLayout(self.top_widget)
            self.top_grid_layout = Qt.QGridLayout()
            self.top_layout.addLayout(self.top_grid_layout)

            self.settings = Qt.QSettings("GNU Radio", "ocudu_channel_broker")
            try:
                self.restoreGeometry(self.settings.value("geometry"))
            except:
                pass

            # ── Variables ─────────────────────────────────────────────────────
            self.samp_rate = samp_rate
            self.snr_db = snr_db = 28.0
            self.k_factor_db = k_factor_db = 3.0
            self.doppler_hz = doppler_hz = 5.0
            self.fading_mode = fading_mode = 1
            self.cfo_hz = cfo_hz = 0.0
            self.drop_prob = drop_prob = 0.0
            self.scenario_id = 0
            self.sir_db = sir_db = 20.0
            self.int_type_idx = int_type_idx = 0    # 0=None, 1=CW, 2=Narrowband
            self.int_freq_mhz = int_freq_mhz = 1.0  # MHz from DC

            # ── Row 0: SNR slider + K-factor slider ──────────────────────────
            self._snr_db_range = qtgui.Range(5, 40, 0.5, snr_db, 200)
            self._snr_db_win = qtgui.RangeWidget(self._snr_db_range,
                self.set_snr_db, "SNR (dB)", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._snr_db_win, 0, 0, 1, 2)

            self._k_factor_db_range = qtgui.Range(-10, 20, 0.5, k_factor_db, 200)
            self._k_factor_db_win = qtgui.RangeWidget(self._k_factor_db_range,
                self.set_k_factor_db, "K-Factor (dB)", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._k_factor_db_win, 0, 2, 1, 2)

            # ── Row 1: Doppler slider + Fading mode dropdown ─────────────────
            self._doppler_hz_range = qtgui.Range(0.1, 300, 1, doppler_hz, 200)
            self._doppler_hz_win = qtgui.RangeWidget(self._doppler_hz_range,
                self.set_doppler_hz, "Doppler (Hz)", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._doppler_hz_win, 1, 0, 1, 2)

            self._fading_combo = Qt.QComboBox()
            for k in sorted(FADING_MODES.keys()):
                self._fading_combo.addItem(FADING_MODES[k], k)
            self._fading_combo.setCurrentIndex(fading_mode)
            self._fading_combo.currentIndexChanged.connect(
                lambda idx: self.set_fading_mode(self._fading_combo.itemData(idx)))
            fading_group = Qt.QGroupBox("Fading Mode:")
            fading_lay = Qt.QHBoxLayout()
            fading_lay.addWidget(self._fading_combo)
            fading_group.setLayout(fading_lay)
            self.top_grid_layout.addWidget(fading_group, 1, 2, 1, 2)

            # ── Row 2: CFO slider + Drop prob slider + Scenario dropdown ─────
            self._cfo_hz_range = qtgui.Range(-500, 500, 1.0, cfo_hz, 200)
            self._cfo_hz_win = qtgui.RangeWidget(self._cfo_hz_range,
                self.set_cfo_hz, "CFO (Hz)", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._cfo_hz_win, 2, 0, 1, 1)

            self._drop_range = qtgui.Range(0, 0.25, 0.01, drop_prob, 200)
            self._drop_win = qtgui.RangeWidget(self._drop_range,
                self.set_drop_prob, "Drop Prob", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._drop_win, 2, 1, 1, 1)

            self._scenario_combo = Qt.QComboBox()
            for k in sorted(SCENARIO_NAMES.keys()):
                self._scenario_combo.addItem(SCENARIO_NAMES[k], k)
            self._scenario_combo.currentIndexChanged.connect(
                lambda idx: self.set_scenario(self._scenario_combo.itemData(idx)))
            scenario_group = Qt.QGroupBox("Scenario:")
            scenario_lay = Qt.QHBoxLayout()
            scenario_lay.addWidget(self._scenario_combo)
            scenario_group.setLayout(scenario_lay)
            self.top_grid_layout.addWidget(scenario_group, 2, 2, 1, 2)

            # ── Row 3: Interference SIR slider + Interference Type dropdown ──
            self._sir_db_range = qtgui.Range(-10, 40, 0.5, sir_db, 200)
            self._sir_db_win = qtgui.RangeWidget(self._sir_db_range,
                self.set_sir_db, "SIR (dB)", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._sir_db_win, 3, 0, 1, 2)

            self._int_type_combo = Qt.QComboBox()
            for k in sorted(INT_TYPES.keys()):
                self._int_type_combo.addItem(INT_TYPES[k], k)
            self._int_type_combo.setCurrentIndex(int_type_idx)
            self._int_type_combo.currentIndexChanged.connect(
                lambda idx: self.set_int_type_idx(self._int_type_combo.itemData(idx)))
            int_type_group = Qt.QGroupBox("Interference:")
            int_type_lay = Qt.QHBoxLayout()
            int_type_lay.addWidget(self._int_type_combo)
            int_type_group.setLayout(int_type_lay)
            self.top_grid_layout.addWidget(int_type_group, 3, 2, 1, 2)

            # ── Row 4: Interference frequency slider ─────────────────────────
            self._int_freq_range = qtgui.Range(-11.0, 11.0, 0.1, int_freq_mhz, 200)
            self._int_freq_win = qtgui.RangeWidget(self._int_freq_range,
                self.set_int_freq_mhz, "Int Freq (MHz)", "counter_slider", float,
                QtCore.Qt.Horizontal)
            self.top_grid_layout.addWidget(self._int_freq_win, 4, 0, 1, 4)

            # ── Blocks ────────────────────────────────────────────────────────
            self.epy_block_broker = gui_channel_broker_source(
                snr_db=snr_db, k_factor_db=k_factor_db, doppler_hz=doppler_hz,
                fading_mode=fading_mode, samp_rate=samp_rate,
                cfo_hz=cfo_hz, drop_prob=drop_prob,
                int_type=INT_TYPE_NAMES[int_type_idx],
                int_freq_hz=int_freq_mhz * 1e6, sir_db=sir_db, seed=seed)

            self.blocks_throttle_0 = blocks.throttle(
                gr.sizeof_gr_complex, samp_rate, True)

            # Frequency Sink
            self.qtgui_freq_sink_0 = qtgui.freq_sink_c(
                2048, window.WIN_BLACKMAN_hARRIS, 0, samp_rate,
                "DL Channel Spectrum", 1)
            self.qtgui_freq_sink_0.set_update_time(0.10)
            self.qtgui_freq_sink_0.set_y_axis(-80, 10)
            self.qtgui_freq_sink_0.set_y_label("Relative Gain", "dB")
            self.qtgui_freq_sink_0.set_trigger_mode(
                qtgui.TRIG_MODE_FREE, 0.0, 0, "")
            self.qtgui_freq_sink_0.enable_autoscale(False)
            self.qtgui_freq_sink_0.enable_grid(True)
            self.qtgui_freq_sink_0.set_fft_average(1.0)
            self.qtgui_freq_sink_0.enable_control_panel(False)
            self.qtgui_freq_sink_0.set_line_label(0, "DL IQ Spectrum")
            self.qtgui_freq_sink_0.set_line_width(0, 2)
            self.qtgui_freq_sink_0.set_line_color(0, "blue")
            self._qtgui_freq_sink_0_win = sip.wrapinstance(
                self.qtgui_freq_sink_0.qwidget(), Qt.QWidget)
            self.top_grid_layout.addWidget(
                self._qtgui_freq_sink_0_win, 5, 0, 2, 2)

            # Time Sink
            self.qtgui_time_sink_0 = qtgui.time_sink_c(
                2048, samp_rate, "DL IQ Waveform", 1, None)
            self.qtgui_time_sink_0.set_update_time(0.10)
            self.qtgui_time_sink_0.set_y_axis(-1, 1)
            self.qtgui_time_sink_0.set_y_label("Amplitude", "")
            self.qtgui_time_sink_0.enable_tags(True)
            self.qtgui_time_sink_0.set_trigger_mode(
                qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
            self.qtgui_time_sink_0.enable_autoscale(True)
            self.qtgui_time_sink_0.enable_grid(True)
            self.qtgui_time_sink_0.set_line_label(0, "I")
            self.qtgui_time_sink_0.set_line_label(1, "Q")
            self.qtgui_time_sink_0.set_line_color(0, "blue")
            self.qtgui_time_sink_0.set_line_color(1, "red")
            self._qtgui_time_sink_0_win = sip.wrapinstance(
                self.qtgui_time_sink_0.qwidget(), Qt.QWidget)
            self.top_grid_layout.addWidget(
                self._qtgui_time_sink_0_win, 5, 2, 2, 2)

            # Constellation Sink
            self.qtgui_const_sink_0 = qtgui.const_sink_c(
                2048, "DL Constellation", 1, None)
            self.qtgui_const_sink_0.set_update_time(0.10)
            self.qtgui_const_sink_0.set_y_axis(-2, 2)
            self.qtgui_const_sink_0.set_x_axis(-2, 2)
            self.qtgui_const_sink_0.set_trigger_mode(
                qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
            self.qtgui_const_sink_0.enable_autoscale(True)
            self.qtgui_const_sink_0.enable_grid(True)
            self.qtgui_const_sink_0.set_line_label(0, "DL IQ")
            self.qtgui_const_sink_0.set_line_color(0, "blue")
            self.qtgui_const_sink_0.set_line_style(0, 0)
            self.qtgui_const_sink_0.set_line_marker(0, 0)
            self._qtgui_const_sink_0_win = sip.wrapinstance(
                self.qtgui_const_sink_0.qwidget(), Qt.QWidget)
            self.top_grid_layout.addWidget(
                self._qtgui_const_sink_0_win, 7, 0, 2, 2)

            # Waterfall Sink
            self.qtgui_waterfall_sink_0 = qtgui.waterfall_sink_c(
                2048, window.WIN_BLACKMAN_hARRIS, 0, samp_rate,
                "DL Waterfall", 1)
            self.qtgui_waterfall_sink_0.set_update_time(0.10)
            self.qtgui_waterfall_sink_0.enable_grid(True)
            self.qtgui_waterfall_sink_0.enable_axis_labels(True)
            self.qtgui_waterfall_sink_0.set_intensity_range(-80, 10)
            self._qtgui_waterfall_sink_0_win = sip.wrapinstance(
                self.qtgui_waterfall_sink_0.qwidget(), Qt.QWidget)
            self.top_grid_layout.addWidget(
                self._qtgui_waterfall_sink_0_win, 7, 2, 2, 2)

            # ── Connections ───────────────────────────────────────────────────
            self.connect((self.epy_block_broker, 0), (self.blocks_throttle_0, 0))
            self.connect((self.blocks_throttle_0, 0), (self.qtgui_freq_sink_0, 0))
            self.connect((self.blocks_throttle_0, 0), (self.qtgui_time_sink_0, 0))
            self.connect((self.blocks_throttle_0, 0), (self.qtgui_const_sink_0, 0))
            self.connect(
                (self.blocks_throttle_0, 0), (self.qtgui_waterfall_sink_0, 0))

            # ── Scenario timer (1s tick) ──────────────────────────────────────
            self._scenario = ScenarioRunner(seed)
            self._scenario_timer = Qt.QTimer()
            self._scenario_timer.timeout.connect(self._scenario_tick)
            self._scenario_timer.start(1000)

        def _scenario_tick(self):
            updates = self._scenario.tick()
            if updates is None:
                return
            self.epy_block_broker.apply_scenario_updates(updates)

        def closeEvent(self, event):
            self.settings = Qt.QSettings("GNU Radio", "ocudu_channel_broker")
            self.settings.setValue("geometry", self.saveGeometry())
            self.stop()
            self.wait()
            event.accept()

        def set_snr_db(self, snr_db):
            self.snr_db = snr_db
            self.epy_block_broker.set_snr_db(snr_db)

        def set_k_factor_db(self, k_factor_db):
            self.k_factor_db = k_factor_db
            self.epy_block_broker.set_k_factor_db(k_factor_db)

        def set_doppler_hz(self, doppler_hz):
            self.doppler_hz = doppler_hz
            self.epy_block_broker.set_doppler_hz(doppler_hz)

        def set_fading_mode(self, mode):
            self.fading_mode = mode
            self.epy_block_broker.set_fading_mode(mode)

        def set_cfo_hz(self, val):
            self.cfo_hz = val
            self.epy_block_broker.set_cfo_hz(val)

        def set_drop_prob(self, val):
            self.drop_prob = val
            self.epy_block_broker.set_drop_prob(val)

        def set_scenario(self, idx):
            self.scenario_id = idx
            self._scenario.set_scenario(idx)

        def set_sir_db(self, val):
            self.sir_db = val
            self.epy_block_broker.set_sir_db(val)

        def set_int_type_idx(self, idx):
            self.int_type_idx = idx
            self.epy_block_broker.set_int_type(INT_TYPE_NAMES.get(idx, 'none'))

        def set_int_freq_mhz(self, val):
            self.int_freq_mhz = val
            self.epy_block_broker.set_int_freq_hz(val * 1e6)

    return ocudu_channel_broker


# ── Headless mode (no QT GUI — for launch script) ───────────────────────────

class ocudu_channel_broker_headless:
    """Runs only the ZMQ relay engine when no visualization is requested.

    The former headless wrapper connected the Python broker source to a GNU
    Radio throttle and null sink. That caused the scheduler to synthesize and
    copy a second 23.04 Msps visualization stream even though no GUI consumed
    it, competing with both synchronous ZMQ relay threads for the Python GIL
    and memory bandwidth. The relay engine already owns its lifecycle, so the
    headless wrapper deliberately avoids starting an unused flow graph.
    """

    def __init__(self, snr_db=28.0, k_factor_db=3.0, doppler_hz=5.0,
                 fading_mode=1, samp_rate=23.04e6,
                 cfo_hz=0.0, drop_prob=0.0,
                 int_type='none', int_freq_hz=1.0e6, sir_db=20.0, seed=1,
                 identity=False, dl_bind=None, dl_connect=None,
                 ul_bind=None, ul_connect=None,
                 channel_semantics='legacy_message_local_v1',
                 dl_profile=None, ul_profile=None, schedule_inputs=None, metrics_every=None):
        self.broker = channel_broker_source(
            snr_db=snr_db, k_factor_db=k_factor_db, doppler_hz=doppler_hz,
            fading_mode=fading_mode, samp_rate=samp_rate,
            cfo_hz=cfo_hz, drop_prob=drop_prob,
            int_type=int_type, int_freq_hz=int_freq_hz, sir_db=sir_db,
            seed=seed, identity=identity, dl_bind=dl_bind, dl_connect=dl_connect,
            ul_bind=ul_bind, ul_connect=ul_connect,
            channel_semantics=channel_semantics, dl_profile=dl_profile, ul_profile=ul_profile,
            schedule_inputs=schedule_inputs, metrics_every=metrics_every)
        self._stopped = threading.Event()
        self._started = False

    def start(self):
        self._stopped.clear()
        self._started = bool(self.broker.start())
        if not self._started:
            self._stopped.set()
        return self._started

    def stop(self):
        if self._started:
            self.broker.stop()
            self._started = False
        self._stopped.set()
        return True

    def wait(self):
        # A relay failure sets the broker stop event. Notice it here so a
        # headless process fails closed instead of waiting forever for a signal.
        while not self._stopped.wait(0.1):
            if self.broker.fatal_error is not None or self.broker._stop.is_set():
                self._stopped.set()


# ── Main ─────────────────────────────────────────────────────────────────────


def run_gui(top_block_cls, args, fading_mode, scenario_id):
    from gnuradio import gr
    from PyQt5 import Qt
    from packaging.version import Version as StrictVersion

    if top_block_cls is None:
        top_block_cls = load_gui_top_block_class()
    if (StrictVersion("4.5.0") <= StrictVersion(Qt.qVersion())
            < StrictVersion("5.0.0")):
        style = gr.prefs().get_string("qtgui", "style", "raster")
        Qt.QApplication.setGraphicsSystem(style)

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(samp_rate=args.samp_rate, seed=args.seed)
    tb.epy_block_broker.set_identity_mode(args.identity)
    tb.set_snr_db(args.snr)
    tb.set_k_factor_db(args.k_factor)
    tb.set_doppler_hz(args.doppler)
    tb.set_fading_mode(fading_mode)
    tb.set_cfo_hz(args.cfo)
    tb.set_drop_prob(args.drop_prob)
    tb.set_scenario(scenario_id)
    if args.interference_type != 'none':
        int_idx = {'cw': 1, 'narrowband': 2}.get(args.interference_type, 0)
        tb.set_int_type_idx(int_idx)
        tb.set_int_freq_mhz(args.interference_freq / 1e6)
        tb.set_sir_db(args.sir)

    tb.start()
    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(
        lambda: sig_handler()
        if tb.epy_block_broker.fatal_error is not None else None
    )

    qapp.aboutToQuit.connect(tb.stop)
    qapp.exec_()
    if tb.epy_block_broker.fatal_error is not None:
        print(
            f"[GRC] Fatal relay error: {tb.epy_block_broker.fatal_error}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(top_block_cls=None, options=None):
    import argparse

    def bounded_float(option, minimum, maximum):
        def parse(value):
            try:
                parsed = float(value)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    f"{option} requires a finite number, got {value!r}"
                ) from exc
            if not math.isfinite(parsed):
                raise argparse.ArgumentTypeError(
                    f"{option} requires a finite number, got {value!r}"
                )
            if not minimum <= parsed <= maximum:
                raise argparse.ArgumentTypeError(
                    f"{option} must be in [{minimum:g}, {maximum:g}], got {parsed:g}"
                )
            return parsed

        return parse

    def uint32(value):
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "--seed requires an unsigned 32-bit decimal integer"
            ) from exc
        if str(parsed) != value or not 0 <= parsed <= 0xFFFFFFFF:
            raise argparse.ArgumentTypeError(
                "--seed requires an unsigned 32-bit decimal integer"
            )
        return parsed

    def endpoint(value):
        try:
            return validate_local_endpoint(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    class EndpointOnce(argparse.Action):
        def __call__(self, parser, namespace, value, option_string=None):
            if getattr(namespace, self.dest) is not None:
                parser.error(f"{option_string} may be supplied only once")
            setattr(namespace, self.dest, value)

    parser = argparse.ArgumentParser(
        description="OCUDU ZMQ Python/NumPy relay; optional legacy GNU Radio GUI",
        allow_abbrev=False)
    parser.add_argument('--channel-semantics',
                        choices=['legacy_message_local_v1', 'fixed_reference_v1', 'grc_cfo_v1', 'grc_static_tdl_a_v1', 'grc_static_tdl_c_v1'],
                        default='legacy_message_local_v1')
    parser.add_argument('--validate-config-only', action='store_true',
                        help='Validate arguments and emit effective settings without creating a relay')
    parser.add_argument('--radio-plan-file', action=EndpointOnce, default=None,
                        help='Preloaded finite sample schedule for the fixed-reference headless profile')
    parser.add_argument('--radio-control-dir', action=EndpointOnce, default=None,
                        help='Existing caller-owned private schedule/control directory')
    parser.add_argument('--radio-metrics-every-messages', action=EndpointOnce, default=None,
                        help='Optional bounded scheduled metrics window in [1,1000000] messages')
    fixed_option_names = set()
    fixed_numeric_option_names = set()
    for direction in ('dl', 'ul'):
        numeric_options = {
            'ref-power': (1e-20, 1e10), 'gain': (0.0, 1.0),
            'noise-snr': (-100.0, 100.0), 'cw-sir': (-100.0, 100.0),
            'cw-freq': (-125e6, 125e6), 'cfo': (-500.0, 500.0),
        }
        for name, (lower, upper) in numeric_options.items():
            option = f'--{direction}-{name}'
            fixed_option_names.add(option)
            fixed_numeric_option_names.add(option)
            parser.add_argument(option, type=bounded_float(option, lower, upper), default=None)
        mode_option = f'--{direction}-mode'
        fixed_option_names.add(mode_option)
        parser.add_argument(mode_option, choices=['identity', 'fixed'], default=None)
        for name in ('noise-off', 'cw', 'tdl-enabled'):
            option = f'--{direction}-{name}'
            fixed_option_names.add(option)
            parser.add_argument(option, action='store_true', default=None)
    parser.add_argument("--identity", action="store_true",
                        help="Byte-identical relay; bypass all IQ impairments in both directions")
    for name in ("dl-bind", "dl-connect", "ul-bind", "ul-connect"):
        parser.add_argument(
            "--" + name, type=endpoint, action=EndpointOnce, default=None,
            help="Headless local endpoint override; caller owns IPC directory and peer lifecycle",
        )
    parser.add_argument("--snr", type=bounded_float("--snr", -100.0, 100.0),
                        default=28.0,
                        help="SNR in dB (default: 28)")
    parser.add_argument("--k-factor",
                        type=bounded_float("--k-factor", -100.0, 100.0),
                        default=3.0,
                        help="Rician K-factor dB (default: 3)")
    parser.add_argument("--doppler",
                        type=bounded_float("--doppler", 0.0, 5000.0),
                        default=None,
                        help="Max Doppler Hz (default: auto from profile)")
    parser.add_argument("--fading", action="store_true",
                        help="Enable flat Rician fading")
    parser.add_argument("--rayleigh", action="store_true",
                        help="Enable flat Rayleigh fading")
    parser.add_argument("--profile", type=str, default="flat",
                        choices=["flat", "epa", "eva", "etu"],
                        help="Delay profile (epa/eva/etu = freq-selective)")
    parser.add_argument("--cfo", type=bounded_float("--cfo", -500.0, 500.0),
                        default=0.0,
                        help="Carrier freq offset Hz (default: 0)")
    parser.add_argument("--drop-prob",
                        type=bounded_float("--drop-prob", 0.0, 1.0),
                        default=0.0,
                        help="Burst drop probability 0-1 (default: 0)")
    parser.add_argument("--scenario", type=str, default="none",
                        choices=["none", "drive-by", "urban-walk",
                                 "edge-of-cell"],
                        help="Time-varying scenario (default: none)")
    parser.add_argument("--interference-type", type=str, default="none",
                        choices=["none", "cw", "narrowband"],
                        help="DL interference type: none | cw | narrowband (default: none)")
    parser.add_argument("--interference-freq",
                        type=bounded_float("--interference-freq", -125.0e6,
                                           125.0e6),
                        default=1.0e6,
                        help="Interference centre frequency offset from DC in Hz (default: 1e6)")
    parser.add_argument("--sir", type=bounded_float("--sir", -100.0, 100.0),
                        default=20.0,
                        help="Signal-to-Interference Ratio in dB (default: 20)")
    parser.add_argument("--no-gui", action="store_true",
                        help="Run headless (no QT GUI)")
    parser.add_argument("--samp-rate",
                        type=bounded_float("--samp-rate", 1000.0, 250.0e6),
                        default=23.04e6,
                        help="Complex sample rate in Hz (default: 23.04e6)")
    parser.add_argument("--seed", type=uint32, default=1,
                        help="Deterministic uint32 master RNG seed (default: 1)")
    options = list(sys.argv[1:] if options is None else options)
    # argparse otherwise misclassifies valid negative scientific notation as
    # another option. The shared profile mapper uses Python's round-trip repr.
    # Join only recognized numeric values; missing/unknown flags still fail.
    normalized_options = []
    index = 0
    while index < len(options):
        option = options[index]
        if (option in fixed_numeric_option_names and index + 1 < len(options)
                and options[index + 1].startswith('-')):
            try:
                float(options[index + 1])
            except ValueError:
                pass
            else:
                normalized_options.append(option + '=' + options[index + 1])
                index += 2
                continue
        normalized_options.append(option)
        index += 1
    options = normalized_options
    args = parser.parse_args(options)
    supplied_options = [value.split('=', 1)[0] for value in options if value.startswith('--')]
    for option in fixed_option_names | {'--channel-semantics'}:
        if supplied_options.count(option) > 1:
            parser.error(f'{option} may be supplied only once')
    cfo_selected = args.channel_semantics == 'grc_cfo_v1'
    tdl_selected = args.channel_semantics in ('grc_static_tdl_a_v1', 'grc_static_tdl_c_v1')
    fixed_selected = args.channel_semantics in ('fixed_reference_v1', 'grc_cfo_v1', 'grc_static_tdl_a_v1', 'grc_static_tdl_c_v1')
    channel_type = study_channel_type(args.channel_semantics)
    if not tdl_selected and {'--dl-tdl-enabled', '--ul-tdl-enabled'}.intersection(supplied_options):
        parser.error('directional TDL flags require --channel-semantics grc_static_tdl_a_v1 or grc_static_tdl_c_v1')
    if tdl_selected and args.radio_metrics_every_messages is not None:
        parser.error('static TDL optional metrics are unsupported')
    if not cfo_selected and {'--dl-cfo', '--ul-cfo'}.intersection(supplied_options):
        parser.error('directional CFO flags require --channel-semantics grc_cfo_v1')
    if bool(args.radio_plan_file) != bool(args.radio_control_dir):
        parser.error('--radio-plan-file and --radio-control-dir must be supplied together')
    if args.radio_plan_file is not None and (not fixed_selected or not args.no_gui):
        parser.error('radio schedules require fixed_reference_v1 and --no-gui')
    metrics_every = None
    if args.radio_metrics_every_messages is not None:
        text = args.radio_metrics_every_messages
        if (not text.isascii() or not text.isdecimal() or text.startswith('0')
                or len(text) > 7 or not 1 <= int(text) <= 1000000):
            parser.error('radio metrics interval must be a decimal integer in [1,1000000]')
        if args.radio_plan_file is None:
            parser.error('radio metrics require a finite schedule')
        metrics_every = int(text)
    legacy_impairment_flags = {
        '--identity', '--snr', '--k-factor', '--doppler', '--fading', '--rayleigh',
        '--profile', '--cfo', '--drop-prob', '--scenario', '--interference-type',
        '--interference-freq', '--sir',
    }
    if not fixed_selected and fixed_option_names.intersection(supplied_options):
        parser.error('directional study flags require --channel-semantics fixed_reference_v1')
    fixed_profiles = {'dl_profile': None, 'ul_profile': None}
    if fixed_selected:
        if not args.no_gui:
            parser.error('fixed_reference_v1 requires --no-gui; the GUI retains legacy controls')
        conflicts = legacy_impairment_flags.intersection(supplied_options)
        if conflicts:
            parser.error('fixed_reference_v1 excludes legacy impairment flags: ' + ', '.join(sorted(conflicts)))
        for direction in ('dl', 'ul'):
            def configured(name, default):
                value = getattr(args, f'{direction}_{name}')
                return default if value is None else value
            ref_power = configured('ref_power', None)
            if ref_power is None:
                parser.error(f'fixed_reference_v1 requires --{direction}-ref-power')
            frequency = configured('cw_freq', 0.0)
            if abs(frequency) > args.samp_rate / 2.0:
                parser.error(f'--{direction}-cw-freq magnitude must not exceed Nyquist')
            fixed_profiles[direction + '_profile'] = {
                'ref_power': ref_power, 'mode': configured('mode', 'fixed'),
                'gain': configured('gain', 1.0),
                'noise_snr_db': configured('noise_snr', 28.0),
                'noise_enabled': not configured('noise_off', False),
                'cw_enabled': configured('cw', False),
                'cw_sir_db': configured('cw_sir', 20.0), 'cw_freq_hz': frequency,
            }
            if cfo_selected:
                fixed_profiles[direction + '_profile']['cfo_hz'] = configured('cfo', 0.0)
            if tdl_selected:
                fixed_profiles[direction + '_profile']['tdl_enabled'] = configured('tdl_enabled', False)
            if cfo_selected or tdl_selected:
                try:
                    channel_type(direction.upper(), args.samp_rate, args.seed,
                                 **fixed_profiles[direction + '_profile'])
                except ValueError as exc:
                    parser.error('directional profile validation failed: ' + str(exc))
    endpoint_overrides = {
        "dl_bind": args.dl_bind, "dl_connect": args.dl_connect,
        "ul_bind": args.ul_bind, "ul_connect": args.ul_connect,
    }
    if not args.no_gui and any(value is not None for value in endpoint_overrides.values()):
        parser.error("endpoint overrides require --no-gui")
    endpoints = {
        "dl_bind": args.dl_bind or "tcp://127.0.0.1:2000",
        "dl_connect": args.dl_connect or "tcp://127.0.0.1:4000",
        "ul_bind": args.ul_bind or "tcp://127.0.0.1:4001",
        "ul_connect": args.ul_connect or "tcp://127.0.0.1:2001",
    }
    if len(set(endpoints.values())) != 4:
        parser.error("all four broker endpoints must be distinct")

    if not fixed_selected and abs(args.interference_freq) > args.samp_rate / 2.0:
        parser.error(
            "--interference-freq magnitude must not exceed Nyquist "
            f"({args.samp_rate / 2.0:g} Hz for sample rate "
            f"{args.samp_rate:g} Hz)"
        )

    # Resolve fading mode
    if args.profile in ('epa', 'eva', 'etu'):
        fading_mode = {'epa': 3, 'eva': 4, 'etu': 5}[args.profile]
        if args.doppler is None:
            args.doppler = DELAY_PROFILES[args.profile]['default_doppler']
    elif args.rayleigh:
        fading_mode = 2
    elif args.fading:
        fading_mode = 1
    else:
        fading_mode = 0

    if args.doppler is None:
        args.doppler = 5.0

    if (fading_mode in FADING_MODE_PROFILES
            and FADING_DOPPLER_OVERSAMPLE * args.doppler > args.samp_rate):
        parser.error(
            "frequency-selective --doppler must not exceed --samp-rate/8"
        )

    scenario_id = {
        "none": 0, "drive-by": 1, "urban-walk": 2, "edge-of-cell": 3
    }[args.scenario]

    schedule_inputs = None
    if args.radio_plan_file is not None:
        from radio_schedule_runtime import validate_inputs
        try:
            channels = {direction: channel_type(direction, args.samp_rate, args.seed,
                        **fixed_profiles[direction.lower() + '_profile']) for direction in ('DL', 'UL')}
            schedule_inputs = validate_inputs(args.radio_plan_file, args.radio_control_dir, channels,
                                              metrics_every=metrics_every)
        except (ValueError, OSError) as exc:
            parser.error('radio schedule validation failed: ' + str(exc))

    if args.validate_config_only:
        if fixed_selected:
            directions = {}
            for direction in ('DL', 'UL'):
                config = dict(fixed_profiles[direction.lower() + '_profile'])
                if config['mode'] == 'identity':
                    config['noise_enabled'] = config['cw_enabled'] = False
                if tdl_selected:
                    config['realization'] = channel_type(direction, args.samp_rate, args.seed, **config).realization()
                else:
                    config['awgn_seed'] = radio_component_seed(args.seed, direction, 'awgn')
                directions[direction] = config
        else:
            directions = {direction: {
                'mode': 'identity' if args.identity else 'legacy',
                'snr_db': args.snr, 'fading_mode': fading_mode,
                'doppler_hz': args.doppler, 'k_factor_db': args.k_factor,
                'cfo_hz': args.cfo if direction == 'DL' else 0.0,
                'drop_prob': args.drop_prob if direction == 'DL' else 0.0,
                'interference_type': args.interference_type if direction == 'DL' else 'none',
            } for direction in ('DL', 'UL')}
        print('RADIO_CONFIG_VALIDATED: ' + json.dumps({
            'schema_version': 'radio_config_validated_v1', 'backend': 'grc',
            'channel_semantics_version': args.channel_semantics,
            'rng_version': 'component_streams_v1' if fixed_selected else 'legacy_direction_streams_v1',
            'master_seed': args.seed, 'sample_rate_hz': args.samp_rate,
            'directions': directions, 'endpoints': endpoints,
            **({'plan_sha256': schedule_inputs[0].sha256} if schedule_inputs is not None else {}),
            'scope': 'configuration_only_no_runtime_qualification',
        }, sort_keys=True, separators=(',', ':'), allow_nan=False))
        return 0

    # Print banner
    print("=" * 65)
    print("  OCUDU ZMQ Legacy LTE-profile Channel Broker (Python/NumPy relay)")
    print("=" * 65)
    print(f"  Semantics: {args.channel_semantics}")
    if args.identity:
        print("  Identity:  enabled (all configured IQ impairments bypassed)")
    print(f"  Fading:    {FADING_MODES[fading_mode]}")
    if fading_mode >= 1:
        print(f"  Doppler:   {args.doppler} Hz")
    if fading_mode == 1:
        print(f"  K-factor:  {args.k_factor} dB")
    if fading_mode >= 3:
        prof = DELAY_PROFILES[FADING_MODE_PROFILES[fading_mode]]
        print(f"  Taps:      {len(prof['delays_ns'])}, "
              f"max delay {prof['delays_ns'][-1]} ns")
    if fixed_selected:
        print(f"  Master seed: {args.seed}; directional fixed parameters follow in RADIO_FIXED_PROFILE")
    else:
        snr_lin = pow(10.0, args.snr / 10.0)
        print(f"  SNR:       {args.snr} dB  (adaptive noise, snr_linear={snr_lin:.1f})")
        print(f"  RNG seed:  {args.seed} (DL={args.seed ^ 0x0D1A5EED}, "
              f"UL={args.seed ^ 0x00A17EED}, scenario={args.seed ^ 0x5CE0A710})")
    if abs(args.cfo) > 0.01:
        print(f"  CFO:       {args.cfo} Hz")
    if args.drop_prob > 0:
        print(f"  Drop:      {args.drop_prob*100:.1f}%")
    if scenario_id > 0:
        print(f"  Scenario:  {SCENARIO_NAMES[scenario_id]}")
    if args.interference_type != 'none':
        print(f"  Interf:    {args.interference_type.upper()}"
              f"  freq={args.interference_freq/1e6:.3f} MHz  SIR={args.sir:.1f} dB")
    gui_label = "headless" if args.no_gui else "QT GUI"
    print(f"  Interface: {gui_label}")
    print("-" * 65)
    # List capabilities beyond C broker
    extras = []
    if fading_mode >= 3:
        extras.append(f"freq-selective fading ({args.profile.upper()})")
    if abs(args.cfo) > 0.01:
        extras.append(f"CFO {args.cfo} Hz")
    if args.drop_prob > 0:
        extras.append(f"burst drops {args.drop_prob*100:.0f}%")
    if scenario_id > 0:
        extras.append(f"scenario: {SCENARIO_NAMES[scenario_id]}")
    if args.interference_type != 'none':
        extras.append(f"{args.interference_type} interference SIR={args.sir:.0f} dB")
    if not args.no_gui:
        extras.append("live GUI control")
    if extras:
        print(f"  Beyond C broker: {', '.join(extras)}")
    print(f"  DL: {endpoints['dl_connect']} -> broker -> {endpoints['dl_bind']}")
    print(f"  UL: {endpoints['ul_connect']} -> broker -> {endpoints['ul_bind']}")
    print("=" * 65)

    if args.no_gui:
        tb = ocudu_channel_broker_headless(
            snr_db=args.snr, k_factor_db=args.k_factor,
            doppler_hz=args.doppler, fading_mode=fading_mode,
            samp_rate=args.samp_rate,
            cfo_hz=args.cfo, drop_prob=args.drop_prob,
            int_type=args.interference_type,
            int_freq_hz=args.interference_freq,
            sir_db=args.sir, seed=args.seed, identity=args.identity,
            channel_semantics=args.channel_semantics,
            schedule_inputs=schedule_inputs, metrics_every=metrics_every,
            **endpoint_overrides, **fixed_profiles)
        tb.start()

        # Scenario runner in background thread
        scenario = ScenarioRunner(args.seed)
        scenario.set_scenario(scenario_id)
        scenario_stop = threading.Event()

        def scenario_loop():
            while not scenario_stop.is_set():
                updates = scenario.tick()
                if updates:
                    tb.broker.apply_scenario_updates(updates)
                scenario_stop.wait(1.0)

        if scenario_id > 0:
            st = threading.Thread(target=scenario_loop, daemon=True)
            st.start()

        def sig_handler(sig, frame):
            print("\n[GRC] Signal caught, stopping...")
            scenario_stop.set()
            # Cleanup and truth finalization run in the common exit path.
            # A signal cannot override a relay/logging/incomplete-schedule error.
            tb.broker._stop.set()

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)

        print("[GRC] Broker running (headless). Ctrl+C to stop.")
        try:
            tb.wait()
        except KeyboardInterrupt:
            pass
        scenario_stop.set()
        tb.stop()
        tb.wait()
        fatal_error = tb.broker.fatal_error
        print("[GRC] Broker stopped.")
        if fatal_error:
            print(f"[GRC] Fatal relay error: {fatal_error}", file=sys.stderr)
            return 1

    else:
        return run_gui(top_block_cls, args, fading_mode, scenario_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
