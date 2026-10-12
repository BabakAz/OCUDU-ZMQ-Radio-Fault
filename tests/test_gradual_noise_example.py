# SPDX-License-Identifier: GPL-3.0-only
"""Independent checks of the reusable G0/G3 broker configuration."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_profile as profiles
import radio_broker_schedule as schedules
import verify_gradual_noise as example


def inputs():
    profile, _ = profiles.load_profile(example.EXAMPLE / "profile.json")
    programs = {arm: schedules.load_schedule(example.EXAMPLE / f"{arm}.schedule.json")[0] for arm in ("G0", "G3")}
    return profile, programs


def test_example_uses_explicit_fixed_power_levels():
    report = example.verify()
    assert report["status"] == "verified" and report["c_preflight"] == "not_requested"
    assert report["reference_power"] == 10_000_000
    assert report["G3_complex_noise_powers"] == pytest.approx([100_000, 1_000_000, 10_000_000])
    assert report["noise_samples"] == {"G0": 0, "G3": 34_560_000}


def test_matched_markers_powers_neutral_downlink_and_exact_restoration():
    profile, programs = inputs()
    assert profile["master_seed"] == 137 and profile["sample_rate_hz"] == 23_040_000
    assert profile["directions"]["DL"]["mode"] == "identity"
    assert profile["directions"]["UL"]["desired_gain"] == 1
    expected = [(0, "baseline"), (92_160_000, "level-1"), (103_680_000, "level-2"),
                (115_200_000, "level-3"), (126_720_000, "restore")]
    for arm in ("G0", "G3"):
        plan = schedules.parse_wire(schedules.compile_plan(profile, programs[arm]))
        for direction in ("DL", "UL"):
            section = plan.directions[direction]
            assert section.duration_samples == 230_400_000
            assert [(row.sample_offset, row.event_id) for row in section.events] == expected
            assert all(row.settings["gain"] == 1 and not row.settings["cw_enabled"] for row in section.events)
            assert section.events[-1].kind == "restore"
            assert dict(section.events[-1].settings) == dict(section.events[0].settings)
            if arm == "G0" or direction == "DL":
                assert all(not row.settings["noise_enabled"] for row in section.events)
            else:
                assert [row.settings["noise_snr_db"] for row in section.events[1:4]] == [20, 10, 0]
                assert all(row.settings["noise_enabled"] for row in section.events[1:4])
                assert not section.events[0].settings["noise_enabled"]
                assert [section.events[i + 1].sample_offset - section.events[i].sample_offset
                        for i in (1, 2, 3)] == [11_520_000] * 3


def test_original_recipes_are_still_separate_from_example():
    import radio_fault
    assert "G0" not in radio_fault.recipe_names() and "G3" not in radio_fault.recipe_names()
    assert len(radio_fault.recipe_names()) == 19


@pytest.mark.parametrize("change", ["boundary", "power", "dl_noise", "g0_noise", "seed", "restore", "marker"])
def test_condition_rejects_rehashed_changes(change):
    profile, programs = inputs()
    if change == "boundary":
        programs["G3"]["directions"]["UL"]["events"][1]["sample_offset"] += 1
    elif change == "power":
        programs["G3"]["directions"]["UL"]["events"][2]["settings"]["noise_snr_db"] = 20
    elif change in ("dl_noise", "g0_noise"):
        arm, direction = ("G3", "DL") if change == "dl_noise" else ("G0", "UL")
        programs[arm]["directions"][direction]["events"][1]["settings"]["noise_enabled"] = True
    elif change == "seed":
        profile["master_seed"] = 41
    elif change == "restore":
        programs["G3"]["directions"]["UL"]["events"][-1]["kind"] = "set"
    else:
        programs["G0"]["directions"]["UL"]["events"][2]["event_id"] = "changed"
    with pytest.raises(ValueError, match="example"):
        example.check_condition(profile, programs)


def test_file_and_compiled_plan_tampering_fail(tmp_path):
    directory = tmp_path / "example"
    shutil.copytree(example.EXAMPLE, directory)
    path = directory / "G3.schedule.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="file hash differs"):
        example.verify(directory)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["compiled_plan_sha256"]["G3"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="compiled plan differs"):
        example.verify(directory)


def test_production_c_parser_accepts_both_plans_without_runtime_outputs(tmp_path):
    lock = json.loads((ROOT / "dependencies/toolchain.lock.json").read_text())["toolchain"]
    binary = tmp_path / "broker"
    subprocess.run([lock["cc_default"], *lock["cflags"], str(ROOT / "scripts/zmq_channel_broker.c"),
                    "-o", str(binary), *lock["ldlibs"]], check=True, capture_output=True, timeout=60)
    report = example.verify(c_binary=binary)
    assert report["c_preflight"]["status"] == "verified"
    assert report["c_preflight"]["arms"] == ["G0", "G3"]
    assert report["c_preflight"]["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_cli_fails_cleanly_on_invalid_example(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_gradual_noise.py"),
                             "--directory", str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1 and json.loads(result.stderr)["status"] == "failed"
