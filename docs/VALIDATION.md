# Validation

Every check here is offline or broker-only: nothing starts a gNB, UE, core,
network service or privileged operation, and nothing transmits RF. Different
checks answer different questions, so results should be quoted with their
level.

| Level | Question | Tool |
|---|---|---|
| L0 | Do sources and builds match their reference identities? | `make verify-provenance` |
| L1 | Are the numerical cores, compilers, parsers and validators correct? | `make test` (unit and fixture tests) |
| L2 | Do the actual broker processes behave as specified between independent peers? | `make validate-local`, `make demo`, the end-to-end tests |
| L3 | What happens in a full RAN under a fault? | Your testbed ([INTEGRATION.md](INTEGRATION.md)); the paper's results |

## Provenance (L0)

```bash
make verify-provenance
```

Checks the SHA-256 of the 22 released broker, recipe-input and contract files
against the reference manifest, recomputes the Python broker's build identity,
and rebuilds the C broker with the reference compiler to compare its binary
hash with the recorded reference build. Without clang
18.1.3 the C comparison is reported as not performed. See
[PROVENANCE.md](../PROVENANCE.md).

## Test suite (L1 with bounded L2 cases)

```bash
make test        # about 2 minutes; compiles C harnesses with clang-18
```

- **C broker cores** (`tests/cpp/*.c` with `tests/test_radio_broker_c_*.py`):
  the production C file is compiled into bounded harnesses that exercise the
  identity relay, fixed-reference DSP, schedules, control and metrics without
  sockets, plus parser and failure paths.
- **Python broker** (`test_radio_broker_grc_*`, `test_radio_schedule_grc`,
  `test_radio_metrics_grc`, `test_radio_static_tdl*`): the actual numerical
  cores against independent power, phase, partition and literal-table oracles.
- **Profiles, schedules, control and resources**: strict parsing, canonical
  hashing, plan compilation, control replies and process-identity reads.
- **Recipes** (`test_radio_fault_recipes.py`): each of the 16 recipes and the
  three V3 aliases reproduces its pinned specification, profile, schedule and
  compiled-plan SHA-256.
- **Stepped-noise example** (`test_gradual_noise_example.py`): pinned files
  and plans, independent event/power checks, tamper rejection and C
  configuration preflight for the neutral and staircase schedules.
- **Truth reconciliation** (`test_radio_fault*.py`): forged, missing,
  duplicated or shifted control and truth records must all be rejected, and
  archived evidence must bind to the expected PID, build and trial identity.
- **End-to-end** (`test_radio_fault_process.py`): complete broker-only trials
  of B5, Python CW, CFO and TDL-C through real brokers, verified and checked
  by the IQ oracle; plus identity-checked stopping.

## Actual-process validators (L2)

```bash
make validate-local
```

runs four validators against the actual brokers with private IPC peers; each
writes a new directory under `artifacts/` containing raw captures, truth,
logs and a `validation.json` report:

| Validator | Cases (C + Python) | Checks |
|---|---|---|
| `validate_radio_broker_identity.py` | 7 + 7 | Byte identity across partitions, signed zeros, extrema, empty and large frames; rejection of misaligned, NaN/Inf and multipart input; clean shutdown with a withheld reply |
| `validate_radio_broker_fixed.py` | 8 + 8 | Fixed-reference baseline, zeros, attenuation, mask, AWGN and CW (each additive also re-partitioned) against analytic oracles |
| `validate_radio_broker_schedule.py` | 17 + 17 | Exact event boundaries, warm-up, partition invariance, identity, incomplete stops and authenticated-control failures |
| `validate_radio_broker_metrics.py` | 8 + 8 | Window accounting against peer captures, timing conservation, instrumentation on/off identity |

There are 80 cases in total. The C broker
is rebuilt with the locked compiler for each run.

## Broker-only trials (L2)

```bash
make demo
.venv/bin/python scripts/radio_fault.py demo grc_ul_tdl_a_500ms
```

Runs one recipe through the full lifecycle with synthetic gNB/UE endpoints
and an IQ-level oracle ([FAULT_RECIPES.md](FAULT_RECIPES.md#broker-only-demo-and-iq-oracle)).

## Stepped-noise configuration example

```bash
make verify-examples
```

Checks the [G0/G3 example](GRADUAL_NOISE.md) file and compiled-plan hashes,
event geometry and fixed-power settings, then validates both plans with the C
broker's configuration-only mode. This check processes no I/Q samples and
does not measure a receiver or application outcome.

## What these checks do not establish

They do not measure RF fidelity, calibrated SNR/SIR, channel conformance,
broker throughput or overhead, or any RAN, service or telemetry outcome.
Those require a full testbed. The paper's own claims, including their
limits, are stated there.
