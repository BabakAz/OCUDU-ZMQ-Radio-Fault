# Provenance

This repository releases the channel brokers and the radio fault-injection
framework of the study behind the paper *Controlled Radio Fault Injection and
Cross-Layer Observability for Open 5G RANs*. The study was carried out in a
private testbed repository (OCUDU-Telemetry) that also contains the OCUDU
integration, jBPF telemetry, measurement pipeline and evidence. This
repository was extracted from it with a fresh history; the files below record
exactly how its contents relate to the code that produced the paper's
results.

Check everything below on your machine with:

```bash
make verify-provenance
```

## Study revisions

| Revision | Role |
|---|---|
| `f43943cc3071dca4a0c10e080f93a26ce32ffc91` | Historical evidence snapshot cited by the paper |
| `ae71bc701e7bdf3511433b94c15496e8b3488f18` | Executed source of the repeated V3 comparison cited by the paper |
| `a175e47203e3f6e3dd06cf57162291b752949fe5` | Study-repository commit from which this release was extracted |

## Released byte-for-byte

These 22 files are byte-identical to the study at all three revisions; their
SHA-256 values are in [`provenance/study_manifest.json`](provenance/study_manifest.json):

- C broker: `scripts/zmq_channel_broker.c`, `scripts/radio_schedule_native.h`,
  `scripts/radio_schedule_sha256.h`, `scripts/radio_metrics_native.h`
- Python broker: `scripts/ocudu_channel_broker.py`, `scripts/radio_schedule_runtime.py`,
  `scripts/radio_broker_metrics.py`, `scripts/radio_static_tdl.py`,
  `scripts/radio_broker_profile.py`, `scripts/radio_broker_schedule.py`
- Control and resources: `scripts/radio_broker_control.py`, `scripts/radio_broker_resources.py`
- Legacy numerical validator: `scripts/validate_broker.py`
- TDL catalog: `config/channel_broker/tr38901_tdl_v19.4.0.json`
- Contracts and fixtures: the eight files in `config/radio_broker/`

The C test harnesses in `tests/cpp/` and `tests/fixtures/` are also copied
unchanged from the extraction commit.

## Recorded build identities

Both brokers put a build identity into their ready and truth records.

**C broker.** The identity is the SHA-256 of the executable. Compiling the
released source with Ubuntu clang 18.1.3 on Ubuntu 24.04 x86_64 (libzmq
4.3.5) and the flags in [`dependencies/toolchain.lock.json`](dependencies/toolchain.lock.json)
reproduces `edccbe563f0cf5e5cd9795d3b8d26ef7350fc6b53b3458e9deff558407c17a9f`
exactly, from any directory. That value appears in every broker truth record
of the V3 repeated comparison and in the C additive AWGN/CW pilots and their
control. The C source has not changed since study commit `953eb01`.

**Python broker.** The identity is a SHA-256 over the names and SHA-256
values of its six modules (`radio_schedule_runtime.source_build_sha256()`).
The released modules give
`8e7821a286ea642b80d8e00ad8b700edf5bb8a23ecda7de4ea4069c9be5cd583`, the value
recorded by the static TDL-C control, its retry and the TDL-C exposure, and
the state at both paper revisions. Earlier Python-broker experiments ran
earlier revisions, identified by their own recorded values (the module list
also grew over time):

| Recorded identity | Study commit | Experiments |
|---|---|---|
| `2df68731…` | `150d883` | GRC CFO control/exposure pair; first GRC additive pilots |
| `4c1ddc6b…` | `902753d` | GRC additive accounting pilots |
| `8d264727…` | `bf7ca54` | GRC additive low-load controls and exposures |
| `4bea8d2f…` | `b062adb` | Static TDL-A control/exposure pair |
| `8e7821a2…` | `8f54ad0` (= released) | Static TDL-C control, retry and exposure |

These earlier revisions remain in the study repository's history. Only the
last is released here.

## Recipes

The study defined its radio conditions in `ocudu_observability_pilot.py` and
the V3 arms in `ocudu_observability_study.py`. Those modules also compose
OCUDU native-telemetry configuration and capture bundles, which belong to
the testbed. `scripts/radio_fault.py` was assembled from the study module by
copying the recipe, schedule and truth-reconciliation functions verbatim
(error messages reworded) and replacing the testbed-specific preparation,
arming and evidence layout with a standalone equivalent. Its specification
schema and protocol identities are unchanged, so every recipe compiles to the
study's exact plan bytes: for all 16 recipes and the N0/B1/B5 arms, the
specification, profile, schedule and compiled-plan SHA-256 match values
generated from the study's own modules
([`tests/test_radio_fault_recipes.py`](tests/test_radio_fault_recipes.py)).
The study's pilot tests were ported to it with their fixtures and oracles
unchanged.

## Adapted files

| File | Change |
|---|---|
| `scripts/validate_radio_broker_{identity,fixed,schedule,metrics}.py` | Read the compiler from `dependencies/toolchain.lock.json` instead of the study's OCUDU lock; hash this repository's lock/project files; record `git_head` as null outside a git checkout instead of failing. |
| `scripts/validate_radio_broker_{schedule,metrics}.py` | Compute the Python broker's expected build identity over its current six modules (see below). |
| `tests/test_radio_broker_c_{fixed,identity}.py`, `tests/test_cpu_broker_contract.py` | Toolchain lock path; launcher-specific assertions removed; documentation checks point to `docs/BROKERS.md`. |
| `tests/test_radio_broker_profile.py` | Radio-rate checks use `config/examples/`; tests of the study's pipeline launcher removed. |
| `tests/test_radio_static_tdl_integration.py`, `tests/test_grc_profile_log_serialization.py` | Use `radio_fault` instead of the study's pilot module. |
| `config/examples/gnb_zmq_broker.yml` | The study's gNB configuration on broker ports 4000/4001, without the testbed's jBPF, native-metrics and remote-control sections. |
| `config/examples/ue_zmq.conf` | The study's srsUE configuration with an explanatory header. |
| `docs/` | Rewritten for standalone use from the study's broker, schedule, fixed-reference and metrics documentation. |

## Fixed while extracting

Two defects in the study's copies of the actual-process validators were found
by running them here. Neither affects the paper's evidence: these validators
are standalone checks, and the study's experiment runner computed the build
identity correctly.

- The schedule and metrics validators still computed the Python broker's
  expected build identity over its four modules of 10 September 2026, so their
  Python cases stopped at the first identity check once the broker's
  composite grew to five and then six modules on 11 September. They now list
  the same six modules independently of the broker.
- The identity and fixed validators required a git checkout to record the
  source revision.

With both repaired, all 80 validator cases (14 identity, 16 fixed, 34
schedule, 16 metrics) pass on both brokers, the same counts the study
recorded.

## Not included

The OCUDU integration and patches, jBPF codelets, native-telemetry and
application capture, the experiment runner that binds them to trials, the
analysis code, the paper's evidence packages, and the external GPU channel
broker are part of the testbed or of other projects and are not released
here.
