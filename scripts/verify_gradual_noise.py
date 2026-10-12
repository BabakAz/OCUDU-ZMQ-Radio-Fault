#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Verify the portable G0/G3 example; optionally preflight a C broker without sockets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import radio_broker_profile as profiles
import radio_broker_schedule as schedules

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/examples/gradual_noise"
FILES = ("profile.json", "G0.schedule.json", "G3.schedule.json")
ARMS = ("G0", "G3")
OFFSETS = (0, 92_160_000, 103_680_000, 115_200_000, 126_720_000)
LEVELS = (20, 10, 0)
REFERENCE = 10_000_000


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def check_condition(profile, programs):
    """Check the named condition independently of the manifest's file hashes."""
    require(profile["sample_rate_hz"] == 23_040_000 and profile["master_seed"] == 137,
            "example rate or seed differs")
    require(profile["reference_provenance"] == "gradual-noise-example-v1",
            "example reference method differs")
    for direction in ("DL", "UL"):
        config = profile["directions"][direction]
        require(config["mode"] == ("identity" if direction == "DL" else "fixed")
                and config["reference_power"] == (1 if direction == "DL" else REFERENCE)
                and config["desired_gain"] == 1 and config["noise"] == {"enabled": False, "snr_db": 0}
                and config["cw"] == {"enabled": False, "sir_db": 0, "frequency_hz": 0},
                "example base profile differs")
        for arm in ARMS:
            program = programs[arm]
            require(program["protocol_id"] == "gradual-noise-example-v1", "example protocol differs")
            plan = program["directions"][direction]
            require(plan["duration_samples"] == 230_400_000, "example duration differs")
            events = plan["events"]
            require(tuple(event["sample_offset"] for event in events) == OFFSETS,
                    "example sample boundaries differ")
            require(tuple(event["event_id"] for event in events)
                    == ("baseline", "level-1", "level-2", "level-3", "restore"),
                    "example matched markers differ")
            require(tuple(event["kind"] for event in events) == ("set", "set", "set", "set", "restore"),
                    "example explicit restoration differs")
            for index, event in enumerate(events):
                active = direction == "UL" and arm == "G3" and index in (1, 2, 3)
                expected = {"gain": 1, "noise_enabled": active,
                            "noise_snr_db": LEVELS[index - 1] if active else 0,
                            "cw_enabled": False, "cw_sir_db": 0, "cw_freq_hz": 0}
                require(event["settings"] == expected, "example directional settings differ")


def verify(directory=EXAMPLE, *, c_binary=None):
    directory = Path(directory)
    manifest = json.loads(schedules.read_regular(directory / "manifest.json", 65_536),
                          object_pairs_hook=profiles.no_duplicates, parse_constant=profiles.reject_nonfinite)
    require(manifest["schema_version"] == "radio_fault_gradual_noise_example_v1", "example manifest schema differs")
    require(set(manifest["files_sha256"]) == set(FILES), "example manifest file set differs")
    require(set(manifest["compiled_plan_sha256"]) == set(ARMS), "example manifest arm set differs")
    for name in FILES:
        require(sha256(schedules.read_regular(directory / name, 65_536)) == manifest["files_sha256"][name],
                f"example file hash differs: {name}")
    profile, _ = profiles.load_profile(directory / "profile.json")
    programs, plans = {}, {}
    for arm in ARMS:
        program, _ = schedules.load_schedule(directory / f"{arm}.schedule.json")
        programs[arm] = schedules.validate_schedule(profile, program)
        plans[arm] = schedules.parse_wire(schedules.compile_plan(profile, program))
        require(plans[arm].sha256 == manifest["compiled_plan_sha256"][arm], f"example compiled plan differs: {arm}")
    check_condition(profile, programs)
    require(manifest["condition"] == {"arm_names": ["G0", "G3"], "reference_power": REFERENCE,
            "reference_levels_db": list(LEVELS), "sample_rate_hz": 23_040_000, "master_seed": 137,
            "whole_UL": True, "DL_identity": True, "schedule_duration_samples": 230_400_000,
            "level_start_samples": list(OFFSETS[1:4]), "level_duration_samples": 11_520_000,
            "restore_sample": OFFSETS[-1]}, "example manifest condition differs")
    report = {"status": "verified", "scope": "configuration only; no radio or receiver result",
              "profile_sha256": plans["G0"].profile_sha256,
              "compiled_plan_sha256": {arm: plans[arm].sha256 for arm in ARMS},
              "reference_power": REFERENCE, "level_start_samples": list(OFFSETS[1:4]),
              "restore_sample": OFFSETS[-1], "noise_samples": {"G0": 0, "G3": 34_560_000},
              "G3_complex_noise_powers": [REFERENCE * 10 ** (-level / 10) for level in LEVELS],
              "c_preflight": "not_requested"}
    if c_binary is not None:
        binary = Path(c_binary).resolve(strict=True)
        validated = []
        # The short absolute path also respects Linux's UNIX-socket path bound.
        # --validate-config-only loads the complete plan but never opens sockets.
        with tempfile.TemporaryDirectory(prefix="rf-g-", dir="/tmp") as temporary:
            for arm in ARMS:
                control = Path(temporary) / arm
                control.mkdir(mode=0o700)
                prepared = schedules.prepare(directory / "profile.json", directory / f"{arm}.schedule.json", control)
                result = subprocess.run([str(binary), *prepared["broker_arguments"]["c"], "--validate-config-only"],
                                        capture_output=True, text=True, timeout=30)
                require(result.returncode == 0, f"C configuration preflight failed for {arm}: {result.stderr.strip()}")
                marker = "RADIO_CONFIG_VALIDATED: "
                rows = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
                require(len(rows) == 1, f"C configuration preflight report missing for {arm}")
                details = json.loads(rows[0])
                require(details["backend"] == "c" and details["master_seed"] == 137
                        and details["sample_rate_hz"] == 23_040_000
                        and details["directions"]["UL"]["ref_power"] == REFERENCE,
                        f"C configuration preflight report differs for {arm}")
                require({path.name for path in control.iterdir()} == {"plan.wire", "control.token"},
                        "C preflight unexpectedly created runtime outputs")
                validated.append(arm)
        report["c_preflight"] = {"status": "verified", "arms": validated, "binary_sha256": sha256(binary.read_bytes())}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=EXAMPLE, help="example configuration directory")
    parser.add_argument("--c-binary", type=Path, help="also load both plans in this C broker without opening sockets")
    args = parser.parse_args(argv)
    try:
        report = verify(args.directory, c_binary=args.c_binary)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
