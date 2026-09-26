import ast
import json
import subprocess
import time as _time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "dependencies/toolchain.lock.json"
C_BROKER = ROOT / "scripts/zmq_channel_broker.c"
GRC_BROKER = ROOT / "scripts/ocudu_channel_broker.py"
GRC_VALIDATOR = ROOT / "scripts/validate_broker.py"
DOCS = ROOT / "docs/BROKERS.md"


def test_c_broker_links_with_locked_clang_18_and_c17(tmp_path):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    toolchain = lock["toolchain"]
    compiler = toolchain["cc_default"]
    expected_version = toolchain["compiler_version"]

    banner = subprocess.run(
        [compiler, "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    assert f"clang version {expected_version}" in banner
    assert toolchain["c_standard"] == "c17"

    subprocess.run(
        [
            compiler,
            "-std=c17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            str(C_BROKER),
            "-o",
            str(tmp_path / "zmq_channel_broker"),
            "-lzmq",
            "-lm",
            "-pthread",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_c_broker_parser_is_strict_and_defaults_match_launcher():
    source = C_BROKER.read_text(encoding="utf-8")

    assert "atof(" not in source
    assert "strtof(" in source
    assert "!isfinite(value)" in source
    assert "Unknown option" in source
    assert "must not exceed Nyquist" in source
    assert "dl_snr_db   = 28.0f" in source
    assert "dl_doppler  = 5.0f" in source
    assert "k_factor_db = 3.0f" in source
    assert 'strcmp(argv[i], "--seed") == 0' in source
    assert "time(NULL)" not in source
    assert "master_seed ^ UINT32_C(0xD1A5EED)" in source
    assert "master_seed ^ UINT32_C(0xA17EED)" in source


def test_c_broker_receive_and_relay_failures_are_bounded_and_process_fatal():
    source = C_BROKER.read_text(encoding="utf-8")

    assert "zmq_msg_recv" in source
    assert "zmq_msg_size" in source
    assert "received_size > MAX_MESSAGE_SIZE" in source
    assert "uint8_t *resized = realloc(*buffer, received_size)" in source
    assert "resized == NULL" in source
    assert "dlen % (2U * sizeof(float)) != 0" in source
    assert "IQ reply length %zu is not cf32-aligned" in source
    assert "if (ctx == NULL)" in source
    assert "if (rep == NULL)" in source
    assert "if (req == NULL)" in source
    assert "thread_error = pthread_create" in source
    assert "CHANNEL_START_READY" in source
    assert "atomic_store_explicit(&fatal_error, 1" in source
    assert "return EXIT_FAILURE" in source


def test_c_broker_cw_sir_uses_complex_iq_power(tmp_path):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    compiler = lock["toolchain"]["cc_default"]
    harness = tmp_path / "cw_sir_test.c"
    executable = tmp_path / "cw_sir_test"
    harness.write_text(
        f"""
#define main ocudu_broker_program_main
#include \"{C_BROKER.as_posix()}\"
#undef main

int main(void) {{
    float samples[] = {{1.0f, 0.0f, 1.0f, 0.0f}};
    interference_state_t interference;
    interference_init(&interference, 1, 0.0f, 4.0f, 1000.0f);
    interference_apply(&interference, samples, 4);
    if (fabsf(samples[0] - 1.5f) > 1e-6f ||
        fabsf(samples[1]) > 1e-6f ||
        fabsf(samples[2] - 1.5f) > 1e-6f ||
        fabsf(samples[3]) > 1e-6f) {{
        return 1;
    }}
    return 0;
}}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            compiler,
            "-std=c17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            str(harness),
            "-o",
            str(executable),
            "-lzmq",
            "-lm",
            "-pthread",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(executable)], check=True)


def test_c_broker_cw_phase_advances_once_per_complex_sample(tmp_path):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    compiler = lock["toolchain"]["cc_default"]
    harness = tmp_path / "cw_phase_test.c"
    executable = tmp_path / "cw_phase_test"
    harness.write_text(
        f"""
#define main ocudu_broker_program_main
#include \"{C_BROKER.as_posix()}\"
#undef main

int main(void) {{
    float samples[] = {{1.0f, 0.0f, 1.0f, 0.0f}};
    interference_state_t interference;
    interference_init(&interference, 1, 100.0f, 100.0f, 1000.0f);
    interference_apply(&interference, samples, 4);
    return interference.phase_step_u64 != 0 &&
           interference.phase_u64 == 2 * interference.phase_step_u64 ? 0 : 1;
}}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            compiler,
            "-std=c17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            str(harness),
            "-o",
            str(executable),
            "-lzmq",
            "-lm",
            "-pthread",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(executable)], check=True)


def test_c_broker_periodic_progress_is_visible_before_process_exit(tmp_path):
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    compiler = lock["toolchain"]["cc_default"]
    harness = tmp_path / "progress_flush_test.c"
    executable = tmp_path / "progress_flush_test"
    output = tmp_path / "broker.log"
    harness.write_text(
        f"""
#define main ocudu_broker_program_main
#include \"{C_BROKER.as_posix()}\"
#undef main

int main(int argc, char **argv) {{
    if (argc != 2 || freopen(argv[1], "w", stdout) == NULL) {{
        return 2;
    }}
    if (setvbuf(stdout, NULL, _IOFBF, 4096) != 0) {{
        return 3;
    }}

    channel_args_t channel = {{.name = "DL"}};
    fading_state_t fading = {{
        .h_I = 0.0f,
        .h_Q = 0.0f,
        .enabled = 1,
        .los_amp = 1.0f,
        .scatter_amp = 0.0f,
    }};
    print_channel_progress(&channel, 10000UL, 23040000UL, &fading,
                           -3.0f, 2.0f);

    FILE *probe = fopen(argv[1], "r");
    if (probe == NULL) {{
        return 4;
    }}
    char line[256] = {{0}};
    int visible = fgets(line, sizeof(line), probe) != NULL &&
                  strstr(line, "[DL] 10000 msgs") != NULL;
    fclose(probe);
    return visible ? 0 : 5;
}}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            compiler,
            "-std=c17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            str(harness),
            "-o",
            str(executable),
            "-lzmq",
            "-lm",
            "-pthread",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(executable), str(output)], check=True)
    assert "[DL] 10000 msgs" in output.read_text(encoding="utf-8")


def test_grc_rejects_ignored_directional_snr_and_bounds_numbers():
    source = GRC_BROKER.read_text(encoding="utf-8")

    assert 'parser.add_argument("--dl-snr"' not in source
    assert 'parser.add_argument("--ul-snr"' not in source
    assert "def bounded_float(option, minimum, maximum):" in source
    assert "math.isfinite(parsed)" in source
    assert 'bounded_float("--drop-prob", 0.0, 1.0)' in source
    assert "must not exceed Nyquist" in source
    assert "frequency-selective --doppler must not exceed --samp-rate/8" in source
    assert "parser.parse_args(options)" in source
    assert "def __init__(self, samp_rate=23.04e6, seed=1):" in source
    assert "samp_rate=args.samp_rate" in source
    assert "top_block_cls(samp_rate=args.samp_rate, seed=args.seed)" in source
    assert 'parser.add_argument("--seed", type=uint32, default=1' in source
    assert "np.random.default_rng(self.seed ^ 0x0D1A5EED)" in source
    assert "np.random.default_rng(self.seed ^ 0x00A17EED)" in source


def test_grc_broker_is_loopback_scoped_and_fails_closed_per_direction():
    source = GRC_BROKER.read_text(encoding="utf-8")

    assert "tcp://0.0.0.0" not in source
    assert "isinstance(fader, FrequencySelectiveFading)" not in source
    assert "sig_power / (2.0 * snr_linear)" in source
    assert "malformed IQ payload length" in source
    assert "MAX_ZMQ_MESSAGE_BYTES = 64 * 1024 * 1024" in source
    assert "setsockopt(_zmq.MAXMSGSIZE, MAX_ZMQ_MESSAGE_BYTES)" in source
    assert '"channel_model": "absolute-time-sum-of-sinusoids"' in source
    assert "FADING_SOS_TERMS = 16" in source
    assert "FADING_MIN_INTERPOLATION_RATE_HZ = 2_500.0" in source
    assert "FADING_DOPPLER_OVERSAMPLE = 8.0" in source
    assert "GRC_CHANNEL_PROGRESS: " in source
    assert "GRC_CHANNEL_SUMMARY: " in source
    assert "ul_mode = fading_mode" in source
    assert "min(fading_mode, 1)" not in source
    assert "desired-plus-interference power" in source
    assert "fatal_errors.append" in source
    assert "ready_ev.set()" in source
    assert "thread.join(timeout=2.0)" in source
    assert "if self.fatal_error is not None:" in source
    assert "return -1" in source
    assert "raise SystemExit(main())" in source
    assert "self._dl_lock = threading.RLock()" in source
    assert "self._ul_lock = threading.RLock()" in source
    assert "with imp['lock']:" in source
    assert "def apply_scenario_updates(self, updates):" in source
    assert "self.epy_block_broker.apply_scenario_updates(updates)" in source
    assert "tb.broker.apply_scenario_updates(updates)" in source
    assert "class ocudu_channel_broker_headless:" in source
    assert "class ocudu_channel_broker_headless(gr.top_block):" not in source
    headless_source = source.split(
        "class ocudu_channel_broker_headless:", 1
    )[1].split("# ── Main", 1)[0]
    assert "blocks.throttle" not in headless_source
    assert "blocks.null_sink" not in headless_source
    assert "self.broker.start()" in headless_source
    assert "self.broker._stop.is_set()" in headless_source


def _load_grc_frequency_selective_fading():
    tree = ast.parse(GRC_BROKER.read_text(encoding="utf-8"))
    required_assignments = {
        "DELAY_PROFILES",
        "FADING_SOS_TERMS",
        "FADING_MIN_INTERPOLATION_RATE_HZ",
        "FADING_DOPPLER_OVERSAMPLE",
    }
    assignments = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in required_assignments
                for target in node.targets)
    ]
    fading_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "FrequencySelectiveFading"
    )
    namespace = {"np": np, "math": __import__("math")}
    exec(
        compile(
            ast.Module(body=assignments + [fading_class], type_ignores=[]),
            str(GRC_BROKER),
            "exec",
        ),
        namespace,
    )
    return namespace["FrequencySelectiveFading"], namespace["DELAY_PROFILES"]


def test_grc_sparse_fir_preserves_cross_message_continuity():
    fading_class, _ = _load_grc_frequency_selective_fading()

    fading = fading_class(
        "epa", 23.04e6, 0.0, np.random.default_rng(424242)
    )
    physical_coefficients = fading._grid_coefficients(np.array([0]))[:, 0]
    coefficients = np.zeros(fading.ntaps, dtype=np.complex64)
    for index, delay in enumerate(fading.tap_indices):
        coefficients[delay] += (
            np.float32(fading.tap_amplitudes[index])
            * physical_coefficients[index]
        )

    samples = (
        np.random.default_rng(7).standard_normal(4096)
        + 1j * np.random.default_rng(8).standard_normal(4096)
    ).astype(np.complex64)
    # Include fragments shorter than EPA's maximum nine-sample delay. Radio
    # peers can produce these during stream alignment, and the FIR must retain
    # exactly the same history as a single contiguous convolution.
    chunk_sizes = (1, 2, 3, 1, 5, 7, 11, 1777)
    chunks = []
    offset = 0
    for chunk_size in chunk_sizes:
        chunks.append(samples[offset:offset + chunk_size])
        offset += chunk_size
    chunks.append(samples[offset:])
    actual = np.concatenate([
        fading.update_and_apply(chunk) for chunk in chunks
    ])
    expected = np.convolve(samples, coefficients)[:len(samples)]
    np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=2e-6)


def test_grc_doppler_trajectory_is_independent_of_zmq_chunk_partition():
    fading_class, _ = _load_grc_frequency_selective_fading()
    samples = (
        np.random.default_rng(71).standard_normal(50_000)
        + 1j * np.random.default_rng(81).standard_normal(50_000)
    ).astype(np.complex64)

    contiguous = fading_class(
        "epa", 23.04e6, 5.0, np.random.default_rng(424242)
    )
    partitioned = fading_class(
        "epa", 23.04e6, 5.0, np.random.default_rng(424242)
    )
    expected = contiguous.update_and_apply(samples)

    chunk_sizes = (1, 2, 3, 1, 5, 7, 11, 9_215, 9_216, 9_217)
    chunks = []
    offset = 0
    for chunk_size in chunk_sizes:
        chunks.append(samples[offset:offset + chunk_size])
        offset += chunk_size
    chunks.append(samples[offset:])
    actual = np.concatenate([
        partitioned.update_and_apply(chunk) for chunk in chunks
    ])

    np.testing.assert_allclose(actual, expected, rtol=2e-6, atol=2e-6)
    assert contiguous._sample_clock == len(samples)
    assert partitioned._sample_clock == len(samples)


def test_grc_frequency_selective_profile_power_and_doppler_contract():
    fading_class, profiles = _load_grc_frequency_selective_fading()
    fading = fading_class(
        "epa", 23.04e6, 5.0, np.random.default_rng(424242)
    )

    target_power = np.square(np.asarray(fading.tap_amplitudes))
    np.testing.assert_allclose(target_power.sum(), 1.0, rtol=1e-12, atol=1e-12)
    relative_db = 10.0 * np.log10(target_power / target_power[0])
    np.testing.assert_allclose(
        relative_db, profiles["epa"]["powers_db"], rtol=0.0, atol=1e-12
    )
    assert np.max(np.abs(fading._doppler_factors * fading.doppler)) <= 5.0

    # A live Doppler change must not inject an artificial phase step at the
    # next IQ sample. Only the subsequent time derivative may change.
    fading.update_and_apply(np.ones(12_345, dtype=np.complex64))
    before = fading._exact_coefficients_at_sample(fading._sample_clock)
    fading.reconfigure(True, 70.0)
    after = fading._exact_coefficients_at_sample(fading._sample_clock)
    np.testing.assert_allclose(after, before, rtol=2e-6, atol=2e-6)
    assert fading._doppler_changes == 1
    assert fading._doppler_min_hz == 5.0
    assert fading._doppler_max_hz == 70.0
    try:
        fading.reconfigure(True, 5_000.0)
    except ValueError as error:
        assert "exceeds the fixed interpolation grid" in str(error)
    else:
        raise AssertionError("undersampled live Doppler reconfiguration was accepted")

    try:
        fading_class("epa", 1_000.0, 200.0, np.random.default_rng(1))
    except ValueError as error:
        assert "at least eight IQ samples" in str(error)
    else:
        raise AssertionError("undersampled initial Doppler was accepted")


def test_grc_frequency_selective_doppler_correlation_tracks_jakes_target():
    from scipy.special import j0 as bessel_j0

    fading_class, _ = _load_grc_frequency_selective_fading()
    fading = fading_class(
        "epa", 23.04e6, 70.0, np.random.default_rng(424242)
    )
    grid_rate = fading.samp_rate / fading._grid_step_samples
    assert grid_rate >= 8.0 * fading.doppler

    # One second spans enough 70 Hz cycles for the deterministic 16-tone
    # ensemble/time average to approximate the Jake's J0 target. Average all
    # seven independently seeded paths and compare a 2 ms lag.
    coefficients = fading._grid_coefficients(
        np.arange(int(grid_rate), dtype=np.int64)
    ).astype(np.complex128)
    lag = int(round(0.002 * grid_rate))
    correlation = np.mean(
        coefficients[:, :-lag] * np.conj(coefficients[:, lag:])
    ) / np.mean(np.abs(coefficients) ** 2)
    expected = float(bessel_j0(2.0 * np.pi * fading.doppler * 0.002))
    assert abs(correlation.real - expected) < 0.05
    assert abs(correlation.imag) < 0.06


def test_grc_frequency_selective_channel_metrics_are_bounded_and_truthful():
    fading_class, _ = _load_grc_frequency_selective_fading()
    fading = fading_class(
        "epa", 23.04e6, 5.0, np.random.default_rng(424242)
    )
    fading.update_and_apply(np.ones(23_040, dtype=np.complex64))
    record = fading.metrics_record("DL", "run")

    assert record["component"] == "ocudu-grc-channel-metric"
    assert record["channel_model"] == "absolute-time-sum-of-sinusoids"
    assert record["profile"] == "epa"
    assert record["scope"] == "run"
    assert record["sample_clock"] == 23_040
    assert record["grid_step_samples"] == 9_216
    assert record["coefficient_grid_rate_hz"] == 2_500.0
    assert record["coefficient_grid_points"] == 3
    assert record["sos_terms_per_tap"] == 16
    assert len(record["tap_target_power"]) == 7
    assert len(record["tap_grid_power_mean"]) == 7
    assert abs(sum(record["tap_target_power"]) - 1.0) < 1e-8
    assert all(value >= 0.0 for value in record["tap_grid_power_min"])
    assert all(
        low <= mean <= high
        for low, mean, high in zip(
            record["tap_grid_power_min"],
            record["tap_grid_power_mean"],
            record["tap_grid_power_max"],
        )
    )


def test_grc_relay_metrics_separate_peer_rate_from_processing_cost():
    tree = ast.parse(GRC_BROKER.read_text(encoding="utf-8"))
    metrics_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RelayMetrics"
    )
    namespace = {
        "RELAY_PROGRESS_MESSAGES": 10_000,
        "_time": _time,
        "json": json,
        "np": np,
    }
    exec(
        compile(
            ast.Module(body=[metrics_class], type_ignores=[]),
            str(GRC_BROKER),
            "exec",
        ),
        namespace,
    )

    metrics = namespace["RelayMetrics"]("DL", 1_000.0, progress_messages=2)
    metrics.start(now_ns=0)
    assert metrics.observe(
        samples=100,
        payload_bytes=800,
        processing_ns=100_000,
        now_ns=1_000_000_000,
    ) is None
    progress = metrics.observe(
        samples=100,
        payload_bytes=800,
        processing_ns=200_000,
        dropped=True,
        now_ns=2_000_000_000,
    )
    prefix = "GRC_RELAY_PROGRESS: "
    assert progress.startswith(prefix)
    payload = json.loads(progress.removeprefix(prefix))
    assert payload == {
        "component": "ocudu-grc-relay-metric",
        "direction": "DL",
        "elapsed_s": 2.0,
        "start_ns": 0, "end_ns": 2_000_000_000, "elapsed_ns": 2_000_000_000,
        "interval_messages": 2,
        "interval_payload_bytes": 1_600,
        "interval_samples": 200,
        "iq_sample_rate_msps": 0.0001,
        "message_rate_hz": 1.0,
        "processing_budget_mean_pct": 0.15,
        "processing_max_us": 200.0,
        "processing_mean_us": 150.0,
        "processing_p95_us": 195.0,
        "raw_payload_gbps": 6e-06,
        "schema_version": 2,
        "scope": "interval",
        "total_messages": 2,
    }
    assert metrics._interval_processing_ns == []

    summary_prefix = "GRC_RELAY_SUMMARY: "
    summary = metrics.summary(now_ns=3_000_000_000)
    assert summary.startswith(summary_prefix)
    final = json.loads(summary.removeprefix(summary_prefix))
    assert final["total_messages"] == 2
    assert final["total_samples"] == 200
    assert final["total_payload_bytes"] == 1_600
    assert final["dropped_messages"] == 1
    assert final["component"] == "ocudu-grc-relay-metric"
    assert final["schema_version"] == 2
    assert final["scope"] == "run"
    assert final["message_rate_hz"] == 0.666667
    assert final["processing_mean_us"] == 150.0
    assert final["processing_max_us"] == 200.0


def test_grc_offline_validator_matches_live_awgn_scope_and_makes_no_live_claim():
    source = GRC_VALIDATOR.read_text(encoding="utf-8")

    assert "if not isinstance(fader, FrequencySelectiveFading)" not in source
    assert source.count("signal_power=desired_power") == 2
    assert (
        "It does not prove UE attach, throughput, channel calibration, or safety."
        in source
    )
    assert (
        "EPA/EVA/ETU and other impairment combinations remain live-gated."
        in source
    )
    assert "PIPELINE STABILITY GUIDE" not in source


def test_grc_complex_awgn_matches_requested_snr_without_importing_gnuradio():
    tree = ast.parse(GRC_BROKER.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "add_awgn"
    )
    namespace = {"np": np}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(GRC_BROKER), "exec"), namespace)

    rng = np.random.default_rng(1234)
    iq = np.ones(200_000, dtype=np.complex64)
    requested_db = 20.0
    impaired = namespace["add_awgn"](iq, 10.0 ** (requested_db / 10.0), rng)
    measured_db = 10.0 * np.log10(
        np.mean(np.abs(iq) ** 2) / np.mean(np.abs(impaired - iq) ** 2)
    )
    assert abs(measured_db - requested_db) < 0.1


def test_broker_docs_state_the_current_contract_and_model_limits():
    docs = " ".join(DOCS.read_text(encoding="utf-8").split())

    assert "clang-18 -std=c17 -O2 -Wall -Wextra -Wpedantic -Werror" in docs
    assert "fixed_reference_v1" in docs and "legacy_message_local_v1" in docs
    assert "`--dl-snr`/`--ul-snr` are C-only" in docs
    assert "not calibrated RF measurements" in docs
    assert "This is not exact Jakes correlation" in docs
    assert "64 MiB" in docs
    for option in ("--radio-plan-file", "--radio-control-dir", "--validate-config-only",
                   "--radio-metrics-every-messages", "--dl-bind", "--no-gui"):
        assert option in docs
