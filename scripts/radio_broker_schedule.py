#!/usr/bin/env python3
"""Compile finite development schedules; never start or arm a broker.

The ASCII wire format keeps the native broker independent of a JSON library.
Each broker validates the entire preloaded wire program and hashes its bytes.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from types import MappingProxyType
from typing import Mapping

import radio_broker_profile as profiles

SCHEMA = "radio_broker_schedule_v1"
WIRE_VERSION = "RADIO_SCHEDULE_WIRE_V1"
CFO_SCHEMA = "radio_broker_schedule_grc_cfo_v1"
CFO_WIRE_VERSION = "RADIO_SCHEDULE_GRC_CFO_WIRE_V1"
TDL_SCHEMA = "radio_broker_schedule_grc_static_tdl_a_v1"
TDL_WIRE_VERSION = "RADIO_SCHEDULE_GRC_STATIC_TDL_A_WIRE_V1"
TDL_C_SCHEMA = "radio_broker_schedule_grc_static_tdl_c_v1"
TDL_C_WIRE_VERSION = "RADIO_SCHEDULE_GRC_STATIC_TDL_C_WIRE_V1"
MAX_PLAN_BYTES = 65536
MAX_DURATION = (1 << 63) - 1
MAX_EVENTS = 32
SETTINGS = ("gain", "noise_enabled", "noise_snr_db", "cw_enabled", "cw_sir_db", "cw_freq_hz")
ID_FIELDS = ("study_id", "protocol_id", "trial_id", "pipeline_id")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
UINT_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)
HEX_PATTERN = re.compile(r"-?0x[0-9a-f]+(?:\.[0-9a-f]+)?p[+-][0-9]+\Z", re.ASCII)


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    sample_offset: int
    event_id: str
    kind: str
    settings: Mapping


@dataclass(frozen=True)
class DirectionPlan:
    duration_samples: int
    base: Mapping
    events: tuple[Event, ...]


@dataclass(frozen=True)
class SchedulePlan:
    sha256: str
    profile_sha256: str
    study_id: str
    protocol_id: str
    trial_id: str
    pipeline_id: str
    master_seed: int
    sample_rate_hz: float
    directions: Mapping[str, DirectionPlan]
    declared_semantics: str | None = None

    @property
    def channel_semantics_version(self):
        # A and C share the enable settings; only the validated wire header
        # determines their model. Preserve inference for older direct callers.
        if self.declared_semantics is not None:
            return self.declared_semantics
        if "tdl_enabled" in self.directions["DL"].base:
            return profiles.TDL_SEMANTICS
        return profiles.CFO_SEMANTICS if "cfo_hz" in self.directions["DL"].base else profiles.SEMANTICS


def require(condition, message):
    if not condition:
        raise ScheduleError(message)


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields),
            f"{label} must contain exactly {', '.join(sorted(fields))}")


def identifier(value, label):
    require(type(value) is str and ID_PATTERN.fullmatch(value) is not None,
            f"{label} must be a 1..64 character ASCII identifier")
    return value


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, f"{label} must be an integer in [{low}, {high}]")
    return value


def finite(value, low, high, label):
    require(type(value) in (float, int), f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ScheduleError(f"{label} must be finite") from exc
    require(math.isfinite(number) and low <= number <= high,
            f"{label} must be finite in [{low}, {high}]")
    return number


def profile_base(config):
    """Effective fixed-core settings; identity ignores enabled additions."""
    active = config["mode"] == "fixed"
    result = {"mode": config["mode"], "ref_power": float(config["reference_power"]),
            "gain": float(config["desired_gain"]),
            "noise_enabled": config["noise"]["enabled"] and active,
            "noise_snr_db": float(config["noise"]["snr_db"]),
            "cw_enabled": config["cw"]["enabled"] and active,
            "cw_sir_db": float(config["cw"]["sir_db"]),
            "cw_freq_hz": float(config["cw"]["frequency_hz"])}
    if "cfo_hz" in config:
        result["cfo_hz"] = float(config["cfo_hz"])
    if "tdl_enabled" in config:
        result["tdl_enabled"] = config["tdl_enabled"]
    return result


def mutable_settings(base):
    return {key: base[key] for key in SETTINGS + (("tdl_enabled",) if "tdl_enabled" in base else
                                                 ("cfo_hz",) if "cfo_hz" in base else ())}


def validate_settings(value, rate, label="settings", *, cfo=False, tdl=False):
    require(not (cfo and tdl), "mixed CFO/TDL semantics")
    exact(value, SETTINGS + (("tdl_enabled",) if tdl else ("cfo_hz",) if cfo else ()), label)
    for key in ("noise_enabled", "cw_enabled"):
        require(type(value[key]) is bool, f"{label}.{key} must be boolean")
    result = {"gain": finite(value["gain"], 0, 1, label + ".gain"),
            "noise_enabled": value["noise_enabled"],
            "noise_snr_db": finite(value["noise_snr_db"], -100, 100, label + ".noise_snr_db"),
            "cw_enabled": value["cw_enabled"],
            "cw_sir_db": finite(value["cw_sir_db"], -100, 100, label + ".cw_sir_db"),
            "cw_freq_hz": finite(value["cw_freq_hz"], -rate / 2, rate / 2, label + ".cw_freq_hz")}
    if cfo:
        result["cfo_hz"] = finite(value["cfo_hz"], -min(500, rate / 2), min(500, rate / 2), label + ".cfo_hz")
        require(result["cfo_hz"] == 0 or abs(result["cfo_hz"]) >= 0.01,
                "nonzero CFO magnitude must be at least 0.01 Hz")
        require(result["gain"] == 1 and not result["noise_enabled"]
                and not result["cw_enabled"] and result["cw_freq_hz"] == 0,
                "grc_cfo_v1 permits only CFO, with gain 1 and additions disabled")
    if tdl:
        require(type(value["tdl_enabled"]) is bool, "tdl_enabled must be boolean")
        require(type(rate) in (int, float) and rate == 23_040_000
                and result == dict(gain=1.0, noise_enabled=False, noise_snr_db=0.0,
                                   cw_enabled=False, cw_sir_db=0.0, cw_freq_hz=0.0),
                "static TDL permits only enable at 23.04 Msps, unit gain and no additions")
        result["tdl_enabled"] = value["tdl_enabled"]
    return result


def validate_direction(value, base, rate, label):
    exact(value, {"duration_samples", "events"}, label)
    duration = integer(value["duration_samples"], 1, MAX_DURATION, label + ".duration_samples")
    require(type(value["events"]) is list and 1 <= len(value["events"]) <= MAX_EVENTS,
            f"{label}.events must contain 1..{MAX_EVENTS} events")
    events, previous, ids = [], -1, set()
    for item in value["events"]:
        exact(item, {"sample_offset", "event_id", "kind", "settings"}, label + ".event")
        offset = integer(item["sample_offset"], 0, duration - 1, label + ".sample_offset")
        require(offset > previous, label + " event offsets must be strictly increasing")
        event_id = identifier(item["event_id"], "event_id")
        require(event_id not in ids, label + " event IDs must be unique")
        require(item["kind"] in ("set", "restore"), "event kind must be set or restore")
        settings = validate_settings(item["settings"], rate, cfo="cfo_hz" in base, tdl="tdl_enabled" in base)
        if item["kind"] == "restore" or base["mode"] == "identity":
            require(settings == mutable_settings(base), "restore/identity settings must equal the initial effective profile")
        events.append({"sample_offset": offset, "event_id": event_id,
                       "kind": item["kind"], "settings": settings})
        previous = offset
        ids.add(event_id)
    require(events[-1]["kind"] == "restore", label + " final event must restore the initial profile")
    return {"duration_samples": duration, "events": events}


def validate_schedule(profile, value):
    profile = profiles.validate_profile(profile)
    exact(value, {"schema_version", "qualification", "profile_sha256", *ID_FIELDS, "directions"}, "schedule")
    schema = (TDL_C_SCHEMA if profile["channel_semantics_version"] == profiles.TDL_C_SEMANTICS else
              TDL_SCHEMA if profile["channel_semantics_version"] == profiles.TDL_SEMANTICS else
              CFO_SCHEMA if profile["channel_semantics_version"] == profiles.CFO_SEMANTICS else SCHEMA)
    require(value["schema_version"] == schema, "unsupported schedule schema")
    require(value["qualification"] == "development_only", "schedule qualification must be development_only")
    expected = hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest()
    require(type(value["profile_sha256"]) is str and value["profile_sha256"] == expected,
            "schedule profile_sha256 differs from the validated profile")
    exact(value["directions"], {"DL", "UL"}, "directions")
    result = {"schema_version": schema, "qualification": "development_only", "profile_sha256": expected,
              **{key: identifier(value[key], key) for key in ID_FIELDS}, "directions": {}}
    for direction in ("DL", "UL"):
        result["directions"][direction] = validate_direction(
            value["directions"][direction], profile_base(profile["directions"][direction]),
            profile["sample_rate_hz"], direction)
    return result


def seconds_to_samples(seconds, sample_rate_hz):
    """Convert decimal duration exactly; reject nonintegral results, no rounding."""
    require(type(seconds) is str and len(seconds) <= 64 and
            re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", seconds) is not None,
            "duration must be a canonical nonnegative decimal string in seconds")
    rate = finite(sample_rate_hz, 1000, 250e6, "sample rate")
    # Fs is the actual represented binary64 value, not an idealized decimal rate.
    samples = Fraction(seconds) * Fraction.from_float(rate)
    require(samples.denominator == 1, "duration does not map to an exact integer sample count")
    return integer(samples.numerator, 0, MAX_DURATION, "converted sample count")


def _settings_tokens(settings):
    return [str(int(settings[key])) if key.endswith("enabled") else float(settings[key]).hex()
            for key in mutable_settings(settings)]


def compile_plan(profile, schedule):
    profile = profiles.validate_profile(profile)
    schedule = validate_schedule(profile, schedule)
    version = (TDL_C_WIRE_VERSION if profile["channel_semantics_version"] == profiles.TDL_C_SEMANTICS else
               TDL_WIRE_VERSION if profile["channel_semantics_version"] == profiles.TDL_SEMANTICS else
               CFO_WIRE_VERSION if profile["channel_semantics_version"] == profiles.CFO_SEMANTICS else WIRE_VERSION)
    lines = [version, "ids " + " ".join(schedule[key] for key in ID_FIELDS),
             "profile " + " ".join((schedule["profile_sha256"], str(profile["master_seed"]),
                                      float(profile["sample_rate_hz"]).hex()))]
    for name in ("DL", "UL"):
        direction = schedule["directions"][name]
        base = profile_base(profile["directions"][name])
        lines.append(" ".join(["direction", name, str(direction["duration_samples"]),
                               str(len(direction["events"])), base["mode"], base["ref_power"].hex(),
                               *_settings_tokens(base)]))
        for event in direction["events"]:
            lines.append(" ".join(["event", str(event["sample_offset"]), event["event_id"], event["kind"],
                                   *_settings_tokens(event["settings"])]))
    wire = ("\n".join([*lines, "end"]) + "\n").encode("ascii")
    require(len(wire) <= MAX_PLAN_BYTES, "compiled plan exceeds byte limit")
    return wire


def read_regular(path, limit, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode), "input must be a regular non-symlink file")
        if private:
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                    "private input must be owned, mode0600 and have one link")
        require(info.st_size <= limit, "input exceeds byte limit")
        data = handle.read(limit + 1)
    require(len(data) <= limit, "input exceeds byte limit")
    return data


def load_schedule(path):
    data = read_regular(path, MAX_PLAN_BYTES)
    value = json.loads(data, object_pairs_hook=profiles.no_duplicates, parse_constant=profiles.reject_nonfinite)
    return value, hashlib.sha256(data).hexdigest()


def parse_wire(raw):
    require(type(raw) is bytes and 0 < len(raw) <= MAX_PLAN_BYTES, "invalid wire byte count")
    try:
        text = raw.decode("ascii")
    except UnicodeError as exc:
        raise ScheduleError("wire plan must be ASCII") from exc
    require(text.endswith("\n") and "\r" not in text and "\x00" not in text, "wire plan must use complete LF lines")
    lines = text[:-1].split("\n")
    require(all(line and len(line) <= 1024 for line in lines), "invalid wire line length")
    cursor = 0

    def take(prefix, count):
        nonlocal cursor
        require(cursor < len(lines), "truncated wire plan")
        tokens = lines[cursor].split(" ")
        cursor += 1
        require(len(tokens) == count and tokens[0] == prefix and all(tokens), "invalid wire fields or order")
        return tokens

    def uint(token, maximum):
        require(len(token) <= 20 and UINT_PATTERN.fullmatch(token) is not None, "noncanonical unsigned integer")
        return integer(int(token), 0, maximum, "wire integer")

    def real(token):
        require(len(token) <= 128 and HEX_PATTERN.fullmatch(token) is not None, "wire floating values must be lowercase C99 hexadecimal")
        try:
            result = float.fromhex(token)
        except (ValueError, OverflowError) as exc:
            raise ScheduleError("invalid hexadecimal float") from exc
        require(math.isfinite(result), "nonfinite hexadecimal float")
        return result

    cfo = lines[0] == CFO_WIRE_VERSION
    tdl_c = lines[0] == TDL_C_WIRE_VERSION
    tdl = lines[0] in (TDL_WIRE_VERSION, TDL_C_WIRE_VERSION)

    def settings(tokens):
        require(tokens[1] in ("0", "1") and tokens[3] in ("0", "1"), "wire boolean must be 0 or 1")
        result = dict(zip(SETTINGS, [real(tokens[0]), tokens[1] == "1", real(tokens[2]),
                                    tokens[3] == "1", real(tokens[4]), real(tokens[5])], strict=True))
        if cfo:
            result["cfo_hz"] = real(tokens[6])
        if tdl:
            require(tokens[6] in ("0", "1"), "wire boolean must be 0 or 1")
            result["tdl_enabled"] = tokens[6] == "1"
        return result

    take(TDL_C_WIRE_VERSION if tdl_c else TDL_WIRE_VERSION if tdl else CFO_WIRE_VERSION if cfo else WIRE_VERSION, 1)
    ids = take("ids", 5)[1:]
    for key, value in zip(ID_FIELDS, ids, strict=True):
        identifier(value, key)
    row = take("profile", 4)
    require(HASH_PATTERN.fullmatch(row[1]) is not None, "invalid profile hash")
    profile_hash, seed, rate = row[1], uint(row[2], (1 << 32) - 1), finite(real(row[3]), 1000, 250e6, "sample rate")
    directions = {}
    for name in ("DL", "UL"):
        row = take("direction", 13 if cfo or tdl else 12)
        require(row[1] == name, "wire directions must be DL then UL")
        duration, count = uint(row[2], MAX_DURATION), uint(row[3], MAX_EVENTS)
        require(1 <= duration and 1 <= count, "empty wire duration or event list")
        require(row[4] in ("fixed", "identity"), "unsupported direction mode")
        base = {"mode": row[4], "ref_power": finite(real(row[5]), 1e-20, 1e10, "reference power"),
                **validate_settings(settings(row[6:]), rate, cfo=cfo, tdl=tdl)}
        require(not tdl or (base["ref_power"] == 1
                and base["mode"] == ("identity" if name == "DL" else "fixed")
                and (name != "DL" or not base["tdl_enabled"])), "invalid static TDL direction")
        require(base["mode"] != "identity" or not (base["noise_enabled"] or base["cw_enabled"]),
                "identity wire settings must disable additions")
        require(not cfo or base["mode"] != "identity" or base["cfo_hz"] == 0,
                "identity wire settings must disable CFO")
        events = []
        for _ in range(count):
            row = take("event", 11 if cfo or tdl else 10)
            events.append({"sample_offset": uint(row[1], MAX_DURATION), "event_id": row[2],
                           "kind": row[3], "settings": settings(row[4:])})
        validated = validate_direction({"duration_samples": duration, "events": events}, base, rate, name)
        directions[name] = DirectionPlan(duration, MappingProxyType(base), tuple(
            Event(event["sample_offset"], event["event_id"], event["kind"], MappingProxyType(event["settings"]))
            for event in validated["events"]))
    take("end", 1)
    require(cursor == len(lines), "trailing wire content")
    return SchedulePlan(hashlib.sha256(raw).hexdigest(), profile_hash, *ids, seed, rate,
                        MappingProxyType(directions),
                        profiles.TDL_C_SEMANTICS if tdl_c else profiles.TDL_SEMANTICS if tdl else
                        profiles.CFO_SEMANTICS if cfo else profiles.SEMANTICS)


def load_wire(path):
    return parse_wire(read_regular(path, MAX_PLAN_BYTES, private=True))


def private_directory(path):
    path = Path(path)
    require(path.is_absolute() and str(path) == str(path.resolve()), "control directory must be canonical and absolute")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ScheduleError("control directory must be owned and mode0700")
    return fd


def prepare(profile_path, schedule_path, directory):
    profile, profile_source_hash = profiles.load_profile(profile_path)
    schedule, schedule_source_hash = load_schedule(schedule_path)
    wire = compile_plan(profile, schedule)
    plan = parse_wire(wire)
    directory = Path(directory)
    require(len(os.fsencode(str(directory / "rb.sock"))) <= 107, "control socket path exceeds Linux limit")
    fd = private_directory(directory)
    created = []
    try:
        require(not os.listdir(fd), "control directory must be empty for preparation")
        for name, data in (("plan.wire", wire), ("control.token", secrets.token_hex(32).encode("ascii"))):
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(out, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                created.append((name, os.fstat(handle.fileno())))
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        actual = os.stat(directory, follow_symlinks=False)
        held = os.fstat(fd)
        require((actual.st_dev, actual.st_ino) == (held.st_dev, held.st_ino), "control directory changed during preparation")
    except BaseException:
        for name, original in created:
            try:
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                os.unlink(name, dir_fd=fd)
        raise
    finally:
        os.close(fd)
    extra = ["--radio-plan-file", str(directory / "plan.wire"), "--radio-control-dir", str(directory)]
    return {"schema_version": "radio_broker_schedule_preparation_v1", "wire_path": str(directory / "plan.wire"),
            "token_path": str(directory / "control.token"), "control_dir": str(directory),
            "plan_sha256": plan.sha256, "config_sha256": plan.profile_sha256,
            "profile_source_sha256": profile_source_hash, "schedule_source_sha256": schedule_source_hash,
            "broker_arguments": {backend: profiles.broker_arguments(profile, backend)
                                 + (["--no-gui"] if backend == "grc" else []) + extra
                                 for backend in (("grc",) if profile["channel_semantics_version"] in (profiles.CFO_SEMANTICS, profiles.TDL_SEMANTICS, profiles.TDL_C_SEMANTICS)
                                                 else ("c", "grc"))}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("--profile", required=True, type=Path)
        command.add_argument("--schedule", required=True, type=Path)
        if name == "prepare":
            command.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.profile, args.schedule, args.directory)
        else:
            profile, _ = profiles.load_profile(args.profile)
            value, _ = load_schedule(args.schedule)
            plan = parse_wire(compile_plan(profile, value))
            result = {"plan_sha256": plan.sha256, "config_sha256": plan.profile_sha256,
                      "event_counts": {name: len(direction.events) for name, direction in plan.directions.items()}}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        print(f"[FAIL] radio broker schedule: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
