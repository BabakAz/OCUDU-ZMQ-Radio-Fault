"""A matching C additive control preserves disabled components and finite bounds."""
import hashlib

import pytest

from test_radio_fault_additive import additive_fixture
from test_radio_fault import IDS, fault, schedules, write_evidence


def normal_fixture():
    _, _, ready, receipts, execution, records = additive_fixture("ul_awgn_500ms")
    spec = fault.specification("c_fixed_normal")
    plan = schedules.parse_wire(schedules.compile_plan(fault.radio_profile(spec), fault.schedule(spec, **IDS)))
    ready.update(plan_sha256=plan.sha256, config_sha256=plan.profile_sha256)
    execution["plan_sha256"] = plan.sha256
    for receipt in receipts:
        receipt["response"]["plan_sha256"] = plan.sha256
    for row in records:
        row.update(plan_sha256=plan.sha256, config_sha256=plan.profile_sha256)
        if row["event_type"] in ("condition_applied", "condition_restored"):
            row["details"]["settings"].update(noise_enabled=False, cw_enabled=False, cw_freq_hz=0)
            row["details"]["changed"] = False
    return spec, plan, ready, receipts, execution, records


def test_c_control_matches_both_additive_base_profiles_and_boundaries():
    spec = fault.specification("c_fixed_normal")
    profile = fault.radio_profile(spec)
    assert spec["backend"] == "c"
    assert spec["reference_power"] == 10_000_000 and spec["component_reference_db"] == 10
    assert "additive_component" not in spec and "cw_frequency_hz" not in spec
    assert spec["max_arm_wall_seconds"] == 60 and spec["settle_seconds"] == 5
    assert spec["ul_bitrate"] == spec["dl_bitrate"] == "5M"
    assert not spec["whole_trial_qualified"] and not spec["scientific_dataset_eligible"]
    assert profile["directions"]["DL"]["reference_power"] == 1
    for kind in ("ul_awgn_500ms", "ul_cw_500ms"):
        other = fault.specification(kind)
        assert profile == fault.radio_profile(other)
        assert fault.pulse_intervals(spec) == fault.pulse_intervals(other) == ((92_160_000, 103_680_000, 1),)
    plan = schedules.parse_wire(schedules.compile_plan(profile, fault.schedule(spec, **IDS)))
    for direction in plan.directions.values():
        assert direction.duration_samples == 230_400_000
        assert [e.sample_offset for e in direction.events] == [0, 92_160_000, 103_680_000]
        assert direction.events[0].settings == direction.events[1].settings == direction.events[2].settings
        assert all(e.settings["gain"] == 1 and not e.settings["noise_enabled"] and not e.settings["cw_enabled"] for e in direction.events)
        assert direction.events[-1].kind == "restore"


@pytest.mark.parametrize("field,value", [
    ("backend", "grc"), ("reference_power", 1), ("component_reference_db", 0),
    ("pulse_duration_samples", 1_152_000), ("pulse_count", 2), ("pulse_gain", 0),
    ("max_arm_wall_seconds", 61), ("scientific_dataset_eligible", True),
    ("additive_component", "awgn"), ("cw_frequency_hz", 1_440_000),
])
def test_c_control_rejects_hidden_faults_or_changed_scope(field, value):
    spec = fault.specification("c_fixed_normal")
    spec[field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_spec(spec)


@pytest.mark.parametrize("change", [None, "noise", "cw", "gain", "missing_restore"])
def test_c_control_archived_verification_reports_zero_exposure_and_rejects_faults(tmp_path, change):
    spec, plan, ready, receipts, execution, records = normal_fixture()
    active = next(r for r in records if r["direction"] == "UL" and r["details"].get("event_id") == "pulse-start")
    if change == "noise": active["details"]["settings"]["noise_enabled"] = True
    elif change == "cw": active["details"]["settings"].update(cw_enabled=True, cw_freq_hz=1_440_000)
    elif change == "gain": active["details"]["settings"]["gain"] = 0
    elif change == "missing_restore":
        records.remove(next(r for r in records if r["direction"] == "UL" and r["event_type"] == "condition_restored"))
        for sequence, row in enumerate(records, 1): row["event_sequence"] = sequence
    evidence = tmp_path / "evidence"; evidence.mkdir(mode=0o700)
    write_evidence(evidence, spec, ready, receipts, execution, records)
    result = fault.verify(evidence, expected_pid=123, expected_build_sha256="b" * 64)
    if change:
        assert not result["qualified"] and result["errors"]
        return
    assert result["qualified"], result["errors"]
    assert result["programmed_ul_noise_samples"] == result["programmed_ul_cw_samples"] == 0
    assert result["programmed_ul_blank_samples"] == result["programmed_ul_attenuated_samples"] == 0
    assert not result["physical_snr_calibrated"] and not result["whole_trial_qualified"]
    assert not result["independent_iq_oracle_performed"]
    assert "broker.log" not in result["references"]


def test_existing_normal_keeps_recorded_specification_and_wire():
    spec = fault.specification("normal")
    wire = schedules.compile_plan(fault.radio_profile(spec), fault.schedule(spec, **IDS))
    assert hashlib.sha256(fault.canonical(spec)).hexdigest() == "5f7388198a32a2a3f1e677febe9e1ab55341dd5ed24756256351211d45fe8d50"
    assert hashlib.sha256(wire).hexdigest() == "c47ee3b69d5e4f468503b1817b277cae6278c325a71d7a849e47020718ae5cd2"
