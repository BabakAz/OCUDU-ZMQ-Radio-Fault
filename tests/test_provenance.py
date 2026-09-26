# SPDX-License-Identifier: GPL-3.0-only
"""The released paper files, build identities and documentation links stay intact."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_provenance.py"


def run(root, *args):
    result = subprocess.run([sys.executable, "-B", str(root / "scripts/verify_provenance.py"), *args],
                            capture_output=True, text=True, timeout=180)
    return result.returncode, json.loads(result.stdout)


def test_checkout_matches_the_paper_revisions_and_recorded_builds():
    code, report = run(ROOT)
    assert code == 0, report["failures"]
    assert report["status"] == "verified"
    assert set(report["files"].values()) == {"match"} and len(report["files"]) == 22
    assert report["python_broker_build"]["status"] == "match"
    assert report["c_broker_build"]["status"] in ("match", "not_checked_reference_compiler_unavailable")


def test_manifest_revisions_and_identities_are_the_ones_the_paper_cites():
    manifest = json.loads((ROOT / "provenance/study_manifest.json").read_text())
    assert manifest["revisions"]["historical_evidence_snapshot"].startswith("f43943c")
    assert manifest["revisions"]["executed_v3_source"].startswith("ae71bc7")
    builds = manifest["recorded_build_identities"]
    assert builds["c_broker_binary_sha256"]["value"].startswith("edccbe56")
    assert builds["python_broker_source_composite_sha256"]["value"].startswith("8e7821a2")
    lock = json.loads((ROOT / "dependencies/toolchain.lock.json").read_text())
    assert lock["reference_build"]["binary_sha256"] == builds["c_broker_binary_sha256"]["value"]


def test_a_modified_paper_file_is_reported(tmp_path):
    for name in ("scripts", "config", "provenance", "dependencies"):
        shutil.copytree(ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "config/radio_broker/schedule_schema.json"
    target.write_bytes(target.read_bytes() + b"\n")
    code, report = run(tmp_path, "--c-binary", str(SCRIPT))
    assert code == 1 and report["status"] == "failed"
    assert report["files"]["config/radio_broker/schedule_schema.json"].startswith("differs")
    assert "config/radio_broker/schedule_schema.json differs from the paper revision" in report["failures"]
    assert "the supplied C broker binary differs from the recorded paper build" in report["failures"]


LINK = re.compile(r"\]\(([^)\s]+)\)")


def test_relative_documentation_links_resolve():
    broken = []
    for document in [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md"), *ROOT.glob("config/*.md")]:
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            path = (document.parent / target.split("#")[0]).resolve()
            if not path.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not broken, broken
