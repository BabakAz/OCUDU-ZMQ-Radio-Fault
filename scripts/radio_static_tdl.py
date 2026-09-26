"""Static SISO TDL-A/TDL-C numerical core and narrow scheduled GRC adapters.

Catalog/scaling: local TR38.901 catalog. FIR design: local scientific CPU
channel overlay (32-tap Kaiser beta6, causal +15-sample common delay).
Gaussian coefficient draws and arithmetic below are a new explicit candidate
contract, not a replay of the separate GPU/SoS implementation.
"""
from pathlib import Path
from types import MappingProxyType
import hashlib
import json
import math

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT/'config/channel_broker/tr38901_tdl_v19.4.0.json'
CATALOG_SHA256 = '2e51ec2a7ac0718627f99ac7952026350fba1294438721ace1edf9ab9d895f91'
TAPS, CENTER, BETA = 32, 15, 6.0
UINT64_MAX = (1 << 64)-1
SCHEMA = 'grc_static_tdl_a_candidate_v1'
SEMANTICS = 'grc_static_tdl_a_v1'
TDL_C_SEMANTICS = 'grc_static_tdl_c_v1'


def finite(value, low, high, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name+' outside finite candidate bounds')
    return float(value)


def tap_seed(master_seed, direction):
    """Existing reserved 'taps' component's uint32 directional derivation."""
    if type(master_seed) is not int or not 0 <= master_seed <= 0xffffffff:
        raise ValueError('master_seed must be uint32')
    if direction not in ('DL', 'UL'):
        raise ValueError('direction must be DL or UL')
    value = master_seed ^ (0x0D1A5EED if direction == 'DL' else 0x00A17EED) ^ 0x54415053
    value ^= value >> 16
    value = (value*0x7FEB352D) & 0xffffffff
    value ^= value >> 15
    value = (value*0x846CA68B) & 0xffffffff
    return value ^ (value >> 16)


def i0_series(x):
    term = total = 1.0
    y = x*x/4.0
    for k in range(1, 33):
        term *= y/(k*k)
        total += term
        if term <= total*1e-16:
            break
    return total


def fractional_filter(fraction):
    fraction = finite(fraction, 0, math.nextafter(1.0, 0.0), 'fraction')
    values = []
    for index in range(TAPS):
        x = index-CENTER-fraction
        sinc = 1.0 if abs(x) < 1e-12 else math.sin(math.pi*x)/(math.pi*x)
        coordinate = 2.0*index/(TAPS-1)-1.0
        window = i0_series(BETA*math.sqrt(max(0.0, 1.0-coordinate*coordinate)))/i0_series(BETA)
        values.append(sinc*window)
    # Fixed prototype recipe: binary64 DC normalization, then one f32 cast.
    return np.asarray(np.asarray(values, np.float64)/math.fsum(values), np.float32)


class StaticTdlAChannel:
    profile = 'tdl-a'
    path_count = 23
    schema = SCHEMA

    def __init__(self, direction, sample_rate_hz, master_seed, *, delay_spread_ns=100.0,
                 occupied_bandwidth_hz=19_080_000.0, enabled=False, max_doppler_hz=0.0):
        rate = finite(sample_rate_hz, 1000, 250e6, 'sample rate')
        spread = finite(delay_spread_ns, 1, 1000, 'delay spread')
        bandwidth = finite(occupied_bandwidth_hz, 1, rate*.83, 'occupied bandwidth')
        if type(enabled) is not bool:
            raise ValueError('enabled must be bool')
        if finite(max_doppler_hz, 0, 0, 'max Doppler') != 0:
            raise ValueError('candidate supports static Doppler zero only')
        seed = tap_seed(master_seed, direction)
        raw = CATALOG.read_bytes()
        if hashlib.sha256(raw).hexdigest() != CATALOG_SHA256:
            raise ValueError('catalog digest differs')
        catalog = json.loads(raw)
        rows = catalog['profiles'][self.profile]['published_taps']
        if len(rows) != self.path_count or any(row['kind'] != 'diffuse' for row in rows):
            raise ValueError(f'candidate requires{self.path_count} diffuse {self.profile.upper()} paths')
        powers = np.asarray([10**(row['power_db']/10) for row in rows], np.float64)
        powers /= math.fsum(powers)
        self.delays = np.asarray([row['normalized_delay']*spread*1e-9*rate for row in rows], np.float64)
        self.powers = powers
        self.rng = np.random.Generator(np.random.PCG64(seed))
        normals = self.rng.standard_normal((self.path_count, 2), dtype=np.float64)
        self.coefficients = (np.sqrt(powers/2)*(normals[:, 0]+1j*normals[:, 1])).astype(np.complex64)
        length = math.floor(float(max(self.delays)))+TAPS
        if length > 4096:
            raise ValueError('candidate FIR history bound exceeded')
        kernel = np.zeros(length, np.complex128)
        self.filters = []
        for delay, coefficient in zip(self.delays, self.coefficients):
            integer = math.floor(float(delay))
            fir = fractional_filter(float(delay)-integer)
            self.filters.append(fir)
            kernel[integer:integer+TAPS] += complex(coefficient)*fir.astype(np.float64)
        self.kernel = kernel.astype(np.complex64)
        for array in [self.delays, self.powers, self.coefficients, self.kernel, *self.filters]:
            array.flags.writeable = False
        self.history = np.zeros(length-1, np.complex64)
        self.direction, self.samp_rate, self.master_seed = direction, rate, master_seed
        self.derived_seed = seed
        self.config = MappingProxyType(dict(profile=self.profile, delay_spread_ns=spread,
            max_doppler_hz=0.0, occupied_bandwidth_hz=bandwidth, tdl_enabled=enabled))
        self.sample_clock = self.tdl_applied_samples = 0

    def update_settings(self, settings):
        if type(settings) is not dict or set(settings) != {'tdl_enabled'} or type(settings['tdl_enabled']) is not bool:
            raise ValueError('only boolean tdl_enabled is mutable')
        changed = settings['tdl_enabled'] != self.config['tdl_enabled']
        self.config = MappingProxyType(dict(self.config, **settings))
        return changed

    def process(self, iq):
        if (not isinstance(iq, np.ndarray) or iq.dtype != np.complex64
                or iq.ndim != 1 or not np.isfinite(iq).all()):
            raise ValueError('input must be finite one-dimensional cf32')
        n = len(iq)
        if n == 0:
            return iq.copy()
        enabled = self.config['tdl_enabled']
        if self.sample_clock > UINT64_MAX-n or self.tdl_applied_samples > UINT64_MAX-(n if enabled else 0):
            raise OverflowError('candidate sample counter overflow')
        extended = np.concatenate((self.history, iq))
        if enabled:
            with np.errstate(over='ignore', invalid='ignore'):
                output = np.convolve(extended.astype(np.complex128), self.kernel.astype(np.complex128), mode='valid').astype(np.complex64)
        else:
            # The same common delay applies before, during and after the fault.
            start = len(self.history)-CENTER
            output = extended[start:start+n].copy()
        if len(output) != n or not np.isfinite(output).all():
            raise ValueError('candidate output is nonfinite or changed sample count')
        self.history[:] = extended[-len(self.history):]
        self.sample_clock += n
        self.tdl_applied_samples += n if enabled else 0
        return output

    def record(self, stage):
        if stage not in ('started', 'final'):
            raise ValueError('record stage must be started or final')
        mean = math.fsum(self.powers*self.delays)
        rms = math.sqrt(math.fsum(self.powers*(self.delays-mean)**2))*1e9/self.samp_rate
        return dict(schema_version=self.schema, qualification='unqualified_prototype', record_type=stage,
            direction=self.direction, sample_rate_hz=self.samp_rate, master_seed=self.master_seed,
            derived_tap_seed=self.derived_seed, catalog_sha256=CATALOG_SHA256,
            standard_version='19.4.0', profile=self.profile, settings=dict(self.config),
            common_delay_samples=CENTER, fir_taps=TAPS, fir_beta=BETA,
            unit_power_semantics='sum_of_ensemble_mean_path_powers',
            achieved_profile_rms_delay_spread_ns=rms, delays_samples=self.delays.tolist(),
            mean_path_powers=self.powers.tolist(), coefficient_cf32_le_hex=self.coefficients.astype('<c8').tobytes().hex(),
            aggregate_fir_cf32_le_hex=self.kernel.astype('<c8').tobytes().hex(),
            history_cf32_le_hex=self.history.astype('<c8').tobytes().hex(),
            sample_clock=self.sample_clock, tdl_applied_samples=self.tdl_applied_samples,
            rng_algorithm='numpy.PCG64+standard_normal_float64', numpy_version=np.__version__,
            initialized_gaussian_normal_variates=2*self.path_count, subsequent_rng_draws=0,
            rng_state=self.rng.bit_generator.state,
            arithmetic='static_path_firs_sum_c128_to_c64_then_convolution_c128_to_c64')


class StaticTdlCChannel(StaticTdlAChannel):
    """TDL-C uses the same static recipe with its 24 diffuse published paths."""
    profile = 'tdl-c'
    path_count = 24
    schema = 'grc_static_tdl_c_candidate_v1'

    def __init__(self, direction, sample_rate_hz, master_seed, *, delay_spread_ns=300.0,
                 occupied_bandwidth_hz=19_080_000.0, enabled=False, max_doppler_hz=0.0):
        super().__init__(direction, sample_rate_hz, master_seed,
            delay_spread_ns=delay_spread_ns, occupied_bandwidth_hz=occupied_bandwidth_hz,
            enabled=enabled, max_doppler_hz=max_doppler_hz)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class TdlReferenceChannel:
    """Selected 23.04 Msps/100 ns static UL channel; DL remains raw identity.

    Only enable is mutable. UL always advances its history and has the same
    15-sample causal delay, including the disabled sham condition.
    """
    core_type = StaticTdlAChannel
    semantics = SEMANTICS

    def __init__(self, direction, samp_rate, master_seed, *, mode, ref_power,
                 gain, noise_enabled, noise_snr_db, cw_enabled, cw_sir_db,
                 cw_freq_hz, tdl_enabled=False):
        from radio_broker_schedule import validate_settings
        settings = validate_settings(dict(gain=gain, noise_enabled=noise_enabled,
            noise_snr_db=noise_snr_db, cw_enabled=cw_enabled, cw_sir_db=cw_sir_db,
            cw_freq_hz=cw_freq_hz, tdl_enabled=tdl_enabled), samp_rate, tdl=True)
        tap_seed(master_seed, direction)
        if (finite(samp_rate, 23_040_000, 23_040_000, 'selected sample rate') != 23_040_000
                or finite(ref_power, 1, 1, 'unused reference power') != 1
                or mode != ('identity' if direction == 'DL' else 'fixed')
                or (direction == 'DL' and tdl_enabled)):
            raise ValueError('TDL requires DL identity and processed UL with unused unit reference')
        self.direction, self.samp_rate, self.master_seed, self.mode = direction, float(samp_rate), master_seed, mode
        self.config = MappingProxyType(dict(mode=mode, ref_power=1.0, **settings))
        self.core = self.core_type(direction, samp_rate, master_seed, enabled=tdl_enabled) if direction == 'UL' else None
        self.sample_clock = 0
        realization = dict(channel_semantics_version=self.semantics, direction=direction,
                           sample_rate_hz=self.samp_rate, master_seed=master_seed,
                           catalog_sha256=CATALOG_SHA256, common_delay_samples=CENTER if self.core else 0)
        if self.core:
            numerical = self.core.record('started')
            for key in ('schema_version', 'qualification', 'record_type', 'history_cf32_le_hex',
                        'sample_clock', 'tdl_applied_samples'):
                numerical.pop(key)
            # The realization is independent of the mutable enable setting.
            numerical['settings'].pop('tdl_enabled')
            realization['numerical_recipe'] = numerical
        self._realization = canonical(realization)
        self.realization_sha256 = hashlib.sha256(self._realization).hexdigest()

    def realization(self):
        return json.loads(self._realization)

    def update_settings(self, settings):
        from radio_broker_schedule import validate_settings
        validated = validate_settings(settings, self.samp_rate, tdl=True)
        if self.mode == 'identity' and validated['tdl_enabled']:
            raise ValueError('TDL cannot be enabled in DL identity')
        changed = validated['tdl_enabled'] != self.config['tdl_enabled']
        if self.core:
            self.core.update_settings({'tdl_enabled': validated['tdl_enabled']})
        self.config = MappingProxyType(dict(self.config, **validated))
        return changed

    def process(self, iq):
        if self.core:
            output = self.core.process(iq)
            self.sample_clock = self.core.sample_clock
            return output
        if (not isinstance(iq, np.ndarray) or iq.dtype != np.complex64
                or iq.ndim != 1 or not np.isfinite(iq).all()):
            raise ValueError('input must be finite one-dimensional cf32')
        if self.sample_clock > UINT64_MAX-len(iq):
            raise OverflowError('sample counter overflow')
        self.sample_clock += len(iq)
        return iq.copy()

    def record(self, stage):
        if stage not in ('started', 'final'):
            raise ValueError('record stage must be started or final')
        history = self.core.history.astype('<c8').tobytes() if self.core else b''
        rng_state = canonical(self.core.rng.bit_generator.state) if self.core else b''
        return dict(schema_version='radio_grc_static_tdl_profile_v1', record_type=stage,
            backend='grc', channel_semantics_version=self.semantics, direction=self.direction,
            sample_rate_hz=self.samp_rate, master_seed=self.master_seed, **dict(self.config),
            realization_sha256=self.realization_sha256, catalog_sha256=CATALOG_SHA256,
            sample_clock=self.sample_clock, tdl_applied_samples=self.core.tdl_applied_samples if self.core else 0,
            common_delay_samples=CENTER if self.core else 0, history_samples=len(history)//8,
            history_sha256=hashlib.sha256(history).hexdigest(),
            rng_state_sha256=hashlib.sha256(rng_state).hexdigest(),
            initialized_gaussian_normal_variates=2*self.core.path_count if self.core else 0, subsequent_rng_draws=0)


class TdlCReferenceChannel(TdlReferenceChannel):
    """Selected 23.04 Msps/300 ns static TDL-C UL; DL remains raw identity."""
    core_type = StaticTdlCChannel
    semantics = TDL_C_SEMANTICS
