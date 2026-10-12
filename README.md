# OCUDU-ZMQ-Radio-Fault

Controlled, finite radio faults for software 5G NR testbeds that connect an
OCUDU or srsRAN-family gNB and srsUE through the ZeroMQ (ZMQ) virtual radio.
Two interchangeable channel brokers sit on the I/Q path between gNB and UE
and apply exact, directional, sample-scheduled impairments: blanking,
attenuation, AWGN, CW interference, carrier-frequency offset and static
3GPP TR 38.901 TDL channels. Each fault is armed through an authenticated
control socket, restored explicitly, and accounted for by the broker in
records that can be verified sample by sample after the run.

This repository provides the brokers and fault-control tools described in
*Controlled Radio Fault Injection and Telemetry Evaluation for Open 5G RANs*
by Babak Azkaei, Maxim Bîtiu, Georgios Exarchakos and Kishor Chandra Joshi.
If you use these tools in research, please [cite the paper](#citing).

```text
 DL I/Q:  gNB TX ──:4000──►  broker  ──:2000──► UE RX     identity (unchanged)
 UL I/Q:  gNB RX ◄──:4001──  broker  ◄──:2001── UE TX     blank · gain · AWGN · CW · CFO · static TDL
                              ▲    │
      STATUS / ARM ───────────┘    └──► truth: armed, applied, restored, final sample counts
      (private UNIX socket + token)
```

## What is included

- **C broker** (`scripts/zmq_channel_broker.c`): a compact relay with
  identity, fixed-reference gain/AWGN/CW, finite schedules, authenticated
  control, truth and metrics, plus legacy AWGN, flat fading and CW modes.
- **Python broker** (`scripts/ocudu_channel_broker.py`): a headless NumPy
  relay with the same contract, plus CFO and static TR 38.901 TDL-A/TDL-C,
  and legacy EPA/EVA/ETU, erasure, narrowband and scenario modes with an
  optional GNU Radio GUI.
- **Fault framework** (`scripts/radio_fault.py`): 16 recipes and three
  aliases (N0, B1, B5), with the full lifecycle:
  prepare, launch, arm, stop and verify.
- **Stepped-noise examples**: G0/G3 profiles and schedules, with a separate
  verifier; see [docs/GRADUAL_NOISE.md](docs/GRADUAL_NOISE.md).
- **Broker-only demo**: any recipe through a real broker between synthetic
  gNB/UE endpoints, verified by the broker's truth records and by an
  independent IQ-level oracle; no RAN software needed.
- **Validation**: about 1,500 tests, four actual-process validators (80
  cases), and source/build provenance checks.

## Quick start

On Ubuntu 24.04 (the reference platform):

```bash
sudo apt install clang-18 libzmq3-dev pkg-config make
curl -LsSf https://astral.sh/uv/install.sh | sh     # or use any Python 3.12+ venv, see below
uv sync --frozen                                    # the pinned Python environment
make broker                                         # build/zmq_channel_broker
make demo                                           # B1: one 50 ms UL blank, end to end
make test                                           # about 2 minutes
make verify-provenance                              # check source and reference-build hashes
```

`make demo` prepares the B1 recipe, starts the C broker between synthetic
peers, arms it, runs 10 s of samples, stops it and verifies the trial. It
reports, among other things, the exact zeroed UL interval the synthetic gNB
received: 1,152,000 samples (50 ms) starting 92,160,000 samples (4 s) after
the arm epoch. Without uv, create a virtual environment and install the pins
from `pyproject.toml` (`numpy==2.5.1 scipy==1.18.0 pyzmq==27.1.0
PyYAML==6.0.3 pytest==9.1.1`); `make` uses `.venv/bin/python` when present.

## Recipes

| Recipe | Broker | UL condition, starting 4 s after arm |
|---|---|---|
| `normal` / `N0` | C | No fault |
| `ul_blank_50ms` / `B1` | C | One 50 ms blank |
| `ul_blank_disperse_5x10ms` / `B5` | C | Five 10 ms blanks, 500 ms apart |
| `ul_attenuation_500ms` | C | −18.06 dB amplitude gain for 500 ms |
| `ul_awgn_500ms`, `ul_cw_500ms` (+ `c_fixed_normal`) | C | AWGN or a +1.44 MHz tone 10 dB below a fixed reference, 500 ms |
| `grc_ul_awgn_500ms`, `grc_ul_cw_500ms` (+ `grc_fixed_normal`) | Python | The same on the Python broker |
| `ul_cfo_500ms` (+ `grc_normal`) | Python | +500 Hz frequency offset, 500 ms |
| `grc_ul_tdl_a_500ms`, `grc_ul_tdl_c_500ms` (+ controls) | Python | Static TDL-A (100 ns) or TDL-C (300 ns), 500 ms |

The recipes above run at 23.04 Msps over a 10 s schedule and leave the
downlink unchanged. `scripts/radio_fault.py list` and `show RECIPE` print the
exact definitions. See [docs/FAULT_RECIPES.md](docs/FAULT_RECIPES.md).
G0/G3 use the separate [stepped-noise examples](docs/GRADUAL_NOISE.md), not
names accepted by `radio_fault.py`.

## With a real gNB and UE

```bash
.venv/bin/python scripts/radio_fault.py prepare B1 /tmp/rf/t1
.venv/bin/python scripts/radio_fault.py launch /tmp/rf/t1 --c-binary build/zmq_channel_broker
# start the core, gNB (config/examples/gnb_zmq_broker.yml) and UE; wait for traffic
.venv/bin/python scripts/radio_fault.py arm /tmp/rf/t1
# observe, then stop the UE and gNB
.venv/bin/python scripts/radio_fault.py stop /tmp/rf/t1
.venv/bin/python scripts/radio_fault.py verify /tmp/rf/t1 --write
```

The gNB points its ZMQ radio at the broker (ports 4000/4001); the UE
configuration is unchanged. See [docs/INTEGRATION.md](docs/INTEGRATION.md)
for compatible component versions, configurations and order of operations.

## Documentation

| Topic | Document |
|---|---|
| Recipes, lifecycle, verification, demo | [docs/FAULT_RECIPES.md](docs/FAULT_RECIPES.md) |
| Using it with OCUDU, srsUE and Open5GS | [docs/INTEGRATION.md](docs/INTEGRATION.md) |
| Broker architecture, options and legacy modes | [docs/BROKERS.md](docs/BROKERS.md) |
| G0/G3 stepped-noise profiles and schedules | [docs/GRADUAL_NOISE.md](docs/GRADUAL_NOISE.md) |
| Fixed-reference power, RNG and records | [docs/FIXED_REFERENCE.md](docs/FIXED_REFERENCE.md) |
| Schedules, control protocol and truth records | [docs/SCHEDULES.md](docs/SCHEDULES.md) |
| Optional per-window metrics | [docs/METRICS_CONTRACT.md](docs/METRICS_CONTRACT.md) |
| Static TDL-A/TDL-C | [docs/STATIC_TDL.md](docs/STATIC_TDL.md) |
| Tests, validators and what they establish | [docs/VALIDATION.md](docs/VALIDATION.md) |
| Fault support and software validation | [docs/FAULT_COVERAGE.md](docs/FAULT_COVERAGE.md) |
| Source and build identities | [PROVENANCE.md](PROVENANCE.md) |

## Repository layout

| Path | Content |
|---|---|
| `scripts/` | Brokers, profile/schedule/control modules, `radio_fault.py`, validators, provenance check |
| `config/radio_broker/` | Schemas, fixtures and validation protocols |
| `config/channel_broker/` | TR 38.901 TDL catalog |
| `config/examples/` | gNB/srsUE configurations and standalone G0/G3 profiles and schedules |
| `dependencies/toolchain.lock.json` | Reference C toolchain and the recorded C binary hash |
| `provenance/` | SHA-256 manifest of the preserved source files |
| `tests/` | Offline, fixture and broker-only end-to-end tests |

## Scope

The brokers are synthetic, digital test instruments for a single-host ZMQ
virtual radio. Component SNR/SIR values are digital ratios to a declared
reference, not calibrated RF measurements; the static TDL path is one scalar
realization, not a time-varying or MIMO channel; nothing here has been used
over the air. Legacy continuous modes are provided as they were, and
[docs/FAULT_COVERAGE.md](docs/FAULT_COVERAGE.md) describes implementation and
software-validation coverage. Telemetry collectors, experiment orchestration,
research data and analysis are outside this repository; consult the paper
for the research evaluation.

## Citing

If you use this software in research, please cite:

Babak Azkaei, Maxim Bîtiu, Georgios Exarchakos, and Kishor Chandra Joshi.
2026. **Controlled Radio Fault Injection and Telemetry Evaluation for Open
5G RANs.** In *MobiHoc ’26*, November 23–26, 2026, Tokyo, Japan. ACM.
[doi:10.1145/3842721.3856774](https://doi.org/10.1145/3842721.3856774).

[BibTeX](docs/CITATION.bib) and [CITATION.cff](CITATION.cff) provide reusable
citation metadata. An arXiv link will be added when the preprint is available.

## License

GPL-3.0-only; see [LICENSE](LICENSE) and [AUTHORS.md](AUTHORS.md). The TDL
catalog transcribes numerical tables from 3GPP TR 38.901; see [NOTICE](NOTICE).
