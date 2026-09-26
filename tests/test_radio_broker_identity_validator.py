"""Offline checks for the L2 evidence validator; no sockets or brokers start."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
import zmq


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "radio_identity_validation", ROOT / "scripts/validate_radio_broker_identity.py")
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def records(*, error=False):
    return [
        {"schema_version": "radio_broker_accounting_v1", "record_type": "final",
         "backend": "c", "direction": direction, "identity": True,
         "input_messages": 3, "input_samples": 7,
         "output_messages": 3, "output_samples": 7,
         "error_count": int(error and direction == "DL"),
         "status": "error" if error else "stopped"}
        for direction in ("DL", "UL")
    ]


EXPECTED = {direction: {"messages": 3, "samples": 7} for direction in ("DL", "UL")}


def validate(rows, **kwargs):
    return VALIDATOR.validate_final_records(
        "\n".join("C_RELAY_ACCOUNTING: " + json.dumps(row) for row in rows),
        "c", EXPECTED, **kwargs)


def test_records_require_independent_peer_reconciliation():
    assert len(validate(records())) == 2
    assert len(validate(records(error=True), error_direction="DL")) == 2
    rows = records()
    rows[0]["input_samples"] = rows[0]["output_samples"] = 8
    with pytest.raises(VALIDATOR.ValidationError, match="unreconciled"):
        validate(rows)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "legacy"), ("backend", "grc"), ("identity", 1),
    ("input_messages", True), ("input_samples", -1), ("output_samples", 7.0),
    ("output_messages", 2 ** 64), ("error_count", -1),
    ("status", "incomplete"), ("direction", "UL"),
])
def test_invalid_or_ambiguous_records_cannot_pass(field, value):
    rows = records()
    rows[0][field] = value
    with pytest.raises(VALIDATOR.ValidationError):
        validate(rows)


def test_missing_and_duplicate_records_cannot_pass():
    for rows in (records()[:1], records() + records()[:1]):
        with pytest.raises(VALIDATOR.ValidationError):
            validate(rows)


def test_sibling_failure_requires_both_records_and_local_error_identity():
    rows = records(error=True)
    rows[1]["status"] = "stopped"
    with pytest.raises(VALIDATOR.ValidationError, match="sibling failure"):
        validate(rows, error_direction="DL")
    rows = records(error=True)
    rows[1]["error_count"] = 1
    with pytest.raises(VALIDATOR.ValidationError, match="local versus sibling"):
        validate(rows, error_direction="DL")


def test_execution_needs_explicit_flag_and_fresh_artifact_path(tmp_path):
    outside = tmp_path / "unused"
    with pytest.raises(SystemExit) as exc:
        VALIDATOR.main(["--output", str(outside)])
    assert exc.value.code == 2 and not outside.exists()
    with pytest.raises(SystemExit) as exc:
        VALIDATOR.main(["--run-local", "--output", str(outside)])
    assert exc.value.code == 2 and not outside.exists()
    with pytest.raises(SystemExit) as exc:
        VALIDATOR.main(["--run-local", "--output", str(ROOT / "artifacts")])
    assert exc.value.code == 2


def test_payloads_cover_same_stream_partitions_and_bounded_finite_edge_cases():
    payloads = VALIDATOR.identity_payloads()
    assert payloads[0] == b"".join(payloads[1:17]) == b"".join(payloads[17:23])
    assert b"" in payloads
    assert any(len(raw) // 8 % 2 for raw in payloads)
    assert max(map(len, payloads)) > 1024 * 1024
    assert sum(map(len, payloads)) <= VALIDATOR.MAX_SOURCE_BYTES_PER_DIRECTION
    for raw in payloads:
        assert len(raw) % 8 == 0
        assert np.isfinite(np.frombuffer(raw, dtype=np.complex64)).all()
    assert b"\x00\x00\x00\x80" in payloads[0]  # signed zero, little-endian host


@pytest.mark.parametrize("failure", ["corruption", "timeout", "cancelled"])
def test_failed_exchange_preserves_peer_ledgers_and_finalizes_cleanup(
    tmp_path, monkeypatch, failure,
):
    """Explicit in-memory validator stubs; no process or ZeroMQ socket opens."""
    class Socket:
        def __init__(self, kind):
            self.kind = kind

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            pass

        connect = bind

        def send(self, raw):
            pass

        def recv_multipart(self):
            if self.kind == zmq.REP:
                return [b"\x01"]
            if failure == "timeout":
                raise zmq.Again()
            if failure == "cancelled":
                raise KeyboardInterrupt()
            return [b"corrupt!"]

        def close(self, **kwargs):
            # All cleanup stages must still run and result.json must survive.
            raise OSError("fixture close failure")

    class Context:
        terminated = False

        def socket(self, kind):
            return Socket(kind)

        def term(self):
            self.terminated = True

    class Child:
        pid = 123456
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, **kwargs):
            return self.returncode

    context = Context()
    monkeypatch.setattr(zmq, "Context", lambda: context)
    monkeypatch.setattr(VALIDATOR.subprocess, "Popen", lambda *a, **k: Child())
    report = VALIDATOR.run_case("c", ["unused-fixture"], "identity", tmp_path)
    assert report["status"] == "failed"
    assert report["source_peer_counts"]["DL"]["messages"] == 1
    assert report["source_peer_counts"]["DL"]["bytes"] == len(VALIDATOR.identity_payloads()[0])
    assert report["receiver_peer_counts"]["DL"]["messages"] == (failure == "corruption")
    assert report["validated_exchange_counts"]["DL"]["messages"] == 0
    assert report["child_reaped"] is True and context.terminated
    assert report["private_endpoint_directory_removed"] is True
    assert len(report["socket_cleanup_errors"]) == 4
    assert bool(report.get("cancelled")) == (failure == "cancelled")
    assert json.loads((tmp_path / "c-identity/result.json").read_text()) == report
