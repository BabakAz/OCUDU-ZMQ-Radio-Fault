#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Owned broker processes and synthetic ZMQ radio peers for broker-only runs.

``launch`` starts a prepared broker in its own session, with stdout/stderr in
the trial directory, and records the process identity (boot ID, start time,
executable). ``stop`` signals only a process whose identity still matches that
record. ``SyntheticRadio`` emulates the gNB and UE ends of the srsRAN-style
ZMQ virtual radio on private IPC endpoints so a recipe can be exercised
end to end without RAN software; its sinks compare every forwarded sample
with the exact sample that was sent (an independent IQ-level oracle).
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import radio_broker_control as control
import radio_fault as fault

LAUNCH_SCHEMA = "radio_fault_launch_v1"
STOP_SCHEMA = "radio_fault_stop_v1"
DEMO_SCHEMA = "radio_fault_demo_v1"
DIRECTIONS = ("DL", "UL")
MAX_RECORDED_RUNS = 64
TDL_COMMON_DELAY = 15


def reference_model(spec):
    """What an unimpaired direction must reproduce exactly, per recipe.

    Gain and additive recipes forward every sample unchanged outside their
    pulse. The static TDL path delays the whole UL by exactly 15 samples in
    control, exposure and restoration alike. CFO rotates the UL phase during
    its pulse, with zero phase at the pulse's first sample.
    """
    spec = fault.validate_spec(spec)
    tdl = spec["kind"] in fault.GRC_TDL_KINDS
    return {direction: {"delay": TDL_COMMON_DELAY if tdl and direction == "UL" else 0,
                        "cfo_hz": spec.get("cfo_hz", 0) if direction == "UL" else 0}
            for direction in DIRECTIONS}


def require(condition, reason):
    fault.require(condition, reason)


def process_identity(pid):
    """Return ({boot_id, start_ticks, executable}, state) or (None, None) if gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        # The command name may contain spaces or parentheses; fields follow the last ')'.
        fields = raw[raw.rindex(b")") + 2:].split()
        state, start_ticks = fields[0].decode(), int(fields[19])
        if state in ("Z", "X"):
            return None, state
        executable = os.readlink(f"/proc/{pid}/exe")
    except (FileNotFoundError, ProcessLookupError):
        return None, None
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return {"boot_id": boot_id, "start_ticks": start_ticks, "executable": executable}, state


def python_identity():
    """Bind the interpreter that runs the Python broker (invocation path kept for venvs)."""
    executable = Path(sys.executable).resolve(strict=True)
    return {"invocation_path": sys.executable, "executable_path": str(executable),
            "executable_sha256": fault.digest(fault.read_bytes(executable, 256 * 1024 ** 2)),
            "version": sys.version.split()[0]}


def broker_command(preparation, *, c_binary=None, endpoints=None):
    """Exact argv for the prepared broker and its expected build identity."""
    backend = preparation["backend"]
    argv = list(preparation["argv"])
    if backend == "c":
        require(c_binary is not None, "the C backend needs --c-binary (build it with `make`)")
        binary = Path(c_binary).resolve(strict=True)
        command = [str(binary), *argv]
        build = fault.digest(fault.read_bytes(binary, 256 * 1024 ** 2))
    else:
        require(c_binary is None, "--c-binary applies only to the C backend")
        command = [sys.executable, "-u", str(fault.ROOT / "scripts" / "ocudu_channel_broker.py"), *argv, "--no-gui"]
        build = fault.expected_grc_source_identity()["build_sha256"]
    if endpoints is not None:
        command += ["--dl-connect", endpoints["dl-up"], "--dl-bind", endpoints["dl-down"],
                    "--ul-connect", endpoints["ul-up"], "--ul-bind", endpoints["ul-down"]]
    return command, build


def _log_tail(directory, limit=2000):
    try:
        return (directory / "broker.log").read_bytes()[-limit:].decode("utf-8", "replace")
    except OSError:
        return ""


def _terminate_owned(child, timeout=10.0):
    if child.poll() is None:
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    return child.returncode


def launch(directory, *, c_binary=None, endpoints=None, ready_timeout=30.0):
    """Start the prepared broker; return (launch record, Popen) once it is authenticated ready.

    Without ``endpoints`` the broker uses the standard ZMQ ports of the virtual
    radio: it connects to the gNB TX (4000) and UE TX (2001) and binds the UE
    RX (2000) and gNB RX (4001) sides.
    """
    directory = fault.private_directory(directory)
    preparation = fault.strict_json(fault.read_private(directory / "preparation.json"))
    command, build = broker_command(preparation, c_binary=c_binary, endpoints=endpoints)
    log_fd = os.open(directory / "broker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                                 cwd=fault.ROOT, start_new_session=True, close_fds=True)
    finally:
        os.close(log_fd)
    try:
        # Popen returns only after a successful exec, so /proc names the broker.
        identity, _state = process_identity(child.pid)
        require(identity is not None, "broker exited during startup:\n" + _log_tail(directory))
        record = {"schema_version": LAUNCH_SCHEMA, "backend": preparation["backend"], "pid": child.pid,
                  "command": command, "expected_build_sha256": build, "process": identity,
                  "endpoints": endpoints or "standard_zmq_ports",
                  "launched_wall_ns": time.time_ns(), "launched_monotonic_ns": time.monotonic_ns()}
        if preparation["backend"] == "grc":
            record["python_identity"] = python_identity()
        fault.write_private(directory / "launch.json", fault.canonical(record))
        deadline = time.monotonic() + ready_timeout
        while not os.path.lexists(directory / "broker_ready.json"):
            require(child.poll() is None, "broker exited before readiness:\n" + _log_tail(directory))
            require(time.monotonic() < deadline, "broker readiness timed out:\n" + _log_tail(directory))
            time.sleep(0.02)
        # Full authentication: private plan/token, ready identity, PID and build.
        with control.ControlClient(directory, timeout=5.0, expected_backend=preparation["backend"],
                                   expected_pid=child.pid, expected_build_sha256=build):
            pass
    except BaseException:
        _terminate_owned(child)
        raise
    return record, child


def stop(directory, *, child=None, timeout=10.0):
    """SIGTERM the launched broker after re-checking its identity; KILL only on timeout."""
    directory = fault.private_directory(directory)
    record = fault.strict_json(fault.read_private(directory / "launch.json"))
    pid = record["pid"]
    result = {"schema_version": STOP_SCHEMA, "pid": pid, "signals": [], "exit_status": None}
    started = time.monotonic()
    if child is not None:
        require(child.pid == pid, "owned child differs from the launch record")
        if child.poll() is None:
            result["signals"].append("SIGTERM")
        returncode = _terminate_owned(child, timeout)
        if returncode == -signal.SIGKILL:
            result["signals"].append("SIGKILL")
        result.update(outcome="exited", exit_status=returncode)
    else:
        identity, _state = process_identity(pid)
        if identity is None:
            result["outcome"] = "already_exited"
        else:
            require(identity == record["process"],
                    "refusing to signal: process identity differs from the launch record")
            try:
                os.kill(pid, signal.SIGTERM)
                result["signals"].append("SIGTERM")
                deadline = time.monotonic() + timeout
                while process_identity(pid)[0] == record["process"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                if process_identity(pid)[0] == record["process"]:
                    os.kill(pid, signal.SIGKILL)
                    result["signals"].append("SIGKILL")
                    while process_identity(pid)[0] == record["process"]:
                        time.sleep(0.05)
            except ProcessLookupError:
                pass  # it exited on its own between the identity check and the signal
            # A process that is not our child cannot be waited on; the broker's
            # final truth records, not an exit status, describe its shutdown.
            result["outcome"] = "exited"
    result["elapsed_seconds"] = time.monotonic() - started
    fault.write_private(directory / "stop.json", fault.canonical(result))
    return result


class _Observation:
    """Sample-exact comparison of received samples with the unimpaired reference.

    The reference is the sent stream delayed by ``delay`` samples (zeros
    before the first sample, like the broker's initial filter history).
    Differing samples are summarized as maximal runs (the first 64 kept in
    detail) and as uncapped totals; with ``cfo_hz`` set, differing samples are
    also correlated against a rotation at that frequency.
    """

    def __init__(self, *, delay=0, cfo_hz=0, sample_rate=fault.RATE):
        import numpy as np
        self.delay, self.cfo_hz, self.sample_rate = delay, cfo_hz, sample_rate
        self.tail = np.zeros(delay, np.complex64)
        self.messages = self.samples = self.nonfinite = self.differing = 0
        self.first_difference = self.last_difference_end = None
        self.reference_energy = self.received_energy = 0.0
        self.rotation = 0j
        self.runs = []                # [start, end, reference_energy, received_energy, difference_energy]
        self.run_overflow = 0
        self.open_run = None

    def add(self, sent, received):
        import numpy as np
        base = self.samples
        if self.delay:
            stream = np.concatenate((self.tail, sent))
            reference, self.tail = stream[:len(sent)], stream[len(sent):]
        else:
            reference = sent
        differs = received != reference
        self.nonfinite += int(np.count_nonzero(~np.isfinite(received)))
        index = np.flatnonzero(differs)
        if index.size:
            y, x = received[index].astype(np.complex128), reference[index].astype(np.complex128)
            self.differing += int(index.size)
            if self.first_difference is None:
                self.first_difference = base + int(index[0])
            self.last_difference_end = base + int(index[-1]) + 1
            self.reference_energy += float(np.sum(np.abs(x) ** 2))
            self.received_energy += float(np.sum(np.abs(y) ** 2))
            if self.cfo_hz:
                phase = -2j * math.pi * self.cfo_hz * (base + index.astype(np.float64)) / self.sample_rate
                self.rotation += complex(np.sum(y * np.conj(x) * np.exp(phase)))
            flags = np.concatenate(([False], differs, [False]))
            edges = np.flatnonzero(flags[1:] != flags[:-1])
            for start, end in zip(edges[::2], edges[1::2]):
                x_part = reference[start:end].astype(np.complex128)
                y_part = received[start:end].astype(np.complex128)
                energies = [float(np.sum(np.abs(part) ** 2)) for part in (x_part, y_part, y_part - x_part)]
                self._extend(base + int(start), base + int(end), energies)
        self.samples += len(sent)
        self.messages += 1

    def _extend(self, start, end, energies):
        if self.open_run is not None and self.open_run[1] == start:
            self.open_run[1] = end
            for index, value in enumerate(energies):
                self.open_run[2 + index] += value
            return
        self._close()
        self.open_run = [start, end, *energies]

    def _close(self):
        if self.open_run is not None:
            if len(self.runs) < MAX_RECORDED_RUNS:
                self.runs.append(self.open_run)
            else:
                self.run_overflow += 1
        self.open_run = None

    def summary(self):
        self._close()
        runs = [{"start_sample": start, "end_sample": end, "samples": end - start,
                 "received_to_sent_power_ratio": received / sent if sent else None,
                 "difference_mean_power": difference / (end - start)}
                for start, end, sent, received, difference in self.runs]
        result = {"messages": self.messages, "samples": self.samples, "nonfinite_samples": self.nonfinite,
                  "reference_delay_samples": self.delay, "differing_samples": self.differing,
                  "first_difference_sample": self.first_difference,
                  "last_difference_end_sample": self.last_difference_end,
                  "received_to_reference_power_ratio": (self.received_energy / self.reference_energy
                                                        if self.reference_energy else None),
                  "differing_runs": runs, "unrecorded_runs": self.run_overflow}
        if self.cfo_hz:
            result.update(cfo_hz=self.cfo_hz,
                          rotation_coherence=abs(self.rotation) / self.reference_energy if self.reference_energy else None,
                          rotation_phase_rad=math.atan2(self.rotation.imag, self.rotation.real))
        return result


class SyntheticRadio:
    """gNB and UE ends of the ZMQ virtual radio around one broker, on private IPC.

    Per direction, a REP source answers each broker request with the next
    frame and a REQ sink requests frames from the broker, as the srsRAN ZMQ
    driver does. Frames cycle a fixed set of constant-envelope random-phase
    signals at mean power ``power`` (the study's fixed digital UL reference),
    so no sent sample is zero and the sink knows each sent sample exactly.
    """

    def __init__(self, ipc_directory, *, frame_samples=23040, frame_count=16, seed=0x5EED, power=1e7,
                 models=None):
        import numpy as np
        import zmq
        require(type(frame_samples) is int and 1 <= frame_samples <= 1 << 20, "frame size must be 1..1048576 samples")
        rng = np.random.Generator(np.random.PCG64(seed))
        phases = rng.uniform(0.0, 2 * math.pi, (frame_count, frame_samples))
        self.frames = (math.sqrt(power) * np.exp(1j * phases)).astype(np.complex64)
        self.payloads = [frame.tobytes() for frame in self.frames]
        self.endpoints = {name: f"ipc://{ipc_directory}/{name}" for name in ("dl-up", "dl-down", "ul-up", "ul-down")}
        self.context = zmq.Context()
        self.sources_stop, self.sinks_stop = threading.Event(), threading.Event()
        models = models or {direction: {"delay": 0, "cfo_hz": 0} for direction in DIRECTIONS}
        self.observations = {direction: _Observation(**models[direction]) for direction in DIRECTIONS}
        self.sent = {direction: 0 for direction in DIRECTIONS}
        self.errors = []
        self.threads = []

    def _socket(self, kind, endpoint, bind):
        import zmq
        sock = self.context.socket(kind)
        sock.setsockopt(zmq.LINGER, 0)
        (sock.bind if bind else sock.connect)(endpoint)
        return sock

    def _source(self, direction):
        import zmq
        try:
            sock = self._socket(zmq.REP, self.endpoints[direction.lower() + "-up"], True)
            try:
                while not self.sources_stop.is_set():
                    if not sock.poll(50):
                        continue
                    sock.recv()
                    sock.send(self.payloads[self.sent[direction] % len(self.payloads)], copy=False)
                    self.sent[direction] += 1
            finally:
                sock.close()
        except Exception as exc:  # reported through self.errors, never swallowed
            self.errors.append(f"{direction} source: {type(exc).__name__}: {exc}")

    def _sink(self, direction):
        import numpy as np
        import zmq
        try:
            sock = self._socket(zmq.REQ, self.endpoints[direction.lower() + "-down"], False)
            observation = self.observations[direction]
            try:
                while not self.sinks_stop.is_set():
                    sock.send(b"\x01")
                    # Complete the exchange even after a stop request so the
                    # broker is never left with an unanswered request.
                    while not sock.poll(100):
                        if self.errors:
                            return
                    parts = sock.recv_multipart()
                    require(len(parts) == 1, "broker reply was not a single frame")
                    received = np.frombuffer(parts[0], np.complex64)
                    sent = self.frames[observation.messages % len(self.frames)]
                    require(len(received) == len(sent), "broker changed the message length")
                    observation.add(sent, received)
            finally:
                sock.close()
        except Exception as exc:
            self.errors.append(f"{direction} sink: {type(exc).__name__}: {exc}")

    def start_sources(self):
        for direction in DIRECTIONS:
            self._spawn(self._source, direction)

    def start_sinks(self):
        for direction in DIRECTIONS:
            self._spawn(self._sink, direction)

    def _spawn(self, target, direction):
        thread = threading.Thread(target=target, args=(direction,), daemon=True, name=f"{target.__name__}-{direction}")
        thread.start()
        self.threads.append(thread)

    def forwarded_messages(self):
        return {direction: observation.messages for direction, observation in self.observations.items()}

    def stop_sinks(self):
        self.sinks_stop.set()
        for thread in self.threads:
            if thread.name.startswith("_sink"):
                thread.join(10)

    def stop_sources(self):
        self.sources_stop.set()
        for thread in self.threads:
            thread.join(10)
        self.context.term()


def iq_oracle(spec, verification, observations):
    """Check sink-observed differences against the programmed support after the arm epoch.

    Gain and additive recipes must differ from the sent stream exactly on
    their pulses (with exact gain or the programmed added power); static TDL
    recipes must differ from the 15-sample-delayed stream exactly on the TDL
    pulse; CFO must differ only inside its pulse, preserve power, and correlate
    with a rotation at the programmed frequency that starts at the pulse.
    The unaffected DL must reproduce every sent sample exactly.
    """
    spec = fault.validate_spec(spec)
    result = {"consistent": True, "directions": {}}
    if not verification.get("qualified"):
        result.update(consistent=False, reason="broker evidence did not qualify; no arm epoch to align")
        return result
    for direction in DIRECTIONS:
        observed = observations[direction]
        arm = verification["directions"][direction]["arm_sample"]
        checks = {"finite": observed["nonfinite_samples"] == 0}
        expected = []
        affected = direction == spec["affected_direction"]
        for start, end, gain in (fault.pulse_intervals(spec) if affected else ()):
            if spec["kind"] in fault.GRC_TDL_KINDS:
                active = spec["tdl_enabled"]
            elif spec["kind"] in fault.GRC_CFO_KINDS:
                active = spec["cfo_hz"] != 0
            else:
                active = gain != 1 or spec.get("additive_component") is not None
            if active:
                expected.append({"start_sample": arm + start, "end_sample": arm + end, "gain": gain})
        runs = observed["differing_runs"]
        if spec["kind"] in fault.GRC_CFO_KINDS and expected:
            (program,) = expected
            length = program["end_sample"] - program["start_sample"]
            cycles = abs(spec["cfo_hz"]) * length / fault.RATE
            onset = -2 * math.pi * spec["cfo_hz"] * program["start_sample"] / fault.RATE
            error = math.remainder(observed["rotation_phase_rad"] - onset, 2 * math.pi)
            checks.update(
                support=(observed["first_difference_sample"] >= program["start_sample"]
                         and observed["last_difference_end_sample"] <= program["end_sample"]),
                # Only the whole-cycle instants may reproduce the input exactly.
                coverage=length - math.ceil(cycles) - 1 <= observed["differing_samples"] <= length,
                power_preserved=abs(observed["received_to_reference_power_ratio"] - 1) < 1e-4,
                frequency=observed["rotation_coherence"] > 0.9999,
                onset_phase=abs(error) < 1e-3)
        else:
            checks.update(complete=observed["unrecorded_runs"] == 0,
                          run_count=len(runs) == len(expected))
            for index, (run, program) in enumerate(zip(runs, expected)):
                checks[f"run_{index}_support"] = (run["start_sample"] == program["start_sample"]
                                                  and run["end_sample"] == program["end_sample"])
                if spec["kind"] in fault.GRC_TDL_KINDS:
                    continue  # a single static realization has no fixed power gain
                if spec.get("additive_component") is None:
                    # Exact CF32 scaling: gain 0 blanks to zero, 1/8 is a power-of-two scale.
                    checks[f"run_{index}_gain"] = run["received_to_sent_power_ratio"] == program["gain"] ** 2
                else:
                    component = spec["reference_power"] * 10 ** (-spec["component_reference_db"] / 10)
                    checks[f"run_{index}_added_power"] = abs(run["difference_mean_power"] / component - 1) < 0.05
        consistent = all(checks.values())
        result["directions"][direction] = {"arm_sample": arm, "reference_delay_samples": observed["reference_delay_samples"],
                                           "programmed": expected, "observed": runs, "checks": checks,
                                           "consistent": consistent}
        result["consistent"] = result["consistent"] and consistent
    return result


def brief(summary):
    """Compact, human-oriented view of a demo summary (the full record is demo.json)."""
    oracle = summary["iq_oracle"]
    def relative(direction, key):
        entry = oracle.get("directions", {}).get(direction, {})
        arm = entry.get("arm_sample", 0)
        rows = [[row["start_sample"] - arm, row["end_sample"] - arm] for row in entry.get(key, [])]
        return rows[:5] + ([f"... {len(rows) - 5} more"] if len(rows) > 5 else [])
    observations = summary["sink_observations"]
    return {"recipe": summary["recipe"], "backend": summary["backend"],
            "verified": summary["verification"]["qualified"], "iq_oracle_consistent": oracle["consistent"],
            "ul_programmed_samples_after_arm": relative("UL", "programmed"),
            "ul_differing_samples_after_arm": relative("UL", "observed"),
            "ul_differing_samples": observations["UL"]["differing_samples"],
            "dl_differing_samples": observations["DL"]["differing_samples"],
            "errors": summary["verification"]["errors"],
            "trial_directory": summary["trial_directory"],
            "full_summary": str(Path(summary["trial_directory"]).parent / "demo.json"),
            "wall_seconds": round(summary["wall_seconds"], 1)}


def _private_empty_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def demo(recipe, *, seed=41, c_binary=None, output=None, frame_samples=23040, warmup_messages=32,
         log=lambda message: print(message, file=sys.stderr, flush=True)):
    """Prepare, launch, drive, arm, stop and verify one recipe with synthetic peers only."""
    spec = fault.recipe_specification(recipe, seed)
    started = time.monotonic()
    base = _private_empty_directory(output) if output is not None else Path(tempfile.mkdtemp(prefix="rf-demo-", dir="/tmp"))
    trial = base / "trial"
    ids = {name: str(uuid.uuid4()) for name in ("study_id", "trial_id", "pipeline_id")}
    preparation = fault.prepare(spec, trial, **ids)
    log(f"[demo] prepared {recipe} ({spec['backend']} broker) in {trial}")
    ipc = tempfile.mkdtemp(prefix="rf-ipc-", dir="/tmp")
    radio = SyntheticRadio(ipc, frame_samples=frame_samples, models=reference_model(spec))
    child = None
    try:
        radio.start_sources()
        record, child = launch(trial, c_binary=c_binary, endpoints=radio.endpoints)
        log(f"[demo] broker PID {child.pid} ready; build {record['expected_build_sha256'][:16]}...")
        radio.start_sinks()
        deadline = time.monotonic() + 30
        while min(radio.forwarded_messages().values()) < warmup_messages:
            require(not radio.errors, "; ".join(radio.errors))
            require(child.poll() is None, "broker exited during warmup:\n" + _log_tail(trial))
            require(time.monotonic() < deadline, "no IQ flowed through the broker during warmup")
            time.sleep(0.01)

        def healthy():
            require(not radio.errors, "; ".join(radio.errors))
            require(child.poll() is None, "broker exited while armed:\n" + _log_tail(trial))

        log(f"[demo] armed after {radio.forwarded_messages()} warmup messages; "
            f"running {spec['duration_samples'] / fault.RATE:g} s of samples")
        execution = fault.run_schedule(trial, expected_pid=child.pid,
                                       expected_build_sha256=record["expected_build_sha256"],
                                       progress_callback=healthy,
                                       identity_sources={"pid": "launch_record", "build_sha256": "launch_record"})
        radio.stop_sinks()
        stopped = stop(trial, child=child)
        child = None
        radio.stop_sources()
        require(not radio.errors, "; ".join(radio.errors))
    finally:
        if child is not None:
            _terminate_owned(child)
        if not radio.sources_stop.is_set():
            radio.sinks_stop.set()
            radio.stop_sources()
        shutil.rmtree(ipc, ignore_errors=True)
    verification = fault.verify(trial)
    fault.write_private(trial / "verification.json", fault.canonical(verification))
    observations = {direction: radio.observations[direction].summary() for direction in DIRECTIONS}
    oracle = iq_oracle(spec, verification, observations)
    summary = {"schema_version": DEMO_SCHEMA, "recipe": recipe, "backend": spec["backend"],
               "trial_directory": str(trial), "plan_sha256": preparation["plan_sha256"],
               "build_sha256": execution["expected_build_sha256"], "broker_stop": stopped,
               "verification": {key: verification.get(key) for key in (
                   "qualified", "errors", "directions", "programmed_ul_blank_samples",
                   "programmed_ul_attenuated_samples", "programmed_ul_noise_samples", "programmed_ul_cw_samples",
                   "programmed_ul_cfo_samples", "programmed_ul_tdl_samples") if key in verification},
               "iq_oracle": oracle, "sink_observations": observations,
               "wall_seconds": time.monotonic() - started}
    fault.write_private(base / "demo.json", fault.canonical(summary))
    log(f"[demo] verification qualified={verification['qualified']}; "
        f"IQ oracle {'consistent' if oracle['consistent'] else 'INCONSISTENT'}; {summary['wall_seconds']:.1f} s")
    return summary
