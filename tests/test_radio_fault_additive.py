"""Fixed additive-recipe power, support and forged-truth checks; no radio stack."""
import copy
import math

import pytest

from test_radio_fault import IDS, STUDY_IDS, fault, fixture, schedules, write_evidence


KINDS = ("ul_awgn_500ms", "ul_cw_500ms")


def additive_fixture(kind):
    # Reuse only the existing control/lifecycle scaffold. The new measurement
    # requirements below are literal independent sample and power expectations.
    _, _, ready, receipts, execution, records = fixture("ul_attenuation_500ms")
    spec = fault.specification(kind)
    profile = fault.radio_profile(spec)
    plan = schedules.parse_wire(schedules.compile_plan(profile, fault.schedule(spec, **IDS)))
    ready.update(plan_sha256=plan.sha256, config_sha256=plan.profile_sha256)
    execution["plan_sha256"] = plan.sha256
    for receipt in receipts:
        receipt["response"]["plan_sha256"] = plan.sha256
    for row in records:
        row.update(plan_sha256=plan.sha256, config_sha256=plan.profile_sha256)
        if row["direction"] != "UL":
            continue
        detail = row["details"]
        if row["event_type"] == "armed":
            state = detail["state_at_arm"]
            state.update(ref_power=10_000_000, noise_std=math.sqrt(500_000), cw_amplitude=1000)
            state["settings"].update(noise_snr_db=10, cw_sir_db=10)
        if row["event_type"] in ("condition_applied", "condition_restored"):
            detail["settings"].update(gain=1, noise_snr_db=10, cw_sir_db=10)
            if detail["event_id"] == "pulse-start":
                if kind == "ul_awgn_500ms":
                    detail["settings"]["noise_enabled"] = True
                else:
                    detail["settings"].update(cw_enabled=True, cw_freq_hz=1_440_000)
    return spec, plan, ready, receipts, execution, records


@pytest.mark.parametrize("kind", KINDS)
def test_selected_addition_has_fixed_power_and_sample_support(kind):
    spec, plan, ready, receipts, execution, records = additive_fixture(kind)
    assert spec["reference_power"] == 10_000_000
    assert spec["component_reference_db"] == 10
    assert spec["pulse_gain"] == 1
    assert fault.pulse_intervals(spec) == ((92_160_000, 103_680_000, 1),)
    ul = plan.directions["UL"]
    assert ul.base["ref_power"] == 10_000_000
    assert ul.duration_samples == 230_400_000
    assert [event.sample_offset for event in ul.events] == [0, 92_160_000, 103_680_000]
    assert all(event.settings["gain"] == 1 for event in ul.events)
    assert ul.events[0].settings == ul.events[-1].settings
    assert ul.events[-1].kind == "restore"
    assert not ul.events[0].settings["noise_enabled"] and not ul.events[0].settings["cw_enabled"]
    active = ul.events[1].settings
    assert active["noise_enabled"] is (kind == "ul_awgn_500ms")
    assert active["cw_enabled"] is (kind == "ul_cw_500ms")
    assert active["cw_freq_hz"] == (1_440_000 if kind == "ul_cw_500ms" else 0)
    for event in plan.directions["DL"].events:
        assert event.settings["gain"] == 1
        assert not event.settings["noise_enabled"] and not event.settings["cw_enabled"]
    completed = fault.validate_control(receipts, execution, plan, ready)
    result = fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=completed)
    assert result["UL"]["restoration_verified"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field,value", [
    ("reference_power", 1), ("component_reference_db", 0),
    ("pulse_duration_samples", 11_520_001), ("pulse_count", 2),
    ("pulse_gain", 0), ("reference_provenance", "unbound"),
    ("cw_frequency_hz", -1_440_000),
])
def test_additive_scope_cannot_silently_change(kind, field, value):
    spec = fault.specification(kind)
    spec[field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_spec(spec)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", ["coefficient", "wrong_component", "one_sample_late",
                                   "no_restoration", "dl_addition", "level", "tone"])
def test_additive_truth_rejects_changed_power_method_support_or_direction(kind, change):
    _, plan, ready, receipts, execution, records = additive_fixture(kind)
    active = next(r for r in records if r["direction"] == "UL" and r["details"].get("event_id") == "pulse-start")
    if change == "coefficient":
        arm = next(r for r in records if r["direction"] == "UL" and r["event_type"] == "armed")
        arm["details"]["state_at_arm"]["noise_std"] = math.sqrt(0.5)
    elif change == "wrong_component":
        s = active["details"]["settings"]
        s["noise_enabled"], s["cw_enabled"] = s["cw_enabled"], s["noise_enabled"]
    elif change == "one_sample_late":
        active["sample_start"] += 1
        active["sample_end"] += 1
        active["details"]["sample_offset"] += 1
        active["details"]["processed_samples"] += 1
    elif change == "no_restoration":
        restored = next(r for r in records if r["direction"] == "UL" and r["event_type"] == "condition_restored")
        restored["details"]["settings"] = copy.deepcopy(active["details"]["settings"])
    elif change == "dl_addition":
        dl = next(r for r in records if r["direction"] == "DL" and r["details"].get("event_id") == "pulse-start")
        dl["details"]["settings"]["noise_enabled"] = True
    elif change == "level":
        active["details"]["settings"]["noise_snr_db"] = 11
    else:
        active["details"]["settings"]["cw_freq_hz"] += 15_000
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution["directions"])


@pytest.mark.parametrize("kind", KINDS)
def test_archived_additive_verification_counts_component_exposure_separately(tmp_path, kind):
    spec, plan, ready, receipts, execution, records = additive_fixture(kind)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    write_evidence(evidence, spec, ready, receipts, execution, records)
    result = fault.verify(evidence, expected_pid=123, expected_build_sha256="b" * 64)
    assert result["qualified"], result["errors"]
    assert result["programmed_ul_blank_samples"] == result["programmed_ul_attenuated_samples"] == 0
    assert result["programmed_ul_noise_samples"] == (11_520_000 if kind == "ul_awgn_500ms" else 0)
    assert result["programmed_ul_cw_samples"] == (11_520_000 if kind == "ul_cw_500ms" else 0)
    assert result["programmed_additive_complex_power"] == 1_000_000
    assert result["physical_snr_calibrated"] is False
    assert result["whole_trial_qualified"] is False
