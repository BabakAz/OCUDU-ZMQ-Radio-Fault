"""Independent numerical checks for the selected static TDL-C recipe."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import radio_static_tdl as channel_model

# Literal TR38.901 v19.4.0 Table7.7.2-3, independently checked against PDF p93.
DELAYS = [0,.2099,.2219,.2329,.2176,.6366,.6448,.6560,.6584,.7935,.8213,
          .9336,1.2285,1.3083,2.1704,2.7105,4.2589,4.6003,5.4902,5.6077,
          6.3065,6.6374,7.0427,8.6523]
DB = [-4.4,-1.2,-3.5,-5.2,-2.5,0,-2.2,-3.9,-7.4,-7.1,-10.7,-11.1,
      -5.1,-6.8,-8.7,-13.2,-13.9,-13.9,-15.8,-17.1,-16,-15.7,-21.6,-22.8]
RATE = 23_040_000
SETTINGS = dict(gain=1.0, noise_enabled=False, noise_snr_db=0.0,
                cw_enabled=False, cw_sir_db=0.0, cw_freq_hz=0.0, tdl_enabled=False)


def engine(**settings):
    return channel_model.StaticTdlCChannel('UL', RATE, 41, **settings)


def wrapper(direction='UL', **settings):
    return channel_model.TdlCReferenceChannel(direction, RATE, 41,
        mode='identity' if direction == 'DL' else 'fixed', ref_power=1.0,
        **dict(SETTINGS, **settings))


def independent_filter(fraction):
    taps = np.sinc(np.arange(32, dtype=np.float64)-15-fraction)*np.kaiser(32, 6)
    return (taps/math.fsum(taps)).astype(np.float32)


def independent_kernel(channel):
    result = np.zeros(91, np.complex128)
    for normalized, coefficient in zip(DELAYS, channel.coefficients):
        delay = normalized*300e-9*RATE
        integer = math.floor(delay)
        for index, weight in enumerate(independent_filter(delay-integer)):
            result[integer+index] += complex(coefficient)*float(weight)
    return result.astype(np.complex64)


def test_literal_catalog_scaling_power_gaussian_recipe_and_state():
    channel = engine()
    rows = json.loads(channel_model.CATALOG.read_text())['profiles']['tdl-c']['published_taps']
    assert len(rows) == 24 and all(row['kind'] == 'diffuse' for row in rows)
    assert [row['normalized_delay'] for row in rows] == DELAYS
    assert [row['power_db'] for row in rows] == DB
    power = np.array([10**(x/10) for x in DB]); power /= math.fsum(power)
    np.testing.assert_array_equal(channel.powers, power)
    np.testing.assert_allclose(channel.delays, np.array(DELAYS)*300e-9*RATE,
                               rtol=4*np.finfo(np.float64).eps)
    assert max(channel.delays) == pytest.approx(59.8046976)
    assert len(channel.kernel) == 91 and len(channel.history) == 90
    assert channel.derived_seed == 346219897
    rng = np.random.Generator(np.random.PCG64(346219897))
    normals = rng.standard_normal((24, 2), dtype=np.float64)
    expected = (np.sqrt(power/2)*(normals[:, 0]+1j*normals[:, 1])).astype(np.complex64)
    assert channel.coefficients.tobytes() == expected.tobytes()
    assert channel.rng.bit_generator.state == rng.bit_generator.state
    assert math.fsum(channel.powers) == pytest.approx(1, abs=1e-15)
    record = channel.record('started')
    assert record['profile'] == record['settings']['profile'] == 'tdl-c'
    assert record['schema_version'] == 'grc_static_tdl_c_candidate_v1'
    assert record['settings']['delay_spread_ns'] == 300.0
    assert record['settings']['max_doppler_hz'] == 0.0
    assert record['achieved_profile_rms_delay_spread_ns'] == pytest.approx(299.9987466415865)
    assert record['history_cf32_le_hex'] == '00'*(90*8)
    assert record['initialized_gaussian_normal_variates'] == 48
    assert record['subsequent_rng_draws'] == 0
    json.dumps(record, allow_nan=False)


def test_every_path_fraction_meets_independent_passband_budget():
    channel = engine()
    frequency = np.linspace(-.415, .415, 257)
    indices = np.arange(32)
    phase = np.exp(-2j*np.pi*frequency[:, None]*indices)
    for delay, actual in zip(channel.delays, channel.filters):
        fraction = float(delay % 1)
        np.testing.assert_allclose(actual, independent_filter(fraction), rtol=1e-6, atol=1e-8)
        response = np.sum(phase*actual, axis=1)
        error = response*np.exp(2j*np.pi*frequency*(15+fraction))
        achieved_delay = np.real(np.sum(phase*(actual*indices), axis=1)/response)
        assert np.max(np.abs(20*np.log10(np.abs(error)))) < .010
        assert np.max(np.abs(np.angle(error))) < .00050
        assert np.max(np.abs(achieved_delay-(15+fraction))) < .010


def test_complete_impulse_and_transfer_function_match_physical_path_sum():
    channel = engine(enabled=True)
    values = np.zeros(127, np.complex64); values[0] = 1
    expected = np.pad(independent_kernel(channel), (0, 127-91))
    np.testing.assert_allclose(channel.process(values), expected, rtol=2e-6, atol=3e-8)
    frequency = np.linspace(-.4140625, .4140625, 257)
    actual = np.sum(np.exp(-2j*np.pi*frequency[:, None]*np.arange(91))*channel.kernel, axis=1)
    ideal = np.sum(np.exp(-2j*np.pi*frequency[:, None]*(15+np.array(DELAYS)[None, :]*300e-9*RATE))
                   *channel.coefficients, axis=1)
    component_bound = 10**(.010/20)-1+.00050
    assert max(abs(actual-ideal)) < component_bound*sum(abs(channel.coefficients))+1e-6
    assert channel.tdl_applied_samples == channel.sample_clock == 127
    assert channel.record('final')['subsequent_rng_draws'] == 0


def drive(channel, values, pattern):
    output = []; cursor = count = 0
    for begin, end, enabled in [(0, 113, False), (113, 719, True), (719, len(values), False)]:
        assert cursor == begin
        channel.update_settings({'tdl_enabled': enabled})
        while cursor < end:
            stop = min(end, cursor+pattern[count % len(pattern)])
            output.append(channel.process(values[cursor:stop]))
            cursor, count = stop, count+1
    return np.concatenate(output)


@pytest.mark.parametrize('pattern', [(1027,), (257,), (1,2,7,31)])
def test_partition_onset_history_and_restore_keep_common_delay(pattern):
    rng = np.random.default_rng(112358)
    values = (rng.normal(size=1027)+1j*rng.normal(size=1027)).astype(np.complex64)
    values[0:2] = np.array([0,0x80000000,0x80000000,0], np.uint32).view(np.complex64)
    channel = engine(); rng_before = copy.deepcopy(channel.rng.bit_generator.state)
    actual = drive(channel, values, pattern)
    delayed = np.concatenate((np.zeros(15, np.complex64), values))[:len(values)]
    expected = delayed.copy(); kernel = independent_kernel(channel)
    for index in range(113, 719):
        expected[index] = sum(complex(kernel[lag])*complex(values[index-lag])
                              for lag in range(min(index+1, len(kernel))))
    np.testing.assert_allclose(actual[113:719], expected[113:719], rtol=2e-6, atol=3e-7)
    assert actual[:113].tobytes() == delayed[:113].tobytes()
    assert actual[719:].tobytes() == delayed[719:].tobytes()
    assert actual.tobytes() == drive(engine(), values, (len(values),)).tobytes()
    assert channel.sample_clock == len(values) and channel.tdl_applied_samples == 606
    assert channel.rng.bit_generator.state == rng_before
    assert channel.history.tobytes() == values[-90:].tobytes()


def test_empty_frames_repeatability_and_direction_seeds():
    channel = engine(); before = channel.record('started')
    assert channel.process(np.empty(0, np.complex64)).size == 0
    assert channel.record('started') == before
    other = channel_model.StaticTdlCChannel('DL', RATE, 41)
    assert other.derived_seed != channel.derived_seed
    assert other.coefficients.tobytes() != channel.coefficients.tobytes()
    assert engine().coefficients.tobytes() == channel.coefficients.tobytes()


@pytest.mark.parametrize('changes', [dict(max_doppler_hz=1), dict(max_doppler_hz=float('nan')),
    dict(delay_spread_ns=0), dict(occupied_bandwidth_hz=RATE), dict(enabled=1)])
def test_unsupported_configuration_rejected(changes):
    with pytest.raises(ValueError):
        engine(**changes)


@pytest.mark.parametrize('settings', [{'tdl_enabled':1}, {'tdl_enabled':True,'profile':'tdl-a'}, {}])
def test_only_boolean_enable_mutable(settings):
    channel = engine(); before = channel.record('started')
    with pytest.raises(ValueError):
        channel.update_settings(settings)
    assert channel.record('started') == before


def test_bad_input_and_overflow_leave_history_and_counters_unchanged():
    channel = engine(enabled=True); before = channel.record('started')
    with pytest.raises(ValueError):
        channel.process(np.array([np.inf], np.complex64))
    assert channel.record('started') == before
    magnitude = float(np.finfo(np.float32).max)
    values = (magnitude*np.exp(-1j*np.angle(channel.kernel[::-1]))).astype(np.complex64)
    assert np.isfinite(values).all()
    with pytest.raises(ValueError, match='output is nonfinite'):
        channel.process(values)
    assert channel.record('started') == before
    channel.sample_clock = (1 << 64)-1; before = channel.record('started')
    with pytest.raises(OverflowError):
        channel.process(np.ones(1, np.complex64))
    assert channel.record('started') == before


def test_wrapper_seals_c_recipe_and_retains_disabled_history():
    channel = wrapper(); realization = channel.realization()
    assert realization['channel_semantics_version'] == 'grc_static_tdl_c_v1'
    recipe = realization['numerical_recipe']
    assert recipe['profile'] == recipe['settings']['profile'] == 'tdl-c'
    assert recipe['settings']['delay_spread_ns'] == 300.0
    assert recipe['initialized_gaussian_normal_variates'] == 48
    assert len(bytes.fromhex(recipe['aggregate_fir_cf32_le_hex'])) == 91*8
    enabled = wrapper(tdl_enabled=True)
    assert enabled.realization_sha256 == channel.realization_sha256
    values = np.arange(113, dtype=np.float32).astype(np.complex64)
    actual = channel.process(values)
    assert actual.tobytes() == np.concatenate((np.zeros(15, np.complex64), values))[:113].tobytes()
    state = channel.record('final')
    assert state['channel_semantics_version'] == 'grc_static_tdl_c_v1'
    assert state['initialized_gaussian_normal_variates'] == 48
    assert state['history_samples'] == 90 and state['common_delay_samples'] == 15
    assert state['history_sha256'] == hashlib.sha256(values[-90:].astype('<c8').tobytes()).hexdigest()
    assert state['tdl_applied_samples'] == 0
    assert len(channel_model.canonical(state)) < 4096
    channel.update_settings(dict(SETTINGS, tdl_enabled=True))
    assert channel.realization() == realization
    assert channel.realization_sha256 == hashlib.sha256(channel_model.canonical(realization)).hexdigest()
    enabled.process(values)
    np.testing.assert_array_equal(channel.process(values), enabled.process(values))


def test_wrapper_dl_is_raw_identity_and_rejects_enable():
    channel = wrapper('DL')
    values = np.array([0,0x80000000,0x80000000,0,0x3f800000,0xbf800000], np.uint32).view(np.complex64)
    assert channel.process(values).tobytes() == values.tobytes()
    realization = channel.realization(); state = channel.record('final')
    assert realization['channel_semantics_version'] == 'grc_static_tdl_c_v1'
    assert 'numerical_recipe' not in realization
    assert state['common_delay_samples'] == state['history_samples'] == 0
    assert state['initialized_gaussian_normal_variates'] == state['tdl_applied_samples'] == 0
    with pytest.raises(ValueError):
        channel.update_settings(dict(SETTINGS, tdl_enabled=True))
    assert channel.record('final') == state
