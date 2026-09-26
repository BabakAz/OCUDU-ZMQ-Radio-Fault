#!/usr/bin/env python3
"""Explicit finite schedule L2 probes using the existing owned IPC harness.

This is development evidence for local DSP, control and truth consistency only.
No radio stack, service, network topology, or campaign is started. The offline
gate imports/tests the independent oracle without invoking --run-local.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sys
import time

import radio_broker_control as control
import radio_broker_profile as profiles
import radio_broker_schedule as schedules
import validate_radio_broker_identity as transport

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config/radio_broker/fixed_reference.fixture.json"
SCHEDULE = ROOT / "config/radio_broker/finite_schedule.fixture.json"
SAMPLE_COUNT = 5003
WARMUP = {"DL": 11, "UL": 19}
PARTITION_BOUNDARIES = (0, 17, 18, 257, 1024, 1537, 2048, 4097, 5003)
CASES = ("piecewise", "piecewise-partition", "piecewise-no-noop", "noop-only",
         "noop-only-partition", "identity", "unarmed-stop", "dl-only-stop", "partial-stop",
         "wrong-token", "stale-instance", "stale-plan", "sequence-gap", "same-sequence-different",
         "repeated-arm", "oversized", "malformed")
ERROR_CASES = frozenset(CASES[9:])
require = transport.require


def case_inputs(case):
    """Derive every case from the reviewed canonical profile and schedule."""
    require(case in CASES, "unknown schedule case")
    profile = json.loads(PROFILE.read_text())
    schedule = json.loads(SCHEDULE.read_text())
    schedule["trial_id"] = case
    if case == "identity":
        for settings in profile["directions"].values():
            settings["mode"] = "identity"
            settings["noise"]["enabled"] = settings["cw"]["enabled"] = False
    if case.startswith("noop-only") or case == "identity":
        for name in ("DL", "UL"):
            base = schedules.mutable_settings(schedules.profile_base(profile["directions"][name]))
            for event in schedule["directions"][name]["events"]:
                event["settings"] = dict(base)
    if case == "piecewise-no-noop":
        schedule["directions"]["DL"]["events"] = [
            e for e in schedule["directions"]["DL"]["events"] if e["event_id"] != "noop"]
    profile = profiles.validate_profile(profile)
    schedule["profile_sha256"] = hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest()
    return profile, schedules.validate_schedule(profile, schedule)


def stream(direction):
    import numpy as np

    warmup = np.resize(np.asarray([1, 1j, -1, -1j], dtype=np.complex64), WARMUP[direction])
    body = np.resize(np.asarray([1, 1j, -1, -1j], dtype=np.complex64), SAMPLE_COUNT)
    body[300:620] = 0
    return warmup.tobytes(), body.tobytes()


def expected_segments(plan, direction, arm, frame_lengths):
    """Independent half-open interval arithmetic, including event-at-message-end deferral."""
    events = plan.directions[direction].events
    result, cursor, next_event = [], 0, 0
    for length in frame_lengths:
        end = cursor + length
        if arm is not None and length:
            while next_event < len(events):
                event = events[next_event]
                start = arm + event.sample_offset
                if start >= end:
                    break
                require(start >= cursor, "independent event fell behind input frontier")
                next_boundary = (arm + events[next_event + 1].sample_offset
                                 if next_event + 1 < len(events) else end)
                result.append((event, start, min(end, next_boundary)))
                next_event += 1
        cursor = end
    return result


def expected_progress(plan, direction, arm, frame_lengths):
    """Only sends forwarding a new transition or the first complete span log progress."""
    if arm is None:
        return []
    program = plan.directions[direction]
    boundaries = [arm + event.sample_offset for event in program.events]
    result, previous, was_complete = [], 0, False
    for messages, length in enumerate(frame_lengths, 1):
        end = previous + length
        transitioned = any(previous <= boundary < end for boundary in boundaries)
        complete = end >= arm + program.duration_samples
        if transitioned or (complete and not was_complete):
            result.append({"input_messages": messages, "output_messages": messages,
                           "input_samples": end, "output_samples": end,
                           "scheduled_end_sample": arm + program.duration_samples,
                           "schedule_complete": complete})
        previous, was_complete = end, complete
    return result


def independent_waveform(plan, direction, source, arm):
    """Analytic gain/CW oracle; the noise generator is deliberately not imported.

    Phase uses Python integers modulo 2^64 and coefficient intervals, independent
    of either production DSP implementation. Disabled-noise intervals are fully
    observable; other intervals are retained for deterministic replay checks.
    """
    import numpy as np

    program = plan.directions[direction]
    settings = dict(program.base)
    expected = np.empty(len(source), dtype=np.complex64)
    observable = np.zeros(len(source), dtype=bool)
    phase = 0
    event_index = 0
    for index, value in enumerate(source):
        if arm is not None and event_index < len(program.events):
            event = program.events[event_index]
            if index == arm + event.sample_offset:
                settings.update(event.settings)
                event_index += 1
        if settings["mode"] == "identity":
            expected[index] = value
            observable[index] = True
            continue
        desired = complex(float(value.real) * settings["gain"], float(value.imag) * settings["gain"])
        desired = complex(np.complex64(desired))
        tone = 0j
        if settings["cw_enabled"]:
            angle = math.ldexp(float(phase), -64) * (2 * math.pi)
            amplitude = math.sqrt(settings["ref_power"] * 10 ** (-settings["cw_sir_db"] / 10))
            tone = complex(np.complex64(complex(amplitude * math.cos(angle), amplitude * math.sin(angle))))
        expected[index] = np.complex64(desired + tone)
        observable[index] = not settings["noise_enabled"]
        frequency = settings["cw_freq_hz"]
        step = math.floor(math.ldexp(abs(frequency) / plan.sample_rate_hz, 64) + 0.5)
        phase = (phase + (-step if frequency < 0 else step)) % (1 << 64)
    return expected, observable


def compare_waveforms(reference, actual):
    import numpy as np

    require(reference.shape == actual.shape and np.isfinite(reference).all() and np.isfinite(actual).all(),
            "waveform shape/nonfinite mismatch")
    delta = actual.astype(np.complex128) - reference.astype(np.complex128)
    maximum = float(np.max(np.abs(delta), initial=0))
    scale = max(1.0, float(np.max(np.abs(reference), initial=0)))
    numerator = float(np.linalg.norm(delta))
    denominator = float(np.linalg.norm(reference.astype(np.complex128)))
    relative = numerator / denominator if denominator else (0.0 if numerator == 0 else math.inf)
    require(relative <= 1e-6 and maximum <= 1e-5 * scale, "waveform differs from frozen tolerance")
    return {"relative_l2": relative, "max_absolute_error": maximum, "reference_scale": scale,
            "samples": len(reference)}


def read_truth(path):
    raw = path.read_bytes()
    require(0 < len(raw) <= 1024 * 1024 and raw.endswith(b"\n"), "missing/truncated truth ledger")
    lines = raw.splitlines()
    require(len(lines) <= 256 and all(0 < len(line) + 1 <= 4096 for line in lines), "truth bounds exceeded")
    return [control.decode_json(line) for line in lines]


def validate_truth(records, *, plan, ready, expected, frames, arms, case):
    """Reconcile bounded truth against independently observed relay exchanges."""
    required = {"schema_version", *schedules.ID_FIELDS, "instance_id", "backend", "build_sha256",
                "config_sha256", "plan_sha256", "direction", "event_sequence", "event_type",
                "monotonic_ns", "wall_ns", "sample_start", "sample_end", "scope", "details"}
    types = {"started", "ready", "arm_requested", "armed", "condition_applied", "condition_restored",
             "progress", "error", "final"}
    for sequence, record in enumerate(records, 1):
        require(type(record) is dict and set(record) == required, "truth common fields differ")
        require(record["schema_version"] == "radio_broker_truth_v1" and
                type(record["event_sequence"]) is int and record["event_sequence"] == sequence,
                "truth sequence gap/duplicate")
        for field in schedules.ID_FIELDS:
            require(record[field] == getattr(plan, field), "truth experiment identity differs")
        for field in ("instance_id", "backend", "build_sha256", "config_sha256", "plan_sha256"):
            require(record[field] == ready[field], "truth runtime identity differs")
        require(control.uint(record["monotonic_ns"]) and record["monotonic_ns"] > 0 and
                control.uint(record["wall_ns"]) and record["wall_ns"] > 0, "truth invalid timestamp")
        # Boundary timestamps may precede another thread's later-enqueued event.
        # Ordering is certified by the ledger sequence, not wall-clock monotonicity.
        require(record["event_type"] in types and type(record["details"]) is dict, "truth event type")
        require(record["direction"] in ("DL", "UL", "control"), "truth direction")
        if record["direction"] == "control":
            require(record["scope"] == "control" and record["sample_start"] is None and
                    record["sample_end"] is None, "control event claims sample range")
        else:
            require(record["scope"] in ("processed_samples", "successfully_forwarded_samples") and
                    control.uint(record["sample_start"]) and control.uint(record["sample_end"]) and
                    record["sample_start"] <= record["sample_end"], "truth sample range")
    for kind in ("started", "ready"):
        require(sum(r["event_type"] == kind and r["direction"] == "control" for r in records) == 1,
                "missing/duplicate control lifecycle event")
    requested = [r for r in records if r["event_type"] == "arm_requested"]
    require(len(requested) == (0 if case == "unarmed-stop" or case in ERROR_CASES - {"repeated-arm"} else 1),
            "wrong arm request event count")
    finals = [r for r in records if r["event_type"] == "final"]
    require(len(finals) == 2 and {r["direction"] for r in finals} == {"DL", "UL"},
            "missing/duplicate directional final")
    require(all(r["event_type"] == "final" for r in records[-2:]), "events after terminal finals")
    errors = [r for r in records if r["event_type"] == "error"]
    require(bool(errors) == (case in ERROR_CASES), "truth error state differs from probe")
    for record in errors:
        reason = record["details"].get("reason")
        require(type(reason) is str and control.REASON.fullmatch(reason), "error reason is not a bounded typed token")
    results = {}
    for name in ("DL", "UL"):
        own = [r for r in records if r["direction"] == name]
        program = plan.directions[name]
        arm = arms[name]
        armed = [r for r in own if r["event_type"] == "armed"]
        require(len(armed) == (0 if arm is None else 1), "wrong directional arm count")
        if arm is not None:
            detail = armed[0]["details"]
            require(armed[0]["scope"] == "processed_samples" and
                    armed[0]["sample_start"] == arm == armed[0]["sample_end"], "wrong armed scope/range")
            require(type(detail["arm_sample"]) is int and type(detail["duration_samples"]) is int and
                    detail["arm_sample"] == arm and detail["duration_samples"] == program.duration_samples,
                    "wrong observed directional epoch")
            require(type(detail["request_sequence"]) is int and detail["request_sequence"] == 3 and
                    control.uint(detail["request_wall_ns"]) and control.uint(detail["request_monotonic_ns"]),
                    "arm request clock/sequence mismatch")
            require(detail["request_monotonic_ns"] <= armed[0]["monotonic_ns"], "arm precedes request")
            require(requested and requested[0]["event_sequence"] < armed[0]["event_sequence"] and
                    all(detail[key] == requested[0]["details"][key] for key in
                        ("request_sequence", "request_wall_ns", "request_monotonic_ns")) and
                    requested[0]["monotonic_ns"] == detail["request_monotonic_ns"] and
                    requested[0]["wall_ns"] == detail["request_wall_ns"], "arm request observation differs")
            snapshot = detail["state_at_arm"]
            require(type(snapshot) is dict and snapshot["sample_clock"] == arm, "warmup clock lost at arm")
            if program.base["mode"] == "fixed":
                require(snapshot["awgn_complex_draws"] == arm and snapshot["awgn_normal_draws"] == 2 * arm,
                        "warmup RNG counters lost at arm")
                frequency = program.base["cw_freq_hz"]
                step = math.floor(math.ldexp(abs(frequency) / plan.sample_rate_hz, 64) + 0.5)
                phase = (arm * (-step if frequency < 0 else step)) % (1 << 64)
                require(snapshot["phase_u64"] == phase, "warmup phase lost at arm")
        observed = [r for r in own if r["event_type"] in ("condition_applied", "condition_restored")]
        wanted = expected_segments(plan, name, arm, frames[name])
        require(len(observed) == len(wanted), "missing/duplicate condition event")
        previous = schedules.mutable_settings(program.base)
        for record, (event, start, end) in zip(observed, wanted):
            detail = record["details"]
            require(record["event_type"] == ("condition_restored" if event.kind == "restore" else "condition_applied")
                    and record["scope"] == "processed_samples" and record["sample_start"] == start and
                    record["sample_end"] == end, "condition boundary/processed scope differs")
            require(detail["event_id"] == event.event_id and detail["kind"] == event.kind and
                    type(detail["sample_offset"]) is int and detail["sample_offset"] == event.sample_offset and
                    detail["settings"] == dict(event.settings)
                    and type(detail["changed"]) is bool and detail["changed"] == (previous != event.settings),
                    "condition settings/no-op differs")
            for key, value in detail["settings"].items():
                require(type(value) is bool if key.endswith("enabled") else type(value) in (int, float),
                        "condition settings have wrong numeric/boolean type")
            require(type(detail.get("processed_samples")) is int and detail["processed_samples"] == end,
                    "condition processed frontier differs")
            require(record["event_sequence"] > armed[0]["event_sequence"] and
                    record["monotonic_ns"] >= armed[0]["monotonic_ns"], "condition precedes arm")
            previous = dict(event.settings)
        final = next(r for r in finals if r["direction"] == name)
        detail = final["details"]
        require(type(detail.get("reason")) is str and control.REASON.fullmatch(detail["reason"]),
                "final reason is not a bounded typed token")
        complete = (arm is not None and expected[name]["samples"] >= arm + program.duration_samples
                    and len(wanted) == len(program.events))
        global_complete = all(arms[d] is not None and expected[d]["samples"] >=
                              arms[d] + plan.directions[d].duration_samples for d in ("DL", "UL"))
        status = "error" if case in ERROR_CASES else ("complete" if global_complete else "incomplete")
        require(detail["status"] == status and detail["armed_sample"] == arm and
                detail["scheduled_end_sample"] == (None if arm is None else arm + program.duration_samples),
                "wrong final completion/epoch")
        require(detail["schedule_complete"] is complete and type(detail["logging_errors"]) is int and
                detail["logging_errors"] == 0 and
                detail["all_events_processed"] is (len(wanted) == len(program.events)) and
                detail["restoration_observed"] is bool(wanted and wanted[-1][0].kind == "restore"),
                "wrong final restoration/logging status")
        require(type(detail["processed_samples"]) is int and type(detail["forwarded_samples"]) is int and
                detail["processed_samples"] == expected[name]["samples"] == detail["forwarded_samples"],
                "final processed/forwarded frontier differs")
        for kind in ("input", "output"):
            for unit in ("messages", "samples"):
                require(type(detail[f"{kind}_{unit}"]) is int and
                        detail[f"{kind}_{unit}"] == expected[name][unit], "final exchange ledger differs")
        progress = [r for r in own if r["event_type"] == "progress"]
        expected_sends = expected_progress(plan, name, arm, frames[name])
        require(len(progress) == len(expected_sends) <= len(program.events) + 1,
                "missing/duplicate/unbounded progress logging")
        for record, send in zip(progress, expected_sends):
            require(all(type(record["details"].get(key)) is type(value) for key, value in send.items()),
                    "progress counter/boolean types differ")
            require(record["scope"] == "successfully_forwarded_samples" and record["sample_start"] == arm
                    and record["sample_end"] == send["output_samples"] and record["details"] == send,
                    "wrong progress forwarding scope/counts")
            preceding = [r for r in observed if r["sample_start"] < send["output_samples"]]
            require(all(r["event_sequence"] < record["event_sequence"] for r in preceding),
                    "progress claims transitions before processing")
        require(final["scope"] == "processed_samples" and final["sample_start"] == 0 and
                final["sample_end"] == expected[name]["samples"], "wrong final processed scope")
        results[name] = {"arm_sample": arm, "condition_events": len(observed),
                         "progress_events": len(progress), "status": status,
                         "processed_samples": detail["processed_samples"]}
    return results


def run_schedule_case(backend, command, case, output):
    profile, schedule = case_inputs(case)
    state = {"frames": {"DL": [], "UL": []}, "arms": {"DL": None, "UL": None}, "responses": []}
    if backend == "c":
        expected_build = transport.digest(command[0])
    else:
        named_hashes = "".join(name + " " + transport.digest(ROOT / "scripts" / name) + "\n"
                              for name in ("ocudu_channel_broker.py", "radio_broker_metrics.py", "radio_broker_profile.py",
                                           "radio_broker_schedule.py", "radio_schedule_runtime.py", "radio_static_tdl.py"))
        expected_build = hashlib.sha256(named_hashes.encode()).hexdigest()

    def setup(private, case_dir):
        directory = private / "control"
        directory.mkdir(mode=0o700)
        transport.write_json(case_dir / "profile.json", profile)
        transport.write_json(case_dir / "schedule.json", schedule)
        prepared = schedules.prepare(case_dir / "profile.json", case_dir / "schedule.json", directory)
        state.update(directory=directory, plan=schedules.load_wire(directory / "plan.wire"))
        return prepared["broker_arguments"][backend]

    def check_exchange(direction, sent, received):
        import numpy as np

        require(len(received) == 1 and len(received[0]) == len(sent), "scheduled message shape changed")
        require(np.isfinite(np.frombuffer(received[0], dtype=np.complex64)).all(), "nonfinite scheduled output")
        if case == "identity":
            require(received == [sent], "identity schedule changed bytes")
        state["frames"][direction].append(len(sent) // 8)

    def interact(exchange, request, private, case_dir, child):
        del request, private, case_dir
        with control.ControlClient(state["directory"], timeout=2, expected_backend=backend,
                                   expected_pid=child.pid, expected_build_sha256=expected_build) as client:
            state["ready"] = dict(client.ready)

            def ask(operation):
                require(client.sequence <= 32, "case control budget exceeded")
                response = client.request(operation)
                state["responses"].append(response)
                require(response["ok"], "unexpected control rejection")
                return response

            require(ask("STATUS")["state"] == "ready", "broker not initially ready")
            for name in ("DL", "UL"):
                warmup, _ = stream(name)
                exchange(name, b"")
                exchange(name, warmup)
            status = ask("STATUS")
            require(all(status["directions"][name]["processed_samples"] == WARMUP[name]
                        and not status["directions"][name]["armed"] for name in ("DL", "UL")),
                    "warmup armed or lost counters")
            if case == "unarmed-stop":
                return
            if case in ERROR_CASES:
                if case == "repeated-arm":
                    ask("ARM")
                    packet = (f"RBCTRL1 4 ARM {client.ready['instance_id']} {client.plan.sha256} ".encode()
                              + client._token + b"\n")
                else:
                    sequence, operation = 3, "STATUS"
                    instance, plan_hash, token = client.ready["instance_id"], client.plan.sha256, client._token
                    if case == "wrong-token":
                        token = (b"0" if token[:1] != b"0" else b"1") + token[1:]
                    elif case == "stale-instance":
                        instance = "0" * 32
                    elif case == "stale-plan":
                        plan_hash = "0" * 64
                    elif case == "sequence-gap":
                        sequence = 4
                    elif case == "same-sequence-different":
                        sequence, operation = 2, "ARM"
                    packet = f"RBCTRL1 {sequence} {operation} {instance} {plan_hash} ".encode() + token + b"\n"
                    if case == "oversized":
                        packet += b"X" * 513
                    elif case == "malformed":
                        packet = b"invalid-control-packet\n"
                # Deliberately malformed probes bypass the safe client builder.
                # Only typed reply summaries are retained; no packet/token bytes.
                with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
                    sock.settimeout(2)
                    sock.connect(str(state["directory"] / "rb.sock"))
                    sock.sendall(packet)
                    try:
                        raw = sock.recv(4097)
                    except (ConnectionResetError, BrokenPipeError):
                        raw = b""
                if raw:
                    require(client._token not in raw and len(raw) <= 4096, "credential/oversized error reply")
                    response = control.decode_json(raw)
                    require(response.get("ok") is False and response.get("state") == "error", "error probe accepted")
                    require(type(response.get("reason")) is str and control.REASON.fullmatch(response["reason"]),
                            "control rejection lacks a bounded typed reason")
                state["rejection"] = {"probe": case, "reply_received": bool(raw),
                                      "reason": response["reason"] if raw else None}
                return
            armed_reply = ask("ARM")
            require(armed_reply["state"] == "arm_pending", "ARM acceptance falsely claims application")
            require(client.retry_last() == armed_reply, "ARM retry is not cached")
            exchange("DL", b"")
            exchange("UL", b"")
            require(not any(d["armed"] for d in ask("STATUS")["directions"].values()), "empty message armed schedule")
            body_dl = stream("DL")[1]
            state["arms"]["DL"] = WARMUP["DL"]
            exchange("DL", body_dl[:17 * 8])
            status = ask("STATUS")
            require(status["directions"]["DL"]["armed"] and not status["directions"]["UL"]["armed"],
                    "directions did not arm independently")
            require(status["directions"]["DL"]["next_event"] == 1, "message-end event applied before a sample")
            exchange("DL", b"")
            require(ask("STATUS")["directions"]["DL"]["next_event"] == 1, "empty message applied boundary event")
            if case == "dl-only-stop":
                exchange("DL", body_dl[17 * 8:])
                return
            state["arms"]["UL"] = WARMUP["UL"]
            body_ul = stream("UL")[1]
            if case == "partial-stop":
                exchange("UL", body_ul[:100 * 8])
                return
            if case.endswith("partition"):
                for left, right in zip(PARTITION_BOUNDARIES[1:], PARTITION_BOUNDARIES[2:]):
                    exchange("DL", body_dl[left * 8:right * 8])
                    exchange("DL", b"")
                for left, right in zip(PARTITION_BOUNDARIES, PARTITION_BOUNDARIES[1:]):
                    exchange("UL", body_ul[left * 8:right * 8])
                    exchange("UL", b"")
            else:
                exchange("DL", body_dl[17 * 8:])
                exchange("UL", body_ul)
            for _ in range(10):
                status = ask("STATUS")
                if status["state"] == "completed":
                    break
                time.sleep(0.01)
            require(status["state"] == "completed", "forwarded finite schedules never completed")

    def finalize(log, selected_backend, expected, raw_paths):
        import numpy as np

        plan = state["plan"]
        require(schedules.compile_plan(profile, schedule) == (state["directory"] / "plan.wire").read_bytes(),
                "input profile/schedule binding changed")
        records = read_truth(state["directory"] / "broker_events.jsonl")
        truth = validate_truth(records, plan=plan, ready=state["ready"], expected=expected,
                               frames=state["frames"], arms=state["arms"], case=case)
        boundary_path = raw_paths["DL"]["source"].parent / "boundaries.csv"
        with boundary_path.open("x", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("direction", "event_id", "kind", "sample_offset",
                                    "arm_sample", "expected_sample_start", "expected_sample_end",
                                    "observed_sample_start", "observed_sample_end", "changed"))
            writer.writeheader()
            for name in ("DL", "UL"):
                observed = [r for r in records if r["direction"] == name and
                            r["event_type"] in ("condition_applied", "condition_restored")]
                for record, (event, left, right) in zip(observed, expected_segments(
                        plan, name, state["arms"][name], state["frames"][name])):
                    writer.writerow({"direction": name, "event_id": event.event_id, "kind": event.kind,
                                     "sample_offset": event.sample_offset, "arm_sample": state["arms"][name],
                                     "expected_sample_start": left, "expected_sample_end": right,
                                     "observed_sample_start": record["sample_start"],
                                     "observed_sample_end": record["sample_end"], "changed": record["details"]["changed"]})
        waveform = {}
        for name, paths in raw_paths.items():
            source = np.fromfile(paths["source"], dtype=np.complex64)
            received = np.fromfile(paths["received"], dtype=np.complex64)
            analytic, observable = independent_waveform(plan, name, source, state["arms"][name])
            waveform[name] = compare_waveforms(analytic[observable], received[observable])
        relays = [json.loads(line[len(transport.PREFIXES[selected_backend]):]) for line in log.splitlines()
                  if line.startswith(transport.PREFIXES[selected_backend])]
        require(len(relays) == 2 and {r["direction"] for r in relays} == {"DL", "UL"}, "missing relay final")
        for record in relays:
            name = record["direction"]
            for kind in ("input", "output"):
                for unit in ("messages", "samples"):
                    require(record[f"{kind}_{unit}"] == expected[name][unit], "relay final differs from observed peers")
            require(record["status"] == ("error" if case in ERROR_CASES else "stopped"), "relay error propagation differs")
        return {"truth_validation": truth, "independent_waveform": waveform, "relay_records": relays,
                "plan_sha256": plan.sha256, "ready_identity": state["ready"],
                "independently_computed_build_sha256": expected_build,
                "control_responses": state["responses"], "rejection": state.get("rejection")}

    def retain(private, case_dir):
        directory = private / "control"
        for name in ("plan.wire", "broker_ready.json", "broker_events.jsonl"):
            path = directory / name
            if path.exists():
                raw = path.read_bytes()
                require(len(raw) <= 1024 * 1024, "retained artifact too large")
                token = directory / "control.token"
                require(not token.exists() or token.read_bytes() not in raw, "credential in retained artifact")
                with (case_dir / name).open("xb") as handle:
                    handle.write(raw)
        transport.write_json(case_dir / "control_observations.json", {
            "responses": state["responses"], "expected_arms": state["arms"],
            "observed_frame_samples": state["frames"], "ready": state.get("ready"),
            "rejection": state.get("rejection"), "independently_computed_build_sha256": expected_build})

    return transport.run_case(backend, command, case, output, broker_arguments=[],
                              supplied_payloads=[], exchange_validator=check_exchange,
                              final_validator=finalize, capture_raw=True, setup_callback=setup,
                              interaction_callback=interact, cleanup_callback=retain,
                              expected_exit_success=case not in ERROR_CASES)


def compare_cases(output, backend):
    import numpy as np

    comparisons = []
    for reference, actual, names in (
        ("piecewise", "piecewise-partition", ("DL", "UL")),
        ("piecewise", "piecewise-no-noop", ("DL", "UL")),
        ("noop-only", "noop-only-partition", ("DL", "UL")),
        ("piecewise", "noop-only", ("UL",)),
    ):
        for name in names:
            left = output / f"{backend}-{reference}"
            right = output / f"{backend}-{actual}"
            require(transport.digest(left / f"{name.lower()}-source.cf32") ==
                    transport.digest(right / f"{name.lower()}-source.cf32"), "replay source stream differs")
            measure = compare_waveforms(np.fromfile(left / f"{name.lower()}-received.cf32", dtype=np.complex64),
                                        np.fromfile(right / f"{name.lower()}-received.cf32", dtype=np.complex64))
            comparisons.append({"reference": reference, "actual": actual, "direction": name, **measure})
    return comparisons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-local", action="store_true")
    parser.add_argument("--backend", choices=("c", "grc", "both"), default="both")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.run_local:
        parser.error("local broker execution requires --run-local")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "artifacts") or output == ROOT / "artifacts" or output.exists():
        parser.error("--output must be a new directory beneath artifacts/")
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    sources = ["scripts/radio_broker_schedule.py", "scripts/radio_broker_control.py",
               "scripts/validate_radio_broker_schedule.py", "scripts/validate_radio_broker_identity.py",
               "scripts/ocudu_channel_broker.py", "scripts/zmq_channel_broker.c",
               "scripts/radio_broker_metrics.py", "scripts/radio_metrics_native.h",
               "scripts/radio_broker_profile.py", "config/radio_broker/schedule_schema.json",
               "config/radio_broker/broker_truth_schema.json", "config/radio_broker/schedule_validation_protocol.yaml",
               "config/radio_broker/fixed_reference.fixture.json", "config/radio_broker/finite_schedule.fixture.json"]
    sources += [str(path.relative_to(ROOT)) for path in (ROOT / "scripts").glob("radio_schedule_*.*")
                if path.suffix in (".py", ".h")]
    hashes = {name: transport.digest(ROOT / name) for name in sorted(set(sources))}
    report = {"schema_version": "radio_broker_schedule_l2_v1", "status": "failed", "evidence_layer": "L2",
              "scope": "finite private IPC control/DSP/truth development probes only", "full_stack_attempts": 0,
              "campaign_attempts": 0, "date_utc": datetime.now(timezone.utc).isoformat(), "cases": [],
              "source_sha256": hashes, "host": {"platform": platform.platform(), "python": sys.version},
              "protocol": {"seed": 41, "schedule_duration_samples": 4097, "post_arm_samples": SAMPLE_COUNT, "warmup": WARMUP,
                           "partition_boundaries": PARTITION_BOUNDARIES, "max_control_requests_per_case": 32,
                           "relative_l2_tolerance": 1e-6, "max_absolute_error_scale": 1e-5}}
    try:
        backends = ("c", "grc") if args.backend == "both" else (args.backend,)
        commands = transport.prepare_commands(output, backends, report)
        for backend in backends:
            for case in CASES:
                result = run_schedule_case(backend, commands[backend], case, output)
                report["cases"].append({"backend": backend, "case": case, "status": result["status"],
                                        "result_sha256": transport.digest(output / f"{backend}-{case}/result.json")})
                print(f"{backend}/{case}: {result['status']}", flush=True)
                require(result["status"] == "passed", result.get("error", "case failed"))
            report.setdefault("waveform_comparisons", {})[backend] = compare_cases(output, backend)
        aggregate = output / "boundaries.csv"
        with aggregate.open("x", newline="") as handle:
            writer = None
            for backend in backends:
                for case in CASES:
                    with (output / f"{backend}-{case}" / "boundaries.csv").open(newline="") as source:
                        reader = csv.DictReader(source)
                        if writer is None:
                            writer = csv.DictWriter(handle, fieldnames=("backend", "case", *reader.fieldnames))
                            writer.writeheader()
                        for row in reader:
                            writer.writerow({"backend": backend, "case": case, **row})
        report["boundaries_sha256"] = transport.digest(aggregate)
        require(hashes == {name: transport.digest(ROOT / name) for name in hashes}, "source changed during validation")
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    transport.write_json(output / "validation.json", report)
    print(f"{report['status']}: {output / 'validation.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
