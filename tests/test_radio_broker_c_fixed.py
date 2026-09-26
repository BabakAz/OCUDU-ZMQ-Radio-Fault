"""L1 numerical checks of the actual C fixed-profile core; no sockets/NR stack.

Engineering protocol: five prespecified seeds, one million complex samples
per AWGN case, 0.1dB power tolerance, six-standard-error mean bounds. Core C
partition replay is exact; these are not cross-backend trajectory guarantees.
"""

import hashlib
import json
import math
from pathlib import Path
import subprocess

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/zmq_channel_broker.c"
HARNESS = ROOT / "tests/cpp/radio_broker_c_fixed_harness.c"
SEEDS = (1, 7, 42, 424242, 4294967295)
SNRS = (-5, 0, 5, 10, 20, 30)


@pytest.fixture(scope="module")
def programs(tmp_path_factory):
    target = tmp_path_factory.mktemp("fixed-c-programs")
    lock = json.loads((ROOT / "dependencies/toolchain.lock.json").read_text())
    compiler = lock["toolchain"]["cc_default"]
    banner = subprocess.check_output([compiler, "--version"], text=True, timeout=5)
    assert f"clang version {lock['toolchain']['compiler_version']}" in banner
    programs = {}
    for name, source in (("fixture", HARNESS), ("cli", SOURCE)):
        executable = target / name
        subprocess.run([compiler, "-std=c17", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                        "-Werror", str(source), "-o", str(executable), "-lzmq", "-lm",
                        "-pthread"], check=True, capture_output=True, timeout=30)
        programs[name] = executable
    return programs


def run_core(programs, tmp_path, data, *, seed=42, direction="DL", reference=1,
             gain=1, snr=20, noise_off=False, cw=False, sir=20, frequency=0,
             rate=23040000, identity=False, chunk=0, switch_at=0, action="none",
             expected_code=0):
    original = np.asarray(data, dtype=np.complex64)
    input_path, output_path = tmp_path / "input.cf32", tmp_path / "output.cf32"
    input_path.write_bytes(original.tobytes())
    args = [str(programs["fixture"]), str(input_path), str(output_path), direction,
            str(seed), str(reference), str(gain), str(snr), str(int(noise_off)),
            str(int(cw)), str(sir), str(frequency), str(rate), str(int(identity)),
            str(chunk), str(switch_at), action]
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    assert result.returncode == expected_code, result.stderr
    if action.startswith("legacy-cw"):
        record = json.loads(next(line.removeprefix("LEGACY_CW_FIXTURE: ")
                                 for line in result.stdout.splitlines()
                                 if line.startswith("LEGACY_CW_FIXTURE: ")))
        return np.frombuffer(output_path.read_bytes(), dtype=np.complex64).copy(), record
    records = [json.loads(line.removeprefix("RADIO_FIXED_PROFILE: "))
               for line in result.stdout.splitlines() if line.startswith("RADIO_FIXED_PROFILE: ")]
    assert [r["record_type"] for r in records] == ["started", "final"]
    record = records[-1]
    assert record["scope"] == "cumulative_processed_samples"
    assert record["units"] == "relative_digital_complex_power"
    assert record["channel_semantics_version"] == "fixed_reference_v1"
    assert record["rng_version"] == "component_streams_v1"
    assert all(math.isfinite(record[k]) for k in record if k.endswith("_energy"))
    return np.frombuffer(output_path.read_bytes(), dtype=np.complex64).copy(), record


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("snr", SNRS)
def test_fixed_noise_power_and_complex_statistics(programs, tmp_path, seed, snr, record_property):
    count = 1_000_000
    data = np.ones(count, dtype=np.complex64)
    output, record = run_core(programs, tmp_path, data, seed=seed, snr=snr)
    noise = output.astype(np.complex128) - data
    expected_power = 10 ** (-snr / 10)
    actual_power = float(np.mean(np.abs(noise) ** 2))
    error_db = 10 * math.log10(actual_power / expected_power)
    assert abs(error_db) <= 0.1
    for component in (noise.real, noise.imag):
        assert abs(float(np.mean(component))) <= 6 * math.sqrt(expected_power / (2 * count))
        assert abs(float(np.var(component)) / (expected_power / 2) - 1) <= 0.02
    assert record["awgn_complex_draws"] == count
    assert record["awgn_normal_draws"] == 2 * count
    assert record["input_energy"] == record["desired_energy"] == count
    assert abs(record["noise_energy"] / count / actual_power - 1) < 1e-5
    record_property("evidence_layer", "L1_file_fixture_actual_C_core")
    record_property("configured_power", expected_power)
    record_property("snr_db", snr)
    record_property("sample_rate_hz", 23040000)
    record_property("measured_power", actual_power)
    record_property("power_error_db", error_db)
    record_property("samples", count)
    record_property("seed", seed)
    record_property("input_sha256", hashlib.sha256(data.tobytes()).hexdigest())
    record_property("output_sha256", hashlib.sha256(output.tobytes()).hexdigest())


def test_silence_and_desired_mask_leave_fixed_noise_present(programs, tmp_path):
    count = 200_000
    zeros = np.zeros(count, dtype=np.complex64)
    noise, reference = run_core(programs, tmp_path, zeros, snr=10)
    masked, record = run_core(programs, tmp_path, np.ones_like(zeros), gain=0, snr=10)
    assert noise.tobytes() == masked.tobytes()
    assert abs(10 * np.log10(np.mean(np.abs(noise.astype(np.complex128)) ** 2) / 0.1)) < 0.1
    assert record["masked_samples"] == count and record["attenuated_samples"] == 0
    assert record["desired_energy"] == reference["desired_energy"] == 0
    assert record["noise_energy"] > 0


def test_gain_and_all_sample_energy_accounting(programs, tmp_path):
    data = (np.arange(31, dtype=np.float32) / 8 + 1j).astype(np.complex64)
    output, record = run_core(programs, tmp_path, data, gain=0.25, noise_off=True)
    np.testing.assert_array_equal(output, data * np.float32(0.25))
    assert record["attenuated_samples"] == len(data) and record["masked_samples"] == 0
    assert record["awgn_complex_draws"] == len(data)
    assert record["noise_energy"] == record["cw_energy"] == 0
    assert record["input_energy"] == float(np.sum(np.abs(data.astype(np.complex128)) ** 2))
    assert math.isclose(record["desired_energy"], record["input_energy"] / 16, rel_tol=1e-14)


@pytest.mark.parametrize("frequency", [-11520000, -123456.75, 0, 123456.75, 11520000])
@pytest.mark.parametrize("sir", [0, 10, 20, 30])
def test_cw_power_frequency_and_phase_against_independent_oracle(
    programs, tmp_path, frequency, sir, record_property,
):
    count, rate = 100_003, 23040000
    output, record = run_core(programs, tmp_path, np.zeros(count, dtype=np.complex64),
                              cw=True, sir=sir, frequency=frequency, noise_off=True, chunk=257)
    amplitude = 10 ** (-sir / 20)
    expected = amplitude * np.exp(2j * np.pi * (frequency / rate) * np.arange(count))
    relative_l2 = np.linalg.norm(output - expected) / np.linalg.norm(expected)
    max_error = float(np.max(np.abs(output - expected)))
    assert relative_l2 <= 1e-6 and max_error <= 1e-5
    actual_power = float(np.mean(np.abs(output.astype(np.complex128)) ** 2))
    error_db = 10 * math.log10(actual_power / (amplitude ** 2))
    assert abs(error_db) <= 0.1
    assert record["cw_energy"] / count == pytest.approx(actual_power, rel=1e-12)
    assert record["phase_u64"] == (record["cw_step_u64"] * count) % (2 ** 64)
    record_property("measured_power", actual_power)
    record_property("configured_power", amplitude ** 2)
    record_property("sir_db", sir)
    record_property("frequency_hz", frequency)
    record_property("sample_rate_hz", rate)
    record_property("samples", count)
    record_property("seed", 42)
    record_property("power_error_db", error_db)
    record_property("relative_l2", float(relative_l2))
    record_property("max_abs_error", max_error)
    record_property("output_sha256", hashlib.sha256(output.tobytes()).hexdigest())


def test_full_chain_exact_partition_and_replay(programs, tmp_path):
    data = np.random.default_rng(37).choice(np.array([1, -1, 1j, -1j], np.complex64), 90001)
    data[30000:40000] = 0
    options = dict(seed=424242, gain=0.3, cw=True, sir=7, frequency=-112233.5, snr=12)
    expected, original = run_core(programs, tmp_path, data, **options)
    for chunk in (1, 257, 1024, 4096, 23040, 0):
        output, record = run_core(programs, tmp_path, data, chunk=chunk, **options)
        assert output.tobytes() == expected.tobytes()
        assert record == original


def test_disabled_additions_keep_awgn_and_cw_clocks_aligned(programs, tmp_path):
    data = np.zeros(10001, dtype=np.complex64)
    reference, _ = run_core(programs, tmp_path, data, snr=10)
    toggled, record = run_core(programs, tmp_path, data, snr=10, noise_off=True,
                               switch_at=4999, action="noise-on", chunk=257)
    assert np.all(toggled[:4999] == 0)
    assert reference[4999:].tobytes() == toggled[4999:].tobytes()
    assert record["awgn_complex_draws"] == len(data)
    tone, _ = run_core(programs, tmp_path, data, cw=True, frequency=-1234.5, noise_off=True)
    switched, record = run_core(programs, tmp_path, data, cw=False, frequency=-1234.5,
                                noise_off=True, switch_at=4999, action="cw-on", chunk=257)
    assert tone[4999:].tobytes() == switched[4999:].tobytes()
    assert record["phase_u64"] == (record["cw_step_u64"] * len(data)) % (2 ** 64)


def test_tone_and_other_direction_do_not_consume_awgn_stream(programs, tmp_path):
    data = np.zeros(40001, dtype=np.complex64)
    noise, base = run_core(programs, tmp_path, data, direction="UL")
    combined, changed = run_core(programs, tmp_path, data, direction="UL", cw=True, frequency=4123)
    tone, _ = run_core(programs, tmp_path, data, direction="UL", cw=True, frequency=4123, noise_off=True)
    np.testing.assert_allclose(combined - tone, noise, atol=3e-8, rtol=1e-6)
    run_core(programs, tmp_path, data, direction="DL", gain=0, snr=-100, cw=True, sir=-100)
    repeated, record = run_core(programs, tmp_path, data, direction="UL")
    assert noise.tobytes() == repeated.tobytes() and record == base
    assert changed["awgn_seed"] == base["awgn_seed"]


def test_identity_preserves_edge_bytes_without_rng_or_nco(programs, tmp_path):
    bits = np.array([0, 0x80000000, 1, 0x80000001, 0x7f7fffff, 0xff7fffff], dtype=np.uint32)
    data = bits.view(np.complex64)
    output, record = run_core(programs, tmp_path, data, identity=True, gain=0, cw=True, frequency=321)
    assert output.tobytes() == data.tobytes()
    assert record["sample_clock"] == len(data)
    assert record["awgn_complex_draws"] == record["awgn_normal_draws"] == record["phase_u64"] == 0
    assert record["input_energy"] == record["desired_energy"] == record["output_energy"]
    assert record["noise_energy"] == record["cw_energy"] == 0


@pytest.mark.parametrize("action", ["sample-overflow", "draw-overflow"])
def test_counter_overflow_rejects_before_mutating_samples(programs, tmp_path, action):
    data = np.ones(3, dtype=np.complex64)
    output, record = run_core(programs, tmp_path, data, action=action, expected_code=7)
    assert output.tobytes() == data.tobytes()
    assert record["input_energy"] == record["output_energy"] == 0


@pytest.mark.parametrize("seed,direction,derived", [
    (0, "DL", 1077339276), (0, "UL", 494003299),
    (1, "DL", 3063538077), (1, "UL", 3934020091),
    (42, "DL", 3083548862), (42, "UL", 3832062725),
    (4294967295, "DL", 3168189938), (4294967295, "UL", 642285092),
])
def test_component_seed_mapping_matches_independent_integer_vectors(
    programs, tmp_path, seed, direction, derived,
):
    _, record = run_core(programs, tmp_path, np.zeros(0, dtype=np.complex64),
                         seed=seed, direction=direction)
    assert record["awgn_seed"] == derived


@pytest.mark.parametrize("frequency,step", [(0, 0), (250, 2 ** 62),
                                          (-250, 3 * 2 ** 62), (500, 2 ** 63)])
def test_nco_step_matches_exact_quadrant_vectors(programs, tmp_path, frequency, step):
    _, record = run_core(programs, tmp_path, np.zeros(0, dtype=np.complex64),
                         frequency=frequency, rate=1000)
    assert record["cw_step_u64"] == step


FIXED = ["--channel-semantics", "fixed_reference_v1", "--dl-ref-power", "1", "--ul-ref-power", "1"]


@pytest.mark.parametrize("legacy", [
    ["--identity"], ["--print-power"], ["--snr", "28"], ["--fading"], ["--doppler", "0"],
    ["--dl-snr", "28"], ["--ul-snr", "28"], ["--k-factor", "3"], ["--rayleigh"],
    ["--interference-type", "none"], ["--interference-freq", "0"], ["--sir", "20"],
])
def test_fixed_profile_rejects_legacy_options_before_context(programs, legacy):
    result = subprocess.run([str(programs["cli"]), *FIXED, *legacy], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and result.stdout == ""
    assert "cannot mix legacy" in result.stderr


@pytest.mark.parametrize("options", [
    ["--dl-mode", "fixed"], ["--dl-ref-power", "1"], ["--ul-cw"], ["--dl-noise-off"],
    ["--channel-semantics", "wrong"], ["--channel-semantics", "fixed_reference_v1"],
    [*FIXED, "--dl-gain", "NaN"], [*FIXED, "--dl-gain", "1.01"],
    [*FIXED, "--dl-cw-freq", "50001", "--srate", "100000"],
    ["--channel-semantics", "fixed_reference_v1", "--dl-ref-power", "0", "--ul-ref-power", "1"],
])
def test_invalid_or_unversioned_profile_fails_before_context(programs, options):
    result = subprocess.run([str(programs["cli"]), *options], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and result.stdout == ""


def test_config_validation_marker_contains_effective_fixed_values_without_sockets(programs):
    result = subprocess.run([str(programs["cli"]), *FIXED, "--validate-config-only",
                             "--dl-gain", "0.375", "--dl-noise-snr", "11.25",
                             "--dl-cw", "--dl-cw-freq", "-12345.625", "--ul-mode", "identity",
                             "--srate", "23040000.125"], check=True, capture_output=True,
                            text=True, timeout=5)
    assert len(result.stdout.splitlines()) == 1
    record = json.loads(result.stdout.removeprefix("RADIO_CONFIG_VALIDATED: "))
    assert record["schema_version"] == "radio_config_validated_v1"
    assert record["channel_semantics_version"] == "fixed_reference_v1"
    assert record["sample_rate_hz"] == 23040000.125
    assert record["directions"]["DL"]["gain"] == 0.375
    assert record["directions"]["DL"]["noise_snr_db"] == 11.25
    assert record["directions"]["DL"]["cw_freq_hz"] == -12345.625
    assert record["directions"]["UL"]["mode"] == "identity"
    assert record["directions"]["UL"]["noise_enabled"] is False


@pytest.mark.parametrize("options", [
    [*FIXED, "--channel-semantics", "legacy_message_local_v1"],
    ["--channel-semantics", "legacy_message_local_v1", *FIXED],
    [*FIXED, "--dl-mode", "fixed", "--dl-mode", "identity"],
    [*FIXED, "--dl-bind", "tcp://127.0.0.1:4001"],
    ["--channel-semantics", "fixed_reference_v1", "--dl-ref-power", "1"],
])
def test_config_validation_does_not_bypass_conflicts_or_incomplete_parameters(programs, options):
    result = subprocess.run([str(programs["cli"]), "--validate-config-only", *options],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and result.stdout == ""


def test_legacy_cw_clock_crosses_silence_and_disabled_intervals(programs, tmp_path):
    rate, frequency = 23040000, 123456
    data = np.ones(128, dtype=np.complex64)
    data[:64] = 0
    output, record = run_core(programs, tmp_path, data, cw=True, noise_off=True,
                              frequency=frequency, chunk=64, action="legacy-cw")
    assert np.all(output[:64] == 0)
    expected = 1 + 0.1 * np.exp(2j * np.pi * frequency / rate * np.arange(64, 128))
    np.testing.assert_allclose(output[64:], expected, rtol=1e-6, atol=1e-6)
    assert record["phase_u64"] == (record["step_u64"] * len(data)) % 2 ** 64
    disabled, _ = run_core(programs, tmp_path, np.ones_like(data), cw=False,
                           frequency=frequency, switch_at=64, action="legacy-cw-on")
    assert np.all(disabled[:64] == 1)
    assert disabled[64:].tobytes() == output[64:].tobytes()


def test_corrected_legacy_cw_clock_is_partition_stable_on_constant_power_input(programs, tmp_path):
    data = np.ones(200003, dtype=np.complex64)
    options = dict(cw=True, frequency=-123456.75, action="legacy-cw")
    reference, original = run_core(programs, tmp_path, data, **options)
    partitioned, record = run_core(programs, tmp_path, data, chunk=257, **options)
    assert reference.tobytes() == partitioned.tobytes() and record == original
    expected = 1 + 0.1 * np.exp(2j * np.pi * (-123456.75 / 23040000) * np.arange(len(data)))
    np.testing.assert_allclose(reference, expected, rtol=1e-6, atol=1e-6)
    # This does not promise whole-chain legacy partition invariance: its power
    # estimates and flat fader remain message-local by their stated model.
