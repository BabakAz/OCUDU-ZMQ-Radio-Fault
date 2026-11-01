#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check that this checkout still contains the brokers and recipes the paper ran.

1. Every file listed in provenance/study_manifest.json must match its SHA-256.
2. The Python broker's build identity (the composite reported in its
   broker_ready and truth records) must equal the recorded value.
3. The C broker is compiled with the reference toolchain into a temporary
   directory and its binary SHA-256 compared with the recorded value. The
   toolchain is the reference compiler (clang 18.1.3) and the GCC installation
   whose C runtime start files and libgcc clang links into the executable
   (clang selects the newest installed GCC). Without either, the check is
   reported as not performed: other toolchains build a working broker, but
   not the recorded bytes. --require-reference-build turns that into a
   failure. With --c-binary, that binary is compared instead.

Nothing is started and no network access is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "provenance/study_manifest.json"
LOCK = ROOT / "dependencies/toolchain.lock.json"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compiler_banner(compiler):
    try:
        return subprocess.run([compiler, "--version"], capture_output=True, text=True, check=True,
                              timeout=10).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def selected_gcc_installation(compiler):
    """The GCC installation whose runtime files clang links, as reported by ``clang -v``."""
    try:
        diagnostics = subprocess.run([compiler, "-v"], capture_output=True, text=True, timeout=10).stderr
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = "Selected GCC installation: "
    return next((os.path.normpath(line[len(prefix):].strip()) for line in diagnostics.splitlines()
                 if line.startswith(prefix)), None)


def reference_toolchain(lock):
    """Return (the not-checked status, or None if the reference toolchain is present; details)."""
    toolchain = lock["toolchain"]
    banner = compiler_banner(toolchain["cc_default"])
    details = {"reference_compiler": banner}
    if banner is None or f"clang version {toolchain['compiler_version']}" not in banner:
        return "not_checked_reference_compiler_unavailable", details
    details.update(gcc_installation=selected_gcc_installation(toolchain["cc_default"]),
                   reference_gcc_installation=lock["reference_build"]["gcc_installation"])
    if details["gcc_installation"] != details["reference_gcc_installation"]:
        return "not_checked_reference_gcc_runtime_unavailable", details
    return None, details


def build_reference(lock, directory):
    """Return (binary or None, build error or None)."""
    toolchain = lock["toolchain"]
    output = Path(directory) / "zmq_channel_broker"
    try:
        subprocess.run([toolchain["cc_default"], *toolchain["cflags"], str(ROOT / "scripts/zmq_channel_broker.c"),
                        "-o", str(output), *toolchain["ldlibs"]], check=True, capture_output=True, text=True,
                       timeout=120)
    except subprocess.CalledProcessError as exc:
        # Keep the compiler's own diagnosis (typically a missing zmq.h) instead of a traceback.
        return None, (exc.stderr or exc.stdout or "").strip()[-2000:] or f"exit status {exc.returncode}"
    except subprocess.TimeoutExpired:
        return None, "the reference build timed out after 120 s"
    return output, None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--c-binary", type=Path,
                        help="check this C broker binary instead of building one with the reference compiler")
    parser.add_argument("--require-reference-build", action="store_true",
                        help="fail instead of skipping the C binary check when the reference toolchain is absent")
    args = parser.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text())
    lock = json.loads(LOCK.read_text())
    report = {"revisions": manifest["revisions"], "files": {}, "failures": []}
    for path, expected in sorted(manifest["identical_files"].items()):
        actual = sha256(ROOT / path) if (ROOT / path).is_file() else None
        report["files"][path] = "match" if actual == expected else f"differs ({actual})"
        if actual != expected:
            report["failures"].append(f"{path} differs from the paper revision")
    sys.path.insert(0, str(ROOT / "scripts"))
    import radio_schedule_runtime
    composite = radio_schedule_runtime.source_build_sha256()
    recorded = manifest["recorded_build_identities"]["python_broker_source_composite_sha256"]["value"]
    report["python_broker_build"] = {"sha256": composite, "recorded_sha256": recorded,
                                     "status": "match" if composite == recorded else "mismatch"}
    if composite != recorded:
        report["failures"].append("Python broker build identity differs from the recorded value")
    recorded = manifest["recorded_build_identities"]["c_broker_binary_sha256"]["value"]
    if args.c_binary is not None and not args.c_binary.is_file():
        report["c_broker_build"] = {"binary": str(args.c_binary), "status": "missing", "recorded_sha256": recorded}
        report["failures"].append(f"the supplied C broker binary does not exist: {args.c_binary}")
    elif args.c_binary is not None:
        actual = sha256(args.c_binary)
        report["c_broker_build"] = {"binary": str(args.c_binary), "sha256": actual, "recorded_sha256": recorded,
                                    "status": "match" if actual == recorded else "differs"}
        if actual != recorded:
            report["failures"].append("the supplied C broker binary differs from the recorded paper build")
    else:
        unavailable, details = reference_toolchain(lock)
        if unavailable is not None:
            # Another toolchain builds a working broker, but not the recorded bytes.
            report["c_broker_build"] = {"status": unavailable, **details, "recorded_sha256": recorded}
            if args.require_reference_build:
                report["failures"].append("the reference toolchain is unavailable, so the C build identity "
                                          f"was not checked ({unavailable})")
        else:
            temporary = tempfile.mkdtemp(prefix="rf-provenance-")
            try:
                binary, error = build_reference(lock, temporary)
                if error is not None:
                    report["c_broker_build"] = {"status": "build_failed", **details, "compiler_output": error,
                                                "recorded_sha256": recorded}
                    report["failures"].append("the reference compiler could not build the C broker; "
                                              "are the libzmq development headers (libzmq3-dev) installed?")
                else:
                    actual = sha256(binary)
                    report["c_broker_build"] = {**details, "sha256": actual, "recorded_sha256": recorded,
                                                "status": "match" if actual == recorded else "mismatch"}
                    if actual != recorded:
                        report["failures"].append("the reference compiler produced a different C broker binary")
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
    try:
        report["libzmq"] = subprocess.run(["pkg-config", "--modversion", "libzmq"], capture_output=True,
                                          text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        report["libzmq"] = None
    report["status"] = "failed" if report["failures"] else "verified"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
