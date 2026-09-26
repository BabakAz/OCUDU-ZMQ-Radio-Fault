#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check that this checkout still contains the brokers and recipes the paper ran.

1. Every file listed in provenance/study_manifest.json must match its SHA-256.
2. The Python broker's build identity (the composite reported in its
   broker_ready and truth records) must equal the recorded value.
3. The C broker is compiled with the reference compiler (clang 18.1.3) into a
   temporary directory and its binary SHA-256 compared with the recorded
   value. Without that compiler the check is reported as not performed: other
   compilers build a working broker, but not the recorded bytes. With
   --c-binary, that binary is compared instead.

Nothing is started and no network access is needed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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




def build_reference(lock, directory):
    toolchain = lock["toolchain"]
    banner = compiler_banner(toolchain["cc_default"])
    if banner is None or f"clang version {toolchain['compiler_version']}" not in banner:
        return None, banner
    output = Path(directory) / "zmq_channel_broker"
    subprocess.run([toolchain["cc_default"], *toolchain["cflags"], str(ROOT / "scripts/zmq_channel_broker.c"),
                    "-o", str(output), *toolchain["ldlibs"]], check=True, capture_output=True, timeout=120)
    return output, banner


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--c-binary", type=Path,
                        help="check this C broker binary instead of building one with the reference compiler")
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
    if args.c_binary is not None:
        actual = sha256(args.c_binary)
        report["c_broker_build"] = {"binary": str(args.c_binary), "sha256": actual, "recorded_sha256": recorded,
                                    "status": "match" if actual == recorded else "differs"}
        if actual != recorded:
            report["failures"].append("the supplied C broker binary differs from the recorded paper build")
    else:
        temporary = tempfile.mkdtemp(prefix="rf-provenance-")
        try:
            binary, banner = build_reference(lock, temporary)
            if binary is None:
                # Another compiler builds a working broker, but not the recorded bytes.
                report["c_broker_build"] = {"status": "not_checked_reference_compiler_unavailable",
                                            "reference_compiler": banner, "recorded_sha256": recorded}
            else:
                actual = sha256(binary)
                report["c_broker_build"] = {"reference_compiler": banner, "sha256": actual,
                                            "recorded_sha256": recorded,
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
