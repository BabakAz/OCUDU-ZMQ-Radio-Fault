"""Strict development profile and broker-CLI mapping, entirely offline."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("radio_profile", ROOT / "scripts/radio_broker_profile.py")
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)
FIXTURE = ROOT / "config/radio_broker/fixed_reference.fixture.json"


def profile():
    return json.loads(FIXTURE.read_text())


def test_shared_profile_preserves_independent_directions_and_versions():
    baseline = profile()
    changed = copy.deepcopy(baseline)
    changed["directions"]["DL"]["noise"]["snr_db"] = 7
    changed["directions"]["DL"]["cw"]["enabled"] = True
    for backend in ("c", "grc"):
        args = PROFILE.broker_arguments(baseline, backend)
        treated = PROFILE.broker_arguments(changed, backend)
        index = args.index("--ul-mode")
        assert treated[treated.index("--ul-mode"):] == args[index:]
        assert args[args.index("--seed") + 1] == treated[treated.index("--seed") + 1]
        assert "--dl-cw" in treated and "--dl-cw" not in args
        assert "--snr" not in args and "--fading" not in args
        assert args[:2] == ["--channel-semantics", "fixed_reference_v1"]
    assert PROFILE.broker_arguments(baseline, "c")[4] == "--srate"
    assert PROFILE.broker_arguments(baseline, "grc")[4] == "--samp-rate"


@pytest.mark.parametrize("path,value", [
    (("master_seed",), True), (("master_seed",), 2**32), (("master_seed",), 1.5),
    (("sample_rate_hz",), float("nan")), (("sample_rate_hz",), 0),
    (("rng_version",), "unknown"), (("qualification",), "campaign_qualified"),
    (("reference_provenance",), ""), (("reference_provenance",), "id\n--bad"),
    (("directions", "DL", "reference_power"), 0),
    (("directions", "DL", "desired_gain"), -1),
    (("directions", "DL", "mode"), "epa"),
    (("directions", "UL", "cw", "frequency_hz"), 12e6),
    (("directions", "UL", "noise", "enabled"), 1),
    (("directions", "UL", "noise", "snr_db"), "20"),
])
def test_invalid_configuration_cannot_become_broker_arguments(path, value):
    data = profile()
    destination = data
    for key in path[:-1]:
        destination = destination[key]
    destination[path[-1]] = value
    with pytest.raises(PROFILE.ProfileError):
        PROFILE.broker_arguments(data, "c")


def test_missing_unknown_and_duplicate_fields_fail(tmp_path):
    data = profile()
    data["schedule"] = []
    with pytest.raises(PROFILE.ProfileError, match="exactly"):
        PROFILE.validate_profile(data)
    data = profile()
    del data["directions"]["UL"]["reference_power"]
    with pytest.raises(PROFILE.ProfileError, match="exactly"):
        PROFILE.validate_profile(data)
    file = tmp_path / "duplicate.json"
    file.write_text(FIXTURE.read_text().replace('"master_seed": 41', '"master_seed": 41, "master_seed": 42'))
    with pytest.raises(PROFILE.ProfileError, match="duplicate"):
        PROFILE.load_profile(file)


def test_bound_read_and_reject_links_nonfinite_or_nonregular_files(tmp_path):
    file = tmp_path / "profile.json"
    file.write_bytes(b" " * (PROFILE.MAX_PROFILE_BYTES + 1))
    with pytest.raises(PROFILE.ProfileError, match="exceeds"):
        PROFILE.load_profile(file)
    file.write_text(FIXTURE.read_text().replace('"master_seed": 41', '"master_seed": NaN'))
    with pytest.raises(PROFILE.ProfileError, match="nonfinite"):
        PROFILE.load_profile(file)
    link = tmp_path / "linked.json"
    link.symlink_to(FIXTURE)
    with pytest.raises(OSError):
        PROFILE.load_profile(link)
    with pytest.raises((OSError, PROFILE.ProfileError)):
        PROFILE.load_profile(tmp_path)


def test_source_hash_and_canonical_hash_have_distinct_meaning(tmp_path):
    value, source_hash = PROFILE.load_profile(FIXTURE)
    assert source_hash == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    compact = tmp_path / "compact.json"
    compact.write_bytes(PROFILE.canonical_bytes(value))
    equivalent, different_source = PROFILE.load_profile(compact)
    assert value == equivalent and different_source != source_hash
    assert PROFILE.canonical_bytes(value) == PROFILE.canonical_bytes(equivalent)


def test_match_actual_radio_sample_rates_without_nominal_fallback(tmp_path):
    data = profile()
    gnb = ROOT / "config/examples/gnb_zmq_broker.yml"
    ue = ROOT / "config/examples/ue_zmq.conf"
    PROFILE.validate_radio_rates(data, gnb, ue)
    data["sample_rate_hz"] = 30.72e6
    with pytest.raises(PROFILE.ProfileError, match="differs"):
        PROFILE.validate_radio_rates(data, gnb, ue)
    changed = tmp_path / "ue.conf"
    changed.write_text(ue.read_text().replace("base_srate=23.04e6", "base_srate=30.72e6"))
    with pytest.raises(PROFILE.ProfileError, match="base_srate|differs"):
        PROFILE.validate_radio_rates(profile(), gnb, changed)


@pytest.mark.parametrize("original,replacement,diagnostic", [
    ("device_name = zmq", "device_name = uhd", "ZMQ UE"),
    ("base_srate=23.04e6", "base_srate=23.04e6, base_srate=30.72e6", "exactly one"),
    ("base_srate=23.04e6", " base_srate =23.04e6", "canonical"),
])
def test_radio_config_cannot_hide_driver_or_rate_ambiguity(tmp_path, original, replacement, diagnostic):
    source = (ROOT / "config/examples/ue_zmq.conf").read_text()
    assert original in source
    changed = tmp_path / "ue.conf"
    changed.write_text(source.replace(original, replacement))
    with pytest.raises(PROFILE.ProfileError, match=diagnostic):
        PROFILE.validate_radio_rates(profile(), ROOT / "config/examples/gnb_zmq_broker.yml", changed)
