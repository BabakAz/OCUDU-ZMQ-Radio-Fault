"""Exact regression against the frozen pre-reuse DSP, without radio processes."""
import importlib
from pathlib import Path
import runpy
import types

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def previous_process():
    return runpy.run_path(str(ROOT / 'tests/fixtures/grc_fixed_process_before_energy_reuse.py'))['process']


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    return importlib.import_module('ocudu_channel_broker')


def samples():
    # Include both zero signs, subnormals, normal-boundary values and maxima.
    lanes = np.array([0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x807fffff,
                      0x00800000, 0x80800000, 0x3f800000, 0xbf800000,
                      0x7f7fffff, 0xff7fffff, 0x3f800001, 0xbf7fffff,
                      0x00000000, 0x80000000], dtype=np.uint32)
    return np.tile(lanes.view(np.complex64), 17)


def make_pair(broker, previous_process, **settings):
    config = dict(direction='UL', sample_rate_hz=23_040_000, master_seed=41,
                  ref_power=10_000_000, noise_snr_db=10, cw_sir_db=10)
    config.update(settings)
    current = broker.FixedReferenceChannel(**config)
    previous = broker.FixedReferenceChannel(**config)
    previous.process = types.MethodType(previous_process, previous)
    current.phase_u64 = previous.phase_u64 = (1 << 64) - 3
    return current, previous


def assert_equal(current, previous, values):
    actual, expected = current.process(values), previous.process(values)
    assert actual.dtype == expected.dtype == np.dtype('complex64')
    assert actual.tobytes() == expected.tobytes()
    assert current.record('final') == previous.record('final')
    assert current.rng.bit_generator.state == previous.rng.bit_generator.state
    return actual


@pytest.mark.parametrize('direction', ['DL', 'UL'])
@pytest.mark.parametrize('gain', [-0.0, 0.125, np.nextafter(1.0, 0.0), 1.0])
@pytest.mark.parametrize('noise,cw', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('frequency', [0.0, -1_440_000.0])
def test_all_gain_component_combinations_preserve_exact_bytes_and_complete_state(
        broker, previous_process, direction, gain, noise, cw, frequency):
    pair = make_pair(broker, previous_process, direction=direction, gain=gain,
                     noise_enabled=noise, cw_enabled=cw, cw_freq_hz=frequency)
    values = samples()
    for begin, end in [(0, 0), (0, 1), (1, 8), (8, 25), (25, len(values)), (0, 0)]:
        assert_equal(*pair, values[begin:end])


@pytest.mark.parametrize('bits', [
    [0, 0x80000000, 0x80000000, 0],
    [1, 0x80000001, 0x007fffff, 0x807fffff],
    [0x00800000, 0x80800000, 0x00800001, 0x80800001],
    [0x7f7fffff, 0xff7fffff, 0x3f800001, 0xbf7fffff],
])
@pytest.mark.parametrize('gain', [0.0, .125, np.nextafter(1.0, 0.0), 1.0])
def test_energy_equivalence_without_large_values_hiding_small_magnitudes(
        broker, previous_process, bits, gain):
    pair = make_pair(broker, previous_process, gain=gain, noise_enabled=False, cw_enabled=False)
    assert_equal(*pair, np.array(bits, np.uint32).view(np.complex64))


@pytest.mark.parametrize('seed', [0, 0xffffffff])
def test_toggle_restore_and_disabled_cw_phase_retain_same_future_output(broker, previous_process, seed):
    pair = make_pair(broker, previous_process, master_seed=seed, noise_enabled=False,
                     cw_enabled=False, cw_freq_hz=1_440_000)
    sequence = [({}, 1), ({'cw_enabled': True}, 7),
                ({'noise_enabled': True, 'gain': 0.125}, 19),
                ({'cw_enabled': False, 'gain': 0.0}, 2),
                ({'noise_enabled': False, 'gain': 1.0}, 31),
                ({'cw_enabled': True, 'cw_freq_hz': -1_440_000}, 13),
                ({'cw_enabled': False, 'cw_freq_hz': 1_440_000}, 1)]
    for settings, count in sequence:
        for engine in pair:
            engine.update_settings({key: settings.get(key, value) for key, value in engine.config.items()
                                    if key not in ('mode', 'ref_power')})
        assert_equal(*pair, samples()[:count])
    assert pair[0].awgn_normal_draws == 2 * sum(count for _, count in sequence)


def test_unity_without_additions_preserves_output_zero_arithmetic_and_draws(broker, previous_process):
    current, previous = make_pair(broker, previous_process, noise_enabled=False, cw_enabled=False)
    values = np.array([0, 0x80000000, 0x80000000, 0], np.uint32).view(np.complex64)
    actual = assert_equal(current, previous, values)
    assert actual.tobytes() != values.tobytes()  # Returning input would alter the established contract.
    assert current.awgn_normal_draws == 4


@pytest.mark.parametrize('mode,gain,noise,cw,expected_calls', [
    ('fixed', 1.0, False, False, 1), ('fixed', 0.0, False, False, 1),
    ('fixed', .125, False, False, 2), ('fixed', 1.0, True, False, 3),
    ('fixed', 1.0, False, True, 3), ('fixed', .125, True, True, 5),
    ('identity', 1.0, False, False, 1),
])
def test_redundant_full_buffer_energy_scans_are_eliminated(
        broker, mode, gain, noise, cw, expected_calls):
    engine = broker.FixedReferenceChannel('UL', 23_040_000, 41, ref_power=1,
                mode=mode, gain=gain, noise_enabled=noise, cw_enabled=cw)
    calls = []
    original = engine._energy
    def counted(values):
        calls.append(len(values))
        return original(values)
    engine._energy = counted
    engine.process(np.ones(31, np.complex64))
    assert calls == [31] * expected_calls


@pytest.mark.parametrize('pattern', [(5003,), (257,), (1, 2, 7, 31, 801)])
def test_actual_sample_schedule_preserves_old_outputs_and_state(
        broker, previous_process, monkeypatch, tmp_path, pattern):
    import test_radio_schedule_grc as helpers
    modules = (broker, importlib.import_module('radio_schedule_runtime'),
               importlib.import_module('radio_broker_schedule'),
               importlib.import_module('radio_broker_profile'))
    for name in ('current', 'previous'):
        (tmp_path / name).mkdir(mode=0o700)
    current, channels, accounting = helpers.fixture(modules, tmp_path / 'current')
    previous, old_channels, old_accounting = helpers.fixture(modules, tmp_path / 'previous')
    for engine in old_channels.values():
        engine.process = types.MethodType(previous_process, engine)
    for name in channels:
        warmup = np.ones(37 if name == 'DL' else 53, np.complex64)
        assert helpers.exchange(current, accounting, name, warmup).tobytes() == helpers.exchange(
            previous, old_accounting, name, warmup).tobytes()
    helpers.arm(current)
    helpers.arm(previous)
    rng = np.random.default_rng(9001)
    values = (rng.standard_normal(5003) + 1j*rng.standard_normal(5003)).astype(np.complex64)
    offset = index = 0
    while offset < len(values):
        stop = min(len(values), offset + pattern[index % len(pattern)])
        for name in channels:
            actual = helpers.exchange(current, accounting, name, values[offset:stop])
            expected = helpers.exchange(previous, old_accounting, name, values[offset:stop])
            assert actual.tobytes() == expected.tobytes()
            assert channels[name].record('final') == old_channels[name].record('final')
            assert channels[name].rng.bit_generator.state == old_channels[name].rng.bit_generator.state
        offset, index = stop, index + 1
    for name in channels:
        assert current.directions[name].next_event == previous.directions[name].next_event
        assert current.directions[name].restoration_observed == previous.directions[name].restoration_observed
        assert current.directions[name].forwarded_samples == previous.directions[name].forwarded_samples


@pytest.mark.parametrize('stride', [2, -1, -3])
def test_strided_input_keeps_exact_energy_and_output(broker, previous_process, stride):
    pair = make_pair(broker, previous_process, noise_enabled=False, cw_enabled=False)
    assert_equal(*pair, samples()[::stride])


@pytest.mark.parametrize('field', ['input', 'desired', 'noise', 'cw', 'output'])
def test_cumulative_energy_overflow_keeps_previous_commit_order(
        broker, previous_process, field):
    pair = make_pair(broker, previous_process, noise_enabled=True, cw_enabled=True)
    before_rng = pair[0].rng.bit_generator.state
    before_phase = pair[0].phase_u64
    # Declared test seam: a finite huge energy reaches the cumulative guard
    # without allocating physically impossible input lengths. DSP/RNG still run.
    for engine in pair:
        engine.energies[field] = 1e308
        engine._energy = lambda values: 1e308 if np.any(values != 0) else 0.0
        with pytest.raises(OverflowError, match='cumulative energy'):
            engine.process(np.ones(2, np.complex64))
        assert engine.sample_clock == engine.awgn_normal_draws == engine.awgn_complex_draws == 0
        assert engine.phase_u64 == before_phase
        assert engine.energies[field] == 1e308
        assert all(value == 0.0 for key, value in engine.energies.items() if key != field)
        assert engine.rng.bit_generator.state != before_rng  # Existing failed-output behavior.
    assert pair[0].record('final') == pair[1].record('final')
    assert pair[0].rng.bit_generator.state == pair[1].rng.bit_generator.state
