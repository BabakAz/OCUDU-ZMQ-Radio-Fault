#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Finite radio fault recipes: freeze, arm, apply, restore and qualify.

A recipe fixes one directional sample-domain condition for the C broker or
the headless Python broker as a profile plus a finite sample schedule. This
module freezes a recipe into a private control directory, arms an already
running broker through its authenticated control socket, and reconciles the
broker's own truth records after it stops. It never starts a RAN component.

Broker sample coordinates and control-receipt clocks are exposure evidence.
They are not a mapping to NR slots, a calibrated receiver SNR, or a
measurement of service impact. The recipe definitions, their specification
schema and their compiled plan identities are unchanged from the study that
produced the paper (see PROVENANCE.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

import radio_broker_control as control
import radio_broker_profile as profiles
import radio_broker_schedule as schedules

ROOT = Path(__file__).resolve().parents[1]
# Recipe specification schema, unchanged from the study so archived
# specifications validate and compile to identical plans.
SCHEMA = "ocudu_observability_pilot_v1"
V3_TRIAL_SCHEMA = "ocudu_observability_study_trial_v3"
V3_ARM_KINDS = {"N0": "normal", "B1": "ul_blank_50ms", "B5": "ul_blank_disperse_5x10ms"}
PREPARATION_SCHEMA = "radio_fault_preparation_v1"
EXECUTION_SCHEMA = "radio_fault_execution_v1"
VERIFICATION_SCHEMA = "radio_fault_verification_v1"
ADDITIVE_KINDS = ("ul_awgn_500ms", "ul_cw_500ms")
GRC_CFO_KINDS = ("grc_normal", "ul_cfo_500ms")
GRC_ADDITIVE_KINDS = ("grc_ul_awgn_500ms", "grc_ul_cw_500ms")
GRC_FIXED_KINDS = ("grc_fixed_normal", *GRC_ADDITIVE_KINDS)
GRC_TDL_A_KINDS = ("grc_tdl_a_normal", "grc_ul_tdl_a_500ms")
GRC_TDL_C_KINDS = ("grc_tdl_c_normal", "grc_ul_tdl_c_500ms")
GRC_TDL_KINDS = (*GRC_TDL_A_KINDS, *GRC_TDL_C_KINDS)
ALL_ADDITIVE_KINDS = (*ADDITIVE_KINDS, *GRC_ADDITIVE_KINDS)
KINDS = ("normal", "c_fixed_normal", "ul_blank_50ms", "ul_blank_disperse_5x10ms", "ul_attenuation_500ms", *ADDITIVE_KINDS, *GRC_CFO_KINDS, *GRC_FIXED_KINDS, *GRC_TDL_KINDS)
TRAFFIC_BITRATES = ("5M", "2M")
FIXED_TRAFFIC_KINDS = ("c_fixed_normal", *ADDITIVE_KINDS, *GRC_FIXED_KINDS, *GRC_TDL_KINDS)
RATE = 23_040_000
DURATION = 10 * RATE
ONSET = 4 * RATE
PULSE = RATE // 20
MAX_WALL_SECONDS = 60
MAX_CONTROL_RECORDS = 64
GRC_SOURCE_FILES = ("ocudu_channel_broker.py", "radio_broker_metrics.py", "radio_broker_profile.py",
                    "radio_broker_schedule.py", "radio_schedule_runtime.py", "radio_static_tdl.py")

DESCRIPTIONS = {
    "normal": "No-fault control (C): DL identity, UL fixed at unit gain; V3 arm N0.",
    "c_fixed_normal": "Control for the C additive recipes: UL reference power 1e7, AWGN/CW disabled.",
    "ul_blank_50ms": "One 50 ms whole-UL blank (desired gain 0) 4 s after arm (C); V3 arm B1.",
    "ul_blank_disperse_5x10ms": "Five 10 ms whole-UL blanks 500 ms apart from 4 s after arm (C); V3 arm B5.",
    "ul_attenuation_500ms": "500 ms whole-UL amplitude gain 0.125 (-18.06 dB) 4 s after arm (C).",
    "ul_awgn_500ms": "500 ms UL AWGN 10 dB below the fixed digital reference power (C).",
    "ul_cw_500ms": "500 ms UL CW tone at +1.44 MHz, 10 dB below the fixed reference power (C).",
    "grc_normal": "Control for the CFO recipe (Python broker, grc_cfo_v1 with zero offset).",
    "ul_cfo_500ms": "500 ms +500 Hz UL carrier-frequency offset (Python broker).",
    "grc_fixed_normal": "Control for the Python-broker additive recipes: reference power 1e7.",
    "grc_ul_awgn_500ms": "500 ms UL AWGN 10 dB below the fixed reference power (Python broker).",
    "grc_ul_cw_500ms": "500 ms UL CW tone at +1.44 MHz, 10 dB below reference (Python broker).",
    "grc_tdl_a_normal": "Control for static TDL-A: common 15-sample UL delay, channel disabled.",
    "grc_ul_tdl_a_500ms": "500 ms static 3GPP TR 38.901 TDL-A (100 ns, zero Doppler) on UL.",
    "grc_tdl_c_normal": "Control for static TDL-C: common 15-sample UL delay, channel disabled.",
    "grc_ul_tdl_c_500ms": "500 ms static 3GPP TR 38.901 TDL-C (300 ns, zero Doppler) on UL.",
}


class FaultError(ValueError):
    """A recipe, preparation, control or evidence gate failed."""


def require(condition, reason):
    if not condition:
        raise FaultError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def read_bytes(path, maximum=1 << 20):
    """Bounded read of a regular file that must not change while it is read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= maximum,
                f"input must be a bounded regular file: {path}")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        def identity(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
        require(identity(before) == identity(after) == identity(Path(path).lstat())
                and len(raw) == before.st_size, f"input changed while reading: {path}")
    return raw


def strict_json(raw):
    """Parse JSON rejecting duplicate keys and nonfinite constants."""
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def constant(value):
        raise FaultError(f"nonfinite JSON value: {value}")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, FaultError):
            raise
        raise FaultError(f"invalid strict JSON: {exc}") from exc


def private_directory(path):
    path = Path(path).absolute()
    require(path == path.resolve(strict=True), "directory must be canonical without symlinks")
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "directory must be private and owned")
    return path


def read_private(path, maximum=1 << 20):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
            "input must be a private, unlinked owner file")
    raw = read_bytes(path, maximum)
    final = path.lstat()
    require((info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
             info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
            (final.st_dev, final.st_ino, final.st_mode, final.st_nlink, final.st_uid,
             final.st_size, final.st_mtime_ns, final.st_ctime_ns), "private input changed")
    return raw


def write_private(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        require(stream.write(raw) == len(raw), "short private write")
        stream.flush()
        os.fsync(stream.fileno())


def specification(kind, seed=41, *, traffic_bitrate="5M"):
    require(kind in KINDS, "unsupported fault recipe")
    require(type(seed) is int and 0 <= seed < 2**32, "recipe seed must be uint32")
    require(type(traffic_bitrate) is str and traffic_bitrate in TRAFFIC_BITRATES,
            "recipe traffic bitrate must be 5M or 2M")
    require(traffic_bitrate == "5M" or kind in FIXED_TRAFFIC_KINDS,
            "2M traffic is supported only by the fixed-reference and TDL recipes")
    result = {"schema_version": SCHEMA, "kind": kind, "master_seed": seed,
            "backend": "c", "affected_direction": "UL", "sample_rate_hz": RATE,
            "duration_samples": DURATION, "pulse_start_sample": ONSET,
            "pulse_duration_samples": PULSE, "max_arm_wall_seconds": MAX_WALL_SECONDS,
            "settle_seconds": 5, "ul_bitrate": traffic_bitrate, "dl_bitrate": traffic_bitrate,
            "whole_trial_qualified": False, "scientific_dataset_eligible": False}
    if kind == "ul_blank_disperse_5x10ms":
        result.update(pulse_duration_samples=RATE // 100, pulse_count=5,
                      pulse_spacing_samples=RATE // 2, pulse_gain=0.0)
    elif kind == "ul_attenuation_500ms":
        result.update(pulse_duration_samples=RATE // 2, pulse_count=1, pulse_gain=0.125)
    elif kind in ADDITIVE_KINDS or kind == "c_fixed_normal":
        # Rounded input-side mean of the retained normal 555dc161 UL prefix.
        # This fixed digital reference is not an occupied-resource or physical SNR.
        result.update(pulse_duration_samples=RATE // 2, pulse_count=1, pulse_gain=1.0,
                      reference_power=10_000_000, component_reference_db=10,
                      reference_provenance="normal-555dc161-ul-whole-prefix-mean-rounded-1e7")
        if kind in ADDITIVE_KINDS:
            result["additive_component"] = "awgn" if kind == "ul_awgn_500ms" else "cw"
        if kind == "ul_cw_500ms":
            result["cw_frequency_hz"] = 1_440_000
    elif kind in GRC_CFO_KINDS:
        result.update(backend="grc", pulse_duration_samples=RATE // 2,
                      pulse_count=1, pulse_gain=1.0,
                      cfo_hz=500 if kind == "ul_cfo_500ms" else 0)
    elif kind in GRC_FIXED_KINDS:
        result.update(backend="grc", pulse_duration_samples=RATE // 2,
                      pulse_count=1, pulse_gain=1.0, reference_power=10_000_000,
                      component_reference_db=10,
                      reference_provenance="normal-555dc161-ul-whole-prefix-mean-rounded-1e7")
        if kind in GRC_ADDITIVE_KINDS:
            result["additive_component"] = "awgn" if kind == "grc_ul_awgn_500ms" else "cw"
        if kind == "grc_ul_cw_500ms":
            result["cw_frequency_hz"] = 1_440_000
    elif kind in GRC_TDL_KINDS:
        result.update(backend="grc", pulse_duration_samples=RATE // 2, pulse_count=1,
                      pulse_gain=1.0, tdl_profile="tdl-c" if kind in GRC_TDL_C_KINDS else "tdl-a",
                      delay_spread_ns=300 if kind in GRC_TDL_C_KINDS else 100,
                      max_doppler_hz=0, occupied_bandwidth_hz=19_080_000,
                      common_ul_delay_samples=15,
                      tdl_enabled=kind in ("grc_ul_tdl_a_500ms", "grc_ul_tdl_c_500ms"))
    return result


def v3_trial_specification(arm, seed=41):
    """One arm of the V3 repeated comparison: the recipe plus its 2 Mbit/s traffic metadata."""
    require(type(arm) is str and arm in V3_ARM_KINDS, "unsupported V3 study arm")
    require(type(seed) is int and 0 <= seed < 2**32, "study seed must be uint32")
    result = specification(V3_ARM_KINDS[arm], seed)
    result.update(schema_version=V3_TRIAL_SCHEMA, study_arm=arm, ul_bitrate="2M", dl_bitrate="2M")
    return result


def recipe_specification(name, seed=41, *, traffic_bitrate=None):
    """Resolve a recipe kind or a V3 arm name (N0, B1, B5) to its exact specification."""
    if name in V3_ARM_KINDS:
        require(traffic_bitrate in (None, "2M"), "V3 arms fix their traffic metadata at 2M")
        return v3_trial_specification(name, seed)
    return specification(name, seed, traffic_bitrate=traffic_bitrate or "5M")


def validate_spec(value):
    require(type(value) is dict, "recipe specification must be an object")
    if value.get("schema_version") == V3_TRIAL_SCHEMA:
        expected = v3_trial_specification(value.get("study_arm"), value.get("master_seed"))
    else:
        expected = specification(value.get("kind"), value.get("master_seed"),
                                 traffic_bitrate=value.get("ul_bitrate"))
    require(canonical(value) == canonical(expected), "recipe specification changed fixed scope or bounds")
    return expected


def radio_profile(spec):
    spec = validate_spec(spec)
    additive = spec["kind"] in ALL_ADDITIVE_KINDS or spec["kind"] in ("c_fixed_normal", "grc_fixed_normal")
    value = {
        "schema_version": "radio_broker_profile_v1", "channel_semantics_version": "fixed_reference_v1",
        "rng_version": "component_streams_v1", "qualification": "development_only",
        "sample_rate_hz": RATE, "master_seed": spec["master_seed"],
        "reference_provenance": spec.get("reference_provenance", "unused-unit-reference-no-additive-components"),
        "directions": {direction: {"mode": "identity" if direction == "DL" else "fixed",
            "reference_power": spec["reference_power"] if additive and direction == "UL" else 1,
            "desired_gain": 1,
            "noise": {"enabled": False, "snr_db": spec["component_reference_db"] if additive and direction == "UL" else 0},
            "cw": {"enabled": False, "sir_db": spec["component_reference_db"] if additive and direction == "UL" else 0,
                   "frequency_hz": 0}}
            for direction in ("DL", "UL")}}
    if spec["kind"] in GRC_CFO_KINDS:
        value.update(schema_version="radio_broker_profile_grc_cfo_v1",
                     channel_semantics_version="grc_cfo_v1")
        for direction in value["directions"].values():
            direction["cfo_hz"] = 0
    if spec["kind"] in GRC_TDL_KINDS:
        tdl_c = spec["kind"] in GRC_TDL_C_KINDS
        value.update(schema_version=profiles.TDL_C_SCHEMA if tdl_c else profiles.TDL_SCHEMA,
                     channel_semantics_version=profiles.TDL_C_SEMANTICS if tdl_c else profiles.TDL_SEMANTICS)
        for direction in value["directions"].values():
            direction["tdl_enabled"] = False
    return profiles.validate_profile(value)


def canonical_uuid(value):
    require(type(value) is str and str(uuid.UUID(value)) == value, "identity must be a canonical UUID")
    return value


def pulse_intervals(spec):
    """Exact selected sample support; no conversion to host time or radio slots."""
    spec = validate_spec(spec)
    gain = spec.get("pulse_gain", 0.0 if spec["kind"] == "ul_blank_50ms" else 1.0)
    return tuple((spec["pulse_start_sample"] + index * spec.get("pulse_spacing_samples", 0),
                  spec["pulse_start_sample"] + index * spec.get("pulse_spacing_samples", 0)
                  + spec["pulse_duration_samples"], gain)
                 for index in range(spec.get("pulse_count", 1)))


def schedule(spec, *, run_id, trial_id, pipeline_id):
    spec = validate_spec(spec)
    run_id, trial_id, pipeline_id = map(canonical_uuid, (run_id, trial_id, pipeline_id))
    profile = radio_profile(spec)
    result = {"schema_version": (schedules.TDL_C_SCHEMA if spec["kind"] in GRC_TDL_C_KINDS else
                                  schedules.TDL_SCHEMA if spec["kind"] in GRC_TDL_A_KINDS else
                                  "radio_broker_schedule_grc_cfo_v1" if spec["kind"] in GRC_CFO_KINDS
                                  else schedules.SCHEMA), "qualification": "development_only",
              "study_id": run_id, "protocol_id": ("observability-study-v3"
                  if spec["schema_version"] == "ocudu_observability_study_trial_v3"
                  else "observability-pilot-v1"),
              "trial_id": trial_id, "pipeline_id": pipeline_id,
              "profile_sha256": digest(profiles.canonical_bytes(profile)), "directions": {}}
    for direction in ("DL", "UL"):
        base = schedules.mutable_settings(schedules.profile_base(profile["directions"][direction]))
        intervals = pulse_intervals(spec)
        events = [{"sample_offset": 0, "event_id": "baseline", "kind": "set", "settings": base}]
        for index, (start, end, gain) in enumerate(intervals, 1):
            prefix = "pulse" if len(intervals) == 1 else f"pulse-{index}"
            pulse = dict(base, gain=gain if direction == "UL" else 1.0)
            if direction == "UL" and spec["kind"] in ALL_ADDITIVE_KINDS:
                if spec["additive_component"] == "awgn":
                    pulse["noise_enabled"] = True
                else:
                    pulse.update(cw_enabled=True, cw_freq_hz=spec["cw_frequency_hz"])
            if direction == "UL" and spec["kind"] in GRC_CFO_KINDS:
                pulse["cfo_hz"] = spec["cfo_hz"]
            if direction == "UL" and spec["kind"] in GRC_TDL_KINDS:
                pulse["tdl_enabled"] = spec["tdl_enabled"]
            events.extend([
                {"sample_offset": start, "event_id": prefix + "-start", "kind": "set", "settings": pulse},
                {"sample_offset": end, "event_id": prefix + "-end", "kind": "restore", "settings": base},
            ])
        result["directions"][direction] = {"duration_samples": DURATION, "events": events}
    return schedules.validate_schedule(profile, result)


def expected_broker_arguments(control_directory, profile, backend=None):
    directory = Path(control_directory)
    if backend is None:
        # Preserve existing C/CFO callers; fixed-reference profiles themselves
        # do not select an executable, so new GRC callers supply the backend.
        backend = "grc" if profile.get("channel_semantics_version") in (profiles.CFO_SEMANTICS, profiles.TDL_SEMANTICS, profiles.TDL_C_SEMANTICS) else "c"
    require(backend in ("c", "grc"), "unsupported broker backend")
    return profiles.broker_arguments(profile, backend) + ["--radio-plan-file", str(directory / "plan.wire"),
                                                     "--radio-control-dir", str(directory)]


def _grc_composite(files):
    require(type(files) is dict and set(files) == {str(ROOT / "scripts" / name) for name in GRC_SOURCE_FILES},
            "GRC source file inventory differs")
    require(all(type(value) is str and control.HEX64.fullmatch(value) for value in files.values()),
            "GRC source digest is invalid")
    return digest("".join(name + " " + files[str(ROOT / "scripts" / name)] + "\n"
                          for name in GRC_SOURCE_FILES).encode())


def expected_grc_source_identity():
    """Bind the Python broker implementation separately from its interpreter."""
    from radio_schedule_runtime import source_build_sha256
    files = {str(ROOT / "scripts" / name): digest(read_bytes(ROOT / "scripts" / name))
             for name in GRC_SOURCE_FILES}
    build = _grc_composite(files)
    require(build == source_build_sha256(), "GRC source inventory changed during binding")
    return {"build_sha256": build, "files": files}


def prepare(spec, directory, *, study_id, trial_id, pipeline_id, gnb_config=None, ue_config=None):
    """Freeze one recipe into a new private control directory; never starts a broker.

    The directory receives the compiled plan and a fresh control token (both
    mode 0600) plus the canonical recipe, profile, schedule and a preparation
    record binding them to the trial identities and the exact broker argv.
    """
    spec = validate_spec(spec)
    study_id, trial_id, pipeline_id = map(canonical_uuid, (study_id, trial_id, pipeline_id))
    profile = radio_profile(spec)
    program = schedule(spec, run_id=study_id, trial_id=trial_id, pipeline_id=pipeline_id)
    directory = Path(directory).absolute()
    require(len(os.fsencode(str(directory / "rb.sock"))) <= 107,
            "control socket path exceeds the Linux limit; choose a shorter directory")
    radio_configs = None
    if gnb_config is not None or ue_config is not None:
        require(gnb_config is not None and ue_config is not None,
                "gNB and UE configurations must be supplied together")
        profiles.validate_radio_rates(profile, gnb_config, ue_config)
        radio_configs = {"gnb_config_sha256": digest(read_bytes(gnb_config)),
                         "ue_config_sha256": digest(read_bytes(ue_config))}
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    # schedules.prepare requires an empty control directory, so its checked
    # inputs are staged in a separate private temporary directory.
    with tempfile.TemporaryDirectory(prefix="radio-fault-") as staging:
        profile_path, schedule_path = Path(staging) / "profile.json", Path(staging) / "schedule.json"
        write_private(profile_path, profiles.canonical_bytes(profile))
        write_private(schedule_path, canonical(program))
        schedules.prepare(profile_path, schedule_path, directory)
    write_private(directory / "recipe.json", canonical(spec))
    write_private(directory / "profile.json", profiles.canonical_bytes(profile))
    write_private(directory / "schedule.json", canonical(program))
    result = {"schema_version": PREPARATION_SCHEMA, "study_id": study_id, "trial_id": trial_id,
              "pipeline_id": pipeline_id, "control_directory": str(directory),
              "backend": spec["backend"], "specification": spec,
              "plan_sha256": digest(read_private(directory / "plan.wire", schedules.MAX_PLAN_BYTES)),
              "config_sha256": program["profile_sha256"],
              "helper_sha256": digest(read_bytes(Path(__file__))),
              "radio_configs": radio_configs,
              "argv": expected_broker_arguments(directory, profile, spec["backend"])}
    if spec["backend"] == "grc":
        result["broker_source_identity"] = expected_grc_source_identity()
    if spec["kind"] in GRC_TDL_KINDS:
        result["tdl_realizations"] = tdl_realizations(profile)
    write_private(directory / "preparation.json", canonical(result))
    return result


def _frontiers(previous, current):
    """A STATUS prefix cannot regress, unarm, or change an observed arm epoch."""
    for direction in ("DL", "UL"):
        old, new = previous["directions"][direction], current["directions"][direction]
        for key in ("processed_samples", "forwarded_samples", "next_event"):
            require(new[key] >= old[key], "broker control frontier regressed")
        if old["armed"]:
            require(new["armed"] and new["arm_sample"] == old["arm_sample"], "broker arm epoch changed")
        require(not old["schedule_complete"] or new["schedule_complete"], "broker completion regressed")


def run_schedule(control_directory, *, expected_pid, expected_build_sha256,
                 progress_callback=None, record_callback=None, identity_sources=None):
    """STATUS, one ARM, then STATUS until both directions complete or the wall bound expires.

    Every receipt is appended durably to control.jsonl before it is checked;
    execution.json is written on success and failure alike.
    """
    directory = private_directory(control_directory)
    preparation = strict_json(read_private(directory / "preparation.json"))
    spec = validate_spec(preparation["specification"])
    require(preparation["control_directory"] == str(directory), "control directory differs from preparation")
    if spec["kind"] in GRC_TDL_KINDS:
        require(canonical(preparation.get("tdl_realizations")) == canonical(tdl_realizations(radio_profile(spec))),
                "prepared TDL realization differs before arm")
    if spec["backend"] == "grc":
        identity = preparation.get("broker_source_identity")
        require(type(identity) is dict and set(identity) == {"build_sha256", "files"}
                and identity["build_sha256"] == expected_build_sha256
                and _grc_composite(identity["files"]) == expected_build_sha256,
                "prepared GRC implementation binding differs before arm")
    progress_callback = progress_callback or (lambda: None)
    record_callback = record_callback or (lambda _row: None)
    result = {"schema_version": EXECUTION_SCHEMA, "qualified": False,
              "plan_sha256": preparation["plan_sha256"], "expected_pid": expected_pid,
              "expected_build_sha256": expected_build_sha256,
              "identity_sources": identity_sources or {"pid": "caller", "build_sha256": "caller"},
              "receipts": [], "errors": []}
    fd = os.open(directory / "control.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream, control.ControlClient(
                directory, expected_backend=spec["backend"], expected_pid=expected_pid,
                expected_build_sha256=expected_build_sha256) as client:
            require(client.plan.sha256 == preparation["plan_sha256"], "prepared plan changed before arm")
            deadline = None
            previous = None
            for index in range(MAX_CONTROL_RECORDS):
                progress_callback()
                operation = "ARM" if index == 1 else "STATUS"
                if index == 1:
                    deadline = time.monotonic_ns() + spec["max_arm_wall_seconds"] * 1_000_000_000
                if deadline is not None:
                    remaining = (deadline - time.monotonic_ns()) / 1e9
                    require(remaining > 0, "arm wall-time limit reached")
                    client.timeout = min(2.0, remaining)
                started = time.monotonic_ns()
                response = client.request(operation)
                finished = time.monotonic_ns()
                row = {"request_sequence": client.sequence - 1 if response["ok"] else response["request_sequence"],
                       "operation": operation, "started_monotonic_ns": started,
                       "finished_monotonic_ns": finished, "response": response}
                raw = canonical(row)
                require(stream.write(raw) == len(raw), "short control receipt write")
                stream.flush()
                os.fsync(stream.fileno())
                result["receipts"].append(row)
                record_callback(row)
                require(response["ok"], "broker rejected a control request")
                if previous is not None:
                    _frontiers(previous, response)
                if index == 0:
                    require(response["state"] == "ready" and not any(
                        d["armed"] for d in response["directions"].values()), "broker was already armed")
                if deadline is not None:
                    require(finished <= deadline, "arm wall-time limit reached")
                if index >= 2 and response["state"] == "completed":
                    result.update(qualified=True, directions=response["directions"])
                    break
                previous = response
                if index >= 1:
                    time.sleep(min(1.0, max(0, (deadline - time.monotonic_ns()) / 1e9)))
            require(result["qualified"], "control request count limit reached")
    except BaseException as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_private(directory / "execution.json", canonical(result))
    return result


def validate_control(records, execution, plan, ready):
    require(3 <= len(records) <= MAX_CONTROL_RECORDS, "missing or excessive control receipts")
    require(execution.get("schema_version") == EXECUTION_SCHEMA
            and execution.get("qualified") is True and execution.get("errors") == []
            and execution.get("receipts") == records and execution.get("plan_sha256") == plan.sha256
            and execution.get("expected_pid") == ready["pid"]
            and execution.get("expected_build_sha256") == ready["build_sha256"], "control execution failed or differs")
    previous, last_finish = None, 0
    for index, row in enumerate(records, 1):
        require(set(row) == {"request_sequence", "operation", "started_monotonic_ns",
                             "finished_monotonic_ns", "response"}, "control receipt fields differ")
        operation = "ARM" if index == 2 else "STATUS"
        require(type(row["request_sequence"]) is int and row["request_sequence"] == index
                and row["operation"] == operation, "control request ordering differs")
        start, end = row["started_monotonic_ns"], row["finished_monotonic_ns"]
        require(control.uint(start) and control.uint(end) and last_finish <= start <= end,
                "control receipt clock regressed")
        response = control.validate_reply(canonical(row["response"]), sequence=index,
                                          operation=operation, ready=ready, plan=plan)
        require(response["ok"], "control receipt records an error")
        if previous is None:
            require(response["state"] == "ready", "initial broker state was not ready")
        else:
            _frontiers(previous, response)
        previous, last_finish = response, end
    require(previous["state"] == "completed" and execution.get("directions") == previous["directions"],
            "control did not observe both forwarded completions")
    require(records[-1]["finished_monotonic_ns"] - records[1]["started_monotonic_ns"]
            <= MAX_WALL_SECONDS * 1_000_000_000, "arm exceeded its wall-time bound")
    return previous["directions"]


def grc_fixed_profiles(raw):
    """Select only typed DSP records from the retained broker stdout."""
    result = {direction: {} for direction in ("DL", "UL")}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.startswith("RADIO_FIXED_PROFILE:"):
            continue
        require(line.startswith("RADIO_FIXED_PROFILE: "), "malformed GRC fixed profile marker")
        try:
            value = control.decode_json(line.removeprefix("RADIO_FIXED_PROFILE: ").encode())
        except control.ControlError as exc:
            raise FaultError("malformed GRC fixed profile JSON") from exc
        direction, kind = value.get("direction"), value.get("record_type")
        require(direction in result and kind in ("started", "final")
                and kind not in result[direction], "duplicate/unknown GRC fixed profile record")
        result[direction][kind] = value
    require(all(set(records) == {"started", "final"} for records in result.values()),
            "missing GRC fixed profile start/final")
    return result


def _validate_grc_fixed_state(state, *, plan, program, direction, clock, stage, arm=False):
    """Check the actual PCG64 record contract, without replaying its samples."""
    from ocudu_channel_broker import FixedReferenceChannel
    template = FixedReferenceChannel(direction, plan.sample_rate_hz, plan.master_seed,
                                     **dict(program.base)).record(stage)
    dynamic = {"awgn_state_hex", "awgn_has_uint32", "awgn_cached_uint32", "sample_clock",
               "awgn_complex_draws", "awgn_normal_draws", "input_energy", "desired_energy",
               "noise_energy", "cw_energy", "output_energy"}
    require(type(state) is dict and set(state) == set(template), "GRC fixed DSP state fields differ")
    require(all(state[key] == value and type(state[key]) is type(value)
                for key, value in template.items() if key not in dynamic),
            "GRC fixed profile/RNG/phase identity differs")
    draws = clock if program.base["mode"] == "fixed" else 0
    require(all(control.uint(state.get(key)) and state[key] == value for key, value in (
        ("sample_clock", clock), ("awgn_complex_draws", draws), ("awgn_normal_draws", 2 * draws))),
        "GRC fixed sample/draw accounting differs")
    require(type(state["awgn_state_hex"]) is str and len(state["awgn_state_hex"]) == 32
            and all(char in "0123456789abcdef" for char in state["awgn_state_hex"])
            and type(state["awgn_has_uint32"]) is bool
            and control.uint(state["awgn_cached_uint32"], (1 << 32) - 1),
            "invalid GRC PCG64 state")
    if not draws:
        require(all(state[key] == template[key] for key in
                    ("awgn_state_hex", "awgn_has_uint32", "awgn_cached_uint32")),
                "GRC identity RNG state advanced")
    for field in ("input_energy", "desired_energy", "noise_energy", "cw_energy", "output_energy"):
        require(type(state[field]) is float and math.isfinite(state[field]) and state[field] >= 0,
                "invalid GRC fixed cumulative energy")
        if clock == 0:
            require(state[field] == 0, "nonzero GRC startup energy before samples")
    require(state["desired_energy"] == state["input_energy"], "GRC unity-gain desired energy differs")
    noise = any(event.settings["noise_enabled"] for event in program.events) and not arm
    cw = any(event.settings["cw_enabled"] for event in program.events) and not arm
    require((state["noise_energy"] > 0 if noise else state["noise_energy"] == 0)
            and (state["cw_energy"] > 0 if cw else state["cw_energy"] == 0),
            "GRC component energy contradicts selected support")
    if not noise and not cw:
        require(state["output_energy"] == state["input_energy"], "GRC no-addition output energy differs")
    if cw:
        expected = sum((program.events[index + 1].sample_offset if index + 1 < len(program.events)
                        else program.duration_samples) - event.sample_offset
                       for index, event in enumerate(program.events) if event.settings["cw_enabled"])
        expected *= program.base["ref_power"] * 10 ** (-program.base["cw_sir_db"] / 10)
        require(math.isclose(state["cw_energy"], expected, rel_tol=1e-6, abs_tol=0),
                "GRC reported CW energy differs from selected cf32 tone support")


def tdl_channel_type(semantics):
    from radio_static_tdl import TdlReferenceChannel, TdlCReferenceChannel
    require(semantics in (profiles.TDL_SEMANTICS, profiles.TDL_C_SEMANTICS), "unsupported TDL semantics")
    return TdlCReferenceChannel if semantics == profiles.TDL_C_SEMANTICS else TdlReferenceChannel


def tdl_realizations(profile):
    channel_type = tdl_channel_type(profile["channel_semantics_version"])
    return {direction: channel_type(direction, profile["sample_rate_hz"], profile["master_seed"],
                **schedules.profile_base(profile["directions"][direction])).realization()
            for direction in ("DL", "UL")}


def _validate_tdl_state(state, *, plan, program, direction, clock, stage, exposure, realization):
    from radio_static_tdl import canonical as tdl_canonical
    channel = tdl_channel_type(plan.channel_semantics_version)(direction, plan.sample_rate_hz, plan.master_seed, **dict(program.base))
    require(canonical(realization) == canonical(channel.realization()), "TDL realization differs")
    template = channel.record(stage)
    template.update(sample_clock=clock, tdl_applied_samples=exposure)
    require(type(state) is dict and set(state) == set(template), "TDL state fields differ")
    require(all(type(state[key]) is type(value) and state[key] == value
                for key, value in template.items() if key != "history_sha256"),
            "TDL state/realization/RNG/exposure differs")
    require(type(state["history_sha256"]) is str and control.HEX64.fullmatch(state["history_sha256"]),
            "TDL history digest is invalid")
    if not clock or direction == "DL":
        require(state["history_sha256"] == template["history_sha256"], "TDL empty history differs")
    require(state["realization_sha256"] == digest(tdl_canonical(realization)), "TDL realization digest differs")


def validate_truth(records, *, plan, ready, receipts, completed, fixed_profiles=None, realizations=None):
    """Validate live broker self-accounting, without inventing a waveform oracle."""
    cfo = getattr(plan, "channel_semantics_version", "fixed_reference_v1") == "grc_cfo_v1"
    tdl = getattr(plan, "channel_semantics_version", "fixed_reference_v1") in (profiles.TDL_SEMANTICS, profiles.TDL_C_SEMANTICS)
    grc = ready.get("backend") == "grc"
    require(ready.get("backend") in ("c", "grc") and (not (cfo or tdl) or grc), "truth backend/semantics differ")
    if tdl:
        require(type(realizations) is dict and set(realizations) == {"DL", "UL"}, "TDL realizations required")
    if grc and not (cfo or tdl):
        require(type(fixed_profiles) is dict and set(fixed_profiles) == {"DL", "UL"},
                "GRC fixed DSP start/final evidence is required")
    fields = {"schema_version", *schedules.ID_FIELDS, "instance_id", "backend", "build_sha256",
              "config_sha256", "plan_sha256", "direction", "event_sequence", "event_type",
              "monotonic_ns", "wall_ns", "sample_start", "sample_end", "scope", "details"}
    require(1 <= len(records) <= 256, "truth record bound exceeded")
    allowed = {"started", "ready", "arm_requested", "armed", "condition_applied",
               "condition_restored", "progress", "final"}
    for sequence, row in enumerate(records, 1):
        require(set(row) == fields and row["schema_version"] == "radio_broker_truth_v1"
                and type(row["event_sequence"]) is int and row["event_sequence"] == sequence,
                "truth schema/sequence differs")
        require(all(row[key] == getattr(plan, key) for key in schedules.ID_FIELDS), "truth trial identity differs")
        require(all(row[key] == ready[key] for key in
                    ("instance_id", "backend", "build_sha256", "config_sha256", "plan_sha256")),
                "truth runtime identity differs")
        require(row["event_type"] in allowed and type(row["details"]) is dict, "truth reports an error or unknown type")
        require(all(control.uint(row[key]) and row[key] > 0 for key in ("monotonic_ns", "wall_ns")),
                "invalid truth clock")
        if row["direction"] == "control":
            require(row["event_type"] in {"started", "ready", "arm_requested"}
                    and row["scope"] == "control" and row["sample_start"] is None
                    and row["sample_end"] is None, "invalid control truth scope")
        else:
            require(row["direction"] in ("DL", "UL")
                    and row["event_type"] in allowed - {"started", "ready", "arm_requested"}
                    and row["scope"] == ("successfully_forwarded_samples" if row["event_type"] == "progress"
                                         else "processed_samples")
                    and control.uint(row["sample_start"]) and control.uint(row["sample_end"])
                    and row["sample_start"] <= row["sample_end"], "invalid directional truth scope")
    lifecycle = {}
    for kind in ("started", "ready", "arm_requested"):
        own = [r for r in records if r["event_type"] == kind]
        require(len(own) == 1 and own[0]["direction"] == "control", "missing/duplicate broker lifecycle")
        lifecycle[kind] = own[0]
    require(lifecycle["started"]["event_sequence"] < lifecycle["ready"]["event_sequence"]
            < lifecycle["arm_requested"]["event_sequence"], "broker lifecycle order differs")
    require(lifecycle["started"]["monotonic_ns"] <= lifecycle["ready"]["monotonic_ns"]
            <= lifecycle["arm_requested"]["monotonic_ns"], "broker lifecycle clock regressed")
    requested = lifecycle["arm_requested"]
    arm_receipt = receipts[1]
    require(arm_receipt["started_monotonic_ns"] <= requested["monotonic_ns"]
            <= arm_receipt["finished_monotonic_ns"], "broker arm timestamp outside control receipt")
    require(requested["details"] == {"request_sequence": 2, "request_wall_ns": requested["wall_ns"],
                                      "request_monotonic_ns": requested["monotonic_ns"]},
            "broker arm request identity differs")
    finals = [r for r in records if r["event_type"] == "final"]
    require(len(finals) == 2 and {r["direction"] for r in finals} == {"DL", "UL"}
            and records[-2:] == finals, "missing/duplicate terminal broker finals")
    results = {}
    for direction in ("DL", "UL"):
        program = plan.directions[direction]
        own = [r for r in records if r["direction"] == direction]
        require(all(left["monotonic_ns"] <= right["monotonic_ns"]
                    for left, right in zip(own, own[1:])), "directional truth clock regressed")
        arms = [r for r in own if r["event_type"] == "armed"]
        require(len(arms) == 1, "missing/duplicate directional arm")
        armed = arms[0]
        arm = completed[direction]["arm_sample"]
        detail = armed["details"]
        require(armed["sample_start"] == arm == armed["sample_end"]
                and detail.get("arm_sample") == arm and detail.get("duration_samples") == program.duration_samples,
                "directional arm epoch differs")
        require(armed["event_sequence"] > requested["event_sequence"]
                and armed["monotonic_ns"] >= requested["monotonic_ns"]
                and all(detail.get(k) == v for k, v in requested["details"].items()), "directional arm request differs")
        state = detail.get("state_at_arm", {})
        require(type(state) is dict and control.uint(state.get("sample_clock"))
                and state["sample_clock"] == arm, "broker warmup epoch absent")
        setting_keys = tuple(schedules.mutable_settings(program.base))
        if tdl:
            _validate_tdl_state(state, plan=plan, program=program, direction=direction,
                                clock=arm, stage="started", exposure=0, realization=realizations[direction])
        else:
            invariants = {"mode": program.base["mode"], "ref_power": program.base["ref_power"],
                          "sample_rate_hz": plan.sample_rate_hz,
                          "rng_version": "component_streams_v1",
                          "rng_algorithm": ("none_cfo_only" if cfo else "numpy.PCG64+standard_normal_float32"
                                            if grc else "glibc_rand_r_box_muller_pair_f32"),
                          "master_seed": plan.master_seed, "phase_u64": 0, "cw_step_u64": 0,
                          "masked_samples": 0, "attenuated_samples": 0}
            if cfo:
                invariants.update(schema_version="radio_grc_cfo_profile_v1", record_type="started",
                                  backend="grc", direction=direction,
                                  channel_semantics_version="grc_cfo_v1", cfo_phase_rad=0, cfo_applied_samples=0,
                                  **schedules.mutable_settings(program.base))
            elif grc:
                invariants.update(**schedules.mutable_settings(program.base))
                _validate_grc_fixed_state(state, plan=plan, program=program, direction=direction,
                                          clock=arm, stage="started", arm=True)
            else:
                invariants["settings"] = schedules.mutable_settings(program.base)
            require(all(state.get(k) == v and
                        (type(state.get(k)) is bool if type(v) is bool else type(state.get(k)) is not bool)
                        for k, v in invariants.items()),
                    "broker arm configuration differs from the fixed recipe plan")
            if cfo:
                require(control.uint(state.get("cfo_applied_samples")), "invalid CFO arm exposure counter")
            # CFO-only has no additive DSP path; its disabled settings and zero
            # cumulative additive energies are checked explicitly instead.
            for field, expected in (() if grc else (
                ("noise_std", math.sqrt(program.base["ref_power"] * 10 ** (-program.base["noise_snr_db"] / 10) / 2)),
                ("cw_amplitude", math.sqrt(program.base["ref_power"] * 10 ** (-program.base["cw_sir_db"] / 10))),
            )):
                require(type(state.get(field)) in (int, float)
                        and math.isclose(state[field], expected, rel_tol=1e-15, abs_tol=0),
                        "broker arm additive coefficient differs: " + field)
            setting_keys = tuple(schedules.mutable_settings(program.base))
            arm_settings = state if grc else state["settings"]
            require(type(arm_settings) is dict and all(
                type(arm_settings.get(key)) is bool if key.endswith("enabled")
                else type(arm_settings.get(key)) in (int, float) for key in setting_keys),
                "broker arm setting types differ")
            expected_draws = arm if not cfo and program.base["mode"] == "fixed" else 0
            require(control.uint(state.get("awgn_complex_draws")) and control.uint(state.get("awgn_normal_draws"))
                    and state["awgn_complex_draws"] == expected_draws
                    and state["awgn_normal_draws"] == 2 * expected_draws, "broker warmup RNG accounting differs")
        observed = [r for r in own if r["event_type"] in ("condition_applied", "condition_restored")]
        require(len(observed) == len(program.events), "missing/duplicate condition transition")
        previous = schedules.mutable_settings(program.base)
        for index, (row, event) in enumerate(zip(observed, program.events)):
            d = row["details"]
            begin = arm + event.sample_offset
            require(row["event_type"] == ("condition_restored" if event.kind == "restore" else "condition_applied")
                    and row["sample_start"] == begin < row["sample_end"]
                    and row["event_sequence"] > armed["event_sequence"]
                    and row["monotonic_ns"] >= armed["monotonic_ns"], "condition boundary differs")
            if index + 1 < len(program.events):
                require(row["sample_end"] <= arm + program.events[index + 1].sample_offset,
                        "condition segment crosses next transition")
            require(d.get("event_id") == event.event_id and d.get("kind") == event.kind
                    and type(d.get("sample_offset")) is int and d["sample_offset"] == event.sample_offset
                    and d.get("settings") == dict(event.settings)
                    and d.get("changed") is (previous != dict(event.settings))
                    and type(d.get("processed_samples")) is int and d["processed_samples"] == row["sample_end"],
                    "condition settings/frontier differ")
            require(all(type(d["settings"][key]) is bool if key.endswith("enabled")
                        else type(d["settings"][key]) in (int, float) for key in setting_keys),
                    "condition setting types differ")
            previous = dict(event.settings)
        progress = [r for r in own if r["event_type"] == "progress"]
        require(1 <= len(progress) <= len(program.events) + 1, "missing/excessive forwarded progress")
        frontier, messages = arm, 0
        for row in progress:
            d = row["details"]
            require(row["sample_start"] == arm and row["sample_end"] > frontier
                    and d.get("output_samples") == row["sample_end"], "forwarded progress regressed")
            require(set(d) == {"input_messages", "output_messages", "input_samples", "output_samples",
                               "scheduled_end_sample", "schedule_complete"}
                    and all(control.uint(d.get(k)) for k in ("input_messages", "output_messages", "input_samples",
                                                             "output_samples", "scheduled_end_sample")),
                    "invalid progress counters")
            require(d["input_messages"] == d["output_messages"] and d["input_samples"] == d["output_samples"]
                    and d["output_messages"] > messages
                    and d["scheduled_end_sample"] == arm + program.duration_samples
                    and type(d.get("schedule_complete")) is bool,
                    "forwarded accounting differs")
            preceding = [event for event in observed if event["sample_start"] < row["sample_end"]]
            require(all(event["event_sequence"] < row["event_sequence"]
                        and event["sample_end"] <= row["sample_end"] for event in preceding),
                    "forwarding precedes processing")
            require(d["schedule_complete"] is (row["sample_end"] >= arm + program.duration_samples
                                               and len(preceding) == len(program.events)), "false progress completion")
            frontier, messages = row["sample_end"], d["output_messages"]
        require(progress[-1]["details"]["schedule_complete"], "schedule completion was not forwarded")
        require(progress[-1]["monotonic_ns"] <= receipts[-1]["finished_monotonic_ns"],
                "completed progress follows completed control receipt")
        final = next(r for r in finals if r["direction"] == direction)
        d = final["details"]
        require(d.get("status") == "complete" and d.get("reason") == ("none" if grc else "completed")
                and d.get("armed_sample") == arm and d.get("scheduled_end_sample") == arm + program.duration_samples
                and all(d.get(k) is True for k in ("all_events_processed", "restoration_observed", "schedule_complete"))
                and type(d.get("logging_errors")) is int and d["logging_errors"] == 0,
                "broker final incomplete/restoration/logging failure")
        for key in ("processed_samples", "forwarded_samples", "input_samples", "output_samples",
                    "input_messages", "output_messages"):
            require(control.uint(d.get(key)), "invalid final counter")
        require(d["processed_samples"] == d["forwarded_samples"] == d["input_samples"] == d["output_samples"]
                >= max(frontier, completed[direction]["forwarded_samples"], completed[direction]["processed_samples"])
                and d["input_messages"] == d["output_messages"] and final["sample_start"] == 0
                and d["output_messages"] >= messages
                and final["monotonic_ns"] >= receipts[-1]["finished_monotonic_ns"]
                and final["sample_end"] == d["processed_samples"], "final broker accounting differs")
        if grc and not (cfo or tdl):
            external = fixed_profiles[direction]
            require(type(external) is dict and set(external) == {"started", "final"},
                    "GRC fixed profile lifecycle differs")
            for stage in ("started", "final"):
                record = dict(external[stage])
                mono, wall = record.pop("monotonic_ns", None), record.pop("wall_ns", None)
                require(control.uint(mono) and mono > 0 and control.uint(wall) and wall > 0,
                        "invalid GRC fixed profile clock")
                if stage == "final":
                    require(record.pop("status", None) == "stopped"
                            and armed["monotonic_ns"] <= mono <= final["monotonic_ns"],
                            "GRC final DSP clock/status differs")
                else:
                    require(lifecycle["started"]["monotonic_ns"] <= mono <= armed["monotonic_ns"],
                            "GRC initial DSP clock differs")
                _validate_grc_fixed_state(record, plan=plan, program=program, direction=direction,
                                          clock=0 if stage == "started" else d["processed_samples"],
                                          stage=stage, arm=stage == "started")
                if stage == "final":
                    require(all(record[key] >= state[key] for key in (
                        "input_energy", "desired_energy", "noise_energy", "cw_energy", "output_energy")),
                        "GRC cumulative energy regressed after arm")
        if tdl:
            expected_exposure = sum(
                (program.events[index + 1].sample_offset if index + 1 < len(program.events)
                 else program.duration_samples) - event.sample_offset
                for index, event in enumerate(program.events) if event.settings["tdl_enabled"])
            _validate_tdl_state(d.get("state_at_finish"), plan=plan, program=program, direction=direction,
                                clock=d["processed_samples"], stage="final", exposure=expected_exposure,
                                realization=realizations[direction])
        if cfo:
            expected_exposure = sum(
                (program.events[index + 1].sample_offset if index + 1 < len(program.events)
                 else program.duration_samples) - event.sample_offset
                for index, event in enumerate(program.events) if event.settings["cfo_hz"] != 0)
            finish = d.get("state_at_finish")
            require(type(finish) is dict, "CFO final DSP state is missing")
            expected_final = {**schedules.mutable_settings(program.base),
                              "schema_version": "radio_grc_cfo_profile_v1", "record_type": "final",
                              "backend": "grc", "direction": direction,
                              "mode": program.base["mode"], "ref_power": program.base["ref_power"],
                              "sample_rate_hz": plan.sample_rate_hz, "master_seed": plan.master_seed,
                              "rng_version": "component_streams_v1", "phase_u64": 0, "cw_step_u64": 0,
                              "channel_semantics_version": "grc_cfo_v1", "rng_algorithm": "none_cfo_only",
                              "sample_clock": d["processed_samples"], "cfo_applied_samples": expected_exposure,
                              "awgn_complex_draws": 0, "awgn_normal_draws": 0,
                              "masked_samples": 0, "attenuated_samples": 0,
                              "noise_energy": 0, "cw_energy": 0}
            require(all(finish.get(key) == value and
                        (type(finish.get(key)) is bool if type(value) is bool else type(finish.get(key)) is not bool)
                        for key, value in expected_final.items()), "CFO final exposure/settings differ")
            require(all(control.uint(finish.get(key)) for key in (
                "sample_clock", "cfo_applied_samples", "awgn_complex_draws", "awgn_normal_draws",
                "masked_samples", "attenuated_samples")), "invalid CFO final integer counter")
            phase = finish.get("cfo_phase_rad")
            require(type(phase) in (int, float) and math.isfinite(phase)
                    and 0 <= phase < 2 * math.pi
                    and abs(math.remainder(phase, 2 * math.pi)) <= 1e-8,
                    "CFO final phase differs from the selected whole-cycle pulse")
        results[direction] = {"arm_sample": arm, "end_sample": arm + program.duration_samples,
                              "processed_samples": d["processed_samples"], "condition_events": len(observed),
                              "restoration_verified": True, "sample_accounting_source": "broker_self_report"}
    return results


def read_jsonl(raw, *, maximum_records, maximum_line=4096):
    require(raw.endswith(b"\n"), "truncated JSONL input")
    lines = raw.splitlines()
    require(0 < len(lines) <= maximum_records and all(0 < len(line) + 1 <= maximum_line for line in lines),
            "JSONL record bounds exceeded")
    return [control.decode_json(line) for line in lines]


def verify(directory, *, broker_log=None, expected_build_sha256=None, expected_pid=None,
           study_id=None, trial_id=None, pipeline_id=None):
    """Reconcile one stopped trial's retained evidence; never contacts a broker.

    Every archived input is recomputed from the recipe and identities, the
    control receipts are replayed against the plan, and the broker truth must
    show the exact programmed transitions, restoration and complete sample
    accounting in both directions. Expected PID and build identity default to
    the values bound at arm time; supply them to verify against an
    independent source (for example the SHA-256 of the C binary).
    """
    directory = private_directory(directory)
    refs = {}
    def read(name, maximum=1 << 20):
        path = directory / name
        raw = read_private(path, maximum)
        refs[name] = {"path": str(path), "sha256": digest(raw)}
        return raw
    result = {"schema_version": VERIFICATION_SCHEMA, "qualified": False, "errors": [],
              "whole_trial_qualified": False, "matched_radio_windows_qualified": False,
              "independent_iq_oracle_performed": False, "references": refs}
    try:
        preparation = strict_json(read("preparation.json"))
        require(type(preparation) is dict and preparation.get("schema_version") == PREPARATION_SCHEMA,
                "preparation record schema differs")
        ids = {"study_id": study_id or preparation.get("study_id"),
               "trial_id": trial_id or preparation.get("trial_id"),
               "pipeline_id": pipeline_id or preparation.get("pipeline_id")}
        ids = {key: canonical_uuid(value) for key, value in ids.items()}
        spec = validate_spec(strict_json(read("recipe.json", 16384)))
        profile = radio_profile(spec)
        require(read("profile.json", 16384) == profiles.canonical_bytes(profile), "archived profile differs")
        program = schedule(spec, run_id=ids["study_id"], trial_id=ids["trial_id"], pipeline_id=ids["pipeline_id"])
        require(read("schedule.json", 65536) == canonical(program), "archived schedule differs")
        wire = read("plan.wire", schedules.MAX_PLAN_BYTES)
        require(wire == schedules.compile_plan(profile, program), "archived wire plan differs")
        plan = schedules.parse_wire(wire)
        require(all(preparation.get(k) == v for k, v in
                    {**ids, "plan_sha256": plan.sha256, "config_sha256": plan.profile_sha256,
                     "backend": spec["backend"], "specification": spec}.items()),
                "archived preparation binding differs")
        require(preparation.get("argv") == expected_broker_arguments(preparation["control_directory"], profile, spec["backend"]),
                "archived broker arguments differ")
        execution = strict_json(read("execution.json"))
        require(type(execution) is dict, "execution record must be an object")
        sources = {"pid": "argument", "build_sha256": "argument"}
        if expected_pid is None:
            expected_pid, sources["pid"] = execution.get("expected_pid"), "execution_record"
        if expected_build_sha256 is None:
            expected_build_sha256, sources["build_sha256"] = execution.get("expected_build_sha256"), "execution_record"
        result["identity_sources"] = sources
        if spec["backend"] == "grc":
            identity = preparation.get("broker_source_identity")
            require(type(identity) is dict and set(identity) == {"build_sha256", "files"}
                    and identity["build_sha256"] == expected_build_sha256
                    and _grc_composite(identity["files"]) == expected_build_sha256,
                    "archived GRC implementation binding differs")
        ready = control.decode_json(read("broker_ready.json", 4096))
        require(set(ready) == {"schema_version", "instance_id", "pid", "plan_sha256", "config_sha256",
                               "backend", "build_sha256", "control_socket"}
                and ready["schema_version"] == "radio_broker_ready_v1" and ready["backend"] == spec["backend"]
                and type(expected_build_sha256) is str and ready["build_sha256"] == expected_build_sha256
                and control.HEX64.fullmatch(expected_build_sha256) and control.HEX32.fullmatch(ready["instance_id"])
                and control.uint(ready["pid"], (1 << 31) - 1) and ready["pid"] > 0
                and control.uint(expected_pid, (1 << 31) - 1) and ready["pid"] == expected_pid
                and ready["plan_sha256"] == plan.sha256 and ready["config_sha256"] == plan.profile_sha256
                and ready["control_socket"] == str(Path(preparation["control_directory"]) / "rb.sock"),
                "archived broker readiness differs")
        receipts = read_jsonl(read("control.jsonl"), maximum_records=MAX_CONTROL_RECORDS)
        completed = validate_control(receipts, execution, plan, ready)
        records = read_jsonl(read("broker_events.jsonl"), maximum_records=256)
        fixed = None
        if spec["kind"] in GRC_FIXED_KINDS:
            path = Path(broker_log) if broker_log is not None else directory / "broker.log"
            raw = read_private(path)
            refs["broker.log"] = {"path": str(path), "sha256": digest(raw)}
            fixed = grc_fixed_profiles(raw)
        directions = validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=completed,
                                    fixed_profiles=fixed, realizations=preparation.get("tdl_realizations"))
        intervals = pulse_intervals(spec)
        result.update(qualified=True, specification=spec, directions=directions, control_anchors=receipts,
                      programmed_ul_blank_samples=sum(end - start for start, end, gain in intervals if gain == 0),
                      programmed_ul_attenuated_samples=sum(end - start for start, end, gain in intervals if 0 < gain < 1))
        if spec["kind"] in ALL_ADDITIVE_KINDS:
            exposure = sum(end - start for start, end, _ in intervals)
            result.update(programmed_ul_noise_samples=exposure if spec["additive_component"] == "awgn" else 0,
                          programmed_ul_cw_samples=exposure if spec["additive_component"] == "cw" else 0,
                          programmed_additive_complex_power=spec["reference_power"] * 10 ** (-spec["component_reference_db"] / 10),
                          additive_power_units="relative_digital_complex_power",
                          physical_snr_calibrated=False)
        if spec["kind"] == "c_fixed_normal":
            result.update(programmed_ul_noise_samples=0, programmed_ul_cw_samples=0,
                          physical_snr_calibrated=False)
        if spec["kind"] in GRC_CFO_KINDS:
            result.update(programmed_ul_cfo_samples=(sum(end - start for start, end, _ in intervals)
                                                    if spec["cfo_hz"] else 0),
                          programmed_ul_cfo_hz=spec["cfo_hz"],
                          cfo_units="digital_complex_rotation_hz")
        if spec["kind"] in GRC_TDL_KINDS:
            result.update(programmed_ul_tdl_samples=(sum(end - start for start, end, _ in intervals)
                                                    if spec["tdl_enabled"] else 0),
                          common_ul_delay_samples=15, tdl_profile=spec["tdl_profile"], delay_spread_ns=spec["delay_spread_ns"],
                          max_doppler_hz=0, channel_model_conformance_claimed=False)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result["errors"].append(str(exc))
    return result


def recipe_names():
    return (*KINDS, *V3_ARM_KINDS)


def describe(spec):
    """Human-oriented summary of one validated specification; no identities involved."""
    spec = validate_spec(spec)
    profile = radio_profile(spec)
    intervals = [{"start_sample": start, "end_sample": end, "gain": gain,
                  "start_seconds": start / RATE, "duration_seconds": (end - start) / RATE}
                 for start, end, gain in pulse_intervals(spec)]
    return {"specification": spec, "profile": profile, "backend": spec["backend"],
            "affected_direction": spec["affected_direction"], "ul_pulses": intervals,
            "schedule_duration_samples": spec["duration_samples"],
            "schedule_duration_seconds": spec["duration_samples"] / RATE,
            "description": DESCRIPTIONS[spec["kind"]]}


def _print_json(value):
    print(json.dumps(value, default=str, sort_keys=True, allow_nan=False, indent=2))


def _file_sha256(path):
    return digest(read_bytes(path, 256 * 1024 ** 2))


def _optional_private_json(path):
    if not os.path.lexists(path):
        return None
    return strict_json(read_private(path))


def _wait_for_ready_record(directory, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            raw = read_private(directory / "broker_ready.json", 4096)
            if raw:
                return control.decode_json(raw)
        except (FileNotFoundError, FaultError, control.ControlError):
            pass
        require(time.monotonic() < deadline, "broker_ready.json did not appear; is the broker running?")
        time.sleep(0.05)


def _resolve_identities(args, directory, preparation):
    """Expected broker PID/build and where each value came from."""
    launch = _optional_private_json(directory / "launch.json")
    sources = {}
    pid = getattr(args, "pid", None)
    build = args.expected_build_sha256
    if pid is not None:
        sources["pid"] = "argument"
    elif launch is not None:
        pid, sources["pid"] = launch["pid"], "launch_record"
    if build is not None:
        sources["build_sha256"] = "argument"
    elif args.c_binary is not None:
        require(preparation["backend"] == "c", "--c-binary applies only to the C backend")
        build, sources["build_sha256"] = _file_sha256(args.c_binary), "c_binary"
    elif launch is not None:
        build, sources["build_sha256"] = launch["expected_build_sha256"], "launch_record"
    elif preparation["backend"] == "grc":
        build, sources["build_sha256"] = expected_grc_source_identity()["build_sha256"], "current_grc_sources"
    if pid is None or build is None:
        # Last resort: the broker's own ready record. The control client still
        # authenticates the socket peer PID/UID against it before any request.
        ready = _wait_for_ready_record(directory, 30.0)
        if pid is None:
            pid, sources["pid"] = ready["pid"], "broker_ready_self_report"
        if build is None:
            build, sources["build_sha256"] = ready["build_sha256"], "broker_ready_self_report"
    return pid, build, sources


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Finite radio fault recipes for the C and Python ZMQ channel brokers.",
        epilog="Typical RAN workflow: prepare -> launch -> start gNB/UE and traffic -> arm -> "
               "stop the RAN -> stop -> verify. Try `demo` first: it needs no RAN software.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("list", help="list the recipes and V3 study arms")
    show = commands.add_parser("show", help="print a recipe's specification, profile and pulses")
    show.add_argument("recipe", choices=recipe_names(), metavar="RECIPE")
    prep = commands.add_parser("prepare", help="freeze a recipe into a new private control directory")
    prep.add_argument("recipe", choices=recipe_names(), metavar="RECIPE")
    prep.add_argument("directory", type=Path, help="new control directory (short path; parent must exist)")
    for command in (show, prep):
        command.add_argument("--seed", type=int, default=41, help="uint32 master seed (default 41)")
        command.add_argument("--traffic-bitrate", choices=TRAFFIC_BITRATES,
                             help="testbed traffic metadata carried in the specification (default 5M)")
    for field in ("study-id", "trial-id", "pipeline-id"):
        prep.add_argument("--" + field, help="canonical UUID (default: a fresh random UUID)")
    prep.add_argument("--gnb-config", type=Path, help="gNB YAML whose ZMQ sample rate must match")
    prep.add_argument("--ue-config", type=Path, help="srsUE configuration whose ZMQ sample rate must match")
    launch = commands.add_parser("launch", help="start the prepared broker on the standard ZMQ ports")
    launch.add_argument("directory", type=Path)
    launch.add_argument("--ready-timeout", type=float, default=30.0)
    arm = commands.add_parser("arm", help="STATUS, ARM, then STATUS until the schedule completes")
    arm.add_argument("directory", type=Path)
    arm.add_argument("--pid", type=int, help="expected broker PID (default: launch record)")
    stop = commands.add_parser("stop", help="stop a broker started by `launch` (identity-checked)")
    stop.add_argument("directory", type=Path)
    stop.add_argument("--timeout", type=float, default=10.0)
    check = commands.add_parser("verify", help="reconcile a stopped trial's plan, receipts and truth")
    check.add_argument("directory", type=Path)
    check.add_argument("--pid", type=int, help="expected broker PID (default: execution record)")
    check.add_argument("--broker-log", type=Path, help="broker stdout log (default: DIRECTORY/broker.log)")
    check.add_argument("--write", action="store_true", help="also write DIRECTORY/verification.json")
    demo = commands.add_parser("demo", help="broker-only end-to-end run with synthetic gNB/UE peers")
    demo.add_argument("recipe", choices=recipe_names(), metavar="RECIPE")
    demo.add_argument("--seed", type=int, default=41)
    demo.add_argument("--output", type=Path, help="new private output directory (default: under /tmp)")
    demo.add_argument("--frame-samples", type=int, default=23040,
                      help="IQ samples per ZMQ message (default 23040, one 1 ms subframe)")
    for command in (launch, arm, check, demo):
        command.add_argument("--c-binary", type=Path, help="compiled C broker (C recipes; see `make`)")
    for command in (arm, check):
        command.add_argument("--expected-build-sha256", help="expected broker build identity")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            width = max(map(len, recipe_names()))
            for name in KINDS:
                print(f"{name:<{width}}  {specification(name)['backend']:<3}  {DESCRIPTIONS[name]}")
            for arm_name, kind in V3_ARM_KINDS.items():
                print(f"{arm_name:<{width}}  c    V3 study arm: recipe {kind} with 2 Mbit/s traffic metadata.")
            return 0
        if args.command == "show":
            _print_json(describe(recipe_specification(args.recipe, args.seed,
                                                      traffic_bitrate=args.traffic_bitrate)))
            return 0
        if args.command == "prepare":
            spec = recipe_specification(args.recipe, args.seed, traffic_bitrate=args.traffic_bitrate)
            ids = {name: getattr(args, name) or str(uuid.uuid4())
                   for name in ("study_id", "trial_id", "pipeline_id")}
            _print_json(prepare(spec, args.directory, **ids,
                                gnb_config=args.gnb_config, ue_config=args.ue_config))
            return 0
        if args.command == "demo":
            import radio_fault_process as process
            summary = process.demo(args.recipe, seed=args.seed, c_binary=args.c_binary,
                                   output=args.output, frame_samples=args.frame_samples)
            _print_json(process.brief(summary))
            return 0 if summary["verification"]["qualified"] and summary["iq_oracle"]["consistent"] else 1
        directory = private_directory(args.directory)
        if args.command == "launch":
            import radio_fault_process as process
            record, _child = process.launch(directory, c_binary=args.c_binary,
                                            ready_timeout=args.ready_timeout)
            _print_json(record)
            return 0
        if args.command == "stop":
            import radio_fault_process as process
            _print_json(process.stop(directory, timeout=args.timeout))
            return 0
        preparation = strict_json(read_private(directory / "preparation.json"))
        if args.command == "arm":
            pid, build, sources = _resolve_identities(args, directory, preparation)
            result = run_schedule(directory, expected_pid=pid, expected_build_sha256=build,
                                  identity_sources=sources)
            _print_json({key: result[key] for key in ("schema_version", "qualified", "plan_sha256",
                                                      "expected_pid", "expected_build_sha256",
                                                      "identity_sources", "directions")})
            return 0
        build = args.expected_build_sha256
        if build is None and args.c_binary is not None:
            build = _file_sha256(args.c_binary)
        result = verify(directory, broker_log=args.broker_log, expected_build_sha256=build,
                        expected_pid=args.pid)
        if args.write:
            write_private(directory / "verification.json", canonical(result))
        _print_json(result)
        return 0 if result["qualified"] else 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "command": args.command, "error": f"{type(exc).__name__}: {exc}"}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
