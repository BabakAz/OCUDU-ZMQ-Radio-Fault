#!/usr/bin/env python3
"""Independent finite metrics validation; explicit --run-local is required.

CPU/RSS describe one external whole-process interval. Directional timing
windows retain their own support. This is accounting qualification, not a
performance ranking, radio deadline claim, or scientific cost campaign.
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
import sys
import time

import radio_broker_control as control
import radio_broker_profile as profiles
import radio_broker_resources as resources
import radio_broker_schedule as schedules
import validate_radio_broker_identity as transport
import validate_radio_broker_schedule as signal_validation

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "config/radio_broker/metrics_validation_fixture.json"
PROTOCOL = ROOT / "config/radio_broker/metrics_validation_protocol.yaml"
CASES = ("identity-ragged", "fixed-ragged-on", "fixed-ragged-off", "fixed-paced", "empty-only",
         "exact-full-window", "final-partial-window", "upstream-delay-stall-stop")
PHASES = ("request_receive", "request_send", "upstream_receive", "processing", "downstream_send")
PARTS = ("input_prepare", "channel_chain", "output_prepare")
OUTCOMES = ("completed", "stopped", "error")
ENERGIES = ("input", "desired", "noise", "cw", "output")
WARMUP = {"DL": 11, "UL": 19}
require = transport.require


def configuration_hash(every):
    return hashlib.sha256(f"radio_broker_metrics_v1\n{every}\n".encode("ascii")).hexdigest()


def histogram(values):
    counts = {}
    for value in values:
        require(control.uint(value), "histogram input outside uint64")
        bin_id = value.bit_length()
        counts[bin_id] = counts.get(bin_id, 0) + 1
    return [[key, counts[key]] for key in sorted(counts)]


def histogram_bounds(rows, *, count, total=None, maximum=None):
    require(type(rows) is list and len(rows) <= 65, "histogram not bounded sparse list")
    previous, population, lower, upper = -1, 0, 0, 0
    for row in rows:
        require(type(row) is list and len(row) == 2 and type(row[0]) is int and
                previous < row[0] <= 64 and control.uint(row[1]) and row[1] > 0,
                "histogram bin order/count invalid")
        bin_id, observations = row
        require(bin_id >= 0, "negative histogram bin")
        lo, hi = (0, 0) if bin_id == 0 else (1 << (bin_id - 1), (1 << bin_id) - 1)
        population += observations
        lower += lo * observations
        upper += hi * observations
        previous = bin_id
    require(population == count, "histogram population differs")
    if total is not None:
        require(lower <= total <= upper, "histogram sum outside bin support")
    if maximum is not None:
        require(control.uint(maximum) and (maximum.bit_length() == rows[-1][0] if rows else maximum == 0),
                "histogram maximum outside last bin")
    quantiles = {}
    for percentile in (95, 99):
        if not population:
            quantiles[f"p{percentile}_ns_bounds"] = None
            continue
        rank = (population * percentile + 99) // 100
        cumulative = 0
        for bin_id, observations in rows:
            cumulative += observations
            if cumulative >= rank:
                quantiles[f"p{percentile}_ns_bounds"] = [0, 0] if bin_id == 0 else [1 << (bin_id - 1), (1 << bin_id) - 1]
                break
    return quantiles


def triplets(value, label):
    require(type(value) is dict and set(value) == set(OUTCOMES), label + " outcome fields differ")
    for outcome, row in value.items():
        require(type(row) is list and len(row) == 3 and all(control.uint(x) for x in row), label + " triplet types")
        count, total, maximum = row
        require((total == maximum == 0) if count == 0 else maximum <= total <= count * maximum,
                label + "." + outcome + " count/sum/max inconsistent")
    return sum(value[outcome][1] for outcome in OUTCOMES)


def ratios(window, sample_rate):
    require(type(sample_rate) in (int, float) and math.isfinite(sample_rate) and sample_rate > 0,
            "invalid sample rate")
    count = window["completed_processing_samples"]
    return {"rtf": window["output_samples"] * 1e9 / (sample_rate * window["wall_ns"]),
            "processing_budget_ratio": window["phases"]["processing"]["completed"][1] * sample_rate /
                (count * 1e9) if count else None,
            "processing_budget_undefined_reason": None if count else "no_completed_processing_samples"}


def validate_window(value, *, every, previous, final_partial):
    fields = {"window_id", "final_partial", "start_ns", "end_ns", "wall_ns", "loop_overhead_ns",
              "input_messages", "output_messages", "input_samples", "output_samples", "input_bytes", "output_bytes",
              "input_sample_start", "input_sample_end", "output_sample_start", "output_sample_end",
              "processed_sample_start", "processed_sample_end", "completed_processing_samples", "energies",
              "phases", "processing_parts", "dsp_core", "message_samples_histogram", "processing_ns_histogram",
              "intentional_mask_samples", "rejected_messages"}
    require(type(value) is dict and set(value) == fields, "metric window fields differ")
    for key in fields - {"final_partial", "energies", "phases", "processing_parts", "dsp_core",
                         "message_samples_histogram", "processing_ns_histogram"}:
        require(control.uint(value[key]), "metric window non-uint64 " + key)
    require(type(value["final_partial"]) is bool and value["final_partial"] is final_partial,
            "final partial window misplaced/missing")
    require(value["end_ns"] > value["start_ns"] > 0 and value["end_ns"] - value["start_ns"] == value["wall_ns"],
            "window elapsed interval invalid")
    require(value["window_id"] == (1 if previous is None else previous["window_id"] + 1), "window sequence gap")
    if previous is not None:
        require(value["start_ns"] == previous["end_ns"], "window time gap/overlap")
    for kind in ("input", "output", "processed"):
        start, end = value[f"{kind}_sample_start"], value[f"{kind}_sample_end"]
        require(start == (0 if previous is None else previous[f"{kind}_sample_end"]) and end >= start,
                "window sample gap/overlap")
        if kind != "processed":
            require(end - start == value[f"{kind}_samples"], "window sample delta differs")
    require(value["output_messages"] < every if final_partial else value["output_messages"] == every,
            "window closure differs from message interval")
    require(value["input_messages"] >= value["output_messages"] and
            value["input_sample_end"] >= value["processed_sample_end"] >= value["output_sample_end"],
            "invalid input/processed/forwarded frontiers")
    require(value["completed_processing_samples"] <= value["processed_sample_end"] - value["processed_sample_start"]
            and value["intentional_mask_samples"] <= value["processed_sample_end"] - value["processed_sample_start"],
            "invalid processed population")
    require(value["input_bytes"] == 8 * value["input_samples"] and
            value["output_bytes"] == 8 * value["output_samples"], "cf32 payload bytes differ")
    require(type(value["energies"]) is dict and set(value["energies"]) == set(ENERGIES) and
            all(type(x) in (int, float) and math.isfinite(x) and x >= 0 for x in value["energies"].values()),
            "window energies invalid")
    require(type(value["phases"]) is dict and set(value["phases"]) == set(PHASES), "phase fields differ")
    phase_sum = sum(triplets(value["phases"][key], key) for key in PHASES)
    require(phase_sum + value["loop_overhead_ns"] == value["wall_ns"], "phase/wall conservation failed")
    require(type(value["processing_parts"]) is dict and set(value["processing_parts"]) == set(PARTS),
            "processing part fields differ")
    for part in PARTS:
        triplets(value["processing_parts"][part], part)
    for outcome in OUTCOMES:
        require(sum(value["processing_parts"][part][outcome][1] for part in PARTS) ==
                value["phases"]["processing"][outcome][1], "nested processing conservation failed")
    core_sum = triplets(value["dsp_core"], "dsp_core")
    require(core_sum <= sum(value["processing_parts"]["channel_chain"][outcome][1] for outcome in OUTCOMES),
            "DSP core exceeds containing channel phase")
    histogram_bounds(value["message_samples_histogram"], count=value["input_messages"], total=value["input_samples"])
    completed = value["phases"]["processing"]["completed"]
    quantiles = histogram_bounds(value["processing_ns_histogram"], count=completed[0], total=completed[1], maximum=completed[2])
    return quantiles


def expected_core_calls(plan, name, arm, frames, backend):
    calls, offset = [], 0
    for size in frames:
        end = offset + size
        inside = sum(offset < arm + event.sample_offset < end for event in plan.directions[name].events) if arm is not None else 0
        calls.append((0 if backend == "c" and size == 0 else 1) + inside)
        offset = end
    return calls


def independent_components(plan, name, source, arm):
    """Compute only deterministic components independently of production modules."""
    import numpy as np

    program = plan.directions[name]
    gains = np.full(len(source), program.base["gain"], dtype=float)
    cw_enabled = np.full(len(source), program.base["cw_enabled"], dtype=bool)
    if arm is not None:
        for event in program.events:
            gains[arm + event.sample_offset:] = event.settings["gain"]
            cw_enabled[arm + event.sample_offset:] = event.settings["cw_enabled"]
    if program.base["mode"] == "identity":
        gains.fill(1)
    desired = (source.astype(np.complex128) * gains).astype(np.complex64)
    # The existing independent schedule oracle preserves NCO phase at every event.
    zero = np.zeros(len(source), dtype=np.complex64)
    tone, observable = signal_validation.independent_waveform(plan, name, zero, arm)
    return desired, tone, (gains == 0 if program.base["mode"] == "fixed" else np.zeros(len(source), dtype=bool)), observable


def validate_records(records, *, truth, plan, ready, frames, arms, captures, every=4,
                     first_upstream_send_ns=None, delays=(), case=None):
    import numpy as np

    common = {"schema_version", *schedules.ID_FIELDS, "instance_id", "backend", "build_sha256", "config_sha256",
              "plan_sha256", "metrics_config_sha256", "direction", "event_sequence", "event_type",
              "monotonic_ns", "wall_ns", "sample_start", "sample_end", "scope", "details"}
    require(0 < len(records) <= 515, "metrics record count bound")
    prior_mono = 0
    for index, record in enumerate(records, 1):
        require(type(record) is dict and set(record) == common, "metric envelope differs")
        require(record["schema_version"] == "radio_broker_metrics_v1" and
                type(record["event_sequence"]) is int and record["event_sequence"] == index,
                "metric event sequence/schema differs")
        require(record["metrics_config_sha256"] == configuration_hash(every), "metric configuration differs")
        for name in schedules.ID_FIELDS:
            require(record[name] == getattr(plan, name), "metric plan identifiers differ")
        for name in ("instance_id", "backend", "build_sha256", "config_sha256", "plan_sha256"):
            require(record[name] == ready[name], "metric runtime identity differs")
        require(control.uint(record["monotonic_ns"]) and record["monotonic_ns"] > 0 and
                control.uint(record["wall_ns"]) and record["wall_ns"] > 0 and type(record["details"]) is dict,
                "metric timestamp/details invalid")
        require(record["monotonic_ns"] >= prior_mono, "metric envelope monotonic clock regressed")
        prior_mono = record["monotonic_ns"]
        require(record["event_type"] in ("started", "window", "final"), "metric event type")
        if record["event_type"] == "started":
            require(record["direction"] == "control" and record["scope"] == "control" and
                    record["sample_start"] is record["sample_end"] is None, "metric started scope")
        else:
            require(record["direction"] in ("DL", "UL") and record["scope"] == "direction" and
                    control.uint(record["sample_start"]) and control.uint(record["sample_end"]), "metric sample scope")
    started = [record for record in records if record["event_type"] == "started"]
    require(len(started) == 1 and records[0] == started[0], "missing/duplicate/late metric start")
    require(started[0]["details"] == {"every_messages": every, "max_windows": 256,
            "histogram": "uint64_bit_length", "sample_rate_hz": plan.sample_rate_hz, "pid": ready["pid"]},
            "metric started configuration differs")
    summaries, rows = {}, []
    for name in ("DL", "UL"):
        own = [record for record in records if record["direction"] == name]
        require(own and own[-1]["event_type"] == "final" and sum(row["event_type"] == "final" for row in own) == 1,
                "missing/duplicate/out-of-order metric final")
        windows = [row for row in own if row["event_type"] == "window"]
        require(1 <= len(windows) <= 256, "metric window count bound")
        require(windows[0]["details"]["start_ns"] >= started[0]["monotonic_ns"], "first metric window precedes startup")
        if first_upstream_send_ns is not None:
            require(windows[0]["details"]["start_ns"] <= first_upstream_send_ns[name],
                    "first metric interval starts after measured work")
        source = np.fromfile(captures[name]["source"], dtype=np.complex64)
        received = np.fromfile(captures[name]["received"], dtype=np.complex64)
        require(len(source) == len(received) == sum(frames[name]), "independent capture count differs")
        desired, tone, masked, observable = independent_components(plan, name, source, arms[name])
        signal_validation.compare_waveforms((desired.astype(np.complex128) + tone).astype(np.complex64)[observable], received[observable])
        noise = received.astype(np.complex128) - desired.astype(np.complex128) - tone.astype(np.complex128)
        core_calls = expected_core_calls(plan, name, arms[name], frames[name], ready["backend"])
        previous, cursor = None, 0
        for window_index, record in enumerate(windows):
            window = record["details"]
            quantiles = validate_window(window, every=every, previous=previous, final_partial=window_index == len(windows) - 1)
            require(record["sample_start"] == window["processed_sample_start"] and
                    record["sample_end"] == window["processed_sample_end"] and record["monotonic_ns"] >= window["end_ns"],
                    "metric envelope range/time differs")
            count = window["input_messages"]
            frame_slice = frames[name][cursor:cursor + count]
            require(len(frame_slice) == count and window["input_messages"] == window["output_messages"] and
                    window["input_samples"] == window["output_samples"] == window["completed_processing_samples"] and
                    window["input_sample_end"] == window["processed_sample_end"] == window["output_sample_end"],
                    "valid fixture window counts do not reconcile")
            require(window["message_samples_histogram"] == histogram(frame_slice), "message histogram differs from peers")
            require(window["dsp_core"]["completed"][0] == sum(core_calls[cursor:cursor + count]),
                    "DSP core segment count differs from independent boundaries")
            require(window["rejected_messages"] == 0, "valid fixture reports IQ rejection")
            for part in PARTS:
                require(window["processing_parts"][part]["completed"][0] == count, "processing part count differs")
                require(all(window["processing_parts"][part][outcome] == [0, 0, 0] for outcome in ("stopped", "error")),
                        "valid fixture reports stopped/error processing part")
            require(all(window["dsp_core"][outcome] == [0, 0, 0] for outcome in ("stopped", "error")),
                    "valid fixture reports stopped/error DSP core")
            last_window = window_index == len(windows) - 1
            stalled_upstream = last_window and case == "upstream-delay-stall-stop" and name == "DL"
            for phase in PHASES:
                require(window["phases"][phase]["error"] == [0, 0, 0], "valid fixture reports phase error")
                completed = window["phases"][phase]["completed"][0]
                expected_completed = count + (1 if stalled_upstream and phase in ("request_receive", "request_send") else 0)
                require(completed == expected_completed, "completed phase count differs from known peer requests")
                expected_stopped = (1,) if stalled_upstream and phase == "upstream_receive" else (
                    (0, 1) if last_window and not stalled_upstream and phase == "request_receive" else (0,))
                require(window["phases"][phase]["stopped"][0] in expected_stopped,
                        "stopped phase differs from known outstanding request")
            withheld_ns = 0
            for observation in delays:
                if observation["direction"] != name or observation["kind"] not in (
                        "upstream_reply_withheld", "post_resource_endpoint_upstream_stall"):
                    continue
                index = ((2 + observation["round"]) // every if observation["kind"] == "upstream_reply_withheld"
                         else len(frames[name]) // every)
                if index == window_index:
                    require(control.uint(observation["start_ns"]) and control.uint(observation["end_ns"]) and
                            observation["end_ns"] - observation["start_ns"] >= observation["requested_ns"] > 0,
                            "introduced peer delay observation invalid")
                    withheld_ns += observation["end_ns"] - observation["start_ns"]
            upstream_exchange_ns = sum(window["phases"][phase][outcome][1]
                                       for phase in ("request_send", "upstream_receive") for outcome in OUTCOMES)
            require(upstream_exchange_ns >= withheld_ns, "upstream phase union omits independently withheld duration")
            left, right = window["processed_sample_start"], window["processed_sample_end"]
            require(window["intentional_mask_samples"] == int(np.count_nonzero(masked[left:right])), "desired mask count differs")
            for key, data in (("input", source), ("desired", desired), ("noise", noise), ("cw", tone), ("output", received)):
                segment = data[left:right].astype(np.complex128)
                expected_energy = float(np.sum(segment.real ** 2 + segment.imag ** 2))
                require(math.isclose(window["energies"][key], expected_energy, rel_tol=1e-5, abs_tol=1e-8),
                        f"independent {name} window{window['window_id']} {key} energy differs")
            rows.append({"direction": name, "window_id": window["window_id"], "final_partial": window["final_partial"],
                         "input_messages": count, "output_samples": window["output_samples"], "wall_ns": window["wall_ns"],
                         "processing_count": window["phases"]["processing"]["completed"][0],
                         "processing_sum_ns": window["phases"]["processing"]["completed"][1],
                         "processing_max_ns": window["phases"]["processing"]["completed"][2],
                         "independently_withheld_ns": withheld_ns, "upstream_exchange_elapsed_ns": upstream_exchange_ns,
                         **ratios(window, plan.sample_rate_hz), **quantiles})
            cursor += count
            previous = window
        require(cursor == len(frames[name]), "metric windows omit/repeat peer frames")
        final = own[-1]
        require(final["monotonic_ns"] >= windows[-1]["details"]["end_ns"], "metric final clock precedes last window")
        expected = {"window_count": len(windows), "input_messages": len(frames[name]), "output_messages": len(frames[name]),
                    "input_samples": len(source), "output_samples": len(source), "processed_samples": len(source),
                    "completed_processing_samples": len(source), "status": "complete", "logging_errors": 0}
        require(final["details"] == expected and all(type(final["details"][key]) is type(value) for key, value in expected.items()),
                "metric final reconciliation differs")
        require(final["sample_start"] == 0 and final["sample_end"] == len(source), "metric final sample range differs")
        truth_final = next(row for row in truth if row["direction"] == name and row["event_type"] == "final")
        for field in ("input_messages", "output_messages", "input_samples", "output_samples", "processed_samples"):
            require(final["details"][field] == truth_final["details"][field], "metrics/truth final count differs")
        summaries[name] = expected
    return {"window_measurements": rows, "metrics_final": summaries}


def read_metrics(path):
    raw = path.read_bytes()
    require(0 < len(raw) <= 16 * 1024 * 1024 and raw.endswith(b"\n"), "missing/truncated/big metrics ledger")
    lines = raw.splitlines()
    require(len(lines) <= 515 and all(0 < len(line) + 1 <= 16384 for line in lines), "metric ledger bounds")
    return [control.decode_json(line) for line in lines]


def fixture_inputs(case):
    fixture = json.loads(FIXTURE.read_text())
    require(tuple(fixture["cases"]) == CASES and fixture["master_seed"] == 41 and fixture["every_messages"] == 4,
            "prospective fixture differs")
    choice = fixture["cases"][case]
    require(sum(choice["frame_samples"]) <= 65537 and len(choice["frame_samples"]) <= 64,
            "fixture sample/message bound")
    profile, schedule = signal_validation.case_inputs("identity" if choice["identity"] else "piecewise")
    schedule["trial_id"] = case
    schedule["protocol_id"] = "rad08_metrics_development_v1"
    return choice, profile, schedules.validate_schedule(profile, schedule)


def payloads_for(choice):
    import numpy as np

    source = np.resize(np.asarray([1, 1j, -1, -1j], dtype=np.complex64), sum(choice["frame_samples"]))
    source[300:620] = 0
    cursor, payloads = 0, []
    for size in choice["frame_samples"]:
        payloads.append(source[cursor:cursor + size].tobytes())
        cursor += size
    return payloads


def build_hash(backend, command):
    if backend == "c":
        return transport.digest(command[0])
    names = ("ocudu_channel_broker.py", "radio_broker_metrics.py", "radio_broker_profile.py",
             "radio_broker_schedule.py", "radio_schedule_runtime.py", "radio_static_tdl.py")
    return hashlib.sha256("".join(name + " " + transport.digest(ROOT / "scripts" / name) + "\n"
                                  for name in names).encode()).hexdigest()


def run_case(backend, command, case, output):
    choice, profile, schedule = fixture_inputs(case)
    payloads = payloads_for(choice)  # Prepared before measured resource interval.
    expected_build = build_hash(backend, command)
    state = {"frames": {"DL": [], "UL": []}, "arms": {"DL": None, "UL": None},
             "responses": [], "resource_snapshots": [], "delays": [], "round": None,
             "first_upstream_send_ns": {}}

    def setup(private, case_dir):
        directory = private / "control"
        directory.mkdir(mode=0o700)
        transport.write_json(case_dir / "profile.json", profile)
        transport.write_json(case_dir / "schedule.json", schedule)
        transport.write_json(case_dir / "fixture.json", choice)
        prepared = schedules.prepare(case_dir / "profile.json", case_dir / "schedule.json", directory)
        state.update(directory=directory, plan=schedules.load_wire(directory / "plan.wire"))
        arguments = prepared["broker_arguments"][backend]
        if choice["metrics"]:
            arguments += ["--radio-metrics-every-messages", "4"]
        return arguments

    def verify_exchange(name, sent, received):
        import numpy as np

        require(len(received) == 1 and len(received[0]) == len(sent) and
                np.isfinite(np.frombuffer(received[0], dtype=np.complex64)).all(), "metric replay malformed output")
        if choice["identity"]:
            require(received == [sent], "identity metrics changed bytes")
        state["frames"][name].append(len(sent) // 8)

    def delay(kind, milliseconds, direction=None):
        begin, cpu_begin = time.monotonic_ns(), time.process_time_ns()
        deadline = begin + milliseconds * 1_000_000
        while True:
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                break
            time.sleep(remaining / 1e9)
        state["delays"].append({"kind": kind, "direction": direction, "round": state["round"],
                                "requested_ns": milliseconds * 1_000_000, "start_ns": begin,
                                "end_ns": time.monotonic_ns(), "harness_cpu_ns": time.process_time_ns() - cpu_begin})

    def before_upstream_send(name, payload):
        del payload
        state["first_upstream_send_ns"].setdefault(name, time.monotonic_ns())
        milliseconds = choice.get("upstream_delay_ms_by_round", {}).get(str(state["round"]), 0)
        if milliseconds and name in choice["delay_directions"]:
            delay("upstream_reply_withheld", milliseconds, name)

    def interact(exchange, request, private, case_dir, child):
        del private, case_dir
        with control.ControlClient(state["directory"], timeout=2, expected_backend=backend,
                                   expected_pid=child.pid, expected_build_sha256=expected_build) as client:
            state["ready"] = dict(client.ready)

            def ask(operation):
                require(client.sequence <= 32, "control request budget")
                response = client.request(operation)
                require(response["ok"], "unexpected metric replay control failure")
                state["responses"].append(response)
                return response

            ask("STATUS")
            for name in ("DL", "UL"):
                exchange(name, b"")
                exchange(name, signal_validation.stream(name)[0])
            before = ask("STATUS")
            require(all(before["directions"][name]["forwarded_samples"] == WARMUP[name] for name in WARMUP),
                    "resource baseline warmup counters differ")
            require(ask("ARM")["state"] == "arm_pending", "unexpected ARM state")
            require(not any(row["armed"] for row in ask("STATUS")["directions"].values()), "quiescent ARM applied")
            with resources.ProcessResources(child.pid) as sampler:
                state["resource_snapshots"].append(sampler.snapshot(dict(WARMUP)))
                for index, payload in enumerate(payloads):
                    state["round"] = index
                    milliseconds = choice.get("before_round_delay_ms", [0] * len(payloads))[index]
                    if milliseconds:
                        delay("before_paired_round", milliseconds)
                    for name in ("DL", "UL"):
                        if payload and state["arms"][name] is None:
                            state["arms"][name] = WARMUP[name]
                        exchange(name, payload)
                measured_end = {name: sum(state["frames"][name]) for name in WARMUP}
                for _ in range(10):
                    after = ask("STATUS")
                    if all(after["directions"][name]["forwarded_samples"] == measured_end[name] for name in WARMUP):
                        break
                    time.sleep(0.001)
                require(all(after["directions"][name]["forwarded_samples"] == measured_end[name] and
                            after["directions"][name]["processed_samples"] == measured_end[name] for name in WARMUP),
                        "resource endpoint differs from quiescent peer counters")
                state["resource_snapshots"].append(sampler.snapshot(measured_end))
            state["resource_interval"] = resources.derive_interval(*state["resource_snapshots"])
            if "post_resource_endpoint_stall" in choice:
                specification = choice["post_resource_endpoint_stall"]
                request(specification["direction"])
                state["round"] = None
                delay("post_resource_endpoint_upstream_stall", specification["hold_ms"], specification["direction"])

    def finalize(log, selected_backend, expected, captures):
        plan = state["plan"]
        require(schedules.compile_plan(profile, schedule) == (state["directory"] / "plan.wire").read_bytes(),
                "metric replay plan binding changed")
        truth = signal_validation.read_truth(state["directory"] / "broker_events.jsonl")
        checked_truth = signal_validation.validate_truth(truth, plan=plan, ready=state["ready"], expected=expected,
                                frames=state["frames"], arms=state["arms"], case="piecewise")
        metric_path = state["directory"] / "broker_metrics.jsonl"
        if choice["metrics"]:
            checked = validate_records(read_metrics(metric_path), truth=truth, plan=plan, ready=state["ready"],
                                       frames=state["frames"], arms=state["arms"], captures=captures,
                                       first_upstream_send_ns=state["first_upstream_send_ns"],
                                       delays=state["delays"], case=case)
        else:
            require(not metric_path.exists(), "disabled metrics created a ledger")
            checked = {"window_measurements": [], "metrics_final": None}
        terminal_dsp = [json.loads(line[len("RADIO_FIXED_PROFILE: "):]) for line in log.splitlines()
                        if line.startswith("RADIO_FIXED_PROFILE: ")]
        terminal_dsp = [row for row in terminal_dsp if row["record_type"] == "final"]
        require(len(terminal_dsp) == 2 and {row["direction"] for row in terminal_dsp} == {"DL", "UL"},
                "missing terminal DSP state")
        relays = transport.validate_final_records(log, selected_backend, expected,
                            identity_by_direction={name: choice["identity"] for name in WARMUP})
        interval = state["resource_interval"]
        require(interval["forwarded_samples"] == {name: expected[name]["samples"] - WARMUP[name] for name in WARMUP},
                "resource/sample population differs")
        return {**checked, "truth_validation": checked_truth, "relay_records": relays,
                "terminal_dsp_state": terminal_dsp, "resource_interval": interval,
                "ready_identity": state["ready"], "independently_computed_build_sha256": expected_build}

    def retain(private, case_dir):
        directory = private / "control"
        token_path = directory / "control.token"
        token = token_path.read_bytes() if token_path.exists() else None
        for name in ("plan.wire", "broker_ready.json", "broker_events.jsonl", "broker_metrics.jsonl"):
            path = directory / name
            if path.exists():
                raw = path.read_bytes()
                require(len(raw) <= 16 * 1024 * 1024 and (token is None or token not in raw), "artifact size/credential failure")
                with (case_dir / name).open("xb") as handle:
                    handle.write(raw)
        transport.write_json(case_dir / "observer.json", {key: state[key] for key in
                            ("frames", "arms", "responses", "resource_snapshots", "delays", "first_upstream_send_ns")})

    return transport.run_case(backend, command, case, output, broker_arguments=[], supplied_payloads=[],
                exchange_validator=verify_exchange, final_validator=finalize, capture_raw=True,
                setup_callback=setup, interaction_callback=interact, cleanup_callback=retain,
                before_upstream_send=before_upstream_send)


def compare_instrumentation(output, backend):
    on = json.loads((output / f"{backend}-fixed-ragged-on/result.json").read_text())
    off = json.loads((output / f"{backend}-fixed-ragged-off/result.json").read_text())
    require(on["valid_source_sha256"] == off["valid_source_sha256"] and on["received_sha256"] == off["received_sha256"] and
            on["validated_exchange_counts"] == off["validated_exchange_counts"], "metrics instrumentation changed bytes/counts")
    compare_terminal_dsp(on["terminal_dsp_state"], off["terminal_dsp_state"], backend)
    return {"source_output_and_terminal_DSP_state": "exact", "metrics_on_resource_interval": on["resource_interval"],
            "metrics_off_resource_interval": off["resource_interval"],
            "interpretation": "one descriptive development pair; observed interval CPU only, deferred writer/finalization excluded; no ranking or overhead threshold"}


def compare_terminal_dsp(on_records, off_records, backend):
    """Compare state exactly while separately checking observation timestamps."""
    def checked(records):
        require(type(records) is list and len(records) == 2 and {row.get("direction") for row in records} == {"DL", "UL"},
                "terminal DSP directions differ")
        result = []
        for row in records:
            require(row.get("schema_version") == "radio_fixed_profile_v1" and row.get("record_type") == "final"
                    and row.get("backend") == backend, "terminal DSP record identity differs")
            if backend == "grc":
                require(row.get("status") == "stopped" and all(control.uint(row.get(key)) and row[key] > 0
                        for key in ("monotonic_ns", "wall_ns")), "terminal DSP observation metadata invalid")
                require(all(type(row.get(key)) is str and control.HEX32.fullmatch(row[key])
                            for key in ("awgn_state_hex", "awgn_increment_hex")) and
                        type(row.get("awgn_has_uint32")) is bool and control.uint(row.get("awgn_cached_uint32"), (1 << 32) - 1),
                        "terminal PCG state missing or invalid")
            else:
                require(not {"monotonic_ns", "wall_ns", "status"}.intersection(row),
                        "unexpected native terminal DSP observation metadata")
                require(control.uint(row.get("awgn_state"), (1 << 32) - 1), "terminal native RNG state missing or invalid")
            result.append({key: value for key, value in row.items() if key not in ("monotonic_ns", "wall_ns")})
        return sorted(result, key=lambda row: row["direction"])
    require(checked(on_records) == checked(off_records), "metrics instrumentation changed terminal DSP state")


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
    sources = ("scripts/zmq_channel_broker.c", "scripts/ocudu_channel_broker.py", "scripts/radio_schedule_native.h",
               "scripts/radio_schedule_sha256.h", "scripts/radio_metrics_native.h", "scripts/radio_broker_metrics.py",
               "scripts/radio_broker_schedule.py", "scripts/radio_schedule_runtime.py", "scripts/radio_broker_profile.py",
               "scripts/radio_broker_control.py", "scripts/radio_broker_resources.py", "scripts/validate_radio_broker_metrics.py",
               "scripts/validate_radio_broker_identity.py", "scripts/validate_radio_broker_schedule.py",
               "config/radio_broker/metrics_validation_fixture.json", "config/radio_broker/metrics_validation_protocol.yaml",
               "config/radio_broker/broker_metrics_schema.json", "docs/METRICS_CONTRACT.md",
               "config/radio_broker/fixed_reference.fixture.json", "config/radio_broker/finite_schedule.fixture.json",
               "dependencies/toolchain.lock.json", "pyproject.toml", "uv.lock")
    hashes = {name: transport.digest(ROOT / name) for name in sources}
    report = {"schema_version": "radio_broker_metrics_l2_v1", "status": "failed", "evidence_layer": "L2",
              "date_utc": datetime.now(timezone.utc).isoformat(), "scope": "finite metrics/resource accounting qualification only",
              "full_stack_attempts": 0, "campaign_attempts": 0, "source_sha256": hashes, "cases": [],
              "host": {"platform": platform.platform(), "python": sys.version}}
    try:
        import numpy
        import zmq
        report["host"].update(numpy=numpy.__version__, pyzmq=zmq.__version__, libzmq=zmq.zmq_version())
        backends = ("c", "grc") if args.backend == "both" else (args.backend,)
        commands = transport.prepare_commands(output, backends, report)
        for backend in backends:
            for case in CASES:
                result = run_case(backend, commands[backend], case, output)
                report["cases"].append({"backend": backend, "case": case, "status": result["status"],
                                       "result_sha256": transport.digest(output / f"{backend}-{case}/result.json")})
                print(f"{backend}/{case}: {result['status']}", flush=True)
                require(result["status"] == "passed", result.get("error", "metric case failed"))
            report.setdefault("instrumentation_pairs", {})[backend] = compare_instrumentation(output, backend)
        with (output / "timing_accounting_validation.csv").open("x", newline="") as handle:
            writer = None
            for backend in backends:
                for case in CASES:
                    result = json.loads((output / f"{backend}-{case}/result.json").read_text())
                    for row in result["window_measurements"]:
                        if writer is None:
                            writer = csv.DictWriter(handle, fieldnames=("backend", "case", *row))
                            writer.writeheader()
                        writer.writerow({"backend": backend, "case": case, **row})
        report["timing_csv_sha256"] = transport.digest(output / "timing_accounting_validation.csv")
        require(hashes == {name: transport.digest(ROOT / name) for name in hashes}, "source changed during metrics validation")
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    transport.write_json(output / "validation.json", report)
    print(f"{report['status']}: {output / 'validation.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
