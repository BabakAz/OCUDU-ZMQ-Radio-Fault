"""Recipe scope, forged evidence and control failure tests; no radio stack.

Ported from the study's pilot tests; fixtures and oracles are unchanged.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_fault as fault
import radio_broker_schedule as schedules

IDS = dict(run_id=str(uuid.UUID(int=1)), trial_id=str(uuid.UUID(int=2)), pipeline_id=str(uuid.UUID(int=3)))


PULSE_ORACLE = {
    "ul_blank_50ms": [(92160000, 93312000, 0.0)],
    "ul_blank_disperse_5x10ms": [(92160000, 92390400, 0.0), (103680000, 103910400, 0.0),
                               (115200000, 115430400, 0.0), (126720000, 126950400, 0.0),
                               (138240000, 138470400, 0.0)],
    "ul_attenuation_500ms": [(92160000, 103680000, 0.125)],
}


def fixture(condition="ul_blank_50ms"):
    """Independent boundary fixture: warmup 11/19, one send per event, then completion."""
    spec = fault.specification(condition)
    intervals = PULSE_ORACLE[condition]
    event_count = 1 + 2 * len(intervals)
    program = fault.schedule(spec, **IDS)
    plan = schedules.parse_wire(schedules.compile_plan(fault.radio_profile(spec), program))
    ready = {"schema_version": "radio_broker_ready_v1", "instance_id": "a" * 32, "backend": "c",
             "pid": 123, "build_sha256": "b" * 64, "config_sha256": plan.profile_sha256,
             "plan_sha256": plan.sha256, "control_socket": "/test/radio/rb.sock"}
    directions = {}
    for name, arm in (("DL", 11), ("UL", 19)):
        directions[name] = {"armed": True, "arm_sample": arm, "processed_samples": arm + 230400000,
                            "forwarded_samples": arm + 230400000, "duration_samples": 230400000,
                            "schedule_complete": True, "next_event": event_count}
    receipts = []
    for sequence, operation, state, start, finish in [(1, "STATUS", "ready", 100, 200),
                                                     (2, "ARM", "arm_pending", 900, 1100),
                                                     (3, "STATUS", "completed", 3000, 3100)]:
        current = copy.deepcopy(directions)
        if sequence < 3:
            for row in current.values():
                row.update(armed=False, arm_sample=None, next_event=0, schedule_complete=False,
                           processed_samples=0, forwarded_samples=0)
        response = {"schema_version": "radio_broker_control_v1", "ok": True,
                    "request_sequence": sequence, "operation": operation,
                    "instance_id": ready["instance_id"], "plan_sha256": plan.sha256,
                    "state": state, "directions": current}
        receipts.append(dict(request_sequence=sequence, operation=operation, response=response,
                             started_monotonic_ns=start, finished_monotonic_ns=finish))
    execution = {"schema_version": fault.EXECUTION_SCHEMA, "qualified": True,
                 "errors": [], "receipts": receipts, "plan_sha256": plan.sha256,
                 "expected_pid": 123, "expected_build_sha256": ready["build_sha256"],
                 "directions": directions}
    records = []
    request = dict(request_sequence=2, request_monotonic_ns=1000, request_wall_ns=1000)
    envelope = {k: ready[k] for k in ("instance_id", "backend", "build_sha256", "config_sha256", "plan_sha256")}
    def add(direction, kind, start=None, end=None, details=None):
        clock = {"started": 100, "ready": 200, "arm_requested": 1000, "final": 5000}.get(kind, 1500 + len(records))
        records.append({"schema_version": "radio_broker_truth_v1", **envelope,
                        **{key: getattr(plan, key) for key in schedules.ID_FIELDS},
                        "event_sequence": len(records) + 1, "event_type": kind, "direction": direction,
                        "monotonic_ns": clock, "wall_ns": clock, "sample_start": start, "sample_end": end,
                        "scope": "control" if direction == "control" else
                        "successfully_forwarded_samples" if kind == "progress" else "processed_samples",
                        "details": details or {}})
    add("control", "started"); add("control", "ready"); add("control", "arm_requested", details=request)
    for direction, arm in (("DL", 11), ("UL", 19)):
        add(direction, "armed", arm, arm, {**request, "arm_sample": arm, "duration_samples": 230400000,
            "state_at_arm": {"sample_clock": arm, "awgn_complex_draws": arm if direction == 'UL' else 0,
                             "awgn_normal_draws": 2 * arm if direction == 'UL' else 0,
                             "mode": "identity" if direction == 'DL' else 'fixed', "ref_power": 1,
                             "sample_rate_hz": 23040000, "master_seed": 41,
                             "rng_version": "component_streams_v1", "rng_algorithm": "glibc_rand_r_box_muller_pair_f32",
                             "settings": {"gain": 1, "noise_enabled": False, "noise_snr_db": 0,
                                          "cw_enabled": False, "cw_sir_db": 0, "cw_freq_hz": 0},
                             "phase_u64": 0, "cw_step_u64": 0, "masked_samples": 0, "attenuated_samples": 0,
                             "noise_std": math.sqrt(.5), "cw_amplitude": 1}})
        settings = {"gain": 1, "noise_enabled": False, "noise_snr_db": 0,
                    "cw_enabled": False, "cw_sir_db": 0, "cw_freq_hz": 0}
        transitions = [(0, "baseline", "set", 1)]
        for index, (begin, end, gain) in enumerate(intervals, 1):
            name = "pulse" if len(intervals) == 1 else f"pulse-{index}"
            transitions += [(begin, name + "-start", "set", gain if direction == "UL" else 1),
                            (end, name + "-end", "restore", 1)]
        for offset, event_id, kind, gain in transitions:
            end_offset = offset + 1
            changed = gain != settings["gain"]
            settings = dict(settings, gain=gain)
            add(direction, "condition_restored" if kind == "restore" else "condition_applied",
                arm + offset, arm + end_offset, {"event_id": event_id, "kind": kind,
                "sample_offset": offset, "changed": changed, "settings": settings,
                "processed_samples": arm + end_offset})
        add(direction, "progress", arm, arm + 230400000, {"input_messages": event_count + 1, "output_messages": event_count + 1,
            "input_samples": arm + 230400000, "output_samples": arm + 230400000,
            "scheduled_end_sample": arm + 230400000, "schedule_complete": True})
    for direction, arm in (("DL", 11), ("UL", 19)):
        count = arm + 230400000
        add(direction, "final", 0, count, {"status": "complete", "reason": "completed",
            "armed_sample": arm, "scheduled_end_sample": count, "processed_samples": count,
            "forwarded_samples": count, "input_samples": count, "output_samples": count,
            "input_messages": event_count + 1, "output_messages": event_count + 1, "logging_errors": 0,
            "all_events_processed": True, "restoration_observed": True, "schedule_complete": True})
    return spec, plan, ready, receipts, execution, records


STUDY_IDS = dict(study_id=IDS["run_id"], trial_id=IDS["trial_id"], pipeline_id=IDS["pipeline_id"])


def write_evidence(directory, spec, ready, receipts, execution, records, *, identity=None,
                   realizations=None, control_directory="/test/radio"):
    """Lay out one stopped trial exactly as prepare, arm and the broker leave it."""
    profile = fault.radio_profile(spec)
    program = fault.schedule(spec, **IDS)
    wire = schedules.compile_plan(profile, program)
    plan = schedules.parse_wire(wire)
    preparation = {"schema_version": fault.PREPARATION_SCHEMA, **STUDY_IDS,
                   "control_directory": control_directory, "backend": spec["backend"],
                   "specification": spec, "plan_sha256": plan.sha256,
                   "config_sha256": plan.profile_sha256, "helper_sha256": "a" * 64,
                   "radio_configs": None,
                   "argv": fault.expected_broker_arguments(control_directory, profile, spec["backend"])}
    if identity is not None:
        preparation["broker_source_identity"] = identity
    if realizations is not None:
        preparation["tdl_realizations"] = realizations
    documents = {"recipe.json": spec, "schedule.json": program, "preparation.json": preparation,
                 "broker_ready.json": ready, "execution.json": execution}
    for name, data in documents.items():
        fault.write_private(directory / name, fault.canonical(data))
    fault.write_private(directory / "profile.json", fault.profiles.canonical_bytes(profile))
    fault.write_private(directory / "plan.wire", wire)
    for name, rows in (("control.jsonl", receipts), ("broker_events.jsonl", records)):
        fault.write_private(directory / name, b"".join(map(fault.canonical, rows)))
    return preparation


def test_exact_sample_profile_is_directional_and_bounded():
    spec, plan, *_ = fixture()
    assert plan.sample_rate_hz == 23040000
    assert plan.directions["UL"].duration_samples == 230400000
    assert [e.sample_offset for e in plan.directions["UL"].events] == [0, 92160000, 93312000]
    assert plan.directions["DL"].base["mode"] == "identity"
    assert all(e.settings["gain"] == 1 for e in plan.directions["DL"].events)
    assert all(not e.settings["noise_enabled"] and not e.settings["cw_enabled"]
               for d in plan.directions.values() for e in d.events)
    normal = fault.schedule(fault.specification("normal"), **IDS)
    assert all(e["settings"]["gain"] == 1 for d in normal["directions"].values() for e in d["events"])
    assert not spec["scientific_dataset_eligible"]


@pytest.mark.parametrize('condition', PULSE_ORACLE)
def test_selected_intervals_exposure_and_restoration_match_independent_boundaries(condition):
    spec, plan, ready, receipts, execution, records = fixture(condition)
    expected = PULSE_ORACLE[condition]
    assert fault.pulse_intervals(spec) == tuple(expected)
    expected_offsets = [0] + [offset for start, end, _ in expected for offset in (start, end)]
    for direction in ('UL', 'DL'):
        events = plan.directions[direction].events
        assert [event.sample_offset for event in events] == expected_offsets
        assert len(events) <= 32 and plan.directions[direction].duration_samples == 230400000
        assert all(not event.settings['noise_enabled'] and not event.settings['cw_enabled'] for event in events)
        assert all(event.settings['gain'] == 1 for event in events[::2])
        assert all(event.kind == 'restore' for event in events[2::2])
        assert [event.settings['gain'] for event in events[1::2]] == [gain if direction == 'UL' else 1 for _, _, gain in expected]
    completed = fault.validate_control(receipts, execution, plan, ready)
    result = fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=completed)
    assert all(direction['restoration_verified'] for direction in result.values())


@pytest.mark.parametrize('condition,field,value', [
    ('ul_blank_disperse_5x10ms', 'pulse_count', 4),
    ('ul_blank_disperse_5x10ms', 'pulse_spacing_samples', 11520001),
    ('ul_blank_disperse_5x10ms', 'pulse_duration_samples', 230401),
    ('ul_blank_disperse_5x10ms', 'pulse_gain', False),
    ('ul_attenuation_500ms', 'pulse_gain', 0.25),
    ('ul_attenuation_500ms', 'pulse_duration_samples', 11520001),
    ('ul_attenuation_500ms', 'pulse_count', 2),
    ('ul_attenuation_500ms', 'pulse_start_sample', 0),
])
def test_new_condition_parameters_are_fixed_not_arbitrary_schedules(condition, field, value):
    spec = fault.specification(condition)
    spec[field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_spec(spec)


@pytest.mark.parametrize('change', ['missing_middle_restore', 'extra_pulse', 'shifted_onset', 'changed_gain'])
def test_dispersed_truth_cannot_hide_or_change_a_middle_pulse(change):
    _, plan, ready, receipts, execution, records = fixture('ul_blank_disperse_5x10ms')
    middle = next(r for r in records if r['direction'] == 'UL' and r['details'].get('event_id') == 'pulse-3-start')
    if change == 'missing_middle_restore':
        records.remove(next(r for r in records if r['direction'] == 'UL' and r['details'].get('event_id') == 'pulse-3-end'))
    elif change == 'extra_pulse':
        records.insert(records.index(middle), copy.deepcopy(middle))
    elif change == 'shifted_onset':
        middle['sample_start'] += 1
        middle['sample_end'] += 1
        middle['details']['sample_offset'] += 1
        middle['details']['processed_samples'] += 1
    else:
        middle['details']['settings']['gain'] = 0.125
    # Keep event numbering consistent so the condition/population check must catch this.
    for index, record in enumerate(records, 1):
        record['event_sequence'] = index
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution['directions'])


@pytest.mark.parametrize('condition,spec_sha,wire_sha', [
    ('normal', '5f7388198a32a2a3f1e677febe9e1ab55341dd5ed24756256351211d45fe8d50',
     'c47ee3b69d5e4f468503b1817b277cae6278c325a71d7a849e47020718ae5cd2'),
    ('ul_blank_50ms', 'ad11121eb6e3d573fef0f4469c47b5eed81e949baf7067b1bf317d35bb070cdc',
     'a7ca7c69af18b492436f0e8e3235c1b9814e5fb744453950e4228f66f3fd6ae1'),
])
def test_completed_conditions_keep_their_recorded_specification_and_wire_identity(condition, spec_sha, wire_sha):
    # Reference identities generated at completed-pilot revision e37f01f with IDS above.
    spec = fault.specification(condition)
    wire = schedules.compile_plan(fault.radio_profile(spec), fault.schedule(spec, **IDS))
    assert hashlib.sha256(fault.canonical(spec)).hexdigest() == spec_sha
    assert hashlib.sha256(wire).hexdigest() == wire_sha


@pytest.mark.parametrize('field,value', [('duration_samples', 230400001), ('backend', 'grc'),
    ('pulse_duration_samples', 2304000), ('max_arm_wall_seconds', 300), ('affected_direction', 'DL'),
    ('master_seed', True), ('sample_rate_hz', 23040000.0), ('scientific_dataset_eligible', True)])
def test_recipe_cannot_silently_expand_scope(field, value):
    spec = fault.specification('ul_blank_50ms'); spec[field] = value
    with pytest.raises(fault.FaultError): fault.validate_spec(spec)


def test_truth_and_control_reconcile_without_waveform_or_window_claim():
    _, plan, ready, receipts, execution, records = fixture()
    completed = fault.validate_control(receipts, execution, plan, ready)
    result = fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=completed)
    assert result['DL']['arm_sample'] == 11 and result['UL']['arm_sample'] == 19
    assert result['UL']['sample_accounting_source'] == 'broker_self_report'


@pytest.mark.parametrize('kind,field,value', [('condition_applied', 'sample_start', 12),
    ('condition_restored', 'scope', 'successfully_forwarded_samples'), ('progress', 'sample_end', 20),
    ('armed', 'sample_start', 0), ('final', 'event_sequence', 0), ('ready', 'plan_sha256', '0'*64)])
def test_forged_truth_boundary_or_binding_rejected(kind, field, value):
    _, plan, ready, receipts, execution, records = fixture()
    next(r for r in records if r['event_type'] == kind)[field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution['directions'])


@pytest.mark.parametrize('field,value', [('status', 'incomplete'), ('reason', 'unarmed_or_partial'),
    ('logging_errors', 1), ('restoration_observed', False), ('schedule_complete', False),
    ('forwarded_samples', 1), ('output_messages', 3), ('processed_samples', True)])
def test_failed_or_conflicting_final_never_qualifies(field, value):
    _, plan, ready, receipts, execution, records = fixture()
    records[-1]['details'][field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution['directions'])


@pytest.mark.parametrize('change', ['missing_final', 'duplicate_event', 'control_clock', 'changed_epoch', 'overdue'])
def test_missing_repeated_or_inconsistent_evidence_fails(change):
    _, plan, ready, receipts, execution, records = fixture()
    if change == 'missing_final': records.pop()
    if change == 'duplicate_event': records.insert(5, copy.deepcopy(records[4]))
    if change == 'control_clock': receipts[1]['finished_monotonic_ns'] = 950
    if change == 'changed_epoch': execution['directions']['UL']['arm_sample'] = 20
    if change == 'overdue': receipts[-1]['finished_monotonic_ns'] += 61_000_000_000
    with pytest.raises((fault.FaultError, fault.control.ControlError)):
        completed = fault.validate_control(receipts, execution, plan, ready)
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=completed)


def test_prepare_binds_exact_inputs_and_never_prints_the_token():
    import tempfile
    spec = fault.specification("ul_blank_50ms")
    # pytest temp paths can exceed the UNIX socket bound; use a short owned
    # directory for preparation only. No sockets or processes are created.
    with tempfile.TemporaryDirectory(prefix="rf-test-", dir="/tmp") as name:
        directory = Path(name) / "trial"
        result = fault.prepare(spec, directory, **STUDY_IDS)
        token = (directory / "control.token").read_text()
        assert len(token) == 64 and token not in json.dumps(result)
        assert "token_path" not in result
        assert result["argv"] == fault.expected_broker_arguments(directory, fault.radio_profile(spec), "c")
        assert schedules.load_wire(directory / "plan.wire").trial_id == IDS["trial_id"]
        assert (directory / "recipe.json").read_bytes() == fault.canonical(spec)
        assert result["plan_sha256"] == hashlib.sha256((directory / "plan.wire").read_bytes()).hexdigest()
        assert {path.name: oct(path.stat().st_mode & 0o777) for path in directory.iterdir()} == dict.fromkeys(
            ["plan.wire", "control.token", "recipe.json", "profile.json", "schedule.json", "preparation.json"], "0o600")
        with pytest.raises(FileExistsError):
            fault.prepare(spec, directory, **STUDY_IDS)


@pytest.mark.parametrize("change", ["long_path", "bad_uuid", "changed_spec", "rate", "half_config"])
def test_prepare_rejects_unsafe_or_inconsistent_inputs(tmp_path, change):
    spec = fault.specification("normal")
    directory, ids, kwargs = tmp_path / "trial", dict(STUDY_IDS), {}
    if change == "long_path":
        directory = tmp_path / ("x" * 120)
    elif change == "bad_uuid":
        ids["trial_id"] = "not-a-uuid"
    elif change == "changed_spec":
        spec["pulse_start_sample"] += 1
    elif change == "rate":
        ue = tmp_path / "ue.conf"
        ue.write_text((ROOT / "config/examples/ue_zmq.conf").read_text().replace("srate = 23.04e6", "srate = 30.72e6"))
        kwargs = dict(gnb_config=ROOT / "config/examples/gnb_zmq_broker.yml", ue_config=ue)
    else:
        kwargs = dict(gnb_config=ROOT / "config/examples/gnb_zmq_broker.yml")
    with pytest.raises(ValueError):
        fault.prepare(spec, directory, **ids, **kwargs)
    assert not directory.exists()


def test_prepare_checks_and_records_matching_radio_configurations():
    import tempfile
    gnb, ue = ROOT / "config/examples/gnb_zmq_broker.yml", ROOT / "config/examples/ue_zmq.conf"
    with tempfile.TemporaryDirectory(prefix="rf-test-", dir="/tmp") as name:
        result = fault.prepare(fault.specification("normal"), Path(name) / "t", **STUDY_IDS,
                               gnb_config=gnb, ue_config=ue)
    assert result["radio_configs"] == {"gnb_config_sha256": hashlib.sha256(gnb.read_bytes()).hexdigest(),
                                       "ue_config_sha256": hashlib.sha256(ue.read_bytes()).hexdigest()}


def test_control_failure_retains_receipt_and_failed_execution(tmp_path, monkeypatch):
    directory = tmp_path / 'radio'; directory.mkdir(mode=0o700)
    preparation = {'control_directory': str(directory), 'specification': fault.specification('normal'),
                   'plan_sha256': 'a'*64}
    fault.write_private(directory / 'preparation.json', fault.canonical(preparation))
    class FailedClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): raise fault.control.ControlError('control_timeout')
        def __exit__(self, *args): pass
    monkeypatch.setattr(fault.control, 'ControlClient', FailedClient)
    with pytest.raises(fault.control.ControlError):
        fault.run_schedule(directory, expected_pid=123, expected_build_sha256='b'*64,
                           progress_callback=lambda: None, record_callback=lambda _: None)
    result = json.loads((directory / 'execution.json').read_text())
    assert result['qualified'] is False and 'control_timeout' in result['errors'][0]
    assert (directory / 'control.jsonl').read_bytes() == b''


def test_jsonl_truncation_and_oversized_record_fail():
    for raw in (b'{"a":1}', b'{}\n'*257, b'{"x":"' + b'x'*4096 + b'"}\n'):
        with pytest.raises(fault.FaultError): fault.read_jsonl(raw, maximum_records=256)


@pytest.mark.parametrize('kind,change', [('progress', 'zero_messages'), ('final', 'zero_messages'),
                                      ('progress', 'early_clock'), ('final', 'early_clock')])
def test_impossible_message_count_or_time_never_passes(kind, change):
    _, plan, ready, receipts, execution, records = fixture()
    row = next(r for r in records if r['event_type'] == kind)
    if change == 'zero_messages': row['details'].update(input_messages=0, output_messages=0)
    else: row['monotonic_ns'] = 1
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution['directions'])


@pytest.mark.parametrize('field,value', [('sample_rate_hz', 1), ('mode', 'identity'),
    ('rng_version', 'madeup'), ('master_seed', 40), ('ref_power', 2), ('noise_std', 0),
    ('phase_u64', 1), ('cw_amplitude', True), ('awgn_complex_draws', 0)])
def test_changed_arm_state_cannot_qualify_selected_profile(field, value):
    _, plan, ready, receipts, execution, records = fixture()
    row = next(r for r in records if r['event_type'] == 'armed' and r['direction'] == 'UL')
    row['details']['state_at_arm'][field] = value
    with pytest.raises(fault.FaultError):
        fault.validate_truth(records, plan=plan, ready=ready, receipts=receipts, completed=execution['directions'])


@pytest.mark.parametrize('condition', PULSE_ORACLE)
@pytest.mark.parametrize('change', [None, 'pid', 'build', 'different_trial', 'changed_condition', 'argv'])
def test_archived_verification_binds_to_external_expectations(tmp_path, change, condition):
    spec, plan, ready, receipts, execution, records = fixture(condition)
    evidence = tmp_path / 'evidence'; evidence.mkdir(mode=0o700)
    preparation = write_evidence(evidence, spec, ready, receipts, execution, records)
    expected = dict(expected_pid=123, expected_build_sha256='b'*64)
    if change == 'pid': expected['expected_pid'] = 124
    if change == 'build': expected['expected_build_sha256'] = 'd'*64
    if change == 'different_trial': expected['trial_id'] = str(uuid.uuid4())
    if change == 'changed_condition':
        (evidence / 'recipe.json').unlink()
        fault.write_private(evidence / 'recipe.json', fault.canonical(fault.specification('normal')))
    if change == 'argv':
        preparation['argv'] = preparation['argv'] + ['--dl-mode', 'fixed']
        (evidence / 'preparation.json').unlink()
        fault.write_private(evidence / 'preparation.json', fault.canonical(preparation))
    result = fault.verify(evidence, **expected)
    assert result['qualified'] is (change is None), result['errors']
    assert not result['whole_trial_qualified'] and not result['matched_radio_windows_qualified']
    if change is None:
        assert result['identity_sources'] == {'pid': 'argument', 'build_sha256': 'argument'}
        assert result['programmed_ul_blank_samples'] == (0 if condition == 'ul_attenuation_500ms' else 1152000)
        assert result['programmed_ul_attenuated_samples'] == (11520000 if condition == 'ul_attenuation_500ms' else 0)
        # Without explicit expectations the arm-time bindings are used and reported.
        defaulted = fault.verify(evidence)
        assert defaulted['qualified'] and defaulted['identity_sources'] == {
            'pid': 'execution_record', 'build_sha256': 'execution_record'}


def test_unfinished_sample_schedule_has_a_wall_deadline_and_retained_failure(tmp_path, monkeypatch):
    directory = tmp_path / 'radio'; directory.mkdir(mode=0o700)
    _, plan, ready, receipts, _, _ = fixture()
    preparation = {'control_directory': str(directory), 'specification': fault.specification('normal'),
                   'plan_sha256': plan.sha256}
    fault.write_private(directory / 'preparation.json', fault.canonical(preparation))
    class StalledClient:
        def __init__(self, *args, **kwargs): self.plan=plan; self.sequence=1
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def request(self, operation):
            row=copy.deepcopy(receipts[0 if self.sequence == 1 else 1]['response'])
            row.update(request_sequence=self.sequence, operation=operation)
            self.sequence+=1
            return row
    tick=[0]
    def clock(): tick[0]+=1_000_000_000; return tick[0]
    monkeypatch.setattr(fault.control, 'ControlClient', StalledClient)
    monkeypatch.setattr(fault.time, 'monotonic_ns', clock)
    monkeypatch.setattr(fault.time, 'sleep', lambda _:None)
    with pytest.raises(fault.FaultError, match='wall-time limit'):
        fault.run_schedule(directory, expected_pid=123, expected_build_sha256='b'*64,
                           progress_callback=lambda:None, record_callback=lambda _:None)
    result=json.loads((directory / 'execution.json').read_text())
    assert not result['qualified'] and 2 < len(result['receipts']) < 64
    assert sum(row['operation']=='ARM' for row in result['receipts']) == 1
