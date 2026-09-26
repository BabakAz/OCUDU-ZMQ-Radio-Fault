"""Independent offline oracle checks; never start brokers, processes or sockets."""

import copy
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC = importlib.util.spec_from_file_location("fixed_validation", SCRIPTS / "validate_radio_broker_fixed.py")
    VALIDATOR = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(VALIDATOR)
finally:
    sys.path.pop(0)


def base_profile():
    return json.loads(VALIDATOR.FIXTURE.read_text())


def synthetic_evidence(tmp_path, case):
    """Analytic receiver fixture plus an independent NumPy Gaussian generator."""
    profile = VALIDATOR.case_profile(base_profile(), case)
    payloads = VALIDATOR.case_payloads(case)
    source = np.frombuffer(b"".join(payloads), dtype=np.complex64)
    n = len(source)
    records, relays, paths = [], [], {}
    expected = {d: {"messages": len(payloads), "samples": n} for d in ("DL", "UL")}
    for index, direction in enumerate(("DL", "UL")):
        config = profile["directions"][direction]
        desired = (source * config["desired_gain"]).astype(np.complex64)
        cw = np.zeros(n, dtype=np.complex64)
        if config["cw"]["enabled"]:
            cw = (np.sqrt(0.1) * np.exp(-2j * np.pi * (np.arange(n) % 16) / 16)).astype(np.complex64)
        noise = np.zeros(n, dtype=np.complex64)
        if config["noise"]["enabled"]:
            rng = np.random.default_rng(8989 + index)
            noise = (np.sqrt(0.005) * (rng.normal(size=n) + 1j * rng.normal(size=n))).astype(np.complex64)
        received = (desired.astype(np.complex128) + cw + noise).astype(np.complex64)
        paths[direction] = {kind: tmp_path / f"{direction}-{kind}.cf32" for kind in ("source", "received")}
        paths[direction]["source"].write_bytes(source.tobytes())
        paths[direction]["received"].write_bytes(received.tobytes())
        row = {
            "schema_version": "radio_fixed_profile_v1", "backend": "c", "direction": direction,
            "channel_semantics_version": "fixed_reference_v1", "rng_version": "component_streams_v1",
            "record_type": "final", "master_seed": profile["master_seed"], "awgn_seed": 100 + index,
            "sample_rate_hz": profile["sample_rate_hz"], "mode": "fixed", "ref_power": 1,
            "gain": config["desired_gain"], "noise_enabled": config["noise"]["enabled"],
            "noise_snr_db": 20, "cw_enabled": config["cw"]["enabled"], "cw_sir_db": 10,
            "cw_freq_hz": config["cw"]["frequency_hz"], "units": "relative_digital_complex_power",
            "scope": "cumulative_processed_samples", "sample_clock": n,
            "awgn_complex_draws": n, "awgn_normal_draws": 2 * n,
            "masked_samples": n if config["desired_gain"] == 0 else 0,
            "attenuated_samples": n if 0 < config["desired_gain"] < 1 else 0,
            "cw_step_u64": (1 << 64) - (1 << 60) if config["cw"]["enabled"] else 0,
        }
        row["phase_u64"] = n * row["cw_step_u64"] % (1 << 64)
        for name, samples in (("input", source), ("desired", desired), ("noise", noise), ("cw", cw), ("output", received)):
            # The test fixture computes its own sums without broker energy code.
            row[name + "_energy"] = float(np.sum(samples.real.astype(float) ** 2 + samples.imag.astype(float) ** 2))
        initial = copy.deepcopy(row)
        initial["record_type"] = "started"
        for key in ("sample_clock", "awgn_complex_draws", "awgn_normal_draws", "masked_samples",
                    "attenuated_samples", "phase_u64", "input_energy", "desired_energy",
                    "noise_energy", "cw_energy", "output_energy"):
            initial[key] = 0
        records.extend((initial, row))
        relays.append({"schema_version": "radio_broker_accounting_v1", "record_type": "final",
                       "backend": "c", "direction": direction, "identity": False,
                       "input_messages": len(payloads), "output_messages": len(payloads),
                       "input_samples": n, "output_samples": n, "error_count": 0, "status": "stopped"})
    return profile, expected, paths, records, relays


def invoke(evidence):
    profile, expected, paths, records, relays = evidence
    log = "\n".join(VALIDATOR.DSP_PREFIX + json.dumps(row) for row in records)
    log += "\n" + "\n".join("C_RELAY_ACCOUNTING: " + json.dumps(row) for row in relays)
    return VALIDATOR.validate_dsp_records(log, "c", profile, expected, paths)


@pytest.mark.parametrize("case", ["baseline", "awgn", "zeros", "attenuation", "mask", "cw"])
def test_independent_analytic_and_statistical_fixtures_pass(tmp_path, case):
    result = invoke(synthetic_evidence(tmp_path, case))
    assert len(result["dsp_records"]) == 4 and len(result["relay_records"]) == 2
    assert result["independent_measurements"]["DL"]["processed_samples"] == VALIDATOR.SAMPLE_COUNT


@pytest.mark.parametrize("field,value", [
    ("sample_clock", 65538), ("awgn_complex_draws", 0), ("awgn_normal_draws", 65537),
    ("masked_samples", 1), ("attenuated_samples", 1), ("phase_u64", 1),
    ("input_energy", -1), ("output_energy", 0), ("noise_energy", float("nan")),
    ("scope", "forwarded"), ("noise_enabled", 1),
])
def test_forged_dsp_counters_and_energies_fail(tmp_path, field, value):
    evidence = synthetic_evidence(tmp_path, "awgn")
    evidence[3][1][field] = value
    with pytest.raises(VALIDATOR.transport.ValidationError):
        invoke(evidence)


def test_processed_and_forwarded_counts_cannot_be_conflated(tmp_path):
    evidence = synthetic_evidence(tmp_path, "awgn")
    evidence[4][0]["output_samples"] -= 1
    with pytest.raises(VALIDATOR.transport.ValidationError, match="unreconciled"):
        invoke(evidence)


def test_wrong_noise_scaling_fails_peer_oracle_even_with_plausible_records(tmp_path):
    evidence = synthetic_evidence(tmp_path, "zeros")
    path = evidence[2]["DL"]["received"]
    bad = np.frombuffer(path.read_bytes(), dtype=np.complex64) * 2
    path.write_bytes(bad.tobytes())
    with pytest.raises(VALIDATOR.transport.ValidationError, match="AWGN power"):
        invoke(evidence)


def test_wrong_cw_frequency_fails_independent_analytic_oracle(tmp_path):
    evidence = synthetic_evidence(tmp_path, "cw")
    path = evidence[2]["DL"]["received"]
    received = np.frombuffer(path.read_bytes(), dtype=np.complex64).copy()
    received[40] += 0.01j
    path.write_bytes(received.tobytes())
    with pytest.raises(VALIDATOR.transport.ValidationError, match="analytic gain/CW"):
        invoke(evidence)


def test_partition_cases_reuse_stream_and_respect_finite_budgets():
    assert len(VALIDATOR.CASES) * 2 <= VALIDATOR.MAX_PROCESS_CASES <= 30
    for case in VALIDATOR.CASES:
        payloads = VALIDATOR.case_payloads(case)
        assert b"" in payloads and sum(map(len, payloads)) * 2 <= VALIDATOR.MAX_RAW_BYTES_PER_DIRECTION
        assert all(len(payload) % 8 == 0 for payload in payloads)
    for plain in ("awgn", "cw"):
        assert b"".join(VALIDATOR.case_payloads(plain)) == b"".join(VALIDATOR.case_payloads(plain + "-partition"))
    base = VALIDATOR.case_profile(base_profile(), "awgn")
    for changed in ("attenuation", "mask", "cw"):
        assert VALIDATOR.case_profile(base_profile(), changed)["directions"]["UL"] == base["directions"]["UL"]


def test_exchange_rejects_changed_boundaries_nonfinite_and_missing_samples():
    for received in ([], [b"\0" * 4], [b"\0" * 8, b""], [np.array([np.nan], dtype=np.complex64).tobytes()]):
        with pytest.raises(VALIDATOR.transport.ValidationError):
            VALIDATOR.validate_exchange("DL", b"\0" * 8, received)


def test_requires_explicit_execution_and_fresh_artifact_destination(tmp_path):
    with pytest.raises(SystemExit) as exc:
        VALIDATOR.main(["--output", str(tmp_path / "unused")])
    assert exc.value.code == 2 and not (tmp_path / "unused").exists()
    with pytest.raises(SystemExit) as exc:
        VALIDATOR.main(["--run-local", "--output", str(tmp_path / "unused")])
    assert exc.value.code == 2
