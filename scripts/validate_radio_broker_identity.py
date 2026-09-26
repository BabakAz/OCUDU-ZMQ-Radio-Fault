#!/usr/bin/env python3
"""Explicit, finite L2 validation of actual CPU brokers with private IPC peers.

No radio stack, GUI, service, namespace, network port, or campaign is started.
The ordinary offline gate may import this module but must never invoke its
--run-local path. Source and received bytes are independently reconciled with
both relay final records and the owned child exit. This is not a cost benchmark.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
CASES = ("identity", "misaligned", "nan", "infinity", "multipart-iq",
         "multipart-request", "stalled-upstream-stop")
IO_TIMEOUT_MS = 4000
STOP_TIMEOUT_S = 5
# Acceptance budget; every blocking I/O also has a 4s timeout. On failure,
# bounded owned-child teardown can add up to two STOP_TIMEOUT_S intervals.
MAX_CASE_SECONDS = 20
MAX_SOURCE_BYTES_PER_DIRECTION = 2 * 1024 * 1024
PREFIXES = {"c": "C_RELAY_ACCOUNTING: ", "grc": "GRC_RELAY_ACCOUNTING: "}


class ValidationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git_head():
    """Checked-out commit, or None outside a git checkout (for example a source archive).

    The report's per-file SHA-256 values identify the sources either way.
    """
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def identity_payloads():
    """Deterministic finite native CF32 patterns, with three partitions of a stream."""
    bits = (0, 0x80000000, 1, 0x80000001, 0x3f800000, 0xbf800000,
            0x7f7fffff, 0xff7fffff)
    stream = b"".join(struct.pack("=I", bits[i % len(bits)]) for i in range(8192))
    fixed = [stream[i:i + 2048] for i in range(0, len(stream), 2048)]
    boundaries = (0, 1, 14, 271, 1298, 3071, 4096)
    irregular = [stream[left * 8:right * 8]
                 for left, right in zip(boundaries, boundaries[1:])]
    return [stream, *fixed, *irregular, b"", b"\0" * 88,
            struct.pack("=ff", 0.25, -0.5) * 131073]


def validate_final_records(log, backend, expected, *, error_direction=None,
                           identity_by_direction=None):
    records = [json.loads(line[len(PREFIXES[backend]):]) for line in log.splitlines()
               if line.startswith(PREFIXES[backend])]
    require(len(records) == 2, "expected exactly two terminal relay records")
    require({r.get("direction") for r in records} == {"DL", "UL"},
            "missing or duplicate terminal direction")
    for record in records:
        direction = record["direction"]
        require(record.get("schema_version") == "radio_broker_accounting_v1"
                and record.get("record_type") == "final"
                and record.get("backend") == backend
                and record.get("identity") is (
                    True if identity_by_direction is None else identity_by_direction[direction]),
                "unexpected relay record identity")
        for name in ("input_messages", "input_samples", "output_messages",
                     "output_samples", "error_count"):
            value = record.get(name)
            require(type(value) is int and 0 <= value < 2 ** 64,
                    f"invalid uint64 {direction}.{name}")
        for suffix in ("messages", "samples"):
            require(record[f"input_{suffix}"] == expected[direction][suffix]
                    == record[f"output_{suffix}"], f"unreconciled {direction} {suffix}")
        if error_direction is None:
            require(record["status"] == "stopped" and record["error_count"] == 0,
                    "clean child stop has an unexpected failure/incomplete record")
        else:
            require(record["status"] == "error", "sibling failure was not propagated")
            require(record["error_count"] == (1 if direction == error_direction else 0),
                    "unexpected local versus sibling error counts")
    return records


def run_case(backend, command, case, output, *, broker_arguments=None,
             supplied_payloads=None, exchange_validator=None,
             final_validator=None, capture_raw=False, setup_callback=None,
             interaction_callback=None, cleanup_callback=None,
             expected_exit_success=True, before_upstream_send=None):
    """Shared owned-child transport, optionally driven by fixed-profile checks.

    Explicit broker_arguments selects the extension path and disables identity
    failure probes. Callbacks verify exchanges/final records inside the same
    failure-preserving report and cleanup boundary as the identity probes.
    """
    # Imported only on the explicit local execution path. These are real sockets.
    import zmq

    case_dir = output / f"{backend}-{case}"
    case_dir.mkdir(mode=0o700)
    report = {"backend": backend, "case": case, "evidence_layer": "L2",
              "transport": "real-libzmq-private-filesystem-ipc", "status": "failed",
              "bounds": {"io_timeout_ms": IO_TIMEOUT_MS,
                         "stop_timeout_s": STOP_TIMEOUT_S,
                         "max_case_seconds": MAX_CASE_SECONDS,
                         "source_bytes_per_direction": MAX_SOURCE_BYTES_PER_DIRECTION}}
    expected = {direction: {"messages": 0, "samples": 0, "bytes": 0}
                for direction in ("DL", "UL")}
    source_hash = {d: hashlib.sha256() for d in expected}
    received_hash = {d: hashlib.sha256() for d in expected}
    source_counts = {d: {"messages": 0, "frames": 0, "bytes": 0} for d in expected}
    received_counts = {d: {"messages": 0, "frames": 0, "bytes": 0} for d in expected}
    frame_hashes = {d: {"sent": [], "received": []} for d in expected}
    child = None
    private_dir = None
    sockets = []
    context = zmq.Context()
    started = time.monotonic()
    log_path = case_dir / "broker.log"
    raw_paths = {d: {kind: case_dir / f"{d.lower()}-{kind}.cf32"
                     for kind in ("source", "received")} for d in expected}
    try:
        if broker_arguments is not None:
            require(supplied_payloads is not None and exchange_validator is not None
                    and final_validator is not None,
                    "extended cases require payload and verification callbacks")
        if capture_raw:
            for paths in raw_paths.values():
                for path in paths.values():
                    path.open("xb").close()
        # The directory is mode0700 and short enough for UNIX-domain addresses.
        private_dir = tempfile.TemporaryDirectory(prefix="rb-", dir="/tmp")
        private = private_dir.name
        if setup_callback is not None:
            broker_arguments = setup_callback(Path(private), case_dir)
        endpoints = {name: f"ipc://{private}/{name}"
                     for name in ("dl-up", "dl-down", "ul-up", "ul-down")}
        upstream, downstream = {}, {}
        for direction in expected:
            for mapping, kind, suffix in ((upstream, zmq.REP, "up"),
                                          (downstream, zmq.REQ, "down")):
                sock = context.socket(kind)
                sockets.append(sock)
                sock.setsockopt(zmq.LINGER, 0)
                sock.setsockopt(zmq.RCVTIMEO, IO_TIMEOUT_MS)
                sock.setsockopt(zmq.SNDTIMEO, IO_TIMEOUT_MS)
                endpoint = endpoints[f"{direction.lower()}-{suffix}"]
                (sock.bind if suffix == "up" else sock.connect)(endpoint)
                mapping[direction] = sock

        arguments = [*command, *(broker_arguments if broker_arguments is not None
                                 else ["--identity", "--fading"]),
                     "--dl-connect", endpoints["dl-up"],
                     "--dl-bind", endpoints["dl-down"],
                     "--ul-connect", endpoints["ul-up"],
                     "--ul-bind", endpoints["ul-down"]]
        report["command"] = arguments
        with log_path.open("xb") as log:
            child = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=ROOT,
                                     start_new_session=True)
            report["owned_pid"] = child.pid

            def bounded_io(sock, operation, *values):
                remaining_ms = int((MAX_CASE_SECONDS - (time.monotonic() - started)) * 1000)
                require(remaining_ms > 0, "case wall budget exceeded")
                timeout = min(IO_TIMEOUT_MS, remaining_ms)
                sock.setsockopt(zmq.RCVTIMEO, timeout)
                sock.setsockopt(zmq.SNDTIMEO, timeout)
                return getattr(sock, operation)(*values)

            def request(direction):
                require(time.monotonic() - started < MAX_CASE_SECONDS,
                        "case wall budget exceeded")
                bounded_io(downstream[direction], "send", b"\x01")
                parts = bounded_io(upstream[direction], "recv_multipart")
                require(parts == [b"\x01"], "upstream request bytes/boundary changed")

            def exchange(direction, payload):
                stats = expected[direction]
                require(stats["bytes"] + len(payload) <= MAX_SOURCE_BYTES_PER_DIRECTION,
                        "source byte budget exceeded")
                request(direction)
                if before_upstream_send is not None:
                    before_upstream_send(direction, payload)
                bounded_io(upstream[direction], "send", payload)
                # Retain observations before the equality assertion so a
                # failed/corrupted exchange remains visible in both ledgers.
                source_counts[direction]["messages"] += 1
                source_counts[direction]["frames"] += 1
                source_counts[direction]["bytes"] += len(payload)
                source_hash[direction].update(payload)
                if capture_raw:
                    with raw_paths[direction]["source"].open("ab") as capture:
                        capture.write(payload)
                frame_hashes[direction]["sent"].append([
                    {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}])
                received = bounded_io(downstream[direction], "recv_multipart")
                received_counts[direction]["messages"] += 1
                received_counts[direction]["frames"] += len(received)
                received_counts[direction]["bytes"] += sum(map(len, received))
                for raw in received:
                    received_hash[direction].update(raw)
                frame_hashes[direction]["received"].append([
                    {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                    for raw in received])
                require(received_counts[direction]["bytes"] <= MAX_SOURCE_BYTES_PER_DIRECTION,
                        "receiver byte budget exceeded")
                if capture_raw:
                    with raw_paths[direction]["received"].open("ab") as capture:
                        for raw in received:
                            capture.write(raw)
                if exchange_validator is None:
                    require(received == [payload], "downstream bytes/boundary changed")
                else:
                    exchange_validator(direction, payload, received)
                stats["messages"] += 1
                stats["samples"] += len(payload) // 8
                stats["bytes"] += len(payload)

            # Both directions demonstrate an actual round trip before each
            # failure probe; an unstarted sibling cannot satisfy this test.
            payloads = supplied_payloads if supplied_payloads is not None else (
                identity_payloads() if case == "identity" else [b"\0" * 56])
            if interaction_callback is None:
                for payload in payloads:
                    for direction in expected:
                        exchange(direction, payload)
            else:
                interaction_callback(exchange, request, Path(private), case_dir, child)

            error_direction = None
            if broker_arguments is not None:
                pass
            elif case == "stalled-upstream-stop":
                request("DL")
                report["unanswered_upstream_request"] = "DL"
            elif case != "identity":
                error_direction = "DL"
                if case == "multipart-request":
                    bad_frames = [b"\x01", b"unsupported"]
                    bounded_io(downstream["DL"], "send_multipart", bad_frames)
                else:
                    request("DL")
                    if case == "multipart-iq":
                        bad_frames = [b"\0" * 8, b"\0" * 8]
                        bounded_io(upstream["DL"], "send_multipart", bad_frames)
                    else:
                        payload = {"misaligned": b"bad",
                                   "nan": struct.pack("=ff", float("nan"), 0),
                                   "infinity": struct.pack("=ff", 0, float("inf"))}[case]
                        bad_frames = [payload]
                        bounded_io(upstream["DL"], "send", payload)
                report["rejected_probe"] = {
                    "direction": "DL", "kind": case,
                    "frames": [{"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                               for raw in bad_frames]}

            if error_direction is None and expected_exit_success:
                # The unreaped direct child is owned by this invocation.
                # No process-name, port scan, or process-group kill is used.
                require(child.poll() is None, "child exited before requested stop")
                child.terminate()
            remaining = MAX_CASE_SECONDS - (time.monotonic() - started)
            require(remaining > 0, "case wall budget exceeded")
            code = child.wait(timeout=min(STOP_TIMEOUT_S, remaining))
            report["exit_code"] = code
            require(code == 0 if error_direction is None and expected_exit_success else code > 0,
                    "unexpected broker exit status")
            if error_direction:
                require(not downstream["DL"].poll(50), "invalid IQ was forwarded")
                if case == "multipart-request":
                    require(not upstream["DL"].poll(50), "multipart request was forwarded")
            if final_validator is None:
                report["relay_records"] = validate_final_records(
                    log_path.read_text(), backend, expected, error_direction=error_direction)
            else:
                report.update(final_validator(log_path.read_text(), backend, expected, raw_paths))
            report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, KeyboardInterrupt):
            report["cancelled"] = True
    finally:
        if child is not None and child.poll() is None:
            try:
                child.terminate()
                try:
                    child.wait(timeout=STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=STOP_TIMEOUT_S)
                    report["forced_kill"] = True
                    report["status"] = "failed"
            except (OSError, subprocess.TimeoutExpired) as exc:
                report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                report["status"] = "failed"
        cleanup_errors = []
        for sock in sockets:
            try:
                sock.close(linger=0)
            except Exception as exc:
                cleanup_errors.append(f"socket close: {type(exc).__name__}: {exc}")
        try:
            context.term()
        except Exception as exc:
            cleanup_errors.append(f"context term: {type(exc).__name__}: {exc}")
        if cleanup_errors:
            report["socket_cleanup_errors"] = cleanup_errors
            report["status"] = "failed"
        if private_dir is not None:
            if cleanup_callback is not None:
                try:
                    cleanup_callback(Path(private_dir.name), case_dir)
                except Exception as exc:
                    report["artifact_cleanup_error"] = f"{type(exc).__name__}: {exc}"
                    report["status"] = "failed"
            try:
                private_dir.cleanup()
                report["private_endpoint_directory_removed"] = True
            except Exception as exc:
                report["directory_cleanup_error"] = f"{type(exc).__name__}: {exc}"
                report["status"] = "failed"
        report["elapsed_seconds"] = time.monotonic() - started
        report["validated_exchange_counts"] = expected
        report["source_peer_counts"] = source_counts
        report["receiver_peer_counts"] = received_counts
        report["valid_source_sha256"] = {d: h.hexdigest() for d, h in source_hash.items()}
        report["received_sha256"] = {d: h.hexdigest() for d, h in received_hash.items()}
        report["frames"] = frame_hashes
        if capture_raw:
            report["raw_captures"] = {
                d: {kind: {"path": path.name, "bytes": path.stat().st_size,
                           "sha256": digest(path)}
                    for kind, path in paths.items() if path.exists()}
                for d, paths in raw_paths.items()}
        report["child_reaped"] = child is not None and child.returncode is not None
        if log_path.exists():
            report["log_sha256"] = digest(log_path)
        if report["elapsed_seconds"] > MAX_CASE_SECONDS:
            report["status"] = "failed"
            report["error"] = "case wall budget exceeded"
        write_json(case_dir / "result.json", report)
    return report


def prepare_commands(output, backends, report):
    """Build the locked C broker and select the real headless Python command."""
    commands = {"grc": [sys.executable, str(ROOT / "scripts/ocudu_channel_broker.py"),
                        "--no-gui"]}
    if "c" in backends:
        lock = json.loads((ROOT / "dependencies/toolchain.lock.json").read_text())
        compiler = lock["toolchain"]["cc_default"]
        banner = subprocess.run([compiler, "--version"], check=True,
                                capture_output=True, text=True, timeout=5).stdout
        require(f"clang version {lock['toolchain']['compiler_version']}" in banner,
                "C compiler does not match locked version")
        binary = output / "zmq_channel_broker"
        build = [compiler, "-std=c17", "-O2", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
                 str(ROOT / "scripts/zmq_channel_broker.c"), "-o", str(binary),
                 "-lzmq", "-lm", "-pthread"]
        result = subprocess.run(build, capture_output=True, text=True, timeout=30)
        (output / "build.log").write_text(result.stdout + result.stderr)
        require(result.returncode == 0, "C broker build failed; see build.log")
        report["c_build"] = {"command": build, "compiler": banner,
                             "binary_sha256": digest(binary), "log_sha256": digest(output / "build.log")}
        report["c_build"]["libzmq_pkg_config_version"] = subprocess.run(
            ["pkg-config", "--modversion", "libzmq"], check=True,
            capture_output=True, text=True, timeout=5).stdout.strip()
        report["c_build"]["dynamic_libraries"] = subprocess.run(
            ["ldd", str(binary)], check=True,
            capture_output=True, text=True, timeout=5).stdout
        commands["c"] = [str(binary)]
    return commands


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local", action="store_true",
                        help="Explicitly start finite owned CPU brokers and private IPC peers")
    parser.add_argument("--backend", choices=("c", "grc", "both"), default="both")
    parser.add_argument("--output", type=Path, required=True,
                        help="New result directory under this checkout's artifacts/")
    args = parser.parse_args(argv)
    if not args.run_local:
        parser.error("local broker execution requires --run-local")
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()) or output == ROOT / "artifacts":
        parser.error("--output must be a new directory beneath artifacts/")
    if output.exists():
        parser.error("--output already exists; results must not be overwritten")
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    sources = ("scripts/zmq_channel_broker.c", "scripts/ocudu_channel_broker.py",
               "scripts/radio_schedule_native.h", "scripts/radio_schedule_sha256.h",
               "scripts/radio_metrics_native.h", "scripts/radio_broker_metrics.py",
               "scripts/radio_schedule_runtime.py", "scripts/radio_broker_schedule.py",
               "scripts/validate_radio_broker_identity.py", "dependencies/toolchain.lock.json",
               "pyproject.toml", "uv.lock")
    source_hashes = {name: digest(ROOT / name) for name in sources}
    report = {"schema_version": "radio_broker_identity_l2_v1", "status": "failed",
              "date_utc": datetime.now(timezone.utc).isoformat(),
              "evidence_layer": "L2", "scope": "finite identity/format/shutdown probes only",
              "full_stack_attempts": 0, "campaign_attempts": 0,
              "source_sha256": source_hashes, "cases": [],
              "host": {"platform": platform.platform(), "python": sys.version,
                       "executable": sys.executable}}
    try:
        import numpy
        import scipy
        import zmq

        report["host"].update(numpy=numpy.__version__, scipy=scipy.__version__,
                              pyzmq=zmq.__version__, pyzmq_libzmq=zmq.zmq_version())
        report["git_head"] = git_head()
        backends = ("c", "grc") if args.backend == "both" else (args.backend,)
        commands = prepare_commands(output, backends, report)
        for backend in backends:
            for case in CASES:
                attempt = {"backend": backend, "case": case, "status": "attempting"}
                report["cases"].append(attempt)
                result = run_case(backend, commands[backend], case, output)
                attempt.update(status=result["status"], result_sha256=digest(
                    output / f"{backend}-{case}" / "result.json"))
                print(f"{backend}/{case}: {result['status']}", flush=True)
                require(result["status"] == "passed", result.get("error", "case failed"))
        require(source_hashes == {name: digest(ROOT / name) for name in sources},
                "source changed during validation")
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    write_json(output / "validation.json", report)
    print(f"{report['status']}: {output / 'validation.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
