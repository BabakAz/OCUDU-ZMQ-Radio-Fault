"""L1 fixed-reference production DSP and L0 CLI contracts; no sockets started."""

import importlib.util
import json
import math
import types
from pathlib import Path

import numpy as np
import pytest


BROKER = Path(__file__).resolve().parents[1] / 'scripts/ocudu_channel_broker.py'
POWER_SAMPLES = 1_000_000
POWER_SEEDS = (1, 17, 41, 424242, 0xFFFFFFFF)
POWER_TOLERANCE_DB = 0.1


@pytest.fixture
def broker():
    spec = importlib.util.spec_from_file_location('fixed_reference_test', BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def channel(broker, **overrides):
    config = dict(direction='DL', sample_rate_hz=1_000_000.0, master_seed=41,
                  ref_power=1.0)
    config.update(overrides)
    return broker.FixedReferenceChannel(**config)


@pytest.mark.parametrize('seed', POWER_SEEDS)
@pytest.mark.parametrize('snr_db', (-5, 0, 5, 10, 20, 30))
def test_fixed_awgn_power_and_iq_statistics_against_independent_oracle(broker, seed, snr_db):
    engine = channel(broker, master_seed=seed, noise_snr_db=snr_db)
    desired = np.ones(POWER_SAMPLES, dtype=np.complex64)
    output = engine.process(desired)
    residual = output.astype(np.complex128) - desired
    expected_power = 10.0 ** (-snr_db / 10.0)
    measured_power = float(np.vdot(residual, residual).real / POWER_SAMPLES)
    assert abs(10.0 * math.log10(measured_power / expected_power)) < POWER_TOLERANCE_DB
    expected_component_variance = expected_power / 2.0
    for component in (residual.real, residual.imag):
        assert abs(float(np.mean(component))) < 6.0 * math.sqrt(expected_component_variance / POWER_SAMPLES)
        assert abs(float(np.var(component)) / expected_component_variance - 1.0) < 0.01
    assert engine.sample_clock == engine.awgn_complex_draws == POWER_SAMPLES
    assert engine.awgn_normal_draws == 2 * POWER_SAMPLES
    assert math.isclose(engine.energies['noise'], measured_power * POWER_SAMPLES, rel_tol=1e-6)


@pytest.mark.parametrize('gain', (0.0, 0.125, 1.0))
def test_reference_noise_continues_under_silence_and_desired_attenuation(broker, gain):
    samples = np.ones(100_003, dtype=np.complex64)
    masked = channel(broker, gain=0.0, noise_snr_db=10.0)
    zero = channel(broker, gain=gain, noise_snr_db=10.0)
    np.testing.assert_array_equal(masked.process(samples), zero.process(np.zeros_like(samples)))
    assert masked.masked_samples == len(samples)
    assert masked.attenuated_samples == 0
    assert zero.masked_samples == (len(samples) if gain == 0 else 0)
    assert zero.attenuated_samples == (len(samples) if 0 < gain < 1 else 0)
    assert zero.energies['noise'] > 0.0
    assert zero.energies['desired'] == 0.0


@pytest.mark.parametrize('frequency', (-137125.0, -7.0, 0.0, 7.0, 137125.0, 500000.0))
@pytest.mark.parametrize('sir_db', (0.0, 10.0, 20.0, 30.0))
def test_cw_power_and_phase_against_absolute_sample_oracle(broker, frequency, sir_db):
    n = 50_003
    engine = channel(broker, noise_enabled=False, cw_enabled=True,
                     cw_freq_hz=frequency, cw_sir_db=sir_db)
    actual = engine.process(np.zeros(n, dtype=np.complex64)).astype(np.complex128)
    amplitude = math.sqrt(10.0 ** (-sir_db / 10.0))
    expected = amplitude * np.exp(2j * np.pi * frequency * np.arange(n) / engine.samp_rate)
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-7 * amplitude)
    power = float(np.vdot(actual, actual).real / n)
    assert abs(10.0 * math.log10(power / amplitude ** 2)) < 1e-5
    assert engine.phase_u64 == (n * engine.cw_step_u64) % (1 << 64)
    assert engine.awgn_complex_draws == n  # disabled noise still consumes innovations


@pytest.mark.parametrize('frequency,expected', [
    (0.0, 0), (125.0, 1 << 61), (-125.0, (1 << 64) - (1 << 61)),
    (500.0, 1 << 63), (-500.0, 1 << 63),
    (math.ldexp(1000.0, -65), 1), (-math.ldexp(1000.0, -65), (1 << 64) - 1),
])
def test_nco_binary64_quantization_known_ticks_and_half_tick(broker, frequency, expected):
    engine = channel(broker, sample_rate_hz=1000.0, cw_freq_hz=frequency)
    assert engine.cw_step_u64 == expected


def test_fixed_composite_replay_is_partition_invariant(broker):
    input_rng = np.random.Generator(np.random.PCG64(772))
    samples = (input_rng.standard_normal(50003) + 1j * input_rng.standard_normal(50003)).astype(np.complex64)
    samples[200:1700] = 0
    parameters = dict(gain=0.125, cw_enabled=True, cw_freq_hz=-271125.0, cw_sir_db=10.0,
                      noise_snr_db=20.0)
    baseline = channel(broker, **parameters)
    expected = baseline.process(samples)
    partitions = [(257,), (1024,), (4096,), (23040,), (1, 2, 7, 31, 257, 801, 4093)]
    for pattern in partitions:
        engine = channel(broker, **parameters)
        outputs = []
        offset = index = 0
        while offset < len(samples):
            end = min(len(samples), offset + pattern[index % len(pattern)])
            outputs.append(engine.process(samples[offset:end]))
            offset, index = end, index + 1
        actual = np.concatenate(outputs)
        difference = actual.astype(np.complex128) - expected
        assert np.linalg.norm(difference) / np.linalg.norm(expected) < 1e-6
        assert np.max(np.abs(difference)) < 1e-5
        assert engine.sample_clock == baseline.sample_clock == len(samples)
        assert engine.phase_u64 == baseline.phase_u64
        assert engine.rng.bit_generator.state == baseline.rng.bit_generator.state
        for name, value in baseline.energies.items():
            assert math.isclose(engine.energies[name], value, rel_tol=1e-12, abs_tol=1e-12)


def test_component_enable_changes_cannot_consume_other_streams(broker):
    input_samples = np.zeros(10007, dtype=np.complex64)
    settings = [dict(noise_enabled=noise, cw_enabled=tone) for noise in (False, True) for tone in (False, True)]
    engines = [channel(broker, cw_freq_hz=-72500, **setting) for setting in settings]
    outputs = [engine.process(input_samples) for engine in engines]
    assert len({json.dumps(engine.rng.bit_generator.state, sort_keys=True) for engine in engines}) == 1
    assert len({engine.phase_u64 for engine in engines}) == 1
    np.testing.assert_array_equal(outputs[0], np.zeros_like(input_samples))
    # Tone-only + noise-only predicts the composite, with one float32 output rounding.
    expected = (outputs[1].astype(np.complex128) + outputs[2]).astype(np.complex64)
    np.testing.assert_array_equal(outputs[3], expected)


def test_dl_settings_do_not_change_ul_output_or_seed(broker):
    normal = broker.channel_broker_source(
        channel_semantics='fixed_reference_v1',
        dl_profile={'ref_power': 1.0}, ul_profile={'ref_power': 0.5},
    )
    changed = broker.channel_broker_source(
        channel_semantics='fixed_reference_v1',
        dl_profile={'ref_power': 10.0, 'gain': 0, 'cw_enabled': True, 'cw_freq_hz': -7000},
        ul_profile={'ref_power': 0.5},
    )
    samples = np.ones(4099, dtype=np.complex64)
    normal._dl_imp['fixed_channel'].process(samples)
    changed._dl_imp['fixed_channel'].process(samples)
    np.testing.assert_array_equal(normal._ul_imp['fixed_channel'].process(samples),
                                  changed._ul_imp['fixed_channel'].process(samples))
    assert normal._dl_imp['fixed_channel'].awgn_seed != normal._ul_imp['fixed_channel'].awgn_seed


def test_identity_mode_is_byte_exact_without_dsp_state_draws(broker):
    raw = np.array([0.0, -0.0, np.finfo(np.float32).max, -np.finfo(np.float32).max,
                    np.finfo(np.float32).tiny, -np.finfo(np.float32).tiny], dtype=np.float32).tobytes()
    engine = channel(broker, mode='identity', gain=0, cw_enabled=True, cw_freq_hz=12345)
    before = engine.rng.bit_generator.state
    output = engine.process(np.frombuffer(raw, dtype=np.complex64))
    assert output.tobytes() == raw
    assert engine.sample_clock == 3
    assert engine.awgn_normal_draws == engine.awgn_complex_draws == engine.phase_u64 == 0
    assert engine.masked_samples == engine.attenuated_samples == 0
    assert engine.rng.bit_generator.state == before
    assert engine.energies['input'] == engine.energies['desired'] == engine.energies['output']
    json.dumps(engine.record('final'), allow_nan=False)


def test_empty_fixed_frame_advances_no_state_or_energy(broker):
    engine = channel(broker, cw_enabled=True)
    before = engine.record('started')
    engine.process(np.empty(0, dtype=np.complex64))
    assert engine.record('started') == before


@pytest.mark.parametrize('counter', ('sample_clock', 'awgn_complex_draws', 'awgn_normal_draws',
                                    'masked_samples', 'attenuated_samples'))
def test_uint64_overflow_fails_before_any_state_or_rng_change(broker, counter):
    gain = 0.0 if counter == 'masked_samples' else 0.5
    engine = channel(broker, gain=gain)
    setattr(engine, counter, (1 << 64) - 1)
    before = engine.record('started')
    rng_before = engine.rng.bit_generator.state
    with pytest.raises(OverflowError):
        engine.process(np.ones(1, dtype=np.complex64))
    assert engine.record('started') == before
    assert engine.rng.bit_generator.state == rng_before


@pytest.mark.parametrize('samples', [np.array([np.nan], dtype=np.complex64),
                                     np.array([np.inf], dtype=np.complex64),
                                     np.ones(2, dtype=np.complex128),
                                     np.ones((2, 2), dtype=np.complex64)])
def test_invalid_numerical_input_changes_no_counters_or_rng(broker, samples):
    engine = channel(broker)
    before = engine.record('started')
    rng_before = engine.rng.bit_generator.state
    with pytest.raises(ValueError):
        engine.process(samples)
    assert engine.record('started') == before
    assert engine.rng.bit_generator.state == rng_before


@pytest.mark.parametrize('method,value', [('set_snr_db', 10), ('set_k_factor_db', 3),
    ('set_doppler_hz', 5), ('set_fading_mode', 0), ('set_cfo_hz', 0),
    ('set_drop_prob', 0), ('set_sir_db', 20), ('set_int_type', 'none'),
    ('set_int_freq_hz', 0), ('set_identity_mode', False), ('apply_scenario_updates', {})])
def test_legacy_controls_cannot_silently_mutate_selected_fixed_profile(broker, method, value):
    engine = broker.channel_broker_source(channel_semantics='fixed_reference_v1',
                                         dl_profile={'ref_power': 1}, ul_profile={'ref_power': 1})
    before = engine._dl_imp['fixed_channel'].record('started')
    with pytest.raises(RuntimeError, match='legacy controls'):
        getattr(engine, method)(value)
    assert engine._dl_imp['fixed_channel'].record('started') == before


@pytest.mark.parametrize('frequency', (-71.0, 71.0))
def test_legacy_cw_silence_consumes_elapsed_samples_before_resuming(broker, frequency):
    phase = [0.0]
    rng = np.random.default_rng(17)
    for silence in (np.empty(0, dtype=np.complex64), np.zeros(257, dtype=np.complex64)):
        assert broker.apply_interference(silence, 'cw', frequency, 4, 1000, phase, rng).tobytes() == silence.tobytes()
    assert math.isclose(phase[0], (2 * math.pi * frequency * 257 / 1000) % (2 * math.pi), abs_tol=1e-12)
    desired = np.ones(13, dtype=np.complex64)
    actual = broker.apply_interference(desired, 'cw', frequency, 4, 1000, phase, rng)
    expected = desired + 0.5 * np.exp(2j * np.pi * frequency * np.arange(257, 270) / 1000)
    np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-6)


BASE_OPTIONS = ['--no-gui', '--channel-semantics', 'fixed_reference_v1',
                '--dl-ref-power', '1', '--ul-ref-power', '1']


@pytest.mark.parametrize('extra', [
    ['--identity'], ['--snr', '28'], ['--fading'], ['--rayleigh'], ['--profile', 'flat'],
    ['--cfo', '0'], ['--drop-prob', '0'], ['--scenario', 'none'], ['--sir', '20'],
    ['--interference-type', 'none'], ['--interference-freq', '0'], ['--doppler', '5'],
    ['--k-factor', '3'], ['--dl-gain', '-1'], ['--ul-gain', '1.1'],
    ['--dl-noise-snr', 'nan'], ['--ul-cw-sir', 'inf'], ['--dl-mode', 'epa'],
    ['--dl-cw-freq', '600000', '--samp-rate', '1000000'],
    ['--dl-gain', '0.5', '--dl-gain', '0.7'],
    ['--channel-semantics', 'fixed_reference_v1'],
])
def test_bad_fixed_profile_cli_fails_before_startup(broker, monkeypatch, extra):
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('unexpected socket startup'))
    with pytest.raises(SystemExit) as error:
        broker.main(options=BASE_OPTIONS + extra)
    assert error.value.code == 2


@pytest.mark.parametrize('options', [
    ['--no-gui', '--dl-mode', 'fixed'], ['--no-gui', '--ul-cw'],
    ['--no-gui', '--dl-ref-power', '1'],
    ['--no-gui', '--channel-semantics', 'fixed_reference_v1'],
    ['--no-gui', '--channel-semantics', 'fixed_reference_v1', '--dl-ref-power', '1'],
    BASE_OPTIONS[1:], BASE_OPTIONS + ['--dl-ref-power', '0'],
])
def test_missing_version_reference_or_headless_is_rejected(broker, monkeypatch, options):
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('unexpected socket startup'))
    with pytest.raises(SystemExit) as error:
        broker.main(options=options)
    assert error.value.code == 2


def test_cli_maps_distinct_directional_parameters_without_starting_sockets(broker, monkeypatch):
    captured = []
    class CaptureHeadless:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self.broker = types.SimpleNamespace(fatal_error=None)
        def start(self): return True
        def wait(self): pass
        def stop(self): pass
    monkeypatch.setattr(broker, 'ocudu_channel_broker_headless', CaptureHeadless)
    monkeypatch.setattr(broker, 'signal', types.SimpleNamespace(signal=lambda *args: None, SIGINT=2, SIGTERM=15))
    result = broker.main(options=BASE_OPTIONS + ['--dl-gain', '0.25', '--dl-cw',
        '--dl-noise-snr', '12', '--dl-cw-sir', '10', '--dl-cw-freq', '-7',
        '--ul-mode', 'identity', '--ul-noise-off', '--samp-rate', '1000'])
    assert result == 0
    assert captured[0]['dl_profile'] == {
        'ref_power': 1, 'mode': 'fixed', 'gain': 0.25, 'noise_enabled': True,
        'noise_snr_db': 12, 'cw_enabled': True, 'cw_sir_db': 10, 'cw_freq_hz': -7,
    }
    assert captured[0]['ul_profile']['mode'] == 'identity'
    assert captured[0]['ul_profile']['noise_enabled'] is False
    assert captured[0]['ul_profile']['cw_enabled'] is False
    assert captured[0]['channel_semantics'] == 'fixed_reference_v1'


@pytest.mark.parametrize('duplicates', [
    ['--dl-mode', 'fixed', '--dl-mode', 'identity'], ['--ul-noise-off', '--ul-noise-off'],
    ['--ul-cw', '--ul-cw'], ['--dl-cw-sir', '20', '--dl-cw-sir=10'],
])
def test_duplicate_profile_fields_reject_in_configuration_only_mode(broker, monkeypatch, duplicates):
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('unexpected socket'))
    with pytest.raises(SystemExit) as error:
        broker.main(options=BASE_OPTIONS + ['--validate-config-only'] + duplicates)
    assert error.value.code == 2


@pytest.mark.parametrize('legacy', [
    {'snr_db': 12}, {'k_factor_db': 0}, {'doppler_hz': 7}, {'fading_mode': 3},
    {'cfo_hz': 10}, {'drop_prob': 0.1}, {'int_type': 'cw'}, {'int_freq_hz': 0},
    {'sir_db': 10},
])
def test_direct_fixed_constructor_rejects_nondefault_legacy_arguments(broker, legacy):
    with pytest.raises(ValueError, match='nondefault legacy'):
        broker.channel_broker_source(channel_semantics='fixed_reference_v1',
                                     dl_profile={'ref_power': 1}, ul_profile={'ref_power': 1}, **legacy)


def test_configuration_only_emits_effective_settings_without_any_constructor(broker, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail('configuration check constructed an engine or socket')
    for name in ('channel_broker_source', 'ocudu_channel_broker_headless', 'FixedReferenceChannel', 'run_gui'):
        monkeypatch.setattr(broker, name, forbidden)
    monkeypatch.setattr(broker._zmq, 'Context', forbidden)
    assert broker.main(options=BASE_OPTIONS + ['--validate-config-only', '--ul-mode', 'identity',
                                              '--ul-cw', '--dl-cw', '--dl-cw-freq', '-7']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0].removeprefix('RADIO_CONFIG_VALIDATED: '))
    assert record['channel_semantics_version'] == 'fixed_reference_v1'
    assert record['directions']['UL']['mode'] == 'identity'
    assert record['directions']['UL']['noise_enabled'] is False
    assert record['directions']['UL']['cw_enabled'] is False
    assert record['directions']['DL']['cw_enabled'] is True
    assert record['directions']['DL']['cw_freq_hz'] == -7


def test_configuration_only_still_validates_incomplete_profiles(broker, monkeypatch):
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('unexpected socket'))
    with pytest.raises(SystemExit) as error:
        broker.main(options=['--no-gui', '--channel-semantics', 'fixed_reference_v1',
                             '--validate-config-only'])
    assert error.value.code == 2


@pytest.mark.parametrize('direction', ('dl', 'ul'))
@pytest.mark.parametrize('option,field', [('noise-snr', 'noise_snr_db'),
                                         ('cw-sir', 'cw_sir_db'), ('cw-freq', 'cw_freq_hz')])
@pytest.mark.parametrize('value', ('-1e-07', '-7e+1'))
def test_config_only_accepts_negative_scientific_numeric_values(broker, monkeypatch, capsys,
                                                               direction, option, field, value):
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('unexpected socket'))
    assert broker.main(options=BASE_OPTIONS + ['--validate-config-only',
                                              f'--{direction}-{option}', value]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0].removeprefix('RADIO_CONFIG_VALIDATED: '))
    assert record['directions'][direction.upper()][field] == float(value)


@pytest.mark.parametrize('extra', [
    ['--dl-cw-freq', '-1e999'], ['--dl-noise-snr', '-nan'],
    ['--dl-gain', '-1e-07'], ['--ul-ref-power', '-1e-07'],
    ['--dl-cw-freq', '--unknown-flag'],
    ['--dl-cw-freq', '-1e-07', '--dl-cw-freq=-2e-07'],
])
def test_negative_numeric_normalization_does_not_weaken_validation(broker, monkeypatch, extra):
    monkeypatch.setattr(broker._zmq, 'Context', lambda: pytest.fail('unexpected socket'))
    with pytest.raises(SystemExit) as error:
        broker.main(options=BASE_OPTIONS + ['--validate-config-only'] + extra)
    assert error.value.code == 2
