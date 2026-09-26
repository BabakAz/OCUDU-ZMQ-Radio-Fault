"""Deterministic L0/L1 native metric conservation; no sockets or NR stack."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_profile as profile_module
import radio_broker_schedule as schedule_module

PROFILE = json.loads((ROOT / "config/radio_broker/fixed_reference.fixture.json").read_text())
PLAN = json.loads((ROOT / "config/radio_broker/finite_schedule.fixture.json").read_text())


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    folder = tmp_path_factory.mktemp("native-metrics")
    programs = {}
    for name, source in [("harness", "tests/cpp/radio_broker_c_metrics_harness.c"),
                         ("cli", "scripts/zmq_channel_broker.c"),
                         ("transport", "tests/cpp/radio_broker_c_metrics_transport_harness.c")]:
        output = folder / name
        subprocess.run(["clang-18", "-std=c17", "-O2", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
                        str(ROOT / source), "-o", str(output), "-lzmq", "-lm", "-pthread"],
                       check=True, capture_output=True, text=True, timeout=30)
        programs[name] = output
    return programs


def run(native, mode, *args):
    result = subprocess.run([str(native["harness"]), mode, *map(str, args)], check=False,
                            capture_output=True, text=True, timeout=10,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    if directory := os.environ.get("RADIO_C_METRICS_L1_RESULTS"):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / ("accumulator-" + mode + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (
        f"native metrics harness {mode!r} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    records = [json.loads(line.split(": ", 1)[1]) for line in result.stdout.splitlines()
               if line.startswith("METRIC: ")]
    return records, result


def populations(window):
    detail = window["details"]
    wall = detail["wall_ns"]
    assert wall > 0 and wall == detail["end_ns"] - detail["start_ns"]
    assert sum(v[1] for p in detail["phases"].values() for v in p.values()) + detail["loop_overhead_ns"] == wall
    for outcome in ["completed", "stopped", "error"]:
        assert sum(p[outcome][1] for p in detail["processing_parts"].values()) == detail["phases"]["processing"][outcome][1]
    assert sum(v[1] for v in detail["dsp_core"].values()) <= sum(v[1] for v in detail["processing_parts"]["channel_chain"].values())
    assert sum(count for _, count in detail["message_samples_histogram"]) == detail["input_messages"]
    assert sum(count for _, count in detail["processing_ns_histogram"]) == detail["phases"]["processing"]["completed"][0]
    assert detail["input_bytes"] == 8 * detail["input_samples"]
    assert detail["output_bytes"] == 8 * detail["output_samples"]
    assert window["sample_start"] == detail["processed_sample_start"]
    assert window["sample_end"] == detail["processed_sample_end"]


@pytest.mark.parametrize("mode", ["normal", "partial", "stall", "slow", "unsent", "processing-error"])
def test_disjoint_and_nested_conservation(native, mode):
    records, _ = run(native, mode)
    windows = [r for r in records if r["event_type"] == "window"]
    assert len(windows) >= 1
    for window in windows:
        populations(window)
    assert [w["details"]["window_id"] for w in windows] == list(range(1, len(windows) + 1))
    assert sum(w["details"]["final_partial"] for w in windows) == 1
    for left, right in zip(windows, windows[1:]):
        assert left["details"]["end_ns"] == right["details"]["start_ns"]
    final = records[-1]["details"]
    for name in ["input_messages", "output_messages", "input_samples", "output_samples", "completed_processing_samples"]:
        assert sum(w["details"][name] for w in windows) == final[name]


def test_first_empty_and_zero_output_tail_are_retained(native):
    records, _ = run(native, "normal")
    first, second, tail, final = [r["details"] for r in records]
    assert first["start_ns"] == 100 and first["wall_ns"] == 170
    assert first["message_samples_histogram"] == [[0, 1], [4, 1]]
    assert first["phases"]["processing"]["completed"] == [2, 80, 40]
    assert first["dsp_core"]["completed"] == [1, 10, 10]
    assert second["intentional_mask_samples"] == 256
    assert tail["output_messages"] == tail["completed_processing_samples"] == 0
    assert tail["phases"]["request_receive"]["stopped"] == [1, 50, 50]
    assert final["status"] == "complete" and final["window_count"] == 3


def test_stopped_unsent_work_keeps_processing_population(native):
    records, _ = run(native, "unsent")
    window, final = [r["details"] for r in records]
    assert window["input_samples"] == window["completed_processing_samples"] == 8
    assert window["output_samples"] == 0
    assert window["phases"]["downstream_send"]["stopped"] == [1, 50, 50]
    assert final["status"] == "incomplete"


def test_failed_partial_processing_uses_parent_outcome(native):
    records, _ = run(native, "processing-error")
    window, final = [r["details"] for r in records]
    assert window["processed_sample_end"] == 4
    assert window["input_samples"] == 8 and window["completed_processing_samples"] == 0
    assert window["processing_parts"]["input_prepare"]["error"] == [1, 10, 10]
    assert window["processing_parts"]["channel_chain"]["error"] == [1, 40, 40]
    assert window["dsp_core"]["completed"] == [1, 10, 10]
    assert window["dsp_core"]["error"] == [1, 10, 10]
    assert final["status"] == "error"


def test_rtf_derived_without_clipping_and_zero_sample_budget_is_undefined(native):
    records, _ = run(native, "normal")
    fast = records[1]["details"]
    assert fast["output_samples"] * 1e9 / (23040000 * fast["wall_ns"]) > 1
    tail = records[2]["details"]
    assert tail["output_samples"] * 1e9 / (23040000 * tail["wall_ns"]) == 0
    slow_records, _ = run(native, "slow")
    slow = slow_records[0]["details"]
    assert 0 < slow["output_samples"] * 1e9 / (23040000 * slow["wall_ns"]) < 1
    assert tail["completed_processing_samples"] == 0  # null ratio + reason, not division


@pytest.mark.parametrize("mode", ["regression", "zero-window", "overflow", "nan-energy", "histogram-mismatch", "window-budget"])
def test_invalid_measurements_fail_closed(native, mode):
    _, result = run(native, mode)
    assert "CHECKED_INVALID: true" in result.stdout


def test_final_close_is_idempotent_and_forbids_later_observation(native):
    records, _ = run(native, "duplicate-close")
    assert len([r for r in records if r["event_type"] == "window"]) == 3
    assert records[-1]["details"]["status"] == "error"


def test_processing_denominator_gap_cannot_be_complete(native):
    records, _ = run(native, "denominator")
    assert records[-1]["details"]["status"] == "incomplete"


def test_uint64_histogram_boundaries(native):
    _, result = run(native, "bins")
    observed = [tuple(map(int, line.split())) for line in result.stdout.splitlines()]
    assert observed == [(0, 0), (1, 1), (2, 2), (3, 2), (4, 3), (7, 3), (8, 4), (1 << 63, 64), ((1 << 64) - 1, 64)]


def private(folder):
    folder.mkdir(mode=0o700)
    (folder / "plan.wire").write_bytes(schedule_module.compile_plan(PROFILE, PLAN))
    (folder / "control.token").write_text("a" * 64)
    for name in ["plan.wire", "control.token"]: (folder / name).chmod(0o600)


def cli(native, folder, extra):
    return subprocess.run([str(native["cli"]), *profile_module.broker_arguments(PROFILE, "c"),
                           "--radio-plan-file", str(folder / "plan.wire"), "--radio-control-dir", str(folder),
                           "--validate-config-only", *extra], capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("value", ["0", "-1", "+1", "01", "1000001", "1.0", "", "nan"])
def test_cli_rejects_invalid_interval_without_outputs(native, tmp_path, value):
    folder = tmp_path / "p"; private(folder)
    assert cli(native, folder, ["--radio-metrics-every-messages", value]).returncode != 0
    assert sorted(p.name for p in folder.iterdir()) == ["control.token", "plan.wire"]


def test_cli_preflight_valid_interval_and_existing_output(native, tmp_path):
    folder = tmp_path / "p"; private(folder)
    assert cli(native, folder, ["--radio-metrics-every-messages", "4"]).returncode == 0
    assert cli(native, folder, ["--radio-metrics-every-messages", "4", "--radio-metrics-every-messages", "4"]).returncode != 0
    existing = folder / "broker_metrics.jsonl"; existing.write_text("keep")
    assert cli(native, folder, ["--radio-metrics-every-messages", "4"]).returncode != 0
    assert existing.read_text() == "keep"
    result = subprocess.run([str(native["cli"]), "--radio-metrics-every-messages", "4", "--validate-config-only"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0


@pytest.mark.parametrize("mode", ["writer", "writer-error", "metrics-budget"])
def test_shared_checked_writer_preserves_separate_limits(native, tmp_path, mode):
    folder = tmp_path / "p"; private(folder)
    _, result = run(native, mode, folder)
    assert "a" * 64 not in result.stdout + result.stderr
    truth = [json.loads(line) for line in (folder / "broker_events.jsonl").read_text().splitlines()]
    assert all(r["schema_version"] == "radio_broker_truth_v1" for r in truth)
    assert all(len(line) + 1 <= 4096 for line in (folder / "broker_events.jsonl").read_bytes().splitlines())
    if mode == "writer":
        lines = (folder / "broker_metrics.jsonl").read_bytes().splitlines()
        records = [json.loads(line) for line in lines]
        assert max(map(len, lines)) > 4096 and max(map(len, lines)) < 16384
        assert [r["event_sequence"] for r in records] == list(range(1, len(records) + 1))
        assert all(r["metrics_config_sha256"] == hashlib.sha256(b"radio_broker_metrics_v1\n2\n").hexdigest() for r in records)
        assert all(r["scope"] == "direction" for r in records if r["direction"] != "control")
        assert [r["monotonic_ns"] for r in records] == sorted(r["monotonic_ns"] for r in records)


def test_checked_writer_marks_fsync_failures_as_fatal_logging_errors(native, tmp_path):
    folder = tmp_path / "p"; private(folder)
    _, result = run(native, "fsync-error", folder)
    status = json.loads(next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                             if line.startswith("WRITER: ")))
    assert status == {"fatal_error": 1, "logging_errors": 2, "fsync_calls": 2, "fsync_errors": 2}
    # Both files were written before their injected sync failure; readable
    # buffered records must not conceal a failed durability/health outcome.
    for name in ("broker_events.jsonl", "broker_metrics.jsonl"):
        records = [json.loads(line) for line in (folder / name).read_text().splitlines()]
        assert [r["event_type"] for r in records] == ["started", "final", "final"]


def test_legacy_power_includes_silence_complex_normalization_and_partial(native):
    _, result = run(native, "power")
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("power DL=6.25 samples=4 ")
    assert lines[0].endswith("final=true")
    assert lines[1].startswith("power DL=0 samples=3 ")
    assert lines[1].endswith("final=false")


def transport(native, mode):
    result = subprocess.run([str(native["transport"]), mode], check=True, capture_output=True, text=True, timeout=5)
    records = [json.loads(line.split(": ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("METRIC:")]
    if directory := os.environ.get("RADIO_C_METRICS_L1_RESULTS"):
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / ("transport-" + mode + ".log")).write_text(result.stdout + result.stderr)
    summary = json.loads(next(line.split(": ", 1)[1] for line in result.stdout.splitlines() if line.startswith("TRANSPORT:")))
    return records, summary, result


def test_production_boundaries_and_disabled_no_clock_reads(native):
    on, a, on_result = transport(native, "enabled")
    off, b, off_result = transport(native, "disabled")
    assert a["clock_reads"] > 0 and b["clock_reads"] == 0 and off == []
    assert {k: a[k] for k in ["output_hash", "draws", "phase", "output_samples"]} == {k: b[k] for k in ["output_hash", "draws", "phase", "output_samples"]}
    windows = [r for r in on if r["event_type"] == "window"]
    for window in windows:
        populations(window)
    assert sum(w["details"]["dsp_core"]["completed"][0] for w in windows) == 2
    assert sum(w["details"]["phases"]["processing"]["completed"][0] for w in windows) == 3
    assert windows[0]["details"]["processing_parts"]["input_prepare"]["completed"][1] > windows[0]["details"]["phases"]["upstream_receive"]["completed"][1]
    assert a["closed"] == b["closed"] == 2
    profiles = [[json.loads(line.split(": ", 1)[1]) for line in result.stdout.splitlines()
                 if line.startswith("RADIO_FIXED_PROFILE: ")]
                for result in (on_result, off_result)]
    assert profiles[0] == profiles[1]
    started, final = profiles[0]
    assert started["record_type"] == "started" and final["record_type"] == "final"
    assert started["awgn_state"] == started["awgn_seed"]
    # glibc rand_r updates its uint32 LCG three times per real uniform;
    # Box-Muller consumes two uniforms for each complex Gaussian pair.
    expected = started["awgn_state"]
    for _ in range(6 * final["awgn_complex_draws"]):
        expected = (1103515245 * expected + 12345) & 0xffffffff
    assert final["awgn_state"] == expected != started["awgn_state"]


@pytest.mark.parametrize("mode,status", [("copy-error", "error"), ("invalid-iq", "error"), ("send-stop", "incomplete"), ("send-clock-error", "error")])
def test_actual_failed_and_stopped_boundaries(native, mode, status):
    records, summary, _ = transport(native, mode)
    assert records[-1]["details"]["status"] == status
    windows = [r for r in records if r["event_type"] == "window"]
    if mode == "send-clock-error":
        assert summary["forwarded"] == summary["output_messages"] == 1
        assert summary["fatal"] == 1
    else:
        assert len(windows) == 1
        populations(windows[0])
        d = windows[0]["details"]
        if mode in ["copy-error", "invalid-iq"]:
            assert d["phases"]["upstream_receive"]["completed"][0] == 1
            assert d["processing_parts"]["input_prepare"]["error"][0] == 1
            assert d["rejected_messages"] == (mode == "invalid-iq")
        else:
            assert d["phases"]["downstream_send"]["stopped"][0] == 1
            assert d["completed_processing_samples"] == 3


def test_power_display_runs_on_actual_identity_path(native):
    _, summary, result = transport(native, "power-identity")
    lines = [line for line in result.stdout.splitlines() if line.startswith("power DL=")]
    assert len(lines) == 1 and lines[0].startswith("power DL=7 samples=8 ") and lines[0].endswith("final=true")
    assert summary["output_messages"] == 3 and summary["output_samples"] == 8


def test_sibling_failure_interrupts_local_phase_but_invalidates_final(native):
    records, summary, _ = transport(native, "sibling-stop")
    window, final = [r["details"] for r in records]
    assert window["phases"]["request_receive"]["stopped"][0] == 1
    assert window["phases"]["request_receive"]["error"][0] == 0
    assert final["status"] == "error" and summary["fatal"] == 1


def test_timespec_uint64_boundary_and_invalid_nanoseconds(native):
    _, result = run(native, "timespec")
    assert "CHECKED_TIMESPEC: true" in result.stdout
