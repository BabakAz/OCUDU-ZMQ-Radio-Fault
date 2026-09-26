"""L1 production-module tests with explicit in-memory transport stubs.

These import actual headless dependencies and execute the relay function, but
never open a socket. The executable C/GRC L2 loopback gate remains separate.
"""

import ast
import collections
import importlib.util
import json
import threading
import types
from pathlib import Path

import numpy as np
import pytest
import zmq


BROKER = Path(__file__).resolve().parents[1] / "scripts/ocudu_channel_broker.py"


@pytest.fixture
def production():
    spec = importlib.util.spec_from_file_location("radio_broker_identity_test", BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def test_explicit_identity_option_is_available():
    tree = ast.parse(BROKER.read_text(encoding="utf-8"))
    options = [
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument" and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert "--identity" in options


def test_identity_configuration_reaches_both_directions(production):
    wrapper = production["ocudu_channel_broker_headless"](identity=True)
    assert wrapper.broker._dl_imp["identity"][0] is True
    assert wrapper.broker._ul_imp["identity"][0] is True
    wrapper.broker.set_identity_mode(False)
    assert wrapper.broker._dl_imp["identity"][0] is False
    assert wrapper.broker._ul_imp["identity"][0] is False


def run_stubbed_relay(production, capsys, payloads, *, identity=True,
                     source_multipart=False, request_multipart=False,
                     send_error=False, sibling_error=False, legacy_fader=None,
                     drop_probability=0.0):
    """One synchronous relay invocation; every socket operation is an explicit stub."""
    stop = threading.Event()
    fatal = collections.deque(["UL relay failed: fixture"] if sibling_error else [], maxlen=1)
    upstream_sent = []
    downstream_sent = []
    replies = iter(payloads)
    request_bytes = b"\x01"

    class SocketStub:
        def __init__(self, upstream):
            self.upstream = upstream

        def setsockopt(self, *args):
            pass

        def getsockopt(self, option):
            assert option == zmq.RCVMORE
            return source_multipart if self.upstream else request_multipart

        def connect(self, address):
            pass

        def bind(self, address):
            pass

        def close(self):
            pass

        def recv(self, **_kwargs):
            if self.upstream:
                return next(replies)
            return request_bytes

        def send(self, payload):
            if self.upstream:
                upstream_sent.append(payload)
            else:
                if send_error:
                    raise zmq.ZMQError(zmq.ENOTSOCK)
                downstream_sent.append(payload)
                if len(downstream_sent) == len(payloads):
                    stop.set()

    class ContextStub:
        def socket(self, kind):
            return SocketStub(kind == zmq.REQ)

        def term(self):
            pass

    production["_zmq"] = types.SimpleNamespace(
        Context=ContextStub, REQ=zmq.REQ, REP=zmq.REP, LINGER=zmq.LINGER,
        RCVTIMEO=zmq.RCVTIMEO, SNDTIMEO=zmq.SNDTIMEO,
        MAXMSGSIZE=zmq.MAXMSGSIZE, RCVMORE=zmq.RCVMORE,
        Again=zmq.Again, ZMQError=zmq.ZMQError,
    )

    class IdentitySentinel:
        samp_rate = 1000.0

        def update_and_apply(self, iq):
            raise AssertionError("identity relay entered the legacy DSP chain")

    impairments = {
        "lock": threading.RLock(), "identity": [identity],
        "fading": [legacy_fader or IdentitySentinel()],
        "drop_prob": [drop_probability], "cfo_hz": [0.0], "cfo_phase": [0.0],
        "snr_linear": [float("inf")], "int_enabled": [False],
    }
    production["relay_thread"](
        "DL", "stub://upstream", "stub://downstream", impairments,
        np.random.default_rng(17), stop, None, [0], threading.Event(), fatal,
    )
    output = capsys.readouterr()
    finals = [
        json.loads(line.removeprefix("GRC_RELAY_ACCOUNTING: "))
        for line in output.out.splitlines()
        if line.startswith("GRC_RELAY_ACCOUNTING: ")
    ]
    assert len(finals) == 1, output
    return finals[0], downstream_sent, upstream_sent, output


def test_identity_forwards_exact_finite_bytes_zeros_and_empty_messages(production, capsys):
    edges = np.array([
        0.0, -0.0, np.finfo(np.float32).tiny, -np.finfo(np.float32).tiny,
        np.finfo(np.float32).max, -np.finfo(np.float32).max,
    ], dtype=np.float32).tobytes()
    payloads = [np.zeros(257, dtype=np.complex64).tobytes(), b"", edges]
    record, received, requests, _ = run_stubbed_relay(production, capsys, payloads)
    assert received == payloads
    assert requests == [b"\x01"] * 3
    assert record == {
        "schema_version": "radio_broker_accounting_v1", "record_type": "final",
        "backend": "grc", "direction": "DL", "identity": True,
        "input_messages": 3, "input_samples": 260,
        "output_messages": 3, "output_samples": 260,
        "error_count": 0, "status": "stopped",
    }


@pytest.mark.parametrize("parts", [(1, 2, 3, 7), (13,), (5, 8)])
def test_identity_preserves_sample_order_across_partitions(production, capsys, parts):
    samples = (np.arange(13) + 1j * np.arange(13)[::-1]).astype(np.complex64)
    ends = np.cumsum((0,) + parts)
    payloads = [samples[start:end].tobytes() for start, end in zip(ends[:-1], ends[1:])]
    record, received, _, _ = run_stubbed_relay(production, capsys, payloads)
    assert b"".join(received) == samples.tobytes()
    assert record["input_samples"] == record["output_samples"] == 13
    assert record["input_messages"] == record["output_messages"] == len(parts)


@pytest.mark.parametrize("raw", [b"\x00" * 7, np.array([np.nan + 0j], dtype=np.complex64).tobytes(),
                                 np.array([np.inf + 0j], dtype=np.complex64).tobytes()])
def test_invalid_iq_fails_before_forwarding_and_counts_error(production, capsys, raw):
    record, received, _, _ = run_stubbed_relay(production, capsys, [raw])
    assert received == []
    assert record["input_messages"] == record["output_messages"] == 0
    assert record["error_count"] == 1
    assert record["status"] == "error"


def test_oversized_iq_fails_before_forwarding(production, capsys):
    production["MAX_ZMQ_MESSAGE_BYTES"] = 16
    record, received, _, _ = run_stubbed_relay(production, capsys, [b"\0" * 24])
    assert received == []
    assert record["input_samples"] == record["output_samples"] == 0
    assert record["error_count"] == 1


@pytest.mark.parametrize("source_multipart,request_multipart", [(True, False), (False, True)])
def test_multipart_is_explicitly_rejected(production, capsys, source_multipart, request_multipart):
    record, received, requests, output = run_stubbed_relay(
        production, capsys, [b"\0" * 8], source_multipart=source_multipart,
        request_multipart=request_multipart,
    )
    assert received == []
    assert "multipart" in output.err
    assert record["error_count"] == 1
    assert record["status"] == "error"
    if request_multipart:
        assert requests == []


def test_failed_send_retains_legal_input_without_inventing_output(production, capsys):
    record, received, _, _ = run_stubbed_relay(production, capsys, [b"\0" * 24], send_error=True)
    assert received == []
    assert record["input_messages"] == 1 and record["input_samples"] == 3
    assert record["output_messages"] == record["output_samples"] == 0
    assert record["status"] == "error" and record["error_count"] == 1


def test_nonfinite_legacy_output_is_rejected(production, capsys):
    class BadFader:
        samp_rate = 1000.0

        def update_and_apply(self, iq):
            return np.full_like(iq, np.nan)

    with np.errstate(invalid="ignore"):
        record, received, _, _ = run_stubbed_relay(
            production, capsys, [b"\0" * 24], identity=False, legacy_fader=BadFader(),
        )
    assert received == []
    assert record["input_samples"] == 3 and record["output_samples"] == 0
    assert record["error_count"] == 1 and record["status"] == "error"


def test_sibling_failure_is_visible_without_local_failure_count(production, capsys):
    record, _, _, _ = run_stubbed_relay(production, capsys, [b"\0" * 8], sibling_error=True)
    assert record["status"] == "error" and record["error_count"] == 0


def test_legacy_empty_frame_is_counted_without_entering_dsp(production, capsys):
    record, received, _, _ = run_stubbed_relay(production, capsys, [b""], identity=False)
    assert received == [b""]
    assert record["input_messages"] == record["output_messages"] == 1
    assert record["input_samples"] == record["output_samples"] == 0
    assert record["status"] == "stopped" and record["error_count"] == 0


def test_legacy_masking_retains_all_samples_and_bypasses_fader(production, capsys):
    raw = np.ones(13, dtype=np.complex64).tobytes()
    record, received, _, _ = run_stubbed_relay(
        production, capsys, [raw], identity=False, drop_probability=1.0,
    )
    assert received == [np.zeros(13, dtype=np.complex64).tobytes()]
    assert record["input_samples"] == record["output_samples"] == 13
    assert record["status"] == "stopped" and record["identity"] is False


def test_legacy_nonidentity_still_applies_channel(production, capsys):
    class GainFader:
        samp_rate = 1000.0

        def update_and_apply(self, iq):
            return iq * np.complex64(2.0)

    samples = np.array([1 + 1j, 0 + 2j, -1 - 1j], dtype=np.complex64)
    record, received, _, _ = run_stubbed_relay(
        production, capsys, [samples.tobytes()], identity=False, legacy_fader=GainFader(),
    )
    assert received == [(samples * np.complex64(2)).tobytes()]
    assert record["input_samples"] == record["output_samples"] == 3
    assert record["error_count"] == 0


def test_identity_change_is_rejected_while_either_relay_is_running(production):
    broker = production["channel_broker_source"](identity=True)
    broker._dl_thread = types.SimpleNamespace(is_alive=lambda: True)
    with pytest.raises(RuntimeError, match="cannot change"):
        broker.set_identity_mode(False)
    assert broker._dl_imp["identity"][0] is True
    assert broker._ul_imp["identity"][0] is True


def test_accounting_overflow_rejects_without_partial_counter_mutation(production):
    accounting = production["RelayAccounting"]("DL", True)
    accounting.input_messages = (1 << 64) - 1
    with pytest.raises(OverflowError):
        accounting.record_input(1)
    assert accounting.input_messages == (1 << 64) - 1
    assert accounting.input_samples == 0
    accounting.output_samples = (1 << 64) - 1
    with pytest.raises(OverflowError):
        accounting.record_output(1)
    assert accounting.output_messages == 0
    assert accounting.output_samples == (1 << 64) - 1


def test_clean_interrupted_accounting_is_incomplete(production):
    accounting = production["RelayAccounting"]("DL", False)
    accounting.record_input(3)
    record = accounting.final_record(sibling_failed=False)
    assert record["status"] == "incomplete" and record["error_count"] == 0
