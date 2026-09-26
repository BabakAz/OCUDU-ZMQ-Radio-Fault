"""Selected GRC-only CFO support, source binding and forged-truth checks."""
import copy

import pytest

from test_radio_fault import IDS, STUDY_IDS, fault, fixture, schedules, write_evidence


KINDS = ("grc_normal", "ul_cfo_500ms")


def cfo_fixture(kind):
    _, _, ready, receipts, execution, records = fixture("ul_attenuation_500ms")
    spec = fault.specification(kind)
    profile = fault.radio_profile(spec)
    plan = schedules.parse_wire(schedules.compile_plan(profile, fault.schedule(spec, **IDS)))
    identity = fault.expected_grc_source_identity()
    ready.update(backend="grc", plan_sha256=plan.sha256, config_sha256=plan.profile_sha256,
                 build_sha256=identity["build_sha256"])
    execution.update(plan_sha256=plan.sha256, expected_build_sha256=identity["build_sha256"])
    for receipt in receipts:
        receipt["response"]["plan_sha256"] = plan.sha256
    for row in records:
        row.update(backend="grc", plan_sha256=plan.sha256, config_sha256=plan.profile_sha256,
                   build_sha256=identity["build_sha256"])
        direction, detail = row["direction"], row["details"]
        if direction == "control":
            continue
        if row["event_type"] == "armed":
            state = detail["state_at_arm"]
            state.update(state.pop("settings"))
            state.update(schema_version="radio_grc_cfo_profile_v1", record_type="started",
                         backend="grc", direction=direction, channel_semantics_version="grc_cfo_v1",
                         cfo_hz=0, cfo_phase_rad=0, cfo_applied_samples=0,
                         rng_algorithm="none_cfo_only", awgn_complex_draws=0, awgn_normal_draws=0)
        if row["event_type"] in ("condition_applied", "condition_restored"):
            active = kind == "ul_cfo_500ms" and direction == "UL"
            detail["settings"].update(gain=1, cfo_hz=500 if active and detail["event_id"] == "pulse-start" else 0)
            detail["changed"] = active and detail["event_id"] != "baseline"
        if row["event_type"] == "final":
            detail["reason"] = "none"
            state = copy.deepcopy(next(r["details"]["state_at_arm"] for r in records
                                       if r["direction"] == direction and r["event_type"] == "armed"))
            state.update(record_type="final", sample_clock=detail["processed_samples"],
                         cfo_applied_samples=11_520_000 if kind == "ul_cfo_500ms" and direction == "UL" else 0,
                         noise_energy=0, cw_energy=0)
            detail["state_at_finish"] = state
    return spec, plan, ready, receipts, execution, records, identity


@pytest.mark.parametrize("kind", KINDS)
def test_grc_selected_support_is_cfo_only_with_restorative_dl_identity(kind):
    spec, plan, ready, receipts, execution, records, _ = cfo_fixture(kind)
    assert spec["backend"] == "grc"
    assert fault.pulse_intervals(spec) == ((92_160_000, 103_680_000, 1),)
    assert plan.channel_semantics_version == "grc_cfo_v1"
    assert [event.sample_offset for event in plan.directions["UL"].events] == [0, 92_160_000, 103_680_000]
    assert [event.settings["cfo_hz"] for event in plan.directions["UL"].events] == [0, 500 if kind == "ul_cfo_500ms" else 0, 0]
    assert all(event.settings["cfo_hz"] == 0 for event in plan.directions["DL"].events)
    assert all(event.settings["gain"] == 1 and not event.settings["noise_enabled"]
               and not event.settings["cw_enabled"] for d in plan.directions.values() for event in d.events)
    completed = fault.validate_control(receipts, execution, plan, ready)
    result = fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=completed)
    assert all(value["restoration_verified"] for value in result.values())
    with pytest.raises(ValueError):
        fault.profiles.broker_arguments(fault.radio_profile(spec), "c")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field,value", [("backend", "c"), ("cfo_hz", -500), ("cfo_hz", True),
                                        ("pulse_duration_samples", 11_520_001), ("pulse_gain", .5),
                                        ("affected_direction", "DL"), ("duration_samples", 230_400_001)])
def test_grc_recipe_rejects_changed_scope(kind, field, value):
    spec = fault.specification(kind); spec[field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_spec(spec)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", ["dl_fault", "frequency", "boundary", "restore", "rng",
                                    "exposure", "float_count", "phase", "phase_nan", "added_noise",
                                    "missing_final", "arm_exposure", "arm_component", "final_mode", "final_reason"])
def test_grc_audit_rejects_wrong_component_support_or_dsp_state(kind, change):
    _, plan, ready, receipts, execution, records, _ = cfo_fixture(kind)
    arm = next(r["details"]["state_at_arm"] for r in records if r["direction"] == "UL" and r["event_type"] == "armed")
    active = next(r for r in records if r["direction"] == "UL" and r["details"].get("event_id") == "pulse-start")
    restore = next(r for r in records if r["direction"] == "UL" and r["event_type"] == "condition_restored")
    final = next(r["details"] for r in records if r["direction"] == "UL" and r["event_type"] == "final")
    if change == "dl_fault":
        next(r["details"]["settings"] for r in records if r["direction"] == "DL" and r["details"].get("event_id") == "pulse-start")["cfo_hz"] = 500
    elif change == "frequency": active["details"]["settings"]["cfo_hz"] += 1
    elif change == "boundary": active["sample_start"] += 1
    elif change == "restore": restore["details"]["settings"]["cfo_hz"] = 500
    elif change == "rng": final["state_at_finish"]["awgn_complex_draws"] = 1
    elif change == "exposure": final["state_at_finish"]["cfo_applied_samples"] += 1
    elif change == "float_count": final["state_at_finish"]["cfo_applied_samples"] = float(final["state_at_finish"]["cfo_applied_samples"])
    elif change == "phase": final["state_at_finish"]["cfo_phase_rad"] = 0.1
    elif change == "phase_nan": final["state_at_finish"]["cfo_phase_rad"] = float("nan")
    elif change == "added_noise": final["state_at_finish"]["noise_energy"] = 1
    elif change == "missing_final": final.pop("state_at_finish")
    elif change == "arm_exposure": arm["cfo_applied_samples"] = 1
    elif change == "arm_component": arm["noise_enabled"] = True
    elif change == "final_mode": final["state_at_finish"]["mode"] = "identity"
    elif change == "final_reason": final["reason"] = "completed"
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution["directions"])


@pytest.mark.parametrize("kind", KINDS)
def test_archived_grc_verification_binds_source_composite_and_cfo_exposure(tmp_path, kind):
    spec, plan, ready, receipts, execution, records, identity = cfo_fixture(kind)
    evidence = tmp_path / "evidence"; evidence.mkdir(mode=0o700)
    write_evidence(evidence, spec, ready, receipts, execution, records, identity=identity)
    result = fault.verify(evidence, expected_pid=123, expected_build_sha256=identity["build_sha256"])
    assert result["qualified"], result["errors"]
    assert result["programmed_ul_cfo_samples"] == (11_520_000 if kind == "ul_cfo_500ms" else 0)
    assert result["programmed_ul_blank_samples"] == result["programmed_ul_attenuated_samples"] == 0
    result = fault.verify(evidence, expected_pid=123, expected_build_sha256="f" * 64)
    assert not result["qualified"]
    assert any("implementation binding" in error for error in result["errors"])
