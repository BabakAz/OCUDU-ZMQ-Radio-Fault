#!/usr/bin/env python3
"""Bounded authenticated control of one already running local development broker.

Reading ready authenticates the instance against private files and the connected
Linux peer PID/UID. This module never starts a broker or retries an ARM implicitly.
Tokens and raw packets are never included in public errors or CLI output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import time

import radio_broker_schedule as schedules

MAX_REPLY = 4096
MAX_REQUESTS = 4096
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
REASON = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


class ControlError(RuntimeError):
    """A bounded, credential-free control failure."""


def require(value, reason):
    if not value:
        raise ControlError(reason)


def uint(value, maximum=(1 << 64) - 1):
    return type(value) is int and 0 <= value <= maximum


def decode_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ControlError("nonfinite_json")

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ControlError("invalid_json") from None
    require(type(result) is dict, "invalid_json_object")
    pending = [result]
    while pending:
        value = pending.pop()
        if type(value) is float:
            require(math.isfinite(value), "nonfinite_json")
        elif type(value) is dict:
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)
    return result


def validate_reply(raw, *, sequence, operation, ready, plan):
    require(0 < len(raw) <= MAX_REPLY, "reply_size")
    result = decode_json(raw)
    required = {"schema_version", "ok", "request_sequence", "operation", "instance_id",
                "plan_sha256", "state", "directions"}
    require(set(result) in (required, required | {"reason"}), "reply_fields")
    require(result["schema_version"] == "radio_broker_control_v1" and
            type(result["ok"]) is bool, "reply_schema")
    require(type(result["request_sequence"]) is int and result["request_sequence"] == sequence
            and result["operation"] == operation, "reply_request_identity")
    require(result["instance_id"] == ready["instance_id"] and
            result["plan_sha256"] == plan.sha256, "reply_instance_identity")
    require(result["state"] in ("ready", "arm_pending", "armed", "completed", "error"),
            "reply_state")
    directions = result["directions"]
    require(type(directions) is dict and set(directions) == {"DL", "UL"}, "reply_directions")
    keys = {"armed", "arm_sample", "processed_samples", "forwarded_samples",
            "duration_samples", "schedule_complete", "next_event"}
    for name, direction in directions.items():
        require(type(direction) is dict and set(direction) == keys, "reply_direction_fields")
        program = plan.directions[name]
        require(type(direction["armed"]) is bool and type(direction["schedule_complete"]) is bool,
                "reply_direction_boolean")
        for key in ("processed_samples", "forwarded_samples", "duration_samples", "next_event"):
            require(uint(direction[key]), "reply_direction_counter")
        require(direction["duration_samples"] == program.duration_samples and
                direction["next_event"] <= len(program.events) and
                direction["forwarded_samples"] <= direction["processed_samples"],
                "reply_direction_frontier")
        arm = direction["arm_sample"]
        require(uint(arm) if direction["armed"] else arm is None, "reply_arm_epoch")
        require(direction["armed"] or direction["next_event"] == 0, "reply_unarmed_event")
        if direction["armed"]:
            require(arm <= direction["processed_samples"] and
                    arm + program.duration_samples < (1 << 64), "reply_arm_range")
        if direction["schedule_complete"]:
            require(direction["armed"] and direction["next_event"] == len(program.events) and
                    direction["forwarded_samples"] >= arm + program.duration_samples,
                    "reply_false_completion")
    require(result["state"] != "completed" or all(d["schedule_complete"] for d in directions.values()),
            "reply_false_global_completion")
    both_armed = all(d["armed"] for d in directions.values())
    require(result["state"] != "armed" or both_armed, "reply_false_global_arm")
    require(result["state"] != "arm_pending" or not both_armed, "reply_false_pending_arm")
    require(result["state"] != "ready" or not any(d["armed"] for d in directions.values()),
            "reply_false_ready_state")
    if not result["ok"]:
        reason = result.get("reason")
        require(type(reason) is str and REASON.fullmatch(reason) is not None,
                "reply_error_reason")
        require(result["state"] == "error", "reply_error_state")
    else:
        require("reason" not in result and result["state"] != "error", "reply_success_state")
    return result


class ControlClient:
    def __init__(self, directory, *, timeout=2.0, expected_backend=None,
                 expected_pid=None, expected_build_sha256=None):
        require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 5,
                "timeout_must_be_finite_at_most_five_seconds")
        self.directory = Path(directory)
        self.timeout = float(timeout)
        self.expected_backend = expected_backend
        self.expected_pid = expected_pid
        self.expected_build_sha256 = expected_build_sha256
        self.ready = None
        self.plan = None
        self.sequence = 1
        self._fd = None
        self._token = None
        self._last_packet = None
        self._last_sequence = None
        self._last_operation = None

    def __enter__(self):
        return self.wait_ready()

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._token = None
        self._last_packet = None

    def _file(self, name, maximum):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and
                    info.st_uid == os.geteuid() and info.st_nlink == 1, "private_file_metadata")
            require(0 <= info.st_size <= maximum, "private_file_size")
            raw = os.read(fd, maximum + 1)
            final = os.fstat(fd)
            require(len(raw) == info.st_size and len(raw) <= maximum and
                    (info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                    (final.st_size, final.st_mtime_ns, final.st_ctime_ns), "private_file_changed")
            return raw
        finally:
            os.close(fd)

    def _directory_unchanged(self):
        actual = os.stat(self.directory, follow_symlinks=False)
        held = os.fstat(self._fd)
        require(stat.S_ISDIR(actual.st_mode) and actual.st_uid == os.geteuid() and
                stat.S_IMODE(actual.st_mode) == 0o700 and
                (actual.st_dev, actual.st_ino) == (held.st_dev, held.st_ino), "private_directory_changed")

    def wait_ready(self):
        deadline = time.monotonic() + self.timeout
        try:
            require(self._fd is None, "client_already_open")
            self._fd = schedules.private_directory(self.directory)
            raw = self._file("plan.wire", schedules.MAX_PLAN_BYTES)
            self.plan = schedules.parse_wire(raw)
            token = self._file("control.token", 64)
            require(len(token) == 64 and HEX64.fullmatch(token.decode("ascii")) is not None,
                    "token_format")
            self._token = token
            while True:
                try:
                    raw = self._file("broker_ready.json", MAX_REPLY)
                    # Exclusive publication may still be between open and write.
                    if not raw:
                        raise FileNotFoundError
                    ready = decode_json(raw)
                    break
                except FileNotFoundError:
                    require(time.monotonic() < deadline, "ready_timeout")
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                except ControlError as exc:
                    if str(exc) not in ("private_file_changed", "invalid_json"):
                        raise
                    require(time.monotonic() < deadline, "ready_timeout")
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            keys = {"schema_version", "instance_id", "pid", "plan_sha256", "config_sha256",
                    "backend", "build_sha256", "control_socket"}
            require(set(ready) == keys and ready["schema_version"] == "radio_broker_ready_v1",
                    "ready_schema")
            require(type(ready["instance_id"]) is str and HEX32.fullmatch(ready["instance_id"])
                    and uint(ready["pid"], (1 << 31) - 1) and ready["pid"] > 0, "ready_instance")
            require(ready["plan_sha256"] == hashlib.sha256(self._file(
                "plan.wire", schedules.MAX_PLAN_BYTES)).hexdigest() == self.plan.sha256 and
                    ready["config_sha256"] == self.plan.profile_sha256, "ready_plan_identity")
            require(ready["backend"] in ("c", "grc") and type(ready["build_sha256"]) is str
                    and HEX64.fullmatch(ready["build_sha256"]), "ready_build_identity")
            require(ready["control_socket"] == str(self.directory / "rb.sock"), "ready_socket_path")
            for expected, actual in ((self.expected_backend, ready["backend"]),
                                     (self.expected_pid, ready["pid"]),
                                     (self.expected_build_sha256, ready["build_sha256"])):
                require(expected is None or expected == actual, "ready_expected_identity")
            self.ready = ready
            self._directory_unchanged()
            return self
        except (OSError, UnicodeError, schedules.ScheduleError):
            self.close()
            raise ControlError("private_control_input_invalid") from None
        except BaseException:
            self.close()
            raise

    def _exchange(self, packet, sequence, operation):
        require(self.ready is not None and self._fd is not None, "client_not_ready")
        deadline = time.monotonic() + self.timeout
        try:
            self._directory_unchanged()
            info = os.stat("rb.sock", dir_fd=self._fd, follow_symlinks=False)
            require(stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_uid == os.geteuid() and info.st_nlink == 1, "control_socket_metadata")
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
                def remaining():
                    left = deadline - time.monotonic()
                    require(left > 0, "control_timeout")
                    sock.settimeout(left)
                remaining()
                sock.connect(str(self.directory / "rb.sock"))
                pid, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                require(uid == os.geteuid() and pid == self.ready["pid"], "control_peer_identity")
                after = os.stat("rb.sock", dir_fd=self._fd, follow_symlinks=False)
                require((info.st_dev, info.st_ino) == (after.st_dev, after.st_ino), "control_socket_changed")
                remaining()
                require(sock.send(packet) == len(packet), "control_short_send")
                remaining()
                raw, _, flags, _ = sock.recvmsg(MAX_REPLY + 1)
                require(not flags & socket.MSG_TRUNC, "reply_truncated")
                require(self._token not in raw, "credential_in_reply")
            self._directory_unchanged()
        except TimeoutError:
            raise ControlError("control_timeout") from None
        except OSError:
            raise ControlError("control_transport_error") from None
        return validate_reply(raw, sequence=sequence, operation=operation, ready=self.ready, plan=self.plan)

    def request(self, operation, *, sequence=None):
        require(operation in ("STATUS", "ARM"), "unsupported_operation")
        sequence = self.sequence if sequence is None else sequence
        require(type(sequence) is int and 1 <= sequence <= MAX_REQUESTS, "request_sequence_range")
        require(self.ready is not None and self._token is not None, "client_not_ready")
        packet = (f"RBCTRL1 {sequence} {operation} {self.ready['instance_id']} {self.plan.sha256} ".encode()
                  + self._token + b"\n")
        self._last_packet, self._last_sequence, self._last_operation = packet, sequence, operation
        result = self._exchange(packet, sequence, operation)
        if result["ok"]:
            self.sequence = sequence + 1
        return result

    def retry_last(self):
        """Explicitly resend the identical last request after an ambiguous result."""
        require(self._last_packet is not None, "no_previous_request")
        result = self._exchange(self._last_packet, self._last_sequence, self._last_operation)
        if result["ok"]:
            self.sequence = self._last_sequence + 1
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--operation", choices=("STATUS", "ARM"), required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        with ControlClient(args.directory, timeout=args.timeout) as client:
            result = client.request(args.operation, sequence=args.sequence)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if result["ok"] else 1
    except ControlError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
