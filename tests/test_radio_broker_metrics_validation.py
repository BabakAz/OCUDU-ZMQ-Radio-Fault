"""Independent deterministic timing/energy evidence; no broker or socket starts."""
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_schedule as schedules
import validate_radio_broker_metrics as validator
sys.path.pop(0)


def outcomes(count=0, total=0, maximum=0, outcome="completed"):
    value = {key: [0, 0, 0] for key in validator.OUTCOMES}
    value[outcome] = [count, total, maximum]
    return value


def full_window():
    return {"window_id": 1, "final_partial": False, "start_ns": 100, "end_ns": 1100,
            "wall_ns": 1000, "loop_overhead_ns": 500, "input_messages": 4, "output_messages": 4,
            "input_samples": 3, "output_samples": 3, "input_bytes": 24, "output_bytes": 24,
            "input_sample_start": 0, "input_sample_end": 3, "output_sample_start": 0, "output_sample_end": 3,
            "processed_sample_start": 0, "processed_sample_end": 3, "completed_processing_samples": 3,
            "energies": {"input": 3., "desired": 3., "noise": 0., "cw": 0., "output": 3.},
            "phases": {key: outcomes(4, 100, 40) for key in validator.PHASES},
            "processing_parts": {"input_prepare": outcomes(4, 30, 10), "channel_chain": outcomes(4, 40, 20),
                                 "output_prepare": outcomes(4, 30, 10)},
            "dsp_core": outcomes(2, 20, 10), "message_samples_histogram": [[0, 2], [1, 1], [2, 1]],
            "processing_ns_histogram": [[4, 1], [5, 2], [6, 1]],
            "intentional_mask_samples": 0, "rejected_messages": 0}


def empty_partial():
    value = copy.deepcopy(full_window())
    value.update(window_id=2, final_partial=True, start_ns=1100, end_ns=2100, loop_overhead_ns=100,
                 input_messages=0, output_messages=0, input_samples=0, output_samples=0,
                 input_bytes=0, output_bytes=0, input_sample_start=3, output_sample_start=3,
                 processed_sample_start=3, completed_processing_samples=0,
                 energies={key: 0. for key in validator.ENERGIES},
                 phases={key: outcomes() for key in validator.PHASES},
                 processing_parts={key: outcomes() for key in validator.PARTS},
                 dsp_core=outcomes(), message_samples_histogram=[], processing_ns_histogram=[])
    value["phases"]["request_receive"] = outcomes(1, 900, 900, "stopped")
    return value


def evidence(tmp_path, backend="c"):
    profile, program = validator.signal_validation.case_inputs("identity")
    plan = schedules.parse_wire(schedules.compile_plan(profile, program))
    ready = {"instance_id": "a" * 32, "backend": backend, "build_sha256": "b" * 64,
             "config_sha256": plan.profile_sha256, "plan_sha256": plan.sha256, "pid": 123}
    records = []
    def add(name, kind, clock, start, end, details):
        records.append({"schema_version": "radio_broker_metrics_v1", **{key: ready[key] for key in ready if key != "pid"},
                        **{key: getattr(plan, key) for key in schedules.ID_FIELDS},
                        "metrics_config_sha256": validator.configuration_hash(4), "direction": name,
                        "event_sequence": len(records) + 1, "event_type": kind, "monotonic_ns": clock,
                        "wall_ns": clock, "sample_start": start, "sample_end": end,
                        "scope": "control" if name == "control" else "direction", "details": details})
    add("control", "started", 100, None, None, {"every_messages": 4, "max_windows": 256,
        "histogram": "uint64_bit_length", "sample_rate_hz": plan.sample_rate_hz, "pid": 123})
    for name in ("DL", "UL"):
        value = full_window()
        if backend == "grc":
            value["dsp_core"] = outcomes(4, 20, 5)
        add(name, "window", 1200, 0, 3, value)
    for name in ("DL", "UL"):
        add(name, "window", 2200, 3, 3, empty_partial())
    final = {"window_count": 2, "input_messages": 4, "output_messages": 4, "input_samples": 3,
             "output_samples": 3, "processed_samples": 3, "completed_processing_samples": 3,
             "status": "complete", "logging_errors": 0}
    for name in ("DL", "UL"):
        add(name, "final", 2300, 0, 3, dict(final))
    captures = {}
    for name in ("DL", "UL"):
        captures[name] = {key: tmp_path / f"{name}-{key}.cf32" for key in ("source", "received")}
        for path in captures[name].values():
            path.write_bytes(np.asarray([1, 1j, -1], dtype=np.complex64).tobytes())
    return records, {"truth": [{"direction": name, "event_type": "final", "details": dict(final)} for name in ("DL", "UL")],
                     "plan": plan, "ready": ready, "frames": {"DL": [0, 1, 2, 0], "UL": [0, 1, 2, 0]},
                     "arms": {"DL": None, "UL": None}, "captures": captures,
                     "first_upstream_send_ns": {"DL": 125, "UL": 130}}


@pytest.mark.parametrize("backend", ["c", "grc"])
def test_independent_complete_windows_empty_final_and_actual_core_populations(tmp_path, backend):
    records, options = evidence(tmp_path, backend)
    result = validator.validate_records(records, **options)
    assert len(result["window_measurements"]) == 4
    assert result["window_measurements"][1]["rtf"] == 0
    assert result["window_measurements"][1]["processing_budget_ratio"] is None


def test_deterministic_rtf_can_be_below_or_above_one_without_clipping():
    window = full_window()
    assert validator.ratios(window, 1_000_000)["rtf"] == 3
    assert validator.ratios(window, 10_000_000)["rtf"] == 0.3
    assert validator.ratios(window, 1_000_000)["processing_budget_ratio"] == pytest.approx(1 / 30)


@pytest.mark.parametrize("mutate", [
    lambda w: w.update(start_ns=101), lambda w: w.update(end_ns=100),
    lambda w: w.update(loop_overhead_ns=501), lambda w: w.update(input_bytes=25),
    lambda w: w.update(output_sample_end=2), lambda w: w.update(processed_sample_end=2),
    lambda w: w.update(input_sample_start=1), lambda w: w.update(completed_processing_samples=4),
    lambda w: w.update(intentional_mask_samples=4), lambda w: w.update(rejected_messages=True),
    lambda w: w.update(input_messages=True), lambda w: w.update(final_partial=True),
    lambda w: w["energies"].update(noise=float("nan")), lambda w: w["energies"].update(input=-1),
    lambda w: w["processing_parts"]["input_prepare"].update(completed=[4, 31, 10]),
    lambda w: w.update(dsp_core=outcomes(2, 50, 25)),
    lambda w: w["phases"]["request_send"].update(completed=[4, 100, 20]),
    lambda w: w["phases"]["request_send"].update(stopped=[0, 1, 1]),
    lambda w: w.update(message_samples_histogram=[[0, 2], [1, 1], [2, 2]]),
    lambda w: w.update(processing_ns_histogram=[[4, 2], [5, 2]]),
])
def test_forged_window_counts_timing_frontiers_histograms_and_energies_fail(mutate):
    window = full_window()
    mutate(window)
    with pytest.raises(validator.transport.ValidationError):
        validator.validate_window(window, every=4, previous=None, final_partial=False)


@pytest.mark.parametrize("field,value", [("window_id", 3), ("start_ns", 1101), ("output_sample_start", 2),
                                         ("processed_sample_start", 0), ("final_partial", False)])
def test_final_partial_is_unique_contiguous_and_not_double_counted(field, value):
    window = empty_partial()
    window[field] = value
    with pytest.raises(validator.transport.ValidationError):
        validator.validate_window(window, every=4, previous=full_window(), final_partial=True)


@pytest.mark.parametrize("mutate", [
    lambda r: r[0].update(metrics_config_sha256="0" * 64),
    lambda r: r[1].update(scope="processed_samples"),
    lambda r: r[1].update(event_sequence=3),
    lambda r: r[2].update(monotonic_ns=1199),
    lambda r: r[1].update(sample_end=4),
    lambda r: r[-1].update(monotonic_ns=2099),
    lambda r: r[-1]["details"].update(window_count=1),
    lambda r: r[-1]["details"].update(status="incomplete"),
    lambda r: r[1]["details"]["energies"].update(input=4),
    lambda r: r[1]["details"].update(intentional_mask_samples=1),
    lambda r: r[1]["details"].update(rejected_messages=1),
    lambda r: r[1]["details"].update(dsp_core=outcomes(3, 20, 10)),
])
def test_independent_record_envelope_state_and_energy_forgeries_fail(tmp_path, mutate):
    records, options = evidence(tmp_path)
    mutate(records)
    with pytest.raises(validator.transport.ValidationError):
        validator.validate_records(records, **options)


def test_old_first_completion_clock_boundary_fails_independent_peer_anchor(tmp_path):
    records, options = evidence(tmp_path)
    options["first_upstream_send_ns"]["DL"] = 99
    with pytest.raises(validator.transport.ValidationError, match="starts after measured work"):
        validator.validate_records(records, **options)


def test_phantom_completed_requests_are_rejected_in_quiescent_fixture(tmp_path):
    records, options = evidence(tmp_path)
    records[3]["details"]["phases"]["request_receive"]["completed"] = [1, 0, 0]
    with pytest.raises(validator.transport.ValidationError, match="known peer requests"):
        validator.validate_records(records, **options)


def test_clean_stop_before_next_receive_opens_is_valid_idle_loop_race(tmp_path):
    records, options = evidence(tmp_path)
    for row in records:
        if row["event_type"] == "window" and row["details"]["final_partial"]:
            row["details"]["phases"]["request_receive"] = outcomes()
            row["details"]["loop_overhead_ns"] = 1000
    validator.validate_records(records, **options)


def test_request_send_and_upstream_receive_union_covers_observed_peer_withholding(tmp_path):
    records, options = evidence(tmp_path)
    options["delays"] = [{"kind": "upstream_reply_withheld", "direction": "DL", "round": 0,
                           "requested_ns": 150, "start_ns": 200, "end_ns": 350}]
    # Each of the two phases is100ns; either phase alone is insufficient.
    result = validator.validate_records(records, **options)
    assert result["window_measurements"][0]["independently_withheld_ns"] == 150
    assert result["window_measurements"][0]["upstream_exchange_elapsed_ns"] == 200
    options["delays"][0]["end_ns"] = 401
    with pytest.raises(validator.transport.ValidationError, match="withheld duration"):
        validator.validate_records(records, **options)


def test_stalled_upstream_has_known_extra_requests_and_one_stopped_receive(tmp_path):
    records, options = evidence(tmp_path)
    options["case"] = "upstream-delay-stall-stop"
    final_partial = records[3]["details"]
    final_partial["phases"]["request_receive"] = outcomes(1, 50, 50)
    final_partial["phases"]["request_send"] = outcomes(1, 100, 100)
    final_partial["phases"]["upstream_receive"] = outcomes(1, 750, 750, "stopped")
    options["delays"] = [{"kind": "post_resource_endpoint_upstream_stall", "direction": "DL", "round": None,
                           "requested_ns": 800, "start_ns": 1200, "end_ns": 2000}]
    validator.validate_records(records, **options)
    final_partial["phases"]["upstream_receive"] = outcomes()
    final_partial["loop_overhead_ns"] = 850
    with pytest.raises(validator.transport.ValidationError, match="stopped phase"):
        validator.validate_records(records, **options)


def test_missing_and_duplicate_final_window_fail_even_if_renumbered(tmp_path):
    for position in (3, 5):
        records, options = evidence(tmp_path)
        records.pop(position)
        for index, row in enumerate(records, 1):
            row["event_sequence"] = index
        with pytest.raises(validator.transport.ValidationError):
            validator.validate_records(records, **options)


def test_unsent_completed_processing_is_in_elapsed_budget_but_not_output_rtf():
    window = full_window()
    window.update(final_partial=True, input_messages=1, output_messages=0, input_samples=8, output_samples=0,
                  input_bytes=64, output_bytes=0, input_sample_end=8, output_sample_end=0,
                  processed_sample_end=8, completed_processing_samples=8, loop_overhead_ns=400,
                  phases={key: outcomes(1, 100, 100) for key in validator.PHASES},
                  processing_parts={"input_prepare": outcomes(1, 30, 30), "channel_chain": outcomes(1, 40, 40),
                                    "output_prepare": outcomes(1, 30, 30)}, dsp_core=outcomes(1, 20, 20),
                  message_samples_histogram=[[4, 1]], processing_ns_histogram=[[7, 1]])
    window["phases"]["downstream_send"] = outcomes(1, 200, 200, "stopped")
    validator.validate_window(window, every=4, previous=None, final_partial=True)
    ratio = validator.ratios(window, 1_000_000)
    assert ratio["rtf"] == 0 and ratio["processing_budget_ratio"] == 0.0125


def test_partially_processed_failure_has_no_completed_processing_sample_denominator():
    window = full_window()
    window.update(final_partial=True, input_messages=1, output_messages=0, input_samples=8, output_samples=0,
                  input_bytes=64, output_bytes=0, input_sample_end=8, output_sample_end=0,
                  processed_sample_end=4, completed_processing_samples=0, loop_overhead_ns=630,
                  phases={key: outcomes(1, 100, 100) for key in validator.PHASES},
                  processing_parts={"input_prepare": outcomes(1, 30, 30, "error"),
                                    "channel_chain": outcomes(1, 40, 40, "error"), "output_prepare": outcomes()},
                  dsp_core=outcomes(1, 20, 20), message_samples_histogram=[[4, 1]], processing_ns_histogram=[])
    window["phases"]["processing"] = outcomes(1, 70, 70, "error")
    window["phases"]["downstream_send"] = outcomes()
    validator.validate_window(window, every=4, previous=None, final_partial=True)
    assert validator.ratios(window, 1_000_000)["processing_budget_ratio"] is None


def test_histogram_extremes_and_quantiles_are_intervals_not_exact_percentiles():
    rows = validator.histogram([0, 1, 2, 3, (1 << 64) - 1])
    assert rows == [[0, 1], [1, 1], [2, 2], [64, 1]]
    result = validator.histogram_bounds(rows, count=5)
    assert result["p99_ns_bounds"] == [1 << 63, (1 << 64) - 1]
    for bad in ([[2, 1], [1, 1]], [[1, 0]], [[1.0, 1]], [[65, 1]], [[1, True]]):
        with pytest.raises(validator.transport.ValidationError):
            validator.histogram_bounds(bad, count=1)


def test_fixture_is_frozen_bounded_and_measured_pair_partitions_match():
    fixture = json.loads(validator.FIXTURE.read_text())
    assert tuple(fixture["cases"]) == validator.CASES
    for case in validator.CASES:
        choice, profile, schedule = validator.fixture_inputs(case)
        assert sum(choice["frame_samples"]) <= 65537 and profile["master_seed"] == 41
        assert schedule["protocol_id"] == "rad08_metrics_development_v1"
        assert sum(len(raw) // 8 for raw in validator.payloads_for(choice)) == sum(choice["frame_samples"])
    assert fixture["cases"]["fixed-ragged-on"]["frame_samples"] == fixture["cases"]["fixed-ragged-off"]["frame_samples"]


def terminal_pair():
    on = [{"schema_version": "radio_fixed_profile_v1", "record_type": "final", "backend": "grc",
           "direction": name, "sample_clock": 65537, "phase_u64": 123, "awgn_state_hex": "a" * 32,
           "awgn_increment_hex": "b" * 32, "awgn_has_uint32": False, "awgn_cached_uint32": 0,
           "monotonic_ns": 1000, "wall_ns": 1000000, "status": "stopped"} for name in ("DL", "UL")]
    off = copy.deepcopy(on)
    for row in off:
        row["monotonic_ns"] += 123456
        row["wall_ns"] += 456789
    return on, off


def test_terminal_state_comparison_preserves_records_and_separates_observation_clocks():
    on, off = terminal_pair()
    saved = copy.deepcopy((on, off))
    validator.compare_terminal_dsp(on, off, "grc")
    assert (on, off) == saved


@pytest.mark.parametrize("field,value", [("awgn_state_hex", "b" * 32), ("phase_u64", 124), ("sample_clock", 65538),
                                         ("monotonic_ns", True), ("wall_ns", 0), ("status", "error")])
def test_changed_terminal_rng_phase_counter_or_invalid_metadata_rejected(field, value):
    on, off = terminal_pair()
    off[0][field] = value
    with pytest.raises(validator.transport.ValidationError):
        validator.compare_terminal_dsp(on, off, "grc")


def test_native_rng_state_must_be_present_and_exact_even_with_identical_capture_counts(tmp_path):
    states = [{"schema_version": "radio_fixed_profile_v1", "record_type": "final", "backend": "c",
               "direction": name, "sample_clock": 65537, "phase_u64": 123, "awgn_state": 24680}
              for name in ("DL", "UL")]
    result = {"valid_source_sha256": {"DL": "source", "UL": "source"},
              "received_sha256": {"DL": "output", "UL": "output"},
              "validated_exchange_counts": {"DL": {"samples": 65537}, "UL": {"samples": 65537}},
              "terminal_dsp_state": states, "resource_interval": {}}
    for label in ("on", "off"):
        directory = tmp_path / f"c-fixed-ragged-{label}"
        directory.mkdir()
        (directory / "result.json").write_text(json.dumps(result))
    validator.compare_instrumentation(tmp_path, "c")
    for value in (24681, None):
        changed = copy.deepcopy(result)
        if value is None:
            changed["terminal_dsp_state"][0].pop("awgn_state")
        else:
            changed["terminal_dsp_state"][0]["awgn_state"] = value
        (tmp_path / "c-fixed-ragged-off/result.json").write_text(json.dumps(changed))
        with pytest.raises(validator.transport.ValidationError):
            validator.compare_instrumentation(tmp_path, "c")


def test_local_process_execution_is_explicit_and_outside_offline_tests(tmp_path):
    with pytest.raises(SystemExit) as caught:
        validator.main(["--output", str(tmp_path / "unused")])
    assert caught.value.code == 2 and not (tmp_path / "unused").exists()
