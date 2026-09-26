#!/usr/bin/env python3
"""Explicit bounded L2 fixed-profile engineering checks using private IPC peers.

Reuses the identity validator's owned-child transport and cleanup. Expected
gain/CW and noise statistics come from the supplied mathematical profile and
raw peer captures, never either broker's DSP implementation. No radio stack,
campaign, calibration acceptance, or processing-cost benchmark is performed.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys

import radio_broker_profile as profile_adapter
import validate_radio_broker_identity as transport


ROOT = transport.ROOT
FIXTURE = ROOT / "config/radio_broker/fixed_reference.fixture.json"
CASES = ("baseline", "awgn", "awgn-partition", "zeros", "attenuation", "mask",
         "cw", "cw-partition")
SAMPLE_COUNT = 65537
MAX_PROCESS_CASES = 16
MAX_RAW_BYTES_PER_DIRECTION = 2 * 1024 * 1024
DSP_PREFIX = "RADIO_FIXED_PROFILE: "
NOISE_POWER_REL_TOL = 0.05
NOISE_MEAN_STD_TOL = 0.025
NOISE_LAG1_TOL = 0.02
require = transport.require


def case_profile(base, case):
    require(case in CASES, "unknown fixed-profile case")
    value = copy.deepcopy(base)
    value["reference_provenance"] = "l2-synthetic-unit-complex-power-v1"
    for direction in ("DL", "UL"):
        value["directions"][direction] = {
            "mode": "fixed", "reference_power": 1, "desired_gain": 1,
            "noise": {"enabled": case != "baseline", "snr_db": 20},
            "cw": {"enabled": False, "sir_db": 10, "frequency_hz": 0},
        }
    if case == "attenuation":
        value["directions"]["DL"]["desired_gain"] = 0.25
    elif case == "mask":
        value["directions"]["DL"]["desired_gain"] = 0
    elif case in ("cw", "cw-partition"):
        value["directions"]["DL"]["noise"]["enabled"] = False
        value["directions"]["DL"]["cw"].update(
            enabled=True, frequency_hz=-value["sample_rate_hz"] / 16)
    return profile_adapter.validate_profile(value)


def case_payloads(case):
    import numpy as np

    require(case in CASES, "unknown fixed-profile case")
    signal = np.zeros(SAMPLE_COUNT, dtype=np.complex64) if case == "zeros" else (
        np.resize(np.array([1, 1j, -1, -1j], dtype=np.complex64), SAMPLE_COUNT))
    raw = signal.tobytes()
    if not case.endswith("-partition"):
        return [b"", raw, b""]
    lengths = (1, 2, 257, 1024, 4093, 8191)
    payloads, offset, index = [b""], 0, 0
    while offset < SAMPLE_COUNT:
        count = min(lengths[index % len(lengths)], SAMPLE_COUNT - offset)
        payloads.append(raw[offset * 8:(offset + count) * 8])
        offset += count
        index += 1
    return [*payloads, b""]


def validate_exchange(direction, source, received):
    import numpy as np

    require(len(received) == 1 and len(received[0]) == len(source),
            f"{direction}: fixed DSP changed the message boundary or sample count")
    require(len(source) % 8 == 0 and np.isfinite(np.frombuffer(
        received[0], dtype=np.complex64)).all(), f"{direction}: invalid received cf32")


def energy(samples):
    import numpy as np

    z = np.asarray(samples, dtype=np.complex128)
    return float(np.vdot(z, z).real)


def independent_components(source, config, sample_rate):
    """Analytic desired signal and exact-period CW; no broker code imported."""
    import numpy as np

    desired = (source.astype(np.complex128) * config["desired_gain"]).astype(np.complex64)
    cw = np.zeros(len(source), dtype=np.complex64)
    if config["cw"]["enabled"]:
        # These fixtures use the exact dyadic frequency -Fs/16. Reducing the
        # cycle count analytically avoids approximating an accumulated NCO.
        ratio = config["cw"]["frequency_hz"] / sample_rate
        cycles = np.remainder(np.arange(len(source), dtype=np.float64) * ratio, 1)
        amplitude = math.sqrt(config["reference_power"] * 10 ** (-config["cw"]["sir_db"] / 10))
        cw = (amplitude * np.exp(2j * np.pi * cycles)).astype(np.complex64)
    return desired, cw


def check_energy(record, name, expected):
    value = record.get(name)
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
            f"invalid DSP {name}")
    require(math.isclose(value, expected, rel_tol=2e-5, abs_tol=1e-7),
            f"DSP {name} does not match independent peer/component energy")


def validate_dsp_records(log, backend, profile, expected, raw_paths):
    import numpy as np

    records = [json.loads(line[len(DSP_PREFIX):]) for line in log.splitlines()
               if line.startswith(DSP_PREFIX)]
    require(len(records) == 4, "expected exactly two started and two final DSP records")
    indexed = {(r.get("direction"), r.get("record_type")): r for r in records}
    require(set(indexed) == {(d, k) for d in ("DL", "UL") for k in ("started", "final")},
            "duplicate or missing DSP lifecycle record")
    measurements = {}
    for direction in ("DL", "UL"):
        config = profile["directions"][direction]
        started, final = (indexed[direction, kind] for kind in ("started", "final"))
        n = expected[direction]["samples"]
        require(n == SAMPLE_COUNT, "unexpected synthetic sample population")
        for row in (started, final):
            fields = {
                "schema_version": "radio_fixed_profile_v1", "backend": backend,
                "channel_semantics_version": "fixed_reference_v1",
                "rng_version": "component_streams_v1",
                "master_seed": profile["master_seed"], "sample_rate_hz": profile["sample_rate_hz"],
                "mode": "fixed", "ref_power": config["reference_power"],
                "gain": config["desired_gain"], "noise_enabled": config["noise"]["enabled"],
                "noise_snr_db": config["noise"]["snr_db"], "cw_enabled": config["cw"]["enabled"],
                "cw_sir_db": config["cw"]["sir_db"], "cw_freq_hz": config["cw"]["frequency_hz"],
                "units": "relative_digital_complex_power", "scope": "cumulative_processed_samples",
            }
            require(all(row.get(k) == v and (type(row.get(k)) is bool if type(v) is bool else True)
                        for k, v in fields.items()), f"{direction}: DSP settings differ from profile")
        counts = {"sample_clock": n, "awgn_complex_draws": n, "awgn_normal_draws": 2 * n,
                  "masked_samples": n if config["desired_gain"] == 0 else 0,
                  "attenuated_samples": n if 0 < config["desired_gain"] < 1 else 0}
        for name, total in counts.items():
            require(type(final.get(name)) is int and final[name] == total
                    and type(started.get(name)) is int and started[name] == 0,
                    f"{direction}: unreconciled DSP {name}")
        step = 0 if config["cw"]["frequency_hz"] == 0 else (1 << 64) - (1 << 60)
        for row, phase in ((started, 0), (final, n * step % (1 << 64))):
            require(type(row.get("cw_step_u64")) is int and row["cw_step_u64"] == step
                    and type(row.get("phase_u64")) is int and row["phase_u64"] == phase,
                    f"{direction}: CW phase/sample clock does not reconcile")
        require(type(final.get("awgn_seed")) is int and 0 <= final["awgn_seed"] < 2 ** 32
                and started.get("awgn_seed") == final["awgn_seed"], "unstable component seed")
        source_path, received_path = raw_paths[direction]["source"], raw_paths[direction]["received"]
        require(source_path.stat().st_size + received_path.stat().st_size
                <= MAX_RAW_BYTES_PER_DIRECTION, "combined raw capture budget exceeded")
        source = np.frombuffer(source_path.read_bytes(), dtype=np.complex64)
        received = np.frombuffer(received_path.read_bytes(), dtype=np.complex64)
        require(len(source) == len(received) == n and np.isfinite(received).all(),
                "raw sample population differs from peer/relay ledger")
        desired, cw = independent_components(source, config, profile["sample_rate_hz"])
        residual = received.astype(np.complex128) - desired - cw
        noise_power = config["reference_power"] * 10 ** (-config["noise"]["snr_db"] / 10)
        stats = {"source_power": energy(source) / n, "desired_power": energy(desired) / n,
                 "cw_power": energy(cw) / n, "residual_power": energy(residual) / n,
                 "receiver_power": energy(received) / n,
                 "processed_samples": final["sample_clock"],
                 "peer_received_samples": len(received)}
        if config["noise"]["enabled"]:
            mean = complex(np.mean(residual))
            lag = abs(np.vdot(residual[:-1], residual[1:])) / ((n - 1) * noise_power)
            require(abs(stats["residual_power"] / noise_power - 1) < NOISE_POWER_REL_TOL,
                    f"{direction}: independent AWGN power check failed")
            require(max(abs(mean.real), abs(mean.imag)) < NOISE_MEAN_STD_TOL * math.sqrt(noise_power),
                    f"{direction}: independent AWGN mean check failed")
            require(lag < NOISE_LAG1_TOL, f"{direction}: independent AWGN lag-one check failed")
            stats.update(expected_noise_power=noise_power, residual_mean_real=mean.real,
                         residual_mean_imag=mean.imag, normalized_lag1=float(lag))
        else:
            require(np.max(np.abs(residual), initial=0) < 2e-7,
                    f"{direction}: analytic gain/CW output check failed")
        for name, samples in (("input_energy", source), ("desired_energy", desired),
                              ("cw_energy", cw), ("output_energy", received)):
            check_energy(final, name, energy(samples))
            require(started.get(name) == 0, "nonzero initial DSP energy")
        check_energy(final, "noise_energy", energy(residual) if config["noise"]["enabled"] else 0)
        require(started.get("noise_energy") == 0, "nonzero initial noise energy")
        measurements[direction] = stats
    require(indexed["DL", "final"]["awgn_seed"] != indexed["UL", "final"]["awgn_seed"],
            "DL and UL share an AWGN component seed")
    relay = transport.validate_final_records(
        log, backend, expected, identity_by_direction={"DL": False, "UL": False})
    return {"relay_records": relay, "dsp_records": records, "independent_measurements": measurements,
            "accounting_scope": "DSP processed samples and relay forwarded samples independently match peers for these successful cases"}


def compare_cases(results, output, backend):
    checks = []
    pairs = [("awgn", "awgn-partition", d) for d in ("DL", "UL")]
    pairs += [("cw", "cw-partition", d) for d in ("DL", "UL")]
    pairs += [("awgn", changed, "UL") for changed in ("attenuation", "mask", "cw")]
    pairs += [("zeros", "mask", "DL")]
    for left, right, direction in pairs:
        a, b = results[left], results[right]
        same = a["received_sha256"][direction] == b["received_sha256"][direction]
        checks.append({"left": left, "right": right, "direction": direction,
                       "assertion": "same received stream bytes", "passed": same})
    transport.write_json(output / f"{backend}-comparisons.json", checks)
    require(all(check["passed"] for check in checks), "replay/directional independence comparison failed")
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local", action="store_true")
    parser.add_argument("--backend", choices=("c", "grc", "both"), default="both")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.run_local:
        parser.error("local broker execution requires --run-local")
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()) or output == ROOT / "artifacts":
        parser.error("--output must be a new directory beneath artifacts/")
    if output.exists():
        parser.error("--output already exists; failures and earlier attempts must be retained")
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    sources = ("scripts/zmq_channel_broker.c", "scripts/ocudu_channel_broker.py",
               "scripts/radio_schedule_native.h", "scripts/radio_schedule_sha256.h",
               "scripts/radio_schedule_runtime.py", "scripts/radio_broker_schedule.py",
               "scripts/radio_broker_metrics.py", "scripts/radio_metrics_native.h",
               "scripts/validate_radio_broker_identity.py", "scripts/validate_radio_broker_fixed.py",
               "scripts/radio_broker_profile.py", "config/radio_broker/fixed_reference.fixture.json",
               "dependencies/toolchain.lock.json", "pyproject.toml", "uv.lock")
    hashes = {name: transport.digest(ROOT / name) for name in sources}
    report = {"schema_version": "radio_broker_fixed_l2_v1", "status": "failed", "evidence_layer": "L2",
              "started_utc": datetime.now(timezone.utc).isoformat(), "source_sha256": hashes,
              "scope": "synthetic fixed gain/AWGN/CW, silence, replay and independent directional control",
              "campaign_attempts": 0, "full_stack_attempts": 0, "cases": [], "comparisons": {},
              "bounds": {"max_process_cases": MAX_PROCESS_CASES, "case_seconds": transport.MAX_CASE_SECONDS,
                         "raw_bytes_per_direction_per_case": MAX_RAW_BYTES_PER_DIRECTION},
              "oracles": {"noise_power_relative_tolerance": NOISE_POWER_REL_TOL,
                          "noise_mean_tolerance_in_noise_rms": NOISE_MEAN_STD_TOL,
                          "noise_lag1_tolerance": NOISE_LAG1_TOL,
                          "cw": "analytic exact-period complex exponential, -Fs/16",
                          "replay": "within-backend exact bytes; cross-backend random samples need not match"},
              "host": {"platform": platform.platform(), "python": sys.version, "executable": sys.executable}}
    try:
        import numpy
        import scipy
        import zmq

        report["host"].update(numpy=numpy.__version__, scipy=scipy.__version__,
                              pyzmq=zmq.__version__, libzmq=zmq.zmq_version())
        report["git_head"] = transport.git_head()
        base, _ = profile_adapter.load_profile(FIXTURE)
        backends = ("c", "grc") if args.backend == "both" else (args.backend,)
        require(len(backends) * len(CASES) <= MAX_PROCESS_CASES, "process-case budget exceeded")
        commands = transport.prepare_commands(output, backends, report)
        for backend in backends:
            results = {}
            for case in CASES:
                profile = case_profile(base, case)
                attempt = {"backend": backend, "case": case, "status": "attempting", "profile": profile}
                report["cases"].append(attempt)
                result = transport.run_case(
                    backend, commands[backend], case, output,
                    broker_arguments=profile_adapter.broker_arguments(profile, backend),
                    supplied_payloads=case_payloads(case), exchange_validator=validate_exchange,
                    final_validator=lambda log, name, expected, paths: validate_dsp_records(
                        log, name, profile, expected, paths), capture_raw=True)
                results[case] = result
                attempt.update(status=result["status"], result_sha256=transport.digest(
                    output / f"{backend}-{case}" / "result.json"))
                print(f"{backend}/{case}: {result['status']}", flush=True)
                require(result["status"] == "passed", result.get("error", "fixed-profile case failed"))
            report["comparisons"][backend] = compare_cases(results, output, backend)
        require(hashes == {name: transport.digest(ROOT / name) for name in sources},
                "source changed during fixed-profile validation")
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    transport.write_json(output / "validation.json", report)
    print(f"{report['status']}: {output / 'validation.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
