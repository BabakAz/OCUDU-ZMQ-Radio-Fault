"""Native RAD06 L0/L1: actual parser/control/DSP, no network or radio stack."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_schedule as schedules
import radio_broker_profile as profiles

PROFILE = json.loads((ROOT / "config/radio_broker/fixed_reference.fixture.json").read_text())
PLAN = json.loads((ROOT / "config/radio_broker/finite_schedule.fixture.json").read_text())
TOKEN = "a" * 64


@pytest.fixture(scope="module")
def programs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("c-schedule-programs")
    result = {}
    for key, source in [("cli", "scripts/zmq_channel_broker.c"),
                        ("harness", "tests/cpp/radio_broker_c_schedule_harness.c"),
                        ("transport", "tests/cpp/radio_broker_c_schedule_transport_harness.c")]:
        target = folder / key
        subprocess.run(["clang-18", "-std=c17", "-O2", "-Wall", "-Wextra", "-Wpedantic",
                        "-Werror", str(ROOT / source), "-o", str(target), "-lzmq", "-lm", "-pthread",
                        *(["-Wl,--wrap=pthread_create"] if key == "transport" else [])],
                       check=True, capture_output=True, text=True, timeout=30)
        result[key] = target
    return result


def private_inputs(folder, wire=None, profile=None, plan=None):
    folder.mkdir(mode=0o700)
    if wire is None:
        wire = schedules.compile_plan(profile or PROFILE, plan or PLAN)
    path = folder / "plan.wire"
    path.write_bytes(wire)
    path.chmod(0o600)
    token = folder / "control.token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return path


def cli(programs, folder, profile=None, extra=()):
    args = profiles.broker_arguments(profile or PROFILE, "c")
    return subprocess.run([str(programs["cli"]), *args, "--radio-plan-file", str(folder / "plan.wire"),
                           "--radio-control-dir", str(folder), "--validate-config-only", *extra],
                          capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("size", [0, 3, 55, 56, 63, 64, 65, 127, 128, 1000000])
@pytest.mark.parametrize("chunk", [1, 17, 65536])
def test_sha256_known_vectors_and_incremental_boundaries(programs, tmp_path, size, chunk):
    payload = b"abc" if size == 3 else b"a" * size
    file = tmp_path / "input"
    file.write_bytes(payload)
    result = subprocess.run([str(programs["harness"]), "hash", str(file), str(chunk)],
                            check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == hashlib.sha256(payload).hexdigest()


def test_production_preflight_matches_shared_wire_without_outputs(programs, tmp_path):
    folder = tmp_path / "private"
    private_inputs(folder)
    result = cli(programs, folder)
    assert result.returncode == 0, result.stderr
    assert "RADIO_CONFIG_VALIDATED:" in result.stdout
    assert {p.name for p in folder.iterdir()} == {"plan.wire", "control.token"}


@pytest.mark.parametrize("mutation", [
    lambda s: s.replace("direction DL 4097", "direction DL 04097"),
    lambda s: s.replace("event 17 ", "event 0 "),
    lambda s: s.replace("attenuation set", "baseline set"),
    lambda s: s.replace(" restore restore ", " restore set "),
    lambda s: s.replace("event 17", "event 4097"),
    lambda s: s.replace("0x1.0000000000000p-2", "0x1.0000000000000p+1"),
    lambda s: s.replace("0x1.0000000000000p-2", "0.25"),
    lambda s: s.replace("0x1.0000000000000p-2", "nan"),
    lambda s: s.replace(" 41 ", " 42 "),
    lambda s: s.replace("direction DL", "direction XX"),
    lambda s: s.replace("ids ", "ids  "),
    lambda s: s.replace("\n", "\r\n"),
    lambda s: s + "end\n",
    lambda s: s[:-1],
    lambda s: s.replace("baseline set", "bad\\id set"),
    lambda s: s.replace("direction DL 4097 6", "direction DL 4097 33"),
])
def test_native_rejects_malformed_wire_before_transport(programs, tmp_path, mutation):
    folder = tmp_path / "private"
    private_inputs(folder, mutation(schedules.compile_plan(PROFILE, PLAN).decode()).encode())
    result = cli(programs, folder)
    assert result.returncode != 0
    assert "RADIO_CONFIG_VALIDATED" not in result.stdout
    assert {p.name for p in folder.iterdir()} == {"plan.wire", "control.token"}


@pytest.mark.parametrize("mutation", ["dir-mode", "token-mode", "plan-mode", "token-newline", "token-symlink", "plan-symlink", "token-hardlink", "relative-dir"])
def test_private_input_validation_fails_closed(programs, tmp_path, mutation):
    folder = tmp_path / "private"
    private_inputs(folder)
    if mutation == "dir-mode": folder.chmod(0o755)
    elif mutation == "token-mode": (folder / "control.token").chmod(0o644)
    elif mutation == "plan-mode": (folder / "plan.wire").chmod(0o644)
    elif mutation == "token-newline": (folder / "control.token").write_text(TOKEN + "\n")
    elif mutation.endswith("symlink"):
        item = folder / ("control.token" if mutation.startswith("token") else "plan.wire")
        actual = item.with_suffix(".actual")
        item.rename(actual)
        item.symlink_to(actual)
    elif mutation == "token-hardlink": os.link(folder / "control.token", folder / "linked")
    elif mutation == "relative-dir": folder = Path(os.path.relpath(folder))
    assert cli(programs, folder).returncode != 0
    assert not (folder / "rb.sock").exists()
    assert not (folder / "broker_events.jsonl").exists()


def run_fixture(programs, tmp_path, partition=0, action="run", profile=None, plan=None):
    folder = tmp_path / f"private-{partition}-{action}"
    private_inputs(folder, profile=profile, plan=plan)
    n = np.arange(31 + 8193)
    source = np.column_stack((np.where(n % 2, .5, -.5), np.where(n % 3, .5, -.5))).astype("<f4")
    source[300:450] = 0
    data = tmp_path / f"input-{partition}-{action}.cf32"
    data.write_bytes(source.tobytes())
    output = tmp_path / f"output-{partition}-{action}.cf32"
    result = subprocess.run([str(programs["harness"]), str(folder / "plan.wire"), str(folder), str(data),
                             str(output), str(partition), "reserved", action],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr + result.stdout
    events = [json.loads(x) for x in (folder / "broker_events.jsonl").read_text().splitlines()]
    assert [x["event_sequence"] for x in events] == list(range(1, len(events) + 1))
    assert all(x["plan_sha256"] == hashlib.sha256((folder / "plan.wire").read_bytes()).hexdigest() for x in events)
    states = [json.loads(line.split(": ", 1)[1]) for line in result.stdout.splitlines() if line.startswith("RADIO_FIXED_PROFILE:")]
    outputs = [np.fromfile(str(output) + suffix, dtype="<f4").reshape(-1, 2) if Path(str(output) + suffix).exists() else None for suffix in ["", ".ul"]]
    return source, outputs, states, events, result


def test_exact_partition_replay_warmup_and_directional_epochs(programs, tmp_path):
    records = [run_fixture(programs, tmp_path, p) for p in [0, 257, 1024, 4096, 1]]
    reference = records[0]
    for _, outputs, states, events, _ in records:
        for d in range(2):
            assert outputs[d].tobytes() == reference[1][d].tobytes()
            assert states[d] == reference[2][d]
        for d, warm in [("DL", 19), ("UL", 31)]:
            armed = [e for e in events if e["direction"] == d and e["event_type"] == "armed"]
            assert len(armed) == 1
            arm = armed[0]
            assert arm["details"]["arm_sample"] == warm
            assert arm["details"]["state_at_arm"]["awgn_normal_draws"] == 2 * warm
            expected = PLAN["directions"][d]["events"]
            observed = [e for e in events if e["direction"] == d and e["event_type"].startswith("condition_")]
            assert [e["sample_start"] for e in observed] == [warm + x["sample_offset"] for x in expected]
            assert [e["details"]["event_id"] for e in observed] == [x["event_id"] for x in expected]
            assert all(e["sample_end"] > e["sample_start"] for e in observed)
            assert next(e for e in observed if e["details"]["event_id"] == "noop")["details"]["changed"] is False
        finals = [e for e in events if e["event_type"] == "final"]
        assert len(finals) == 2 and all(e["details"]["status"] == "complete" for e in finals)
        assert [e["details"]["forwarded_samples"] for e in finals] == [8193 + 19, 8193 + 31]


def test_identity_noop_events_and_empty_frames_do_not_draw_or_arm(programs, tmp_path):
    profile, plan = copy.deepcopy(PROFILE), copy.deepcopy(PLAN)
    for direction in ["DL", "UL"]:
        profile["directions"][direction]["mode"] = "identity"
        settings = schedules.profile_base(profile["directions"][direction])
        settings = {k: settings[k] for k in schedules.SETTINGS}
        for event in plan["directions"][direction]["events"]: event["settings"] = settings.copy()
    plan["profile_sha256"] = hashlib.sha256(profiles.canonical_bytes(profiles.validate_profile(profile))).hexdigest()
    source, outputs, states, events, _ = run_fixture(programs, tmp_path, 1, "identity", profile, plan)
    assert all(out.tobytes() == source[31:].tobytes() for out in outputs)
    assert all(s["awgn_normal_draws"] == s["phase_u64"] == 0 for s in states)
    assert all(e["details"]["changed"] is False for e in events if e["event_type"].startswith("condition_"))
    _, _, _, empty_events, _ = run_fixture(programs, tmp_path, 0, "empty-only")
    assert not any(e["event_type"] in ["armed", "condition_applied", "condition_restored"] for e in empty_events)
    assert all(e["details"]["status"] == "incomplete" for e in empty_events if e["event_type"] == "final")


def test_gain_and_phase_continuous_frequency_change_independent_oracle(programs, tmp_path):
    plan = copy.deepcopy(PLAN)
    base = plan["directions"]["DL"]["events"][0]["settings"]
    events = []
    for offset, name, gain, enabled, freq in [(0, "positive", .5, True, 1440000), (17, "negative", .25, True, -720000), (257, "disabled", 0, False, -720000), (1024, "enabled", 1, True, -720000)]:
        settings = dict(base, gain=gain, noise_enabled=False, cw_enabled=enabled, cw_freq_hz=freq)
        events.append(dict(sample_offset=offset, event_id=name, kind="set", settings=settings))
    events.append(copy.deepcopy(plan["directions"]["DL"]["events"][-1]))
    plan["directions"]["DL"]["events"] = events
    source, outputs, _, _, _ = run_fixture(programs, tmp_path, 1, "run", plan=plan)
    expected = []
    mask = (1 << 64) - 1
    phase = (19 * (1 << 60)) & mask
    for n in range(2048):
        event = next(e for e in reversed(events) if e["sample_offset"] <= n)
        c = event["settings"]
        scaled = math.ldexp(abs(c["cw_freq_hz"]) / 23040000, 64)
        step = math.floor(scaled + .5)
        if c["cw_freq_hz"] < 0: step = -step
        angle = math.ldexp(float(phase), -64) * (2 * math.pi)
        desired = (source[n + 31].astype(np.float64) * c["gain"]).astype(np.float32)
        tone = (np.array([math.cos(angle), math.sin(angle)]) * math.sqrt(.1)).astype(np.float32) if c["cw_enabled"] else np.zeros(2, np.float32)
        expected.append((desired.astype(np.float64) + tone.astype(np.float64)).astype(np.float32))
        phase = (phase + step) & mask
    expected = np.asarray(expected)
    error = outputs[0][:2048].astype(float) - expected
    assert np.linalg.norm(error) / np.linalg.norm(expected) <= 1e-6
    assert np.max(np.abs(error)) <= 1e-5 * max(1, np.max(np.abs(expected)))


@pytest.mark.parametrize("action", ["retry", "wrong-token", "rearm", "unsent", "arm-overflow", "writer-failure", "queue-overflow"])
def test_control_retries_and_failure_truth_are_explicit(programs, tmp_path, action):
    _, _, _, events, result = run_fixture(programs, tmp_path, 0, action)
    finals = [e for e in events if e["event_type"] == "final"]
    if action == "retry":
        assert len([e for e in events if e["event_type"] == "arm_requested"]) == 1
        assert all(e["details"]["status"] == "complete" for e in finals)
    elif action == "unsent":
        assert all(e["details"]["status"] == "incomplete" for e in finals)
        assert all(not e["details"]["schedule_complete"] for e in finals)
        assert all(e["details"]["processed_samples"] > e["details"]["forwarded_samples"] for e in finals)
    elif action not in ["writer-failure", "queue-overflow"]:
        assert len(finals) == 2 and all(e["details"]["status"] == "error" for e in finals)
    assert TOKEN not in result.stdout + result.stderr + json.dumps(events)


@pytest.mark.parametrize("mode,forwarded,errors", [
    ("sample-overflow", 0, 1), ("message-overflow", 0, 1),
    ("close-after-stop", 1, 2), ("close-after-fatal", 0, 3),
])
def test_actual_channel_presend_overflow_and_cleanup_failures(programs, mode, forwarded, errors):
    result = subprocess.run([str(programs["transport"]), mode], check=True,
                            capture_output=True, text=True, timeout=5)
    record = json.loads(next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                             if line.startswith("LIFECYCLE:")))
    assert record == dict(forwarded=forwarded, closed=2, errors=errors, fatal=1, transport="L1_stub")
    accounting = json.loads(next(line.split(": ", 1)[1] for line in result.stdout.splitlines()
                                 if line.startswith("C_RELAY_ACCOUNTING:")))
    assert accounting["status"] == "error"


def test_production_main_second_thread_failure_has_two_final_accounts(programs):
    result = subprocess.run([str(programs["transport"]), "second-thread-failure"],
                            check=True, capture_output=True, text=True, timeout=5)
    records = [json.loads(line.split(": ", 1)[1]) for line in result.stdout.splitlines()
               if line.startswith("C_RELAY_ACCOUNTING:")]
    assert sorted(record["direction"] for record in records) == ["DL", "UL"]
    assert all(record["status"] == "error" for record in records)


@pytest.mark.parametrize("name", ["plan.wire", "control.token"])
def test_preflight_rejects_fifo_without_waiting_for_a_writer(programs, tmp_path, name):
    folder = tmp_path / "private"
    private_inputs(folder)
    (folder / name).unlink()
    os.mkfifo(folder / name, 0o600)
    result = cli(programs, folder)
    assert result.returncode != 0
    assert not (folder / "broker_events.jsonl").exists()


def test_shared_hex_underflow_and_lexical_bounds(programs, tmp_path):
    wire = schedules.compile_plan(PROFILE, PLAN).decode()
    # Replaces UL's exact zero with a finite hexadecimal value rounding to zero.
    underflow = wire.replace("0x0.0p+0", "0x1.0p-9999")
    folder = tmp_path / "underflow"
    private_inputs(folder, underflow.encode())
    assert cli(programs, folder).returncode == 0
    for i, value in enumerate(["0x0." + "0" * 124 + "p+0", "0x0." + "0" * 1100 + "p+0"]):
        folder = tmp_path / f"long-{i}"
        private_inputs(folder, wire.replace("0x0.0p+0", value).encode())
        assert cli(programs, folder).returncode != 0


@pytest.mark.parametrize("where", ["base", "event"])
def test_identity_wire_cannot_enable_additions(programs, tmp_path, where):
    profile, plan = copy.deepcopy(PROFILE), copy.deepcopy(PLAN)
    for direction in ["DL", "UL"]:
        profile["directions"][direction]["mode"] = "identity"
        base = schedules.profile_base(profile["directions"][direction])
        for event in plan["directions"][direction]["events"]:
            event["settings"] = {key: base[key] for key in schedules.SETTINGS}
    plan["profile_sha256"] = hashlib.sha256(profiles.canonical_bytes(profile)).hexdigest()
    lines = schedules.compile_plan(profile, plan).decode().splitlines()
    index = 3 if where == "base" else 4
    tokens = lines[index].split(" ")
    tokens[7 if where == "base" else 5] = "1"
    lines[index] = " ".join(tokens)
    folder = tmp_path / "private"
    private_inputs(folder, ("\n".join(lines) + "\n").encode())
    assert cli(programs, folder, profile).returncode != 0


def test_noop_and_dl_independence(programs, tmp_path):
    plans = [copy.deepcopy(PLAN) for _ in range(3)]
    plans[1]["directions"]["DL"]["events"] = [
        event for event in plans[1]["directions"]["DL"]["events"] if event["event_id"] != "noop"]
    plans[2]["directions"]["DL"]["events"][1]["settings"]["gain"] = .125
    results = []
    for i, plan in enumerate(plans):
        folder = tmp_path / str(i)
        folder.mkdir()
        results.append(run_fixture(programs, folder, 257, plan=plan))
    assert results[0][1][0].tobytes() == results[1][1][0].tobytes()
    assert results[0][2] == results[1][2]
    assert all(result[1][1].tobytes() == results[0][1][1].tobytes() for result in results)
    assert results[0][1][0].tobytes() != results[2][1][0].tobytes()


def test_each_restore_must_equal_base(programs, tmp_path):
    folder = tmp_path / "private"
    wire = schedules.compile_plan(PROFILE, PLAN).decode().replace("attenuation set", "attenuation restore")
    private_inputs(folder, wire.encode())
    assert cli(programs, folder).returncode != 0


@pytest.mark.parametrize("name", ["rb.sock", "broker_ready.json", "broker_events.jsonl"])
@pytest.mark.parametrize("symlink", [False, True])
def test_existing_outputs_fail_preflight_and_remain_owned_by_caller(programs, tmp_path, name, symlink):
    folder = tmp_path / "private"
    private_inputs(folder)
    item = folder / name
    if symlink:
        item.symlink_to(folder / "absent")
    else:
        item.write_bytes(b"foreign-owned-existing-artifact")
    assert cli(programs, folder).returncode != 0
    if symlink:
        assert item.is_symlink()
    else:
        assert item.read_bytes() == b"foreign-owned-existing-artifact"
