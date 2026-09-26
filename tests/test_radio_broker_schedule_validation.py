"""Independent offline oracle forgeries; no processes/sockets or live campaign."""
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_schedule as schedules
import validate_radio_broker_schedule as validator
sys.path.pop(0)


def plan_for(case="piecewise"):
    profile, schedule = validator.case_inputs(case)
    return schedules.parse_wire(schedules.compile_plan(profile, schedule))


def evidence():
    """Hand-derived fixture boundaries and send counters, independent of validator helpers."""
    plan = plan_for()
    ready = {"instance_id": "a" * 32, "backend": "c", "build_sha256": "b" * 64,
             "config_sha256": plan.profile_sha256, "plan_sha256": plan.sha256}
    records = []
    request = {"request_sequence": 3, "request_wall_ns": 1000, "request_monotonic_ns": 1000}

    def add(direction, kind, start=None, end=None, details=None, scope="control"):
        clock = 1000 if kind == "arm_requested" else (100 if kind in ("started", "ready") else 2000 + len(records))
        records.append({"schema_version": "radio_broker_truth_v1", **ready,
                        **{key: getattr(plan, key) for key in schedules.ID_FIELDS},
                        "event_sequence": len(records) + 1, "event_type": kind, "direction": direction,
                        "monotonic_ns": clock, "wall_ns": clock, "sample_start": start,
                        "sample_end": end, "scope": scope, "details": details or {}})

    def progress(direction, arm, samples, messages, complete):
        add(direction, "progress", arm, samples,
            {"input_messages": messages, "output_messages": messages, "input_samples": samples,
             "output_samples": samples, "scheduled_end_sample": arm + 4097, "schedule_complete": complete},
            "successfully_forwarded_samples")

    add("control", "started")
    add("control", "ready")
    add("control", "arm_requested", details=request)
    ranges = {"DL": [(11, 28), (28, 268), (268, 1035), (1035, 1548), (1548, 2059), (2059, 5014)],
              "UL": [(19, 1556), (1556, 2067), (2067, 5022)]}
    for direction, arm in (("DL", 11), ("UL", 19)):
        add(direction, "armed", arm, arm,
            {**request, "arm_sample": arm, "duration_samples": 4097,
             "state_at_arm": {"sample_clock": arm, "awgn_complex_draws": arm,
                              "awgn_normal_draws": 2 * arm,
                              "phase_u64": (11 * (1 << 60)) % (1 << 64) if direction == "DL" else 0}},
            "processed_samples")
        previous = schedules.mutable_settings(plan.directions[direction].base)
        for index, (event, (start, end)) in enumerate(zip(plan.directions[direction].events, ranges[direction])):
            add(direction, "condition_restored" if event.kind == "restore" else "condition_applied", start, end,
                {"event_id": event.event_id, "kind": event.kind, "sample_offset": event.sample_offset,
                 "settings": dict(event.settings), "changed": previous != event.settings,
                 "processed_samples": end}, "processed_samples")
            previous = dict(event.settings)
            if direction == "DL" and index == 0:
                progress("DL", 11, 28, 4, False)
        progress(direction, arm, 5014 if direction == "DL" else 5022, 6 if direction == "DL" else 4, True)
    expected = {"DL": {"messages": 6, "samples": 5014}, "UL": {"messages": 4, "samples": 5022}}
    for direction, arm in (("DL", 11), ("UL", 19)):
        counts = expected[direction]
        add(direction, "final", 0, counts["samples"],
            {"status": "complete", "reason": "none", "armed_sample": arm,
             "scheduled_end_sample": arm + 4097, "processed_samples": counts["samples"],
             "forwarded_samples": counts["samples"], "input_messages": counts["messages"],
             "output_messages": counts["messages"], "input_samples": counts["samples"],
             "output_samples": counts["samples"], "logging_errors": 0, "all_events_processed": True,
             "restoration_observed": True, "schedule_complete": True}, "processed_samples")
    return records, {"plan": plan, "ready": ready, "expected": expected,
                     "frames": {"DL": [0, 11, 0, 17, 0, 4986], "UL": [0, 19, 0, 5003]},
                     "arms": {"DL": 11, "UL": 19}, "case": "piecewise"}


def test_hand_derived_exact_boundaries_counts_and_truth_pass():
    records, options = evidence()
    result = validator.validate_truth(records, **options)
    assert result["DL"]["condition_events"] == 6 and result["UL"]["progress_events"] == 1


@pytest.mark.parametrize("kind,field,value", [
    ("condition_applied", "sample_start", 12),
    ("condition_applied", "sample_end", 29),
    ("condition_applied", "scope", "successfully_forwarded_samples"),
    ("armed", "sample_start", 0),
    ("armed", "scope", "successfully_forwarded_samples"),
    ("progress", "sample_end", 29),
    ("progress", "scope", "processed_samples"),
    ("final", "sample_end", 5013),
    ("final", "scope", "successfully_forwarded_samples"),
])
def test_forged_observed_ranges_or_scopes_fail(kind, field, value):
    records, options = evidence()
    next(r for r in records if r["event_type"] == kind)[field] = value
    with pytest.raises(validator.transport.ValidationError):
        validator.validate_truth(records, **options)


@pytest.mark.parametrize("kind,field,value", [
    ("condition_applied", "processed_samples", 29),
    ("condition_applied", "changed", True),
    ("condition_applied", "sample_offset", 1),
    ("condition_applied", "sample_offset", False),
    ("armed", "request_sequence", 2),
    ("armed", "request_wall_ns", 999),
    ("armed", "request_monotonic_ns", 999),
    ("progress", "input_messages", 3),
    ("progress", "schedule_complete", True),
    ("final", "logging_errors", 1),
    ("final", "logging_errors", False),
    ("final", "output_messages", 5),
    ("final", "schedule_complete", False),
    ("final", "restoration_observed", False),
])
def test_forged_event_details_fail(kind, field, value):
    records, options = evidence()
    next(r for r in records if r["event_type"] == kind)["details"][field] = value
    with pytest.raises(validator.transport.ValidationError):
        validator.validate_truth(records, **options)


@pytest.mark.parametrize("kind", ["progress", "condition_applied", "armed", "final"])
def test_missing_or_duplicate_records_fail_even_with_renumbered_ledger(kind):
    for duplicate in (False, True):
        records, options = evidence()
        position = next(i for i, row in enumerate(records) if row["event_type"] == kind)
        if duplicate:
            records.insert(position, copy.deepcopy(records[position]))
        else:
            records.pop(position)
        for sequence, row in enumerate(records, 1):
            row["event_sequence"] = sequence
        with pytest.raises(validator.transport.ValidationError):
            validator.validate_truth(records, **options)


def test_message_end_and_empty_cannot_apply_next_condition():
    plan = plan_for()
    ranges = validator.expected_segments(plan, "DL", 11, [11, 17, 0])
    assert [(event.sample_offset, left, right) for event, left, right in ranges] == [(0, 11, 28)]
    ranges = validator.expected_segments(plan, "DL", 11, [11, 17, 0, 1])
    assert [(event.sample_offset, left, right) for event, left, right in ranges] == [(0, 11, 28), (17, 28, 29)]


def test_gain_and_cw_oracle_observes_silence_mask_and_warmup_phase():
    plan = plan_for()
    warmup, body = validator.stream("DL")
    source = np.frombuffer(warmup + body, dtype=np.complex64)
    expected, observable = validator.independent_waveform(plan, "DL", source, 11)
    absolute = np.arange(len(source))
    known_tone = np.sqrt(0.1) * np.exp(2j * np.pi * (absolute % 16) / 16)
    assert np.array_equal(np.flatnonzero(observable), np.arange(268, 2059))
    np.testing.assert_allclose(expected[1035:2059], known_tone[1035:2059], atol=1e-7, rtol=0)
    np.testing.assert_allclose(expected[311:631], known_tone[311:631], atol=1e-7, rtol=0)
    altered = expected[observable].copy()
    altered[0] += 0.001
    with pytest.raises(validator.transport.ValidationError):
        validator.compare_waveforms(expected[observable], altered)


def test_frozen_cases_are_exactly_seventeen_and_plan_variants_are_bound():
    assert len(validator.CASES) == 17 and len(set(validator.CASES)) == 17
    for case in validator.CASES:
        plan = plan_for(case)
        assert plan.master_seed == 41 and plan.directions["DL"].duration_samples == 4097
    assert len(plan_for("piecewise-no-noop").directions["DL"].events) == 5
    assert plan_for("identity").directions["DL"].base["mode"] == "identity"


def test_validator_main_requires_explicit_local_execution(tmp_path):
    with pytest.raises(SystemExit) as caught:
        validator.main(["--output", str(tmp_path / "unused")])
    assert caught.value.code == 2 and not (tmp_path / "unused").exists()
