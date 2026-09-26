"""Selected GRC fixed-reference recipe contracts, no live radio or sample replay."""
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from test_radio_fault import IDS, STUDY_IDS, fault, fixture, schedules, write_evidence
from ocudu_channel_broker import FixedReferenceChannel


KINDS = ("grc_fixed_normal", "grc_ul_awgn_500ms", "grc_ul_cw_500ms")


def fixed_fixture(kind):
    # Reuse only control/ordering scaffolding. Actual small warmup DSP supplies
    # the GRC record shape; final counters below are a declared synthetic fixture.
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
    external = {}
    for direction, arm in (("DL", 11), ("UL", 19)):
        core = FixedReferenceChannel(direction, plan.sample_rate_hz, plan.master_seed,
                                     **dict(plan.directions[direction].base))
        started = dict(core.record("started"), monotonic_ns=120, wall_ns=120)
        core.process(np.ones(arm, dtype=np.complex64))
        warmup = core.record("started")
        count = arm + 230_400_000
        final = dict(core.record("final"), monotonic_ns=4900, wall_ns=4900, status="stopped",
                     sample_clock=count, awgn_complex_draws=count if direction == "UL" else 0,
                     awgn_normal_draws=2*count if direction == "UL" else 0,
                     input_energy=float(count), desired_energy=float(count), output_energy=float(count))
        if direction == "UL" and kind != "grc_fixed_normal":
            component = "noise" if kind == "grc_ul_awgn_500ms" else "cw"
            final[component + "_energy"] = 11_520_000.0 * 1_000_000.0
            final["output_energy"] += final[component + "_energy"]
        external[direction] = dict(started=started, final=final)
        own = [row for row in records if row["direction"] == direction]
        next(row for row in own if row["event_type"] == "armed")["details"]["state_at_arm"] = warmup
        previous = schedules.mutable_settings(plan.directions[direction].base)
        events = [row for row in own if row["event_type"].startswith("condition_")]
        for row, event in zip(events, plan.directions[direction].events):
            row["details"].update(settings=dict(event.settings), changed=previous != dict(event.settings))
            previous = dict(event.settings)
        next(row for row in own if row["event_type"] == "final")["details"]["reason"] = "none"
    for row in records:
        row.update(backend="grc", plan_sha256=plan.sha256, config_sha256=plan.profile_sha256,
                   build_sha256=identity["build_sha256"])
    return spec, plan, ready, receipts, execution, records, external, identity


def audit(values):
    _, plan, ready, receipts, execution, records, external, _ = values
    completed = fault.validate_control(receipts, execution, plan, ready)
    return fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts,
                                completed=completed, fixed_profiles=external)


@pytest.mark.parametrize("kind", KINDS)
def test_selected_grc_fixed_path_has_literal_support_and_actual_record_shape(kind):
    values = fixed_fixture(kind)
    spec, plan, _, _, _, records, external, _ = values
    assert spec["backend"] == "grc" and plan.channel_semantics_version == "fixed_reference_v1"
    assert fault.pulse_intervals(spec) == ((92_160_000, 103_680_000, 1),)
    assert spec["reference_power"] == 10_000_000 and spec["component_reference_db"] == 10
    for name in ("DL", "UL"):
        events = plan.directions[name].events
        assert [event.sample_offset for event in events] == [0, 92_160_000, 103_680_000]
        assert events[0].settings == events[-1].settings and events[-1].kind == "restore"
        assert all(event.settings["gain"] == 1 for event in events)
        assert events[1].settings["noise_enabled"] is (name == "UL" and kind == "grc_ul_awgn_500ms")
        assert events[1].settings["cw_enabled"] is (name == "UL" and kind == "grc_ul_cw_500ms")
        assert events[1].settings["cw_freq_hz"] == (1_440_000 if name == "UL" and kind == "grc_ul_cw_500ms" else 0)
    assert all("state_at_finish" not in row["details"] for row in records if row["event_type"] == "final")
    assert external["UL"]["final"]["rng_algorithm"] == "numpy.PCG64+standard_normal_float32"
    assert all(row["restoration_verified"] for row in audit(values).values())


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field,value", [
    ("backend", "c"), ("reference_power", 1), ("component_reference_db", 0),
    ("pulse_duration_samples", 11_520_001), ("pulse_gain", .5), ("cfo_hz", 500),
])
def test_grc_fixed_selection_cannot_silently_change_backend_or_condition(kind, field, value):
    spec = fault.specification(kind)
    spec[field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_spec(spec)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", [
    "rng_algorithm", "nested_c_settings", "c_coefficients", "phase", "float_count", "draw_count",
    "rng_state", "rng_increment", "numpy_version", "wrong_component", "restore", "final_count",
    "final_noise_flag", "final_type", "final_mode", "final_status", "final_clock", "missing_final",
    "float_phase", "nonfinite_energy", "wrong_power", "c_final_reason", "dl_addition",
    "nonzero_startup", "energy_regression",
])
def test_fixed_truth_rejects_wrong_backend_shape_or_tampered_measurement(kind, change):
    values = fixed_fixture(kind)
    _, _, _, _, _, records, external, _ = values
    arm = next(row["details"]["state_at_arm"] for row in records if row["direction"] == "UL" and row["event_type"] == "armed")
    final = external["UL"]["final"]
    active = next(row["details"] for row in records if row["direction"] == "UL" and row["details"].get("event_id") == "pulse-start")
    if change == "rng_algorithm": arm["rng_algorithm"] = "glibc_rand_r_box_muller_pair_f32"
    elif change == "nested_c_settings": arm["settings"] = {key: arm.pop(key) for key in schedules.SETTINGS}
    elif change == "c_coefficients": arm["noise_std"] = 707.106
    elif change == "phase": arm["phase_u64"] = 1
    elif change == "float_count": final["awgn_normal_draws"] = float(final["awgn_normal_draws"])
    elif change == "draw_count": final["awgn_normal_draws"] -= 2
    elif change == "rng_state": final["awgn_state_hex"] = "not-pcg64"
    elif change == "rng_increment": final["awgn_increment_hex"] = "0" * 32
    elif change == "numpy_version": arm["rng_library_version"] = "different-library"
    elif change == "wrong_component": active["settings"]["noise_enabled"] = not active["settings"]["noise_enabled"]
    elif change == "restore": next(row["details"]["settings"] for row in records if row["direction"] == "UL" and row["event_type"] == "condition_restored")["gain"] = .5
    elif change == "final_count": final["sample_clock"] -= 1
    elif change == "final_noise_flag": final["noise_enabled"] = True
    elif change == "final_type": final["record_type"] = "started"
    elif change == "final_mode": final["mode"] = "identity"
    elif change == "final_status": final["status"] = "error"
    elif change == "final_clock": final["monotonic_ns"] = 5001
    elif change == "missing_final": external["UL"].pop("final")
    elif change == "float_phase": final["phase_u64"] = 0.0
    elif change == "nonfinite_energy": final["input_energy"] = float("nan")
    elif change == "wrong_power": final["cw_energy"] = 1.0
    elif change == "c_final_reason": next(row["details"] for row in records if row["direction"] == "UL" and row["event_type"] == "final")["reason"] = "completed"
    elif change == "dl_addition": external["DL"]["final"]["noise_energy"] = 1.0
    elif change == "nonzero_startup": external["UL"]["started"]["input_energy"] = 1.0
    elif change == "energy_regression":
        for key in ("input_energy", "desired_energy", "output_energy"):
            external["DL"]["final"][key] = 0.0
    with pytest.raises(fault.FaultError):
        audit(values)


def stdout_bytes(external):
    lines = [b"ordinary broker banner\n"]
    for stage in ("started", "final"):
        for name in ("DL", "UL"):
            lines.append(b"RADIO_FIXED_PROFILE: " + fault.canonical(external[name][stage]))
    return b"".join(lines)


@pytest.mark.parametrize("change", ["duplicate", "missing", "malformed", "unknown"])
def test_grc_stdout_cannot_hide_missing_duplicate_or_malformed_typed_records(change):
    external = fixed_fixture("grc_fixed_normal")[6]
    raw = stdout_bytes(external)
    if change == "duplicate": raw += b"RADIO_FIXED_PROFILE: " + fault.canonical(external["UL"]["final"])
    elif change == "missing": raw = b"\n".join(raw.splitlines()[:-1]) + b"\n"
    elif change == "malformed": raw += b"RADIO_FIXED_PROFILE: {broken}\n"
    else: raw = raw.replace(b'"direction":"UL"', b'"direction":"control"')
    with pytest.raises(ValueError):
        fault.grc_fixed_profiles(raw)


@pytest.mark.parametrize("kind", KINDS)
def test_archived_verification_binds_fixed_grc_stdout_and_programmed_exposure(tmp_path, kind):
    values = fixed_fixture(kind)
    spec, plan, ready, receipts, execution, records, external, identity = values
    evidence = tmp_path / "evidence"; evidence.mkdir(mode=0o700)
    write_evidence(evidence, spec, ready, receipts, execution, records, identity=identity)
    fault.write_private(evidence / "broker.log", stdout_bytes(external))
    result = fault.verify(evidence, expected_pid=123, expected_build_sha256=identity["build_sha256"])
    assert result["qualified"], result["errors"]
    assert result["references"]["broker.log"]["sha256"] == fault.digest(stdout_bytes(external))
    if kind != "grc_fixed_normal":
        assert result["programmed_ul_noise_samples"] == (11_520_000 if kind == "grc_ul_awgn_500ms" else 0)
        assert result["programmed_ul_cw_samples"] == (11_520_000 if kind == "grc_ul_cw_500ms" else 0)
        assert result["programmed_additive_complex_power"] == 1_000_000
    assert not result["whole_trial_qualified"] and not result["independent_iq_oracle_performed"]
    moved = tmp_path / "elsewhere.log"
    (evidence / "broker.log").rename(moved)
    assert not fault.verify(evidence, expected_pid=123, expected_build_sha256=identity["build_sha256"])["qualified"]
    assert fault.verify(evidence, broker_log=moved, expected_pid=123,
                        expected_build_sha256=identity["build_sha256"])["qualified"]


@pytest.mark.parametrize("kind", KINDS)
def test_preparation_explicitly_selects_grc_for_shared_fixed_wire(kind):
    import tempfile
    spec = fault.specification(kind)
    with tempfile.TemporaryDirectory(prefix="fixed-grc-") as short:
        prep = fault.prepare(spec, Path(short) / "t", **STUDY_IDS)
        expected = fault.profiles.broker_arguments(fault.radio_profile(spec), "grc")
        assert prep["argv"][:len(expected)] == expected
        assert prep["specification"]["backend"] == prep["backend"] == "grc"
        assert prep["broker_source_identity"] == fault.expected_grc_source_identity()
