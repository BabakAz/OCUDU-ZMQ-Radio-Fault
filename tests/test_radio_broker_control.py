"""Offline control validation: no brokers or sockets are started by this suite."""
import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import socket
import stat
import struct

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import radio_broker_control as control
import radio_broker_schedule as schedules
sys.path.pop(0)


def prepared(tmp_path):
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    schedules.prepare(ROOT / "config/radio_broker/fixed_reference.fixture.json",
                      ROOT / "config/radio_broker/finite_schedule.fixture.json", directory)
    plan = schedules.load_wire(directory / "plan.wire")
    ready = {"schema_version": "radio_broker_ready_v1", "instance_id": "a" * 32,
             "pid": os.getpid(), "plan_sha256": plan.sha256,
             "config_sha256": plan.profile_sha256, "backend": "c",
             "build_sha256": "b" * 64, "control_socket": str(directory / "rb.sock")}
    (directory / "broker_ready.json").write_text(json.dumps(ready))
    (directory / "broker_ready.json").chmod(0o600)
    return directory, plan, ready


def reply(plan, ready):
    return {"schema_version": "radio_broker_control_v1", "ok": True,
            "request_sequence": 1, "operation": "STATUS", "instance_id": ready["instance_id"],
            "plan_sha256": plan.sha256, "state": "ready", "directions": {
                name: {"armed": False, "arm_sample": None, "processed_samples": 19,
                       "forwarded_samples": 19, "duration_samples": direction.duration_samples,
                       "schedule_complete": False, "next_event": 0}
                for name, direction in plan.directions.items()}}


def test_private_ready_binds_plan_and_expected_pid(tmp_path):
    directory, plan, ready = prepared(tmp_path)
    with control.ControlClient(directory, expected_backend="c", expected_pid=os.getpid()) as client:
        assert client.ready == ready and client.plan.sha256 == plan.sha256
    with pytest.raises(control.ControlError, match="expected_identity"):
        control.ControlClient(directory, expected_pid=os.getpid() + 1).wait_ready()


@pytest.mark.parametrize("name", ["plan.wire", "control.token", "broker_ready.json"])
def test_rejects_world_readable_private_files(tmp_path, name):
    directory, _, _ = prepared(tmp_path)
    (directory / name).chmod(0o644)
    with pytest.raises(control.ControlError, match="private_file_metadata"):
        control.ControlClient(directory).wait_ready()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "token_newline", "directory_mode"])
def test_rejects_private_input_aliases_and_invalid_token(tmp_path, kind):
    directory, _, _ = prepared(tmp_path)
    token = directory / "control.token"
    secret = token.read_text()
    if kind == "symlink":
        other = tmp_path / "saved"
        token.rename(other)
        token.symlink_to(other)
    elif kind == "hardlink":
        os.link(token, tmp_path / "alias")
    elif kind == "token_newline":
        token.write_text(secret + "\n")
    else:
        directory.chmod(0o755)
    with pytest.raises(control.ControlError) as caught:
        control.ControlClient(directory).wait_ready()
    assert secret not in str(caught.value)


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(request_sequence=True),
    lambda r: r.update(instance_id="0" * 32),
    lambda r: r.update(operation="ARM"),
    lambda r: r.update(state="completed"),
    lambda r: r.update(state="armed"),
    lambda r: r["directions"]["DL"].update(armed=1),
    lambda r: r["directions"]["UL"].update(forwarded_samples=20),
    lambda r: r["directions"]["UL"].update(next_event=33),
    lambda r: r["directions"]["UL"].update(next_event=1),
    lambda r: r["directions"]["DL"].update(schedule_complete=True),
    lambda r: r["directions"]["DL"].update(duration_samples=4098),
])
def test_rejects_forged_reply_identity_frontiers_and_completion(tmp_path, mutation):
    _, plan, ready = prepared(tmp_path)
    value = reply(plan, ready)
    mutation(value)
    with pytest.raises(control.ControlError):
        control.validate_reply(json.dumps(value).encode(), sequence=1, operation="STATUS", ready=ready, plan=plan)


def test_duplicate_keys_nonfinite_and_oversized_replies_fail(tmp_path):
    _, plan, ready = prepared(tmp_path)
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":[{"b":1e999}]}', b" " * 4097):
        with pytest.raises(control.ControlError):
            control.validate_reply(raw, sequence=1, operation="STATUS", ready=ready, plan=plan)


def test_explicit_retry_reuses_same_packet_and_sequence(tmp_path, monkeypatch):
    directory, plan, ready = prepared(tmp_path)
    packets = []
    with control.ControlClient(directory) as client:
        def exchange(packet, sequence, operation):
            packets.append(packet)
            value = reply(plan, ready)
            value.update(request_sequence=sequence, operation=operation)
            return value
        monkeypatch.setattr(client, "_exchange", exchange)
        first = client.request("ARM")
        assert client.retry_last() == first
        assert packets[0] == packets[1] and client.sequence == 2
        client.request("STATUS")
        assert packets[-1].startswith(b"RBCTRL1 2 STATUS ")


@pytest.mark.parametrize("transient", ["private_file_changed", "partial_json"])
def test_ready_publication_snapshot_race_is_retried_within_deadline(tmp_path, monkeypatch, transient):
    directory, _, _ = prepared(tmp_path)
    client = control.ControlClient(directory, timeout=0.2)
    original = client._file
    ready_reads = 0
    def read(name, maximum):
        nonlocal ready_reads
        if name == "broker_ready.json":
            ready_reads += 1
            if ready_reads == 1:
                if transient == "partial_json":
                    return b'{"schema_version":'
                raise control.ControlError("private_file_changed")
        return original(name, maximum)
    monkeypatch.setattr(client, "_file", read)
    with client:
        assert ready_reads == 2


@pytest.mark.parametrize("fault", ["none", "pid", "uid", "truncated", "credential", "timeout", "inode"])
def test_packet_transport_checks_connected_peer_size_secret_and_socket_identity(tmp_path, monkeypatch, fault):
    directory, plan, ready = prepared(tmp_path)
    client = control.ControlClient(directory).wait_ready()
    metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.geteuid(), st_nlink=1, st_dev=1, st_ino=2)
    actual_stat = control.os.stat
    socket_stats = 0
    def fake_stat(path, *args, **kwargs):
        nonlocal socket_stats
        if path == "rb.sock":
            socket_stats += 1
            if fault == "inode" and socket_stats == 2:
                return SimpleNamespace(**{**vars(metadata), "st_ino": 3})
            return metadata
        return actual_stat(path, *args, **kwargs)
    class FakeSocket:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def settimeout(self, timeout):
            assert 0 < timeout <= 2
        def connect(self, path):
            assert path == str(directory / "rb.sock")
        def getsockopt(self, level, option, size):
            assert (level, option, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return struct.pack("3i", os.getpid() + (fault == "pid"),
                               os.geteuid() + (fault == "uid"), os.getegid())
        def send(self, packet):
            assert packet.startswith(b"RBCTRL1 1 STATUS ") and len(packet) <= 512
            return len(packet)
        def recvmsg(self, maximum):
            assert maximum == 4097
            if fault == "timeout":
                raise TimeoutError
            raw = json.dumps(reply(plan, ready)).encode()
            if fault == "credential":
                raw += client._token
            return raw, [], socket.MSG_TRUNC if fault == "truncated" else 0, None
    monkeypatch.setattr(control.os, "stat", fake_stat)
    monkeypatch.setattr(control.socket, "socket", lambda *args: FakeSocket())
    try:
        if fault == "none":
            assert client.request("STATUS")["ok"]
        else:
            with pytest.raises(control.ControlError) as caught:
                client.request("STATUS")
            assert client._token.decode() not in str(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 5.01, True])
def test_timeout_is_strictly_finite_and_bounded(tmp_path, timeout):
    with pytest.raises(control.ControlError):
        control.ControlClient(tmp_path, timeout=timeout)
