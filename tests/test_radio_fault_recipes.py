# SPDX-License-Identifier: GPL-3.0-only
"""Every recipe keeps the exact identity it had in the paper's study; CLI surface checks.

The pinned digests were generated from the study's own recipe module
(ocudu_observability_pilot.py / ocudu_observability_study.py at the executed
revision) with seed 41 and the identities below. A change here means a trial
prepared with this repository would no longer be the trial the paper ran.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_profile as profiles
import radio_broker_schedule as schedules
import radio_fault as fault

IDS = dict(run_id=str(uuid.UUID(int=1)), trial_id=str(uuid.UUID(int=2)), pipeline_id=str(uuid.UUID(int=3)))

# name: (specification, profile, schedule, compiled wire plan) SHA-256
PINNED = {
    "normal": ("5f7388198a32a2a3f1e677febe9e1ab55341dd5ed24756256351211d45fe8d50",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "8bcf4cebfce5b390d3f2b4e4f94b8422cb3df394a8426d6a3080709ff28afdd0",
        "c47ee3b69d5e4f468503b1817b277cae6278c325a71d7a849e47020718ae5cd2"),
    "c_fixed_normal": ("cdf24e7245b2170e8b96ac1d784692ce5c20e77d6631574fea53e6c77cec6efc",
        "ea32ea264a4d8d30572779ecb715a2570d5382ad80a52d1dc1abd190ec13e033",
        "c91d7b7da63feab69a12479d2bf830d69e1f600511d13721a90ac3e7220940fc",
        "7234cff5bae401ca92bc745406f5ba38cb0ed601a25a231d9a6723f3ea8860a6"),
    "ul_blank_50ms": ("ad11121eb6e3d573fef0f4469c47b5eed81e949baf7067b1bf317d35bb070cdc",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "8a1ad40f322a18e49406020332fa9d124a94b07bf2727cd082b6d06d531e51da",
        "a7ca7c69af18b492436f0e8e3235c1b9814e5fb744453950e4228f66f3fd6ae1"),
    "ul_blank_disperse_5x10ms": ("78b4bc61186a8e0bf7f165033f5db09780bd5cb31b41efccdc2823c728b1a235",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "59f567b3cda805456981b79fc1cfb47e903391ee56fc84f29c4de9e81a2c2019",
        "bf2568812b1c5728fc5d2ce5b89b39bfdda15b71a8a11c44cc3428c085df6bd7"),
    "ul_attenuation_500ms": ("9ad1192ebac607568433fc906c9985a84904bf36d3f866acb6ab7a49b04911c4",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "c9b23bcffc3fe936106da7b0ebd6955694d73bab0866ccdb7143f37e24d98339",
        "176671986d8747b38a151eb8ff5405ce4694b9e2227e42712c79906939252cff"),
    "ul_awgn_500ms": ("137b9c94e1fcde760b2487632b443441ac065b1aa6812bd164c0e16bd6523d60",
        "ea32ea264a4d8d30572779ecb715a2570d5382ad80a52d1dc1abd190ec13e033",
        "9357a41ce80151b2a47678e86ac902316dfafbaf7d24381ecf9b940e5ced1261",
        "32a4a36e8eddd3a1212da4c90f28bfc0e868e04d1c2b2a5ec646719ac100002f"),
    "ul_cw_500ms": ("c7b2ff3583f7666e698e2b28944842a4ef6d9b0d37a4cf252287869f82ecae4b",
        "ea32ea264a4d8d30572779ecb715a2570d5382ad80a52d1dc1abd190ec13e033",
        "5690cfdef1ce4b376db9f482ac20999f67fb0a3517735487882c510752839d8a",
        "2052094672fe4d01447ce2c8fa578fc21d30d4b23bb7d696ed5db8b6a2431bfb"),
    "grc_normal": ("f329591802dc35632603931ae851b20fe0773420c6152d9f595701b1248137a1",
        "d26ec2da08ab35ba8cf9f3f18da0bf45d2811aeaef2f8f8edb8a0712259ba7ad",
        "fe7bf7ebcf15e9591314284a7c813acea20bb0dbcd99ce1a0101e3086921543b",
        "a42336fb1afe4fe1faec4b8acefb8ddaee8c807a77e4f6bb20bc620e1c0cef6d"),
    "ul_cfo_500ms": ("4859c1fde45943b7db09948e4701a2b3a14363863504d3139a08507c82fa1cf3",
        "d26ec2da08ab35ba8cf9f3f18da0bf45d2811aeaef2f8f8edb8a0712259ba7ad",
        "b265e32307a729ae10e594f407ec158fc60af2860027423249cc3ae2c403cd9f",
        "1e1e2fd199eabc7e77176bf910965edb909c99243f87f1e6ed70d52b5b08b152"),
    "grc_fixed_normal": ("b7b1ce03eb02b60058707f89c1b8801a763541b28dbc919c919e03f2255c8460",
        "ea32ea264a4d8d30572779ecb715a2570d5382ad80a52d1dc1abd190ec13e033",
        "c91d7b7da63feab69a12479d2bf830d69e1f600511d13721a90ac3e7220940fc",
        "7234cff5bae401ca92bc745406f5ba38cb0ed601a25a231d9a6723f3ea8860a6"),
    "grc_ul_awgn_500ms": ("0503f4647c769f475e1c213289cb3fad678d54bf3796f47e2fa08c2537fbcce4",
        "ea32ea264a4d8d30572779ecb715a2570d5382ad80a52d1dc1abd190ec13e033",
        "9357a41ce80151b2a47678e86ac902316dfafbaf7d24381ecf9b940e5ced1261",
        "32a4a36e8eddd3a1212da4c90f28bfc0e868e04d1c2b2a5ec646719ac100002f"),
    "grc_ul_cw_500ms": ("336eb3678eb61b25385c6f4638223e0c65b0f25d7676106c835895fd0f8415f4",
        "ea32ea264a4d8d30572779ecb715a2570d5382ad80a52d1dc1abd190ec13e033",
        "5690cfdef1ce4b376db9f482ac20999f67fb0a3517735487882c510752839d8a",
        "2052094672fe4d01447ce2c8fa578fc21d30d4b23bb7d696ed5db8b6a2431bfb"),
    "grc_tdl_a_normal": ("06ac0217b2545a24f5d3fd3577de7984862f71ca605e55956119305d29f51186",
        "ec6d8928b66a240ac4e3701d6f6a9d9957dae74b94739d2002d43278f3bfee33",
        "ba71604566aabb9228c917c018395dcfe037546922e77b4e6073373091210609",
        "e708effa089ea080c5286bd35a362958e495c2d4b5b86c0afdaf286bb36f70c2"),
    "grc_ul_tdl_a_500ms": ("51fb13e508855e4d6e16e26be57e60eb928f8cc51c87fa1849b874a63c29d4e9",
        "ec6d8928b66a240ac4e3701d6f6a9d9957dae74b94739d2002d43278f3bfee33",
        "1598c2d3d41b53810a02c647039d2c864231daa1f281e87fe40675733dc4b8c6",
        "bf440e79184ae7821a733c74d3401df44cbc0215a52ccb25e08a345394dcca96"),
    "grc_tdl_c_normal": ("117c52889a661e293cc4f853beeeafd6b620b1184755f7de14c101151ee3c6c8",
        "664326d0a1caab9c0b9fa03bfdd28e97fbdad8069e11422685963125810f47c5",
        "4ba2796a10fa52ea2911e435eb40bab1a29cc9117a4bc0fdb8e31a4d062ac4d2",
        "8ec048a52b0c43e1f2b73958388358bbbaf49d6bab8336897824f8611cfff01d"),
    "grc_ul_tdl_c_500ms": ("d82bd054bb521fbe6e1d996d93893f1da9e5ae1c44abfb41d26a04e732ef1019",
        "664326d0a1caab9c0b9fa03bfdd28e97fbdad8069e11422685963125810f47c5",
        "7cbf041d1c9967ecd58b8165d9707f8760c72b153b87b8d7fd700e3024ddd509",
        "4fc179f38dca3c085f6ed975a921b897f5eac77600f47e3763c06d64aeb38d73"),
    "N0": ("89a86edbb27f5414a5b089d59bbe9f656c77e8f81e1e8eaf178f6572803c11ee",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "116627ba858149ee4d9375eea5f7f7cabd4e431523dc3b4421315ac1446b253e",
        "32f7d93d73dc8bd08b39185c514361c672c1bf526c58c317faa1ee5b89f8d936"),
    "B1": ("43dbab9ab821361d3045982a1a8ca9180ff8411d87f876450e0d5a6d45751694",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "6f84eedd453237376c4c5870244e63ad8355d6edde75a5b13d4211a0ce7659d4",
        "482df8492c3956ee98156fc2bae803438f46364b404609d5b5d3ccea64fc8049"),
    "B5": ("3545abecef7ca54c99905229a9be29d6d94ae79ab70a4d6619f57531ed5b8395",
        "6e54cc0ca40e8c480134f8b8b3eae95bd81f278969142ec8c7514761ebd29c08",
        "060cf2c744135b004f21c57ccc5378931859b42f5188751b9e5120700abc9b8f",
        "9e3c74829a74e0f250701367e3a4d4618442646c7a496b35978a873f61de5e0e"),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def test_every_recipe_and_v3_arm_is_pinned():
    assert set(PINNED) == set(fault.recipe_names()) == {*fault.KINDS, "N0", "B1", "B5"}
    assert set(fault.DESCRIPTIONS) == set(fault.KINDS)


@pytest.mark.parametrize("name", PINNED)
def test_recipe_reproduces_the_study_identity(name):
    spec = fault.recipe_specification(name)
    profile = fault.radio_profile(spec)
    program = fault.schedule(spec, **IDS)
    wire = schedules.compile_plan(profile, program)
    assert (sha(fault.canonical(spec)), sha(profiles.canonical_bytes(profile)),
            sha(fault.canonical(program)), sha(wire)) == PINNED[name]
    assert fault.validate_spec(spec) == spec
    assert schedules.parse_wire(wire).sha256 == PINNED[name][3]


@pytest.mark.parametrize("arm,kind", [("N0", "normal"), ("B1", "ul_blank_50ms"), ("B5", "ul_blank_disperse_5x10ms")])
def test_v3_arms_differ_from_their_recipe_only_in_study_metadata(arm, kind):
    arm_spec, base = fault.recipe_specification(arm), fault.specification(kind)
    assert {key for key in arm_spec if arm_spec.get(key) != base.get(key)} == {
        "schema_version", "study_arm", "ul_bitrate", "dl_bitrate"}
    assert arm_spec["ul_bitrate"] == arm_spec["dl_bitrate"] == "2M"
    assert fault.pulse_intervals(arm_spec) == fault.pulse_intervals(base)
    assert fault.radio_profile(arm_spec) == fault.radio_profile(base)
    assert fault.schedule(arm_spec, **IDS)["protocol_id"] == "observability-study-v3"
    assert fault.schedule(base, **IDS)["protocol_id"] == "observability-pilot-v1"
    with pytest.raises(fault.FaultError):
        fault.recipe_specification(arm, traffic_bitrate="5M")


def test_blank_arms_have_equal_programmed_blank_samples():
    """The paper's B1/B5 contrast holds the total blanked sample count fixed."""
    def blanked(name):
        return sum(end - start for start, end, gain in fault.pulse_intervals(fault.recipe_specification(name)) if gain == 0)
    assert blanked("B1") == blanked("B5") == 1_152_000 == 50 * fault.RATE // 1000
    assert blanked("N0") == 0


@pytest.mark.parametrize("value", [None, [], {"kind": "normal"}, {"schema_version": fault.V3_TRIAL_SCHEMA, "study_arm": "B2"}])
def test_malformed_specifications_are_rejected(value):
    with pytest.raises((fault.FaultError, TypeError)):
        fault.validate_spec(value)


def cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/radio_fault.py"), *args],
                          capture_output=True, text=True, timeout=60)


def test_cli_lists_every_recipe_with_its_backend():
    result = cli("list")
    assert result.returncode == 0, result.stderr
    rows = {line.split()[0]: line.split()[1] for line in result.stdout.splitlines()}
    assert rows == {name: fault.recipe_specification(name)["backend"] for name in PINNED}


def test_cli_show_reports_exact_pulses_and_rejects_unknown_recipes():
    result = cli("show", "B5")
    assert result.returncode == 0, result.stderr
    shown = json.loads(result.stdout)
    assert [pulse["start_sample"] for pulse in shown["ul_pulses"]] == [92_160_000 + k * 11_520_000 for k in range(5)]
    assert {pulse["duration_seconds"] for pulse in shown["ul_pulses"]} == {0.01}
    assert shown["schedule_duration_seconds"] == 10
    assert cli("show", "B6").returncode != 0


def test_cli_prepare_writes_a_private_control_directory_without_starting_anything():
    with tempfile.TemporaryDirectory(prefix="rf-cli-", dir="/tmp") as name:
        directory = Path(name) / "t"
        result = cli("prepare", "B1", str(directory), *(f"--{key.replace('_', '-')}={value}" for key, value in (
            ("study_id", IDS["run_id"]), ("trial_id", IDS["trial_id"]), ("pipeline_id", IDS["pipeline_id"]))))
        assert result.returncode == 0, result.stderr
        prepared = json.loads(result.stdout)
        assert prepared["plan_sha256"] == PINNED["B1"][3]
        assert (directory.stat().st_mode & 0o777) == 0o700
        assert not any(path.name in ("rb.sock", "broker_ready.json", "broker_events.jsonl")
                       for path in directory.iterdir())
        again = cli("prepare", "B1", str(directory))
        assert again.returncode == 1 and "FileExistsError" in again.stderr
