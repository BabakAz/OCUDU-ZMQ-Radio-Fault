"""Independent numerical reference checks for the static TDL-A core."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import radio_static_tdl as candidate

# Independent literal standard-table data for the selected TDL-A only.
DELAYS = [0,.3819,.4025,.5868,.4610,.5375,.6708,.575,.7618,1.5375,1.8978,
          2.2242,2.1718,2.4942,2.5119,3.0582,4.081,4.4579,4.5695,4.7966,5.0066,5.3043,9.6586]
DB = [-13.4,0,-2.2,-4,-6,-8.2,-9.9,-10.5,-7.5,-15.9,-6.6,-16.7,-12.4,
      -15.2,-10.8,-11.3,-12.7,-16.2,-18.3,-18.9,-16.6,-19.9,-29.7]


def engine(**settings):
    return candidate.StaticTdlAChannel('UL', 23_040_000, 41, **settings)


def independent_filter(fraction):
    # NumPy special functions independently evaluate the scalar series design.
    taps = np.sinc(np.arange(32, dtype=np.float64)-15-fraction)*np.kaiser(32, 6)
    return (taps/math.fsum(taps)).astype(np.float32)


def independent_kernel(channel):
    result = np.zeros(math.floor(max(DELAYS)*100e-9*23_040_000)+32, np.complex128)
    # Coefficients are checked independently against the seeded Gaussian recipe below.
    for normalized, coefficient in zip(DELAYS, channel.coefficients):
        delay = normalized*100e-9*23_040_000
        integer = math.floor(delay)
        for index, weight in enumerate(independent_filter(delay-integer)):
            result[integer+index] += complex(coefficient)*float(weight)
    return result.astype(np.complex64)


def test_catalog_normalization_gaussian_recipe_and_exact_state():
    channel = engine()
    rows = json.loads(candidate.CATALOG.read_text())['profiles']['tdl-a']['published_taps']
    assert [r['normalized_delay'] for r in rows] == DELAYS
    assert [r['power_db'] for r in rows] == DB
    power = np.array([10**(x/10) for x in DB]); power /= math.fsum(power)
    np.testing.assert_array_equal(channel.powers, power)
    np.testing.assert_allclose(channel.delays, np.array(DELAYS)*100e-9*23_040_000, rtol=4*np.finfo(np.float64).eps)
    rng = np.random.Generator(np.random.PCG64(channel.derived_seed))
    z = rng.standard_normal((23,2), dtype=np.float64)
    expected = (np.sqrt(power/2)*(z[:,0]+1j*z[:,1])).astype(np.complex64)
    assert channel.coefficients.tobytes() == expected.tobytes()
    assert channel.rng.bit_generator.state == rng.bit_generator.state
    assert math.fsum(channel.powers) == pytest.approx(1, abs=1e-15)
    record = channel.record('started')
    assert record['achieved_profile_rms_delay_spread_ns'] == pytest.approx(100.00579391604872)
    assert record['history_cf32_le_hex'] == '00'*(len(channel.history)*8)
    assert record['initialized_gaussian_normal_variates'] == 46
    json.dumps(record, allow_nan=False)


@pytest.mark.parametrize('fraction', [0,.125,.5,.875,np.nextafter(1.,0.)])
def test_fractional_impulse_and_passband_against_analytic_delay(fraction):
    actual = candidate.fractional_filter(float(fraction))
    np.testing.assert_allclose(actual, independent_filter(fraction), rtol=1e-6, atol=1e-8)
    frequency = np.linspace(-.415,.415,257)
    phase = np.exp(-2j*np.pi*frequency[:,None]*np.arange(32))
    response = np.sum(phase*actual, axis=1)
    error = response*np.exp(2j*np.pi*frequency*(15+fraction))
    delay = np.real(np.sum(phase*(actual*np.arange(32)),axis=1)/response)
    assert np.max(np.abs(20*np.log10(np.abs(error)))) < .010
    assert np.max(np.abs(np.angle(error))) < .00050
    assert np.max(np.abs(delay-(15+fraction))) < .010


def test_every_selected_path_fraction_meets_passband_budget():
    channel = engine()
    for delay in channel.delays:
        test_fractional_impulse_and_passband_against_analytic_delay(float(delay % 1))


def test_complete_impulse_matches_independent_physical_path_superposition():
    channel = engine(enabled=True)
    values = np.zeros(127,np.complex64); values[0] = 1
    actual = channel.process(values)
    expected = np.pad(independent_kernel(channel),(0,127-len(channel.kernel)))
    np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=3e-8)
    assert channel.tdl_applied_samples == channel.sample_clock == 127
    assert channel.record('final')['subsequent_rng_draws'] == 0


def drive(channel, values, pattern):
    output = []; cursor = count = 0
    for begin,end,enabled in [(0,17,False),(17,211,True),(211,len(values),False)]:
        assert cursor == begin
        channel.update_settings({'tdl_enabled': enabled})
        while cursor < end:
            stop = min(end,cursor+pattern[count % len(pattern)])
            output.append(channel.process(values[cursor:stop]))
            cursor, count = stop, count+1
    return np.concatenate(output)


@pytest.mark.parametrize('pattern', [(1027,), (257,), (1,2,7,31)])
def test_partition_onset_history_and_immediate_restore_keep_common_delay(pattern):
    rng = np.random.default_rng(112358)
    values = (rng.normal(size=1027)+1j*rng.normal(size=1027)).astype(np.complex64)
    values[0:2] = np.array([0,0x80000000,0x80000000,0],np.uint32).view(np.complex64)
    channel = engine()
    rng_before = copy.deepcopy(channel.rng.bit_generator.state)
    actual = drive(channel,values,pattern)
    reference = np.concatenate((np.zeros(15,np.complex64), values))[:len(values)]
    expected = reference.copy()
    kernel = independent_kernel(channel)
    # Independent scalar causal convolution over original complete input history.
    for index in range(17,211):
        expected[index] = sum(complex(kernel[lag])*complex(values[index-lag])
                              for lag in range(min(index+1,len(kernel))))
    np.testing.assert_allclose(actual[17:211],expected[17:211],rtol=2e-6,atol=3e-7)
    assert actual[:17].tobytes() == reference[:17].tobytes()
    assert actual[211:].tobytes() == reference[211:].tobytes()
    contiguous = drive(engine(),values,(len(values),))
    assert actual.tobytes() == contiguous.tobytes()
    assert channel.sample_clock == len(values) and channel.tdl_applied_samples == 194
    assert channel.rng.bit_generator.state == rng_before
    assert channel.history.tobytes() == values[-len(channel.history):].tobytes()


def test_empty_frames_and_direction_seeds_are_independent():
    channel = engine(); before = channel.record('started')
    assert channel.process(np.empty(0,np.complex64)).size == 0
    assert channel.record('started') == before
    other = candidate.StaticTdlAChannel('DL',23_040_000,41)
    assert other.derived_seed != channel.derived_seed
    assert other.coefficients.tobytes() != channel.coefficients.tobytes()
    assert engine().coefficients.tobytes() == channel.coefficients.tobytes()


@pytest.mark.parametrize('changes', [dict(max_doppler_hz=1),dict(max_doppler_hz=float('nan')),
    dict(delay_spread_ns=0),dict(occupied_bandwidth_hz=23_040_000),dict(enabled=1)])
def test_unsupported_configuration_is_rejected(changes):
    with pytest.raises(ValueError): engine(**changes)


@pytest.mark.parametrize('settings', [{'tdl_enabled':1},{'tdl_enabled':True,'max_doppler_hz':0},{}])
def test_only_boolean_enable_is_mutable(settings):
    channel = engine(); before = channel.record('started')
    with pytest.raises(ValueError): channel.update_settings(settings)
    assert channel.record('started') == before


def test_counter_and_nonfinite_input_fail_without_state_change():
    channel = engine(enabled=True)
    channel.sample_clock = (1<<64)-1
    before = channel.record('started')
    with pytest.raises(OverflowError): channel.process(np.ones(1,np.complex64))
    assert channel.record('started') == before
    with pytest.raises(ValueError): channel.process(np.array([np.inf],np.complex64))
    assert channel.record('started') == before


def test_whole_channel_frequency_response_matches_selected_delays_and_tap_powers():
    channel = engine(enabled=True)
    f = np.linspace(-.4140625,.4140625,257)
    phase = np.exp(-2j*np.pi*f[:,None]*np.arange(len(channel.kernel)))
    actual = np.sum(phase*channel.kernel,axis=1)
    ideal = np.sum(np.exp(-2j*np.pi*f[:,None]*(15+np.array(DELAYS)[None,:]*100e-9*23_040_000))
                   *channel.coefficients,axis=1)
    # Absolute transfer error stays meaningful near random channel notches.
    component_bound = 10**(.010/20)-1+.00050
    assert max(abs(actual-ideal)) < component_bound*sum(abs(channel.coefficients))+1e-6


def test_output_overflow_does_not_commit_input_history_or_counters():
    channel = engine(enabled=True)
    before = channel.record('started')
    magnitude = float(np.finfo(np.float32).max)
    values = (magnitude*np.exp(-1j*np.angle(channel.kernel[::-1]))).astype(np.complex64)
    assert np.isfinite(values).all()
    with pytest.raises(ValueError,match='output is nonfinite'):
        channel.process(values)
    assert channel.record('started') == before
