#!/usr/bin/env python3
"""Independent bounded Linux whole-process resource snapshots.

No process is started or signalled. CPU is an aggregate over broker threads;
neither per-direction CPU nor an exact measured-window memory peak is inferred.
The explicit sampler owns its proc-directory and optional pidfd handles only.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import re
import select
import time

MAX_OBSERVATIONS = 64
MAX_PROC_BYTES = 65536
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
UINT64_MAX = (1 << 64) - 1


class ResourceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ResourceError(message)


def uint(value):
    return type(value) is int and 0 <= value <= UINT64_MAX


def parse_stat(raw, expected_pid):
    """Linux stat fields; comm may itself contain whitespace and parentheses."""
    require(type(raw) is str and len(raw) <= MAX_PROC_BYTES, "invalid_proc_stat")
    opening, closing = raw.find("("), raw.rfind(")")
    require(opening > 0 and closing > opening and raw[closing + 1:closing + 2] == " ", "invalid_proc_stat")
    try:
        pid = int(raw[:opening].strip())
        fields = raw[closing + 2:].split()
        require(len(fields) >= 22, "short_proc_stat")
        values = {"pid": pid, "comm": raw[opening + 1:closing], "state": fields[0],
                  "user_ticks": int(fields[11]), "system_ticks": int(fields[12]),
                  "starttime_ticks": int(fields[19]), "rss_pages": int(fields[21])}
    except (ValueError, IndexError):
        raise ResourceError("invalid_proc_stat_value") from None
    require(pid == expected_pid and all(uint(values[key]) for key in
            ("user_ticks", "system_ticks", "starttime_ticks", "rss_pages")), "invalid_proc_stat_identity_counter")
    require(len(values["state"]) == 1 and values["state"] not in ("Z", "X", "x"), "process_not_live")
    return values


def parse_status(raw, expected_pid):
    require(type(raw) is str and len(raw) <= MAX_PROC_BYTES, "invalid_proc_status")
    wanted = {"Pid", "Tgid", "Uid", "VmRSS", "VmHWM"}
    values = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in wanted:
            require(key not in values, "duplicate_proc_status_field")
            values[key] = value.split()
    require(set(values) == wanted, "missing_proc_status_field")
    try:
        require(len(values["Pid"]) == len(values["Tgid"]) == 1 and
                int(values["Pid"][0]) == expected_pid == int(values["Tgid"][0]), "status_pid_mismatch")
        require(len(values["Uid"]) == 4, "status_uid_fields")
        uid = [int(value) for value in values["Uid"]]
        memory = {}
        for source, target in (("VmRSS", "approximate_rss_bytes"), ("VmHWM", "approximate_lifetime_hwm_bytes")):
            require(len(values[source]) == 2 and values[source][1] == "kB", "status_memory_units")
            memory[target] = int(values[source][0]) * 1024
    except ValueError:
        raise ResourceError("invalid_proc_status_value") from None
    require(all(uint(value) for value in [*uid, *memory.values()]), "invalid_proc_status_counter")
    require(memory["approximate_lifetime_hwm_bytes"] >= memory["approximate_rss_bytes"], "status_memory_regression")
    return {"uids": uid, **memory}


def cpu_ticks(stat):
    return stat["user_ticks"] + stat["system_ticks"]


def derive_interval(before, after):
    """Derive ratios only for the exact observed DL+UL population and time support."""
    required = {"identity", "clock_ticks_per_second", "page_size_bytes", "observation_id", "cpu_ns",
                "harness_process_cpu_ns", "observation_start_ns", "observation_end_ns", "cpu_read_start_ns",
                "cpu_read_end_ns", "forwarded_samples", "stat_before", "stat_after",
                "approximate_rss_bytes", "approximate_lifetime_hwm_bytes"}
    require(all(type(row) is dict and required <= set(row) for row in (before, after)), "missing_resource_endpoint_fields")
    require(before["identity"] == after["identity"], "resource_identity_changed")
    require(before["clock_ticks_per_second"] == after["clock_ticks_per_second"] and
            before["page_size_bytes"] == after["page_size_bytes"], "resource_units_changed")
    for row in (before, after):
        require(all(uint(row[key]) for key in ("cpu_ns", "cpu_read_start_ns", "cpu_read_end_ns",
                                               "harness_process_cpu_ns", "observation_start_ns",
                                               "observation_end_ns", "observation_id")), "invalid_resource_counter")
        require(row["observation_start_ns"] <= row["cpu_read_start_ns"] <= row["cpu_read_end_ns"] <=
                row["observation_end_ns"], "resource_clock_regression")
        require(set(row["forwarded_samples"]) == {"DL", "UL"} and
                all(uint(value) for value in row["forwarded_samples"].values()), "invalid_resource_population")
    require(after["cpu_read_start_ns"] > before["cpu_read_end_ns"], "overlapping_resource_observations")
    require(after["observation_start_ns"] > before["observation_end_ns"] and
            after["observation_id"] == before["observation_id"] + 1 and before["observation_id"] >= 1,
            "resource_observation_sequence_or_support")
    require(after["cpu_ns"] >= before["cpu_ns"] and
            after["harness_process_cpu_ns"] >= before["harness_process_cpu_ns"], "resource_cpu_regression")
    for row in (before, after):
        require(1 <= row["observation_id"] <= MAX_OBSERVATIONS, "resource_observation_sequence")
        for field in ("user_ticks", "system_ticks"):
            require(uint(row["stat_before"][field]) and uint(row["stat_after"][field]) and
                    row["stat_after"][field] >= row["stat_before"][field], "resource_tick_regression")
    for field in ("user_ticks", "system_ticks"):
        require(after["stat_before"][field] >= before["stat_after"][field], "resource_tick_regression")
    deltas = {}
    for name in ("DL", "UL"):
        require(after["forwarded_samples"][name] >= before["forwarded_samples"][name], "resource_sample_regression")
        deltas[name] = after["forwarded_samples"][name] - before["forwarded_samples"][name]
    cpu = after["cpu_ns"] - before["cpu_ns"]
    wall_min = after["cpu_read_start_ns"] - before["cpu_read_end_ns"]
    wall_max = after["cpu_read_end_ns"] - before["cpu_read_start_ns"]
    # Twice the midpoint wall interval is kept integer; no epoch rounding.
    twice_wall_midpoint = (after["cpu_read_start_ns"] + after["cpu_read_end_ns"] -
                          before["cpu_read_start_ns"] - before["cpu_read_end_ns"])
    ticks_per_second = before["clock_ticks_per_second"]
    require(type(ticks_per_second) is int and ticks_per_second > 0, "invalid_tick_rate")
    tick_lower = cpu_ticks(after["stat_before"]) - cpu_ticks(before["stat_after"])
    tick_upper = cpu_ticks(after["stat_after"]) - cpu_ticks(before["stat_before"])
    # Two independently quantized user/system counters can differ by <2 ticks.
    require((tick_lower - 2) * 1_000_000_000 <= cpu * ticks_per_second <=
            (tick_upper + 2) * 1_000_000_000, "process_cpu_tick_crosscheck_failed")
    total_samples = sum(deltas.values())
    rss = [row["approximate_rss_bytes"] for row in (before, after)]
    hwm = [row["approximate_lifetime_hwm_bytes"] for row in (before, after)]
    require(all(uint(value) for value in [*rss, *hwm]), "invalid_resource_memory")
    require(hwm[1] >= hwm[0], "resource_lifetime_hwm_regression")
    return {"schema_version": "radio_broker_resource_interval_v1", "identity": before["identity"],
            "support": "whole_process_between_bracketed_cpu_reads; all_threads; no_children",
            "exclusions": "startup,teardown,final_partial_metrics_and_any_deferred_writer_work_after_endpoint; no_writer_drain_barrier",
            "cpu_ns": cpu, "wall_ns_lower_bound": wall_min, "wall_ns_upper_bound": wall_max,
            "twice_wall_ns_midpoint": twice_wall_midpoint,
            "cpu_cores_midpoint": 2 * cpu / twice_wall_midpoint,
            "cpu_cores_lower_bound": cpu / wall_max, "cpu_cores_upper_bound": cpu / wall_min,
            "forwarded_samples": deltas, "total_forwarded_complex_samples": total_samples,
            "cpu_seconds_per_million_complex_samples": cpu / (1000 * total_samples) if total_samples else None,
            "cpu_per_sample_undefined_reason": None if total_samples else "no_forwarded_samples",
            "cpu_tick_delta_bounds": [tick_lower, tick_upper], "tick_quantization_allowance": 2,
            "approximate_sampled_rss_max_bytes": max(rss), "rss_observation_count": 2,
            "approximate_lifetime_hwm_bytes": max(hwm),
            "memory_scope": "approximate_proc_status; sampled_RSS_max_is_not_exact_peak; HWM_includes_startup",
            "harness_process_cpu_ns": after["harness_process_cpu_ns"] - before["harness_process_cpu_ns"]}


class ProcessResources:
    def __init__(self, pid, *, expected_uid=None, max_observations=MAX_OBSERVATIONS):
        require(type(pid) is int and 1 <= pid < (1 << 31), "invalid_resource_pid")
        require(type(max_observations) is int and 2 <= max_observations <= MAX_OBSERVATIONS,
                "invalid_resource_observation_budget")
        self.pid = pid
        self.uid = os.geteuid() if expected_uid is None else expected_uid
        self.limit, self.observations = max_observations, 0
        self.proc_fd = self.pidfd = None
        self.identity = None
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        try:
            self.proc_fd = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            require(os.fstat(self.proc_fd).st_uid == self.uid, "resource_proc_owner")
            if hasattr(os, "pidfd_open"):
                try:
                    self.pidfd = os.pidfd_open(pid, 0)
                except OSError as exc:
                    if exc.errno not in (errno.ENOSYS, errno.EINVAL):
                        raise
            before = self._identity()
            libc = ctypes.CDLL(None, use_errno=True)
            lookup = libc.clock_getcpuclockid
            lookup.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_int))
            lookup.restype = ctypes.c_int
            clock = ctypes.c_int()
            require(lookup(pid, ctypes.byref(clock)) == 0, "process_cpu_clock_unavailable")
            self.clock_id = clock.value
            self.cpu_clock_resolution_ns = round(time.clock_getres(self.clock_id) * 1_000_000_000)
            executable = self._executable_hash()
            after = self._identity()
            require(before == after, "resource_identity_changed")
            self.identity = {**after, "executable_sha256": executable}
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        first_error = None
        for name in ("pidfd", "proc_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)
                except OSError as exc:
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _read(self, name):
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=self.proc_fd)
        try:
            raw = os.read(fd, MAX_PROC_BYTES + 1)
            require(len(raw) <= MAX_PROC_BYTES, "proc_file_exceeds_bound")
            return raw.decode("ascii")
        finally:
            os.close(fd)

    def _live(self):
        if self.pidfd is not None:
            poller = select.poll()
            poller.register(self.pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
            require(not poller.poll(0), "resource_process_exited")

    def _identity(self):
        self._live()
        stat = parse_stat(self._read("stat"), self.pid)
        status = parse_status(self._read("status"), self.pid)
        require(status["uids"][0] == status["uids"][1] == self.uid, "resource_uid_changed")
        executable = os.stat("exe", dir_fd=self.proc_fd, follow_symlinks=True)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id) is not None,
                "invalid_resource_boot_id")
        return {"boot_id": boot_id, "pid": self.pid, "starttime_ticks": stat["starttime_ticks"],
                "uid": self.uid, "uids": status["uids"], "executable_device": executable.st_dev,
                "executable_inode": executable.st_ino, "executable_bytes": executable.st_size,
                "executable_mtime_ns": executable.st_mtime_ns}

    def _executable_hash(self):
        fd = os.open("exe", os.O_RDONLY | os.O_CLOEXEC, dir_fd=self.proc_fd)
        try:
            require(os.fstat(fd).st_size <= MAX_EXECUTABLE_BYTES, "resource_executable_too_large")
            digest, total = hashlib.sha256(), 0
            while True:
                raw = os.read(fd, 1024 * 1024)
                if not raw:
                    break
                total += len(raw)
                require(total <= MAX_EXECUTABLE_BYTES, "resource_executable_too_large")
                digest.update(raw)
            return digest.hexdigest()
        finally:
            os.close(fd)

    def snapshot(self, forwarded_samples):
        require(self.proc_fd is not None and self.observations < self.limit, "resource_observation_budget_or_closed")
        require(type(forwarded_samples) is dict and set(forwarded_samples) == {"DL", "UL"} and
                all(uint(value) for value in forwarded_samples.values()), "invalid_resource_population")
        begin = time.monotonic_ns()
        try:
            first = self._identity()
            require(first == {k: v for k, v in self.identity.items() if k != "executable_sha256"},
                    "resource_identity_changed")
            stat_before = parse_stat(self._read("stat"), self.pid)
            memory = parse_status(self._read("status"), self.pid)
            cpu_start = time.monotonic_ns()
            cpu_ns = time.clock_gettime_ns(self.clock_id)
            cpu_end = time.monotonic_ns()
            stat_after = parse_stat(self._read("stat"), self.pid)
            require(first == self._identity(), "resource_identity_changed")
        except (OSError, UnicodeError, ValueError):
            raise ResourceError("resource_observation_unavailable") from None
        end = time.monotonic_ns()
        require(begin <= cpu_start <= cpu_end <= end and uint(cpu_ns), "resource_clock_regression")
        self.observations += 1
        return {"schema_version": "radio_broker_resource_snapshot_v1", "observation_id": self.observations,
                "identity": dict(self.identity), "clock_ticks_per_second": self.clock_ticks,
                "page_size_bytes": self.page_size, "cpu_clock_method": "POSIX_process_clock_all_threads",
                "cpu_clock_resolution_ns": self.cpu_clock_resolution_ns, "pidfd_held": self.pidfd is not None,
                "observation_start_ns": begin, "observation_end_ns": end,
                "cpu_read_start_ns": cpu_start, "cpu_read_end_ns": cpu_end, "cpu_ns": cpu_ns,
                "stat_before": stat_before, "stat_after": stat_after,
                **{k: v for k, v in memory.items() if k != "uids"},
                "forwarded_samples": dict(forwarded_samples), "harness_process_cpu_ns": time.process_time_ns()}
