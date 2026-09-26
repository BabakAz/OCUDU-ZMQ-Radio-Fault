#!/usr/bin/env python3
"""Validate a development CPU-radio profile and map it to existing broker CLIs.

This adapter starts no component. It supplies constant initial settings only;
it is not an armed schedule, a calibration instrument, or campaign authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys


MAX_PROFILE_BYTES = 16384
SCHEMA = "radio_broker_profile_v1"
SEMANTICS = "fixed_reference_v1"
CFO_SCHEMA = "radio_broker_profile_grc_cfo_v1"
CFO_SEMANTICS = "grc_cfo_v1"
TDL_SCHEMA = "radio_broker_profile_grc_static_tdl_a_v1"
TDL_SEMANTICS = "grc_static_tdl_a_v1"
TDL_C_SCHEMA = "radio_broker_profile_grc_static_tdl_c_v1"
TDL_C_SEMANTICS = "grc_static_tdl_c_v1"
RNG_VERSION = "component_streams_v1"


class ProfileError(ValueError):
    pass


def exact_fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ProfileError(f"{label} must contain exactly: {', '.join(sorted(fields))}")


def number(value, minimum, maximum, label):
    if type(value) not in (int, float):
        raise ProfileError(f"{label} must be a finite number")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise ProfileError(f"{label} must be a finite number") from exc
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        raise ProfileError(f"{label} must be finite in [{minimum:g}, {maximum:g}]")
    return converted


def validate_profile(value):
    exact_fields(value, {"schema_version", "channel_semantics_version", "rng_version",
                         "sample_rate_hz", "master_seed", "reference_provenance",
                         "qualification", "directions"}, "profile")
    cfo = value["schema_version"] == CFO_SCHEMA
    tdl_c = value["schema_version"] == TDL_C_SCHEMA
    tdl = value["schema_version"] in (TDL_SCHEMA, TDL_C_SCHEMA)
    for key, expected in (("schema_version", TDL_C_SCHEMA if tdl_c else TDL_SCHEMA if tdl else CFO_SCHEMA if cfo else SCHEMA),
                          ("channel_semantics_version", TDL_C_SEMANTICS if tdl_c else TDL_SEMANTICS if tdl else CFO_SEMANTICS if cfo else SEMANTICS),
                          ("rng_version", RNG_VERSION), ("qualification", "development_only")):
        if value[key] != expected:
            raise ProfileError(f"{key} must be {expected!r}")
    rate = number(value["sample_rate_hz"], 1000, 250e6, "sample_rate_hz")
    seed = value["master_seed"]
    if type(seed) is not int or not 0 <= seed < 2 ** 32:
        raise ProfileError("master_seed must be an unsigned 32-bit integer")
    reference = value["reference_provenance"]
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", reference):
        raise ProfileError("reference_provenance must be a 1..96 character ASCII record identifier")
    exact_fields(value["directions"], {"DL", "UL"}, "directions")
    normalized = dict(value, sample_rate_hz=rate, directions={})
    for direction, config in value["directions"].items():
        exact_fields(config, {"mode", "reference_power", "desired_gain", "noise", "cw"}
                     | ({"tdl_enabled"} if tdl else {"cfo_hz"} if cfo else set()), direction)
        if config["mode"] not in ("identity", "fixed"):
            raise ProfileError(f"{direction}.mode must be identity or fixed")
        reference_power = number(config["reference_power"], 1e-20, 1e10, f"{direction}.reference_power")
        gain = number(config["desired_gain"], 0, 1, f"{direction}.desired_gain")
        exact_fields(config["noise"], {"enabled", "snr_db"}, f"{direction}.noise")
        exact_fields(config["cw"], {"enabled", "sir_db", "frequency_hz"}, f"{direction}.cw")
        for component in ("noise", "cw"):
            if type(config[component]["enabled"]) is not bool:
                raise ProfileError(f"{direction}.{component}.enabled must be boolean")
        snr = number(config["noise"]["snr_db"], -100, 100, f"{direction}.noise.snr_db")
        sir = number(config["cw"]["sir_db"], -100, 100, f"{direction}.cw.sir_db")
        frequency = number(config["cw"]["frequency_hz"], -rate / 2, rate / 2, f"{direction}.cw.frequency_hz")
        normalized["directions"][direction] = {
            "mode": config["mode"], "reference_power": reference_power,
            "desired_gain": gain, "noise": {"enabled": config["noise"]["enabled"], "snr_db": snr},
            "cw": {"enabled": config["cw"]["enabled"], "sir_db": sir, "frequency_hz": frequency}}
        if cfo:
            offset = number(config["cfo_hz"], -min(500, rate / 2), min(500, rate / 2),
                            f"{direction}.cfo_hz")
            if offset != 0 and abs(offset) < 0.01:
                raise ProfileError("nonzero CFO magnitude must be at least 0.01 Hz")
            if (gain != 1 or config["noise"]["enabled"] or config["cw"]["enabled"]
                    or frequency != 0 or (config["mode"] == "identity" and offset != 0)):
                raise ProfileError("grc_cfo_v1 requires gain 1, disabled additions and zero CFO for identity")
            normalized["directions"][direction]["cfo_hz"] = offset
        if tdl:
            if (rate != 23_040_000 or reference_power != 1 or gain != 1
                    or config["noise"]["enabled"] or config["cw"]["enabled"]
                    or snr != 0 or sir != 0 or frequency != 0
                    or config["mode"] != ("identity" if direction == "DL" else "fixed")
                    or type(config["tdl_enabled"]) is not bool
                    or (direction == "DL" and config["tdl_enabled"])):
                raise ProfileError(f"static TDL-{'C' if tdl_c else 'A'} requires 23.04 Msps, DL identity, UL fixed, unit gain/reference and no additions")
            normalized["directions"][direction]["tdl_enabled"] = config["tdl_enabled"]
    return normalized


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def reject_nonfinite(value):
    raise ProfileError(f"nonfinite JSON constant {value}")


def load_profile(path):
    # Refuse FIFOs/symlinks and bound the read before parsing untrusted lengths.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ProfileError("profile must be a regular non-symlink file")
        raw = handle.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ProfileError(f"profile exceeds {MAX_PROFILE_BYTES} bytes")
    profile = json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=reject_nonfinite)
    return validate_profile(profile), hashlib.sha256(raw).hexdigest()


def canonical_bytes(profile):
    return (json.dumps(validate_profile(profile), sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def broker_arguments(profile, backend):
    value = validate_profile(profile)
    if backend not in ("c", "grc"):
        raise ProfileError("profile backend must be c or grc")
    cfo = value["channel_semantics_version"] == CFO_SEMANTICS
    tdl = value["channel_semantics_version"] in (TDL_SEMANTICS, TDL_C_SEMANTICS)
    if (cfo or tdl) and backend != "grc":
        raise ProfileError(value["channel_semantics_version"] + " requires the grc backend")
    args = ["--channel-semantics", value["channel_semantics_version"], "--seed", str(value["master_seed"]),
            "--srate" if backend == "c" else "--samp-rate", repr(value["sample_rate_hz"])]
    for direction in ("DL", "UL"):
        c = value["directions"][direction]
        prefix = "--" + direction.lower()
        args.extend([prefix + "-mode", c["mode"], prefix + "-ref-power", repr(c["reference_power"]),
                     prefix + "-gain", repr(c["desired_gain"]),
                     prefix + "-noise-snr", repr(c["noise"]["snr_db"]),
                     prefix + "-cw-sir", repr(c["cw"]["sir_db"]),
                     prefix + "-cw-freq", repr(c["cw"]["frequency_hz"])])
        if not c["noise"]["enabled"]:
            args.append(prefix + "-noise-off")
        if c["cw"]["enabled"]:
            args.append(prefix + "-cw")
        if cfo:
            args.extend([prefix + "-cfo", repr(c["cfo_hz"])])
        if tdl and c["tdl_enabled"]:
            args.append(prefix + "-tdl-enabled")
    return args


def validate_radio_rates(profile, gnb_path, ue_path):
    """Match initial profile Fs to both actual radio configs; no nominal fallback."""
    import configparser
    import yaml

    with Path(gnb_path).open() as handle:
        config = yaml.safe_load(handle)
    radio = config.get("ru_sdr", {}) if isinstance(config, dict) else {}
    if not isinstance(radio, dict) or radio.get("device_driver") != "zmq":
        raise ProfileError("study profile requires a ZMQ gNB radio")
    gnb_rate = number(radio.get("srate"), 0.001, 250, "gNB ru_sdr.srate") * 1e6
    ue = configparser.ConfigParser(interpolation=None)
    if not ue.read(ue_path) or not ue.has_option("rf", "srate"):
        raise ProfileError("UE configuration lacks rf.srate")
    if ue.get("rf", "device_name", fallback="") != "zmq":
        raise ProfileError("study profile requires a ZMQ UE radio")
    try:
        ue_rate = number(float(ue.get("rf", "srate")), 1000, 250e6, "UE rf.srate")
    except ValueError as exc:
        raise ProfileError("UE rf.srate must be finite") from exc
    expected = validate_profile(profile)["sample_rate_hz"]
    for label, rate, device_args in (("gNB", gnb_rate, radio.get("device_args", "")),
                                    ("UE", ue_rate, ue.get("rf", "device_args", fallback=""))):
        if not isinstance(device_args, str):
            raise ProfileError(f"{label} device_args must be text")
        # Whitespace cannot hide a duplicate declaration. Reject irregular
        # tokens instead of guessing how each backend parses their spaces.
        fields = [token.partition("=") for token in device_args.split(",")]
        tokens = [value for key, separator, value in fields
                  if key.strip() == "base_srate" and separator]
        if len(tokens) != 1:
            raise ProfileError(f"{label} must have exactly one base_srate")
        if any(key != key.strip() or value != value.strip() or not separator
               for key, separator, value in fields):
            raise ProfileError(f"{label} device_args must use canonical key=value tokens")
        try:
            base_rate = number(float(tokens[0]), 1000, 250e6, label + " base_srate")
        except ValueError as exc:
            raise ProfileError(f"{label} base_srate must be finite") from exc
        if rate != expected or base_rate != expected:
            raise ProfileError(f"{label} sample rate differs from study profile {expected:g}Hz")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--backend", choices=("c", "grc"), required=True)
    parser.add_argument("--gnb-config", type=Path)
    parser.add_argument("--ue-config", type=Path)
    args = parser.parse_args(argv)
    try:
        profile, source_sha = load_profile(args.profile)
        if bool(args.gnb_config) != bool(args.ue_config):
            raise ProfileError("gNB and UE rate checks must be supplied together")
        if args.gnb_config:
            validate_radio_rates(profile, args.gnb_config, args.ue_config)
        print(json.dumps({"schema_version": "radio_broker_profile_mapping_v1",
                          "backend": args.backend, "argv": broker_arguments(profile, args.backend),
                          "profile": profile, "source_sha256": source_sha,
                          "canonical_sha256": hashlib.sha256(canonical_bytes(profile)).hexdigest()},
                         sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[FAIL] radio broker profile: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
