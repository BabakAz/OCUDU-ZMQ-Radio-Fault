# Source and build provenance

This repository preserves versioned broker sources, numerical contracts and
recipe identities. The SHA-256 manifest and checks make code changes visible
and distinguish a matching reference build from a build made with a different
toolchain. They do not establish radio or service performance.

```bash
make verify-provenance
```

## Pinned source files

[`provenance/study_manifest.json`](provenance/study_manifest.json) records the
SHA-256 values of 22 preserved files:

- C broker: `scripts/zmq_channel_broker.c`, `scripts/radio_schedule_native.h`,
  `scripts/radio_schedule_sha256.h`, `scripts/radio_metrics_native.h`
- Python broker: `scripts/ocudu_channel_broker.py`, `scripts/radio_schedule_runtime.py`,
  `scripts/radio_broker_metrics.py`, `scripts/radio_static_tdl.py`,
  `scripts/radio_broker_profile.py`, `scripts/radio_broker_schedule.py`
- Control and resources: `scripts/radio_broker_control.py`, `scripts/radio_broker_resources.py`
- Numerical validator: `scripts/validate_broker.py`
- TDL catalog: `config/channel_broker/tr38901_tdl_v19.4.0.json`
- Contracts and fixtures: the eight files in `config/radio_broker/`

These hashes remain unchanged in this update. The source manifest and
`tests/test_provenance.py` detect missing or modified files. The manifest's
existing filename is retained for compatibility.

## C build identity

The C broker reports the SHA-256 of its executable in ready and truth
records. The reference build uses Ubuntu clang 18.1.3 on Ubuntu 24.04 x86_64,
libzmq 4.3.5, and the compiler/linker flags in
[`dependencies/toolchain.lock.json`](dependencies/toolchain.lock.json).
Its SHA-256 is:

```text
edccbe563f0cf5e5cd9795d3b8d26ef7350fc6b53b3458e9deff558407c17a9f
```

`make verify-provenance` rebuilds the broker and compares this hash. If the
reference compiler is unavailable, it reports the binary check as not
performed. Other toolchains can produce working brokers with different
hashes; a source match alone does not establish binary identity.

## Python build identity

`radio_schedule_runtime.source_build_sha256()` computes SHA-256 over the
names and SHA-256 values of six Python broker modules. The preserved source
composite is:

```text
8e7821a286ea642b80d8e00ad8b700edf5bb8a23ecda7de4ea4069c9be5cd583
```

The verifier recomputes this identity independently of a running process.
Dependency versions remain separately pinned in `pyproject.toml` and
`uv.lock`; the source composite does not identify installed dependencies.

## Recipe and example identities

The 16 recipes and N0/B1/B5 aliases retain their canonical specification,
profile, schedule and compiled-plan hashes. The expected values are checked
by [`tests/test_radio_fault_recipes.py`](tests/test_radio_fault_recipes.py).

The G0/G3 profiles and schedules under
[`config/examples/gradual_noise/`](config/examples/gradual_noise/) have their
own verifier, [`scripts/verify_gradual_noise.py`](scripts/verify_gradual_noise.py).
They are separate examples and do not add names to `radio_fault.py`.
See [GRADUAL_NOISE.md](docs/GRADUAL_NOISE.md) for usage and validation scope.

## Software and research scope

This repository contains broker software, configuration examples and
software checks. Research data, experiment orchestration, telemetry
collectors and analysis remain outside its scope. Refer to the
[accompanying paper](README.md#citing) for the research and cite it when
using the software in research.
