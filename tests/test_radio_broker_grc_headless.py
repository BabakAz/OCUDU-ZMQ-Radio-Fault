"""L0/L1 headless imports and local endpoint contracts; no broker sockets."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "scripts/ocudu_channel_broker.py"
VALIDATOR = ROOT / "scripts/validate_broker.py"


@pytest.fixture
def broker():
    spec = importlib.util.spec_from_file_location("headless_contract_test", BROKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_headless_import_needs_no_gui_substitutes():
    # A forbidden import is a failure, not a fake replacement for the dependency.
    code = """
import builtins, importlib.util, sys
original = builtins.__import__
def import_guard(name, *args, **kwargs):
    if name.split('.')[0] in {'gnuradio', 'PyQt5', 'sip'}:
        raise RuntimeError('unexpected GUI import: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = import_guard
spec = importlib.util.spec_from_file_location('production', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
engine = module.ocudu_channel_broker_headless(identity=True).broker
assert engine._dl_imp['identity'][0] and engine._ul_imp['identity'][0]
assert not any(name.split('.')[0] in {'gnuradio', 'PyQt5', 'sip'} for name in sys.modules)
try:
    module.main(options=['--help'])
except SystemExit as error:
    assert error.code == 0
else:
    raise AssertionError('help did not exit explicitly')
"""
    result = subprocess.run([sys.executable, "-c", code, str(BROKER)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--dl-bind" in result.stdout


def test_gui_factory_requests_actual_runtime(broker, monkeypatch):
    import builtins
    imported = []
    original = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "gnuradio":
            imported.append(name)
            raise ModuleNotFoundError("fixture: actual GUI runtime is unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    with pytest.raises(ModuleNotFoundError, match="actual GUI runtime"):
        broker.load_gui_top_block_class()
    assert imported == ["gnuradio"]


@pytest.mark.parametrize("endpoint", [
    "tcp://127.0.0.1:1", "tcp://127.0.0.1:65535",
    "ipc:///tmp/private/dl.sock", "ipc:///tmp/private/with spaces.sock",
    "ipc:///" + "x" * 106,
])
def test_local_endpoint_grammar_accepts_supported_addresses(broker, endpoint):
    assert broker.validate_local_endpoint(endpoint) == endpoint


@pytest.mark.parametrize("endpoint", [
    "", "tcp://localhost:1234", "tcp://0.0.0.0:1234", "tcp://[::1]:1234",
    "tcp://127.0.0.2:1234", "tcp://127.0.0.1:0", "tcp://127.0.0.1:65536",
    "tcp://127.0.0.1:01", "tcp://127.0.0.1:+12", "tcp://127.0.0.1:-12",
    "tcp://127.0.0.1:12 ", "tcp://127.0.0.1:12/path", "tcp://127.0.0.1:１２",
    "ipc://relative", "ipc://@abstract", "ipc:///", "ipc:///tmp/",
    "ipc:///tmp//endpoint", "ipc:///tmp/./endpoint", "ipc:///tmp/../endpoint",
    "ipc:///tmp/endpoint\n", "ipc:///tmp/endpoint\0", "ipc:///tmp/*", "inproc://x",
    "ipc:///" + "x" * 107,
])
def test_bad_endpoint_fails_before_any_socket_or_gui(broker, monkeypatch, endpoint):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid endpoint reached runtime startup")
    monkeypatch.setattr(broker._zmq, "Context", forbidden)
    monkeypatch.setattr(broker, "run_gui", forbidden)
    with pytest.raises(SystemExit) as error:
        broker.main(options=["--no-gui", "--identity", "--dl-bind", endpoint])
    assert error.value.code == 2


@pytest.mark.parametrize("options", [
    ["--no-gui", "--dl-bind", "tcp://127.0.0.1:4000"],
    ["--no-gui", "--dl-bind", "ipc:///tmp/x", "--ul-bind", "ipc:///tmp/x"],
    ["--dl-bind", "ipc:///tmp/x"],
    ["--no-gui", "--dl-bind", "ipc:///tmp/x", "--dl-bind", "ipc:///tmp/y"],
])
def test_endpoint_duplicates_or_gui_overrides_fail_before_startup(broker, monkeypatch, options):
    monkeypatch.setattr(broker._zmq, "Context", lambda: pytest.fail("unexpected socket"))
    with pytest.raises(SystemExit) as error:
        broker.main(options=options)
    assert error.value.code == 2


def test_endpoint_values_reach_headless_engine_without_opening_sockets(broker):
    endpoints = {
        "dl_bind": "ipc:///tmp/private/dl_out", "dl_connect": "ipc:///tmp/private/dl_in",
        "ul_bind": "ipc:///tmp/private/ul_out", "ul_connect": "ipc:///tmp/private/ul_in",
    }
    engine = broker.ocudu_channel_broker_headless(identity=True, **endpoints).broker
    assert {key: getattr(engine, key) for key in endpoints} == endpoints
    defaults = broker.ocudu_channel_broker_headless().broker
    assert defaults.dl_bind == "tcp://127.0.0.1:2000"
    assert defaults.dl_connect == "tcp://127.0.0.1:4000"
    assert defaults.ul_bind == "tcp://127.0.0.1:4001"
    assert defaults.ul_connect == "tcp://127.0.0.1:2001"


def test_direct_engine_constructor_rejects_unsafe_or_duplicate_endpoints(broker):
    with pytest.raises(ValueError, match="endpoint must use"):
        broker.channel_broker_source(dl_bind="tcp://0.0.0.0:2000")
    with pytest.raises(ValueError, match="distinct"):
        broker.channel_broker_source(dl_bind="tcp://127.0.0.1:4000")


def test_validator_does_not_hide_late_production_import_error(tmp_path):
    broken = tmp_path / "broken_broker.py"
    broken.write_text(BROKER.read_text(encoding="utf-8") +
                      "\nraise RuntimeError('deliberate late source import failure')\n", encoding="utf-8")
    env = dict(os.environ, GRC_BROKER_SCRIPT=str(broken))
    result = subprocess.run([sys.executable, str(VALIDATOR), "--quick"],
                            env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert "deliberate late source import failure" in result.stderr
    assert "VALIDATION SUMMARY" not in result.stdout


def test_validator_import_uses_full_real_production_module():
    code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('validator', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert hasattr(module.broker_mod, 'ocudu_channel_broker_headless')
assert hasattr(module.broker_mod, 'main')
assert not any(name.split('.')[0] in {'gnuradio', 'PyQt5', 'sip'} for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code, str(VALIDATOR)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
