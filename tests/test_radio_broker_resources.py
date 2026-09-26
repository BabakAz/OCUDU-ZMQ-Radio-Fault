"""Offline Linux-accounting parser/interval forgeries; no live processes sampled."""
import copy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_resources as resources
sys.path.pop(0)


def raw_stat(comm="worker (complex name)"):
    fields = ["R"] + ["0"] * 21
    for index, value in ((11, 8), (12, 2), (19, 123), (21, 10)):
        fields[index] = str(value)
    return "123 (" + comm + ") " + " ".join(fields)


def status():
    return "Pid:\t123\nTgid:\t123\nUid:\t1001\t1001\t1001\t1001\nVmRSS:\t4 kB\nVmHWM:\t8 kB\n"


def observations():
    identity = {"pid": 123, "starttime_ticks": 300, "boot_id": "boot", "uid": 1001,
                "executable_sha256": "a" * 64}
    before = {"identity": identity, "clock_ticks_per_second": 100, "page_size_bytes": 4096,
              "observation_id": 1, "observation_start_ns": 1_000_000_000, "observation_end_ns": 1_000_000_300,
              "cpu_read_start_ns": 1_000_000_100, "cpu_read_end_ns": 1_000_000_200,
              "cpu_ns": 100_000_000, "harness_process_cpu_ns": 10_000_000,
              "stat_before": {"user_ticks": 8, "system_ticks": 2},
              "stat_after": {"user_ticks": 8, "system_ticks": 2},
              "approximate_rss_bytes": 4096, "approximate_lifetime_hwm_bytes": 8192,
              "forwarded_samples": {"DL": 11, "UL": 19}}
    after = copy.deepcopy(before)
    for name in ("observation_start_ns", "observation_end_ns", "cpu_read_start_ns", "cpu_read_end_ns"):
        after[name] += 1_000_000_000
    after.update(observation_id=2, cpu_ns=150_000_000, harness_process_cpu_ns=15_000_000,
                 forwarded_samples={"DL": 50011, "UL": 50019})
    after["stat_before"]["user_ticks"] = after["stat_after"]["user_ticks"] = 13
    return before, after


def test_proc_stat_handles_nested_parentheses_and_known_linux_field_positions():
    result = resources.parse_stat(raw_stat(), 123)
    assert result == {"pid": 123, "comm": "worker (complex name)", "state": "R", "user_ticks": 8,
                      "system_ticks": 2, "starttime_ticks": 123, "rss_pages": 10}


@pytest.mark.parametrize("value,pid", [("broken", 123), (raw_stat().replace(" R ", " Z "), 123),
                                       (raw_stat(), 124), (raw_stat().replace(" 8 2 ", " -1 2 "), 123),
                                       ("123 (x) R 0 0", 123)])
def test_proc_stat_rejects_bad_identity_liveness_and_counters(value, pid):
    with pytest.raises(resources.ResourceError):
        resources.parse_stat(value, pid)


def test_rss_is_explicit_linux_kib_and_lifetime_hwm():
    assert resources.parse_status(status(), 123) == {
        "uids": [1001] * 4, "approximate_rss_bytes": 4096, "approximate_lifetime_hwm_bytes": 8192}


@pytest.mark.parametrize("mutate", [
    lambda raw: raw.replace("VmRSS:\t4 kB\n", ""),
    lambda raw: raw + "VmRSS:\t4 kB\n",
    lambda raw: raw.replace("4 kB", "4 MB"),
    lambda raw: raw.replace("4 kB", "-4 kB"),
    lambda raw: raw.replace("Tgid:\t123", "Tgid:\t124"),
    lambda raw: raw.replace("1001\t1001\t1001\t1001", "1001"),
])
def test_proc_status_rejects_missing_ambiguous_or_wrong_unit_fields(mutate):
    with pytest.raises(resources.ResourceError):
        resources.parse_status(mutate(status()), 123)


def test_independent_whole_process_cpu_interval_and_quantized_crosscheck():
    result = resources.derive_interval(*observations())
    assert result["cpu_ns"] == 50_000_000
    assert result["cpu_cores_midpoint"] == 0.05
    assert result["cpu_cores_lower_bound"] < 0.05 < result["cpu_cores_upper_bound"]
    assert result["cpu_seconds_per_million_complex_samples"] == 0.5
    assert result["total_forwarded_complex_samples"] == 100000
    assert result["harness_process_cpu_ns"] == 5_000_000
    assert "deferred_writer_work" in result["exclusions"]


def test_zero_sample_cpu_cost_is_missing_with_reason_not_zero():
    before, after = observations()
    after["forwarded_samples"] = before["forwarded_samples"].copy()
    result = resources.derive_interval(before, after)
    assert result["cpu_seconds_per_million_complex_samples"] is None
    assert result["cpu_per_sample_undefined_reason"] == "no_forwarded_samples"
    assert result["cpu_ns"] == 50_000_000


@pytest.mark.parametrize("field,value", [
    ("cpu_ns", 99_000_000), ("cpu_ns", True), ("cpu_ns", 9_000_000_000),
    ("harness_process_cpu_ns", 9), ("cpu_read_end_ns", 0),
    ("cpu_read_start_ns", 1_000_000_100), ("observation_id", 1), ("observation_id", 3),
    ("observation_start_ns", 2_000_000_301), ("observation_end_ns", 2_000_000_001),
    ("clock_ticks_per_second", 1000), ("page_size_bytes", 8192),
    ("approximate_rss_bytes", -1), ("approximate_lifetime_hwm_bytes", 4096),
])
def test_forged_resource_clock_identity_units_and_quantities_fail(field, value):
    before, after = observations()
    after[field] = value
    with pytest.raises(resources.ResourceError):
        resources.derive_interval(before, after)


@pytest.mark.parametrize("identity_field", ["pid", "starttime_ticks", "boot_id", "uid", "executable_sha256"])
def test_pid_reuse_or_other_identity_change_is_not_a_cpu_delta(identity_field):
    before, after = observations()
    after["identity"][identity_field] = "changed"
    with pytest.raises(resources.ResourceError, match="identity"):
        resources.derive_interval(before, after)


def test_ticks_and_directional_sample_counters_cannot_regress():
    before, after = observations()
    after["stat_before"]["system_ticks"] = 1
    with pytest.raises(resources.ResourceError, match="tick_regression"):
        resources.derive_interval(before, after)
    before, after = observations()
    after["forwarded_samples"]["UL"] = 18
    with pytest.raises(resources.ResourceError, match="sample_regression"):
        resources.derive_interval(before, after)


def test_partial_observation_and_closed_or_exhausted_sampler_fail_without_defaults():
    before, after = observations()
    after.pop("cpu_ns")
    with pytest.raises(resources.ResourceError, match="missing_resource_endpoint"):
        resources.derive_interval(before, after)
    sampler = resources.ProcessResources.__new__(resources.ProcessResources)
    sampler.proc_fd, sampler.observations, sampler.limit = None, 0, 64
    with pytest.raises(resources.ResourceError):
        sampler.snapshot({"DL": 0, "UL": 0})
    sampler.proc_fd, sampler.observations = 9, 64
    with pytest.raises(resources.ResourceError):
        sampler.snapshot({"DL": 0, "UL": 0})


def test_cleanup_attempts_both_owned_descriptors_when_first_close_fails(monkeypatch):
    sampler = resources.ProcessResources.__new__(resources.ProcessResources)
    sampler.pidfd, sampler.proc_fd = 123, 456
    attempted = []
    def close(fd):
        attempted.append(fd)
        if fd == 123:
            raise OSError("injected first close failure")
    monkeypatch.setattr(resources.os, "close", close)
    with pytest.raises(OSError, match="first close"):
        sampler.close()
    assert attempted == [123, 456] and sampler.pidfd is sampler.proc_fd is None
    sampler.close()
    assert attempted == [123, 456]
