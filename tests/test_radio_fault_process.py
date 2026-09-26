# SPDX-License-Identifier: GPL-3.0-only
"""Owned broker lifecycle, the IQ-level oracle, and complete broker-only trials.

The end-to-end cases start real C and Python brokers between synthetic gNB/UE
peers on private IPC endpoints; no RAN software, network port or privilege is
used. They use 10 ms messages so every programmed boundary falls inside a
message and must be split exactly by the broker.
"""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_fault as fault
import radio_fault_process as process

LOCK = json.loads((ROOT / "dependencies/toolchain.lock.json").read_text())


@pytest.fixture(scope="module")
def c_binary(tmp_path_factory):
    output = tmp_path_factory.mktemp("c-broker") / "zmq_channel_broker"
    toolchain = LOCK["toolchain"]
    subprocess.run([toolchain["cc_default"], *toolchain["cflags"], str(ROOT / "scripts/zmq_channel_broker.c"),
                    "-o", str(output), *toolchain["ldlibs"]], check=True, capture_output=True, timeout=120)
    return output


@pytest.mark.parametrize("recipe", ["B5", "grc_ul_cw_500ms", "ul_cfo_500ms", "grc_ul_tdl_c_500ms"])
def test_complete_broker_only_trial_qualifies_and_matches_the_iq_oracle(c_binary, recipe):
    spec = fault.recipe_specification(recipe)
    with tempfile.TemporaryDirectory(prefix="rf-e2e-", dir="/tmp") as name:
        summary = process.demo(recipe, c_binary=c_binary if spec["backend"] == "c" else None,
                               output=Path(name) / "demo", frame_samples=230_400, log=lambda _message: None)
        trial = Path(summary["trial_directory"])
        assert summary["verification"]["qualified"], summary["verification"]["errors"]
        assert summary["iq_oracle"]["consistent"], summary["iq_oracle"]
        assert summary["broker_stop"]["exit_status"] == 0 and summary["broker_stop"]["signals"] == ["SIGTERM"]
        ul = summary["iq_oracle"]["directions"]["UL"]
        assert len(ul["programmed"]) == len(fault.pulse_intervals(spec))
        assert all(ul["checks"].values()) and len(ul["checks"]) >= 3
        assert summary["iq_oracle"]["directions"]["DL"]["observed"] == []
        assert summary["sink_observations"]["UL"]["reference_delay_samples"] == (15 if "tdl" in recipe else 0)
        # Independent re-verification of the retained evidence, from disk only.
        again = fault.verify(trial)
        assert again["qualified"] and again["directions"] == summary["verification"]["directions"]
        names = {path.name for path in trial.iterdir()}
        assert {"plan.wire", "control.token", "broker_ready.json", "broker_events.jsonl", "control.jsonl",
                "execution.json", "launch.json", "stop.json", "broker.log", "verification.json"} <= names
        assert all(os.stat(trial / name).st_mode & 0o077 == 0 for name in names if name != "rb.sock")
        token = (trial / "control.token").read_bytes()
        assert all(token not in (trial / name).read_bytes() for name in names - {"control.token", "rb.sock"})


def test_stop_refuses_to_signal_a_process_whose_identity_differs(tmp_path):
    directory = tmp_path / "trial"
    directory.mkdir(mode=0o700)
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        identity, _state = process.process_identity(sleeper.pid)
        forged = dict(identity, start_ticks=identity["start_ticks"] + 1)
        fault.write_private(directory / "launch.json", fault.canonical({"pid": sleeper.pid, "process": forged}))
        with pytest.raises(fault.FaultError, match="refusing to signal"):
            process.stop(directory, timeout=1)
        assert sleeper.poll() is None
        assert not (directory / "stop.json").exists()
    finally:
        sleeper.kill()
        sleeper.wait()


def test_stop_terminates_a_matching_unowned_process_and_records_it(tmp_path):
    directory = tmp_path / "trial"
    directory.mkdir(mode=0o700)
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        identity, _state = process.process_identity(sleeper.pid)
        fault.write_private(directory / "launch.json", fault.canonical({"pid": sleeper.pid, "process": identity}))
        # Not passing the Popen object exercises the unowned path used after
        # `launch` has returned; an exited but unreaped process counts as gone.
        result = process.stop(directory, timeout=5)
        sleeper.wait(5)
        assert result["signals"] == ["SIGTERM"] and result["outcome"] == "exited"
        assert sleeper.returncode == -signal.SIGTERM
        assert json.loads((directory / "stop.json").read_text())["pid"] == sleeper.pid
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def test_observation_merges_runs_across_message_boundaries():
    observation = process._Observation()
    sent = np.full(8, 3 + 4j, np.complex64)
    first, second = sent.copy(), sent.copy()
    first[6:] = 0
    second[:3] = 0
    second[5] = 1
    observation.add(sent, first)
    observation.add(sent, second)
    summary = observation.summary()
    assert [(run["start_sample"], run["end_sample"]) for run in summary["differing_runs"]] == [(6, 11), (13, 14)]
    assert summary["differing_runs"][0]["received_to_sent_power_ratio"] == 0
    assert summary["samples"] == 16 and summary["messages"] == 2


def observation(runs=(), **fields):
    value = {"differing_runs": list(runs), "nonfinite_samples": 0, "unrecorded_runs": 0,
             "reference_delay_samples": 0}
    value.update(fields)
    return value


def oracle_inputs(recipe, runs, arm=1000, **ul_fields):
    spec = fault.recipe_specification(recipe)
    verification = {"qualified": True, "directions": {d: {"arm_sample": arm} for d in ("DL", "UL")}}
    return spec, verification, {"DL": observation(), "UL": observation(runs, **ul_fields)}


def run(start, end, ratio=0.0, difference=0.0):
    return {"start_sample": start, "end_sample": end, "samples": end - start,
            "received_to_sent_power_ratio": ratio, "difference_mean_power": difference}


@pytest.mark.parametrize("runs,consistent", [
    ([run(1000 + 92_160_000, 1000 + 93_312_000)], True),
    ([run(1001 + 92_160_000, 1000 + 93_312_000)], False),              # one sample late
    ([run(1000 + 92_160_000, 1000 + 93_312_000, ratio=1e-6)], False),  # not an exact blank
    ([], False),                                                        # blank not observed
    ([run(1000 + 92_160_000, 1000 + 93_312_000), run(5, 6)], False),    # extra disturbance
])
def test_oracle_requires_exact_support_and_gain(runs, consistent):
    assert process.iq_oracle(*oracle_inputs("B1", runs))["consistent"] is consistent


def test_oracle_checks_additive_power_and_rejects_dl_or_unqualified_evidence():
    exact = run(1000 + 92_160_000, 1000 + 103_680_000, ratio=1.1, difference=1_000_000 * 1.01)
    assert process.iq_oracle(*oracle_inputs("ul_awgn_500ms", [exact]))["consistent"]
    weak = dict(exact, difference_mean_power=500_000)
    assert not process.iq_oracle(*oracle_inputs("ul_awgn_500ms", [weak]))["consistent"]
    spec, verification, observations = oracle_inputs("ul_awgn_500ms", [exact])
    observations["DL"]["differing_runs"] = [run(1, 2, ratio=1.0)]
    assert not process.iq_oracle(spec, verification, observations)["consistent"]
    verification["qualified"] = False
    assert not process.iq_oracle(spec, verification, oracle_inputs("B1", [])[2])["consistent"]


@pytest.mark.parametrize("recipe,runs,consistent", [
    ("grc_ul_tdl_a_500ms", [run(1000 + 92_160_000, 1000 + 103_680_000, ratio=1.1)], True),
    ("grc_ul_tdl_a_500ms", [run(1000 + 92_160_000, 1000 + 103_680_015, ratio=1.1)], False),
    ("grc_tdl_c_normal", [], True),
    ("grc_tdl_c_normal", [run(0, 15)], False),
])
def test_oracle_aligns_tdl_to_the_common_delay(recipe, runs, consistent):
    result = process.iq_oracle(*oracle_inputs(recipe, runs, reference_delay_samples=15))
    assert result["consistent"] is consistent
    assert result["directions"]["UL"]["reference_delay_samples"] == 15


def cfo_observation(arm=1000, **changes):
    start, length = arm + 92_160_000, 11_520_000
    fields = dict(first_difference_sample=start + 1, last_difference_end_sample=start + length,
                  differing_samples=length - 250, received_to_reference_power_ratio=1.0,
                  rotation_coherence=1.0, rotation_phase_rad=math.remainder(-2 * math.pi * 500 * start / 23_040_000, 2 * math.pi),
                  cfo_hz=500, unrecorded_runs=186)
    fields.update(changes)
    return fields


@pytest.mark.parametrize("change,consistent", [
    ({}, True),
    ({"first_difference_sample": 1000 + 92_160_000 - 1}, False),    # rotation before the pulse
    ({"differing_samples": 11_520_000 - 400}, False),             # rotation missing on part of the pulse
    ({"received_to_reference_power_ratio": 0.99}, False),          # not a pure rotation
    ({"rotation_coherence": 0.64}, False),                         # wrong frequency
    ({"rotation_phase_rad": 0.5}, False),                          # wrong onset
])
def test_oracle_checks_cfo_support_frequency_and_onset(change, consistent):
    result = process.iq_oracle(*oracle_inputs("ul_cfo_500ms", [], **cfo_observation(**change)))
    assert result["consistent"] is consistent, result["directions"]["UL"]["checks"]


def test_cfo_control_must_reproduce_every_sample():
    assert process.iq_oracle(*oracle_inputs("grc_normal", []))["consistent"]
    assert not process.iq_oracle(*oracle_inputs("grc_normal", [run(5, 7, ratio=1.0)]))["consistent"]


def test_c_backend_requires_a_binary_and_grc_rejects_one():
    with tempfile.TemporaryDirectory(prefix="rf-cmd-", dir="/tmp") as name:
        c_prep = fault.prepare(fault.specification("normal"), Path(name) / "c",
                               study_id="00000000-0000-0000-0000-000000000001",
                               trial_id="00000000-0000-0000-0000-000000000002",
                               pipeline_id="00000000-0000-0000-0000-000000000003")
        with pytest.raises(fault.FaultError, match="needs --c-binary"):
            process.broker_command(c_prep)
        grc_prep = dict(c_prep, backend="grc")
        with pytest.raises(fault.FaultError, match="only to the C backend"):
            process.broker_command(grc_prep, c_binary="/bin/true")
        command, _build = process.broker_command(grc_prep)
        assert command[:3] == [sys.executable, "-u", str(ROOT / "scripts/ocudu_channel_broker.py")]
        assert command[-1] == "--no-gui"
