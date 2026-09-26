"""L1 C relay/format/accounting checks using production code and stubbed I/O.

The harness opens no ZeroMQ socket. Parsing a real binary's --help/invalid
arguments is also offline. These checks do not establish L2 broker acceptance,
NR behavior, noise calibration, or replay equivalence of the legacy DSP chain.
"""

import json
import itertools
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/zmq_channel_broker.c"
HARNESS = ROOT / "tests/cpp/radio_broker_c_identity_harness.c"
PREFIX = "C_RELAY_ACCOUNTING: "
HARNESS_PREFIX = "HARNESS_RESULT: "


@pytest.fixture(scope="module")
def c_identity_programs(tmp_path_factory):
    directory = tmp_path_factory.mktemp("radio-c-identity")
    lock = json.loads((ROOT / "dependencies/toolchain.lock.json").read_text())
    compiler = lock["toolchain"]["cc_default"]
    banner = subprocess.run(
        [compiler, "--version"], check=True, capture_output=True, text=True,
        timeout=10,
    ).stdout.splitlines()[0]
    assert f"clang version {lock['toolchain']['compiler_version']}" in banner
    programs = {}
    for name, source in (("harness", HARNESS), ("cli", SOURCE)):
        program = directory / name
        subprocess.run(
            [compiler, "-std=c17", "-O2", "-Wall", "-Wextra", "-Wpedantic",
             "-Werror", str(source), "-o", str(program), "-lzmq", "-lm", "-pthread"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        programs[name] = program
    return programs


def run_fixture(programs, mode):
    result = subprocess.run(
        [str(programs["harness"]), mode], check=True, capture_output=True,
        text=True, timeout=5,
    )
    records = [json.loads(line[len(PREFIX):]) for line in result.stdout.splitlines()
               if line.startswith(PREFIX)]
    io_records = [json.loads(line[len(HARNESS_PREFIX):])
                  for line in result.stdout.splitlines()
                  if line.startswith(HARNESS_PREFIX)]
    assert len(records) == len(io_records) == 1
    record, observed = records[0], io_records[0]
    assert record["schema_version"] == "radio_broker_accounting_v1"
    assert record["record_type"] == "final"
    assert record["backend"] == "c"
    assert record["direction"] == "DL"
    for field in ("input_messages", "input_samples", "output_messages",
                  "output_samples", "error_count"):
        assert type(record[field]) is int and 0 <= record[field] < 2 ** 64
    assert observed["transport"] == "stubbed-in-memory"
    assert observed["sockets_closed"] == 1
    assert observed["request_mismatch"] == 0
    assert record["output_messages"] == observed["forwarded_messages"]
    assert record["output_samples"] == observed["forwarded_samples"]
    return record, observed, result


@pytest.mark.parametrize("mode,messages,samples", [
    ("identity-contiguous", 1, 4096),
    ("identity-fixed", 16, 4096),
    ("identity-irregular", 7, 4096),
    ("identity-odd", 1, 11),
    ("identity-zeros", 1, 11),
    ("empty", 1, 0),
])
def test_identity_preserves_finite_bytes_and_all_sample_counts(
    c_identity_programs, mode, messages, samples,
):
    record, observed, _ = run_fixture(c_identity_programs, mode)
    assert record["identity"] is True
    assert record["status"] == "stopped"
    assert record["input_messages"] == record["output_messages"] == messages
    assert record["input_samples"] == record["output_samples"] == samples
    assert record["error_count"] == observed["fatal"] == 0
    # Includes signed zeros, subnormals and finite float32 extrema, with all
    # impairment switches set in the fixture to prove identity overrides them.
    assert observed["byte_mismatch"] == 0


def test_identity_keeps_same_ordered_bytes_across_partitions(c_identity_programs):
    hashes = []
    for mode in ("identity-contiguous", "identity-fixed", "identity-irregular"):
        _, observed, _ = run_fixture(c_identity_programs, mode)
        hashes.append(observed["output_hash_fnv1a64"])
    # In addition to each output's direct input memcmp, compare the continuous
    # ordered byte stream under three message partitions. This is identity
    # evidence only; it makes no legacy impairment partition claim.
    assert len(set(hashes)) == 1


def test_legacy_silent_frames_are_counted_after_successful_send(c_identity_programs):
    record, observed, result = run_fixture(c_identity_programs, "counter-baseline")
    assert record["identity"] is False
    assert record["input_messages"] == record["output_messages"] == 10000
    assert record["input_samples"] == record["output_samples"] == 110000
    assert "10000 msgs, 0.1 M samples processed" in result.stdout
    assert record["status"] == "stopped"
    assert observed["byte_mismatch"] == record["error_count"] == 0


@pytest.mark.parametrize("mode", ["legacy-awgn", "legacy-fading-cw"])
def test_legacy_impairments_remain_selected_without_identity(c_identity_programs, mode):
    record, observed, _ = run_fixture(c_identity_programs, mode)
    assert record["identity"] is False
    assert record["status"] == "stopped"
    assert record["input_samples"] == record["output_samples"] == 11
    assert observed["byte_mismatch"] == 1
    assert record["error_count"] == 0


@pytest.mark.parametrize("mode,error_fragment", [
    ("nan", "nonfinite IQ input"),
    ("infinity", "nonfinite IQ input"),
    ("misaligned", "not cf32-aligned"),
    ("multipart-request", "receive downstream request is multipart"),
    ("multipart-iq", "receive upstream IQ reply is multipart"),
    ("oversized", "limit is 67108864 bytes"),
])
def test_invalid_message_fails_before_count_or_forward(
    c_identity_programs, mode, error_fragment,
):
    record, observed, result = run_fixture(c_identity_programs, mode)
    assert record["status"] == "error"
    assert record["input_messages"] == record["input_samples"] == 0
    assert record["output_messages"] == record["output_samples"] == 0
    assert record["error_count"] == observed["fatal"] == 1
    assert error_fragment in result.stderr
    if mode == "multipart-request":
        assert observed["received_iq_messages"] == 0


@pytest.mark.parametrize("mode,status,errors,fatal", [
    ("failed-send", "error", 1, 1),
    ("overflow-output", "error", 1, 1),
    ("interrupted-send", "incomplete", 0, 0),
    ("sibling-failed-send", "error", 0, 1),
])
def test_failure_or_interruption_preserves_input_output_difference(
    c_identity_programs, mode, status, errors, fatal,
):
    record, observed, result = run_fixture(c_identity_programs, mode)
    assert record["input_messages"] == 1
    assert record["input_samples"] == 11
    assert record["output_messages"] == record["output_samples"] == 0
    assert record["status"] == status
    assert record["error_count"] == errors
    assert observed["fatal"] == fatal
    if mode == "overflow-output":
        assert "nonfinite IQ output" in result.stderr


def test_identity_cli_is_explicit_and_invalid_options_stay_fail_closed(c_identity_programs):
    help_result = subprocess.run(
        [str(c_identity_programs["cli"]), "--identity", "--help"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    assert "Byte-exact finite cf32 relay in both directions" in help_result.stdout
    invalid = subprocess.run(
        [str(c_identity_programs["cli"]), "--identity", "--snr", "NaN"],
        capture_output=True, text=True, timeout=5,
    )
    assert invalid.returncode == 2
    assert "requires a finite number" in invalid.stderr
    assert "Active:" not in invalid.stdout


ENDPOINT_OPTIONS = ("--dl-bind", "--dl-connect", "--ul-bind", "--ul-connect")


@pytest.mark.parametrize("option", ENDPOINT_OPTIONS)
@pytest.mark.parametrize("endpoint", [
    "tcp://127.0.0.1:1",
    "tcp://127.0.0.1:65535",
    "ipc:///tmp/radio-fixture/endpoint.sock",
    "ipc:///tmp/radio fixture/endpoint.sock",
    "ipc:///" + "a" * 106,  # 107-byte Linux sockaddr_un pathname ceiling.
])
def test_local_endpoint_options_accept_canonical_syntax_without_sockets(
    c_identity_programs, option, endpoint,
):
    result = subprocess.run(
        [str(c_identity_programs["cli"]), option, endpoint, "--help"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    assert "Usage:" in result.stdout
    assert "Active:" not in result.stdout
    assert "DL endpoints:" not in result.stdout


@pytest.mark.parametrize("endpoint", [
    "", "tcp://127.0.0.1:", "tcp://127.0.0.1:0", "tcp://127.0.0.1:65536",
    "tcp://127.0.0.1:01", "tcp://127.0.0.1:+2000", "tcp://127.0.0.1:-1",
    "tcp://127.0.0.1:2000/path", "tcp://127.0.0.1:2000 ",
    "tcp://127.0.0.1:999999999999999999999999999999999999999999999999",
    "tcp://localhost:2000", "tcp://0.0.0.0:2000", "tcp://[::1]:2000",
    "tcp://192.0.2.1:2000", "inproc://example",
    "ipc://relative", "ipc://@abstract", "ipc:///", "ipc:///tmp/radio/",
    "ipc:///tmp//radio", "ipc:///tmp/./radio", "ipc:///tmp/../radio",
    "ipc:///tmp/.", "ipc:///tmp/..", "ipc:///tmp/*", "ipc:///tmp/a\nb",
    "ipc:///tmp/a\x7fb", "ipc:///" + "a" * 107,
])
def test_invalid_endpoint_is_rejected_by_actual_cli_before_context(
    c_identity_programs, endpoint,
):
    result = subprocess.run(
        [str(c_identity_programs["cli"]), "--dl-bind", endpoint],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "--dl-bind requires tcp://127.0.0.1:" in result.stderr
    # Invalid parsing exits before the startup banner and zmq_ctx_new().
    assert result.stdout == ""


@pytest.mark.parametrize("option", ENDPOINT_OPTIONS)
def test_missing_or_repeated_endpoint_option_fails_before_startup(
    c_identity_programs, option,
):
    for args, expected in [
        ([option], f"{option} requires a value"),
        ([option, "ipc:///tmp/a", option, "ipc:///tmp/b"], "duplicate endpoint option"),
    ]:
        result = subprocess.run(
            [str(c_identity_programs["cli"]), *args], capture_output=True,
            text=True, timeout=5,
        )
        assert result.returncode == 2
        assert expected in result.stderr
        assert result.stdout == ""


@pytest.mark.parametrize("first,second", list(itertools.combinations(ENDPOINT_OPTIONS, 2)))
def test_duplicate_endpoint_pairs_are_rejected_before_context(
    c_identity_programs, first, second,
):
    result = subprocess.run(
        [str(c_identity_programs["cli"]), first, "ipc:///tmp/same", second,
         "ipc:///tmp/same"], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "all four relay endpoints must be distinct" in result.stderr
    assert result.stdout == ""


def test_endpoint_override_cannot_alias_an_unchanged_default(c_identity_programs):
    result = subprocess.run(
        [str(c_identity_programs["cli"]), "--dl-bind", "tcp://127.0.0.1:4001"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "all four relay endpoints must be distinct" in result.stderr
    assert result.stdout == ""


def test_help_states_legacy_fader_limits(c_identity_programs):
    result = subprocess.run(
        [str(c_identity_programs["cli"]), "--help"], check=True,
        capture_output=True, text=True, timeout=5,
    )
    assert "does not impose a positive minimum gain" in result.stdout
    assert "message-stepped AR1 approximation, not exact Jakes fading" in result.stdout
    assert "(safe)" not in result.stdout
