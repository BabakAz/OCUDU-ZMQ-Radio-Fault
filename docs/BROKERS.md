# C and Python channel brokers

Both brokers are standalone processes that sit on the ZeroMQ (ZMQ) I/Q path
of an OCUDU or srsRAN-family gNB and srsUE. They forward every complex-float
(CF32) message in both directions and transform the samples in place. No gNB
or UE source change is needed. The two backends share one profile, schedule,
control and truth contract; the Python broker adds signal models that the
compact C relay does not implement.

| | C broker | Python broker |
|---|---|---|
| Source | `scripts/zmq_channel_broker.c` (+ three headers) | `scripts/ocudu_channel_broker.py` (+ five modules) |
| Runtime | libzmq, libm, pthreads | NumPy, SciPy, pyzmq (headless); GNU Radio/Qt only for the optional GUI |
| Finite recipes | identity, gain/blanking, attenuation, AWGN, CW | the same, plus CFO and static TR 38.901 TDL-A/TDL-C |
| Legacy continuous modes | message-local AWGN, flat Rician/Rayleigh, DL CW | the same, plus EPA/EVA/ETU, CFO, erasures, narrowband noise, scenarios, GUI |
| Build identity in records | SHA-256 of its own executable | SHA-256 composite of its six source modules |

The brokers are synthetic test instruments. Their SNR/SIR settings are digital
power ratios, not calibrated RF measurements, and none of the models is a
certified 3GPP channel implementation.

## Port topology

The srsRAN ZMQ radio uses REQ/REP pairs: a receiver sends a request and the
transmitter replies with one message of samples. A broker splices itself into
both links:

```text
Direct (no broker):
  gNB TX  REP :2000  <- UE RX  REQ
  UE TX   REP :2001  <- gNB RX REQ

With a broker:
  gNB TX  REP :4000  <- broker DL REQ   [impair]  broker DL REP :2000  <- UE RX  REQ
  UE TX   REP :2001  <- broker UL REQ   [impair]  broker UL REP :4001  <- gNB RX REQ
```

The UE configuration is unchanged; the gNB's `ru_sdr.device_args` move to
`tx_port=tcp://127.0.0.1:4000,rx_port=tcp://127.0.0.1:4001`
([example](../config/examples/gnb_zmq_broker.yml)). Both brokers accept
`--dl-bind`, `--dl-connect`, `--ul-bind` and `--ul-connect` to override these
defaults with distinct canonical loopback TCP endpoints or absolute
`ipc://` paths; the caller owns any IPC directory. The Python broker requires
`--no-gui` with endpoint overrides.

## Relay behavior

Each direction runs in its own thread and repeats:

1. receive a request from downstream (its REP socket) and forward it upstream
   (its REQ socket);
2. receive one complete I/Q message from upstream;
3. validate it: a single frame, a whole number of CF32 samples, at most
   64 MiB, every value finite;
4. apply the configured transformation, splitting the message internally at
   any scheduled event boundary;
5. send one reply of the original length downstream.

Messages are never dropped, merged, reordered or resized. Malformed input,
nonfinite output, allocation, socket and thread failures are fatal to the
whole process rather than leaving one direction silently alive. An empty
frame is a legal message with zero samples.

## Channel semantics

Every run selects one versioned semantics with `--channel-semantics`:

| Semantics | Backends | Meaning |
|---|---|---|
| `legacy_message_local_v1` (default) | C, Python | Historical continuous modes; noise and CW are scaled from each message's own power. |
| `fixed_reference_v1` | C, Python | Directional fixed-reference profile: desired gain, AWGN and CW relative to a declared reference power. All finite gain, blank, attenuation, AWGN and CW recipes use it. |
| `grc_cfo_v1` | Python | Fixed reference with a finite directional carrier-frequency offset. |
| `grc_static_tdl_a_v1`, `grc_static_tdl_c_v1` | Python | Static scalar TR 38.901 TDL-A (100 ns) or TDL-C (300 ns) UL channel. |

### Fixed-reference profile

For one direction with CF32 input `x[n]`, desired amplitude gain `g` and
declared reference complex power `P_ref`:

```text
y[n] = g * x[n] + cw[n] + noise[n]
P_noise = P_ref * 10^(-noise_snr_db / 10)
P_cw    = P_ref * 10^(-cw_sir_db / 10)
```

Noise and tone power stay fixed through silence, attenuation and blanking
(`g = 0`): muting the desired signal leaves any configured noise or
interferer present. The AWGN streams and the modulo-2^64 CW oscillator
advance on every sample, even when disabled, so a component's state never
depends on message boundaries. A direction in `identity` mode forwards its
finite input bytes exactly. The complete arithmetic, random-stream derivation
and record formats are in [FIXED_REFERENCE.md](FIXED_REFERENCE.md).

Each backend is repeatable with itself for a given seed and input history,
but C (`rand_r` Box–Muller) and Python (NumPy PCG64) do not produce the same
noise bytes. Compare conditions only within one backend.

### Finite schedules and authenticated control

With `--radio-plan-file PATH --radio-control-dir DIR`, a broker preloads a
compiled finite schedule of up to 32 events per direction at exact sample
offsets and exposes a private UNIX control socket with two operations,
`STATUS` and `ARM`. Each direction arms at its next nonempty message and
counts events from that sample. Every change of settings, the restoration and
the final sample accounting are written to `broker_events.jsonl`. See
[SCHEDULES.md](SCHEDULES.md); `scripts/radio_fault.py` drives this interface
([FAULT_RECIPES.md](FAULT_RECIPES.md)).

`--radio-metrics-every-messages N` adds bounded per-window timing, sample and
energy accounting ([METRICS_CONTRACT.md](METRICS_CONTRACT.md)).

### CFO and static TDL (Python broker)

`grc_cfo_v1` rotates the selected direction by `exp(j·2π·f·k/Fs)`, with zero
phase at the first sample of the offset and `|f| ≤ 500 Hz`; outside the
programmed interval the samples pass unchanged. The static TDL semantics
apply one seed-derived scalar realization of the selected TR 38.901 profile
through fractional-delay filters, with a common 15-sample UL delay that is
present in the control, during exposure and after restoration alike, so only
the channel changes at a boundary. See [STATIC_TDL.md](STATIC_TDL.md).

## Accounting records

Every run, scheduled or not, ends each direction with one final line on
stdout prefixed `C_RELAY_ACCOUNTING: ` or `GRC_RELAY_ACCOUNTING: ` and schema
`radio_broker_accounting_v1`:

| Field | Meaning |
|---|---|
| `backend`, `direction`, `identity` | C/GRC, DL/UL and whether the identity bypass was selected |
| `input_messages`, `input_samples` | Validated finite single-frame inputs (zeros count) |
| `output_messages`, `output_samples` | Complete successful downstream sends |
| `error_count` | Local failures (a sibling failure can leave this zero) |
| `status` | `error` on a relay failure, `incomplete` on a clean stop with unequal counters, otherwise `stopped` |

A successful ZMQ send means the socket accepted the message, not that the
peer processed it. Reconcile with independent peer observations; the
validators in `scripts/` and the demo's IQ oracle do exactly that.

Fixed-reference runs also print `RADIO_FIXED_PROFILE: ` started/final JSON
records per direction with cumulative input, desired, noise, CW and output
energies (the Python broker adds its PCG64 state).

## C broker

Build with the reference toolchain (`make broker`), which reproduces the
binary used in the paper:

```bash
clang-18 -std=c17 -O2 -Wall -Wextra -Wpedantic -Werror \
  scripts/zmq_channel_broker.c -o build/zmq_channel_broker -lzmq -lm -pthread
```

Any C17 compiler builds a working broker, but only this exact toolchain
reproduces the recorded binary SHA-256 ([PROVENANCE.md](../PROVENANCE.md)).
`build/zmq_channel_broker --help` lists every option; the main groups are:

```text
--identity                         byte-exact CF32 relay in both directions
--channel-semantics <version>      legacy_message_local_v1 (default) | fixed_reference_v1
--dl-mode/--ul-mode identity|fixed
--dl-ref-power/--ul-ref-power P    required for fixed_reference_v1, [1e-20, 1e10]
--dl-gain/--ul-gain g              desired amplitude gain [0, 1]
--dl-noise-snr/--ul-noise-snr dB   and --dl-noise-off/--ul-noise-off
--dl-cw/--ul-cw, --dl-cw-sir/--ul-cw-sir dB, --dl-cw-freq/--ul-cw-freq Hz
--radio-plan-file PATH --radio-control-dir DIR   finite schedule and control
--radio-metrics-every-messages N   optional metrics windows
--validate-config-only             check the configuration without sockets
--srate Hz (default 23.04e6)       --seed uint32 (default 1)
legacy: --snr, --dl-snr, --ul-snr, --fading, --k-factor, --rayleigh,
        --doppler, --dl-doppler, --ul-doppler,
        --interference-type none|cw, --interference-freq, --sir, --print-power
```

## Python broker

```bash
.venv/bin/python scripts/ocudu_channel_broker.py --no-gui [options]
```

The headless relay imports only NumPy, SciPy and pyzmq. It accepts the same
fixed-reference, schedule, control, metrics and endpoint options (with
`--samp-rate` instead of `--srate`), plus `--dl-cfo/--ul-cfo` for
`grc_cfo_v1` and `--dl-tdl-enabled/--ul-tdl-enabled` for the static TDL
semantics. Legacy continuous options: `--snr`, `--k-factor`, `--doppler`,
`--fading`, `--rayleigh`, `--profile flat|epa|eva|etu`, `--cfo`,
`--drop-prob`, `--scenario none|drive-by|urban-walk|edge-of-cell`,
`--interference-type none|cw|narrowband`, `--interference-freq`, `--sir`.

Without `--no-gui` the broker opens a Qt window with sliders and spectrum,
time, constellation and waterfall views. That GUI needs GNU Radio 3.10,
PyQt5 and sip from the system (for example `apt install gnuradio`) plus the
`gui` dependency group; it is a legacy interactive tool and is not used by
any recipe.

## Parser bounds

Both implementations reject unknown options, missing or non-numeric values,
infinities, NaNs and out-of-range values:

| Option | Accepted range |
|---|---|
| `--snr`, `--k-factor`, `--sir`, component SNR/SIR | −100 to 100 dB |
| `--doppler` | 0 to 5,000 Hz |
| `--srate` / `--samp-rate` | 1,000 to 250,000,000 Hz |
| `--cfo`, `--dl-cfo`, `--ul-cfo` (Python) | −500 to 500 Hz |
| `--drop-prob` (Python) | 0 to 1 |
| CW and interference frequency | within ±Fs/2 |
| `--seed` | 0 to 4,294,967,295 |

`--dl-snr`/`--ul-snr` are C-only; `--profile`, `--cfo`, `--drop-prob`,
`--scenario` and narrowband interference are Python-only. For EPA/EVA/ETU the
sample rate must be at least eight times the Doppler frequency.

## Legacy continuous modes

These modes predate the fixed-reference contract and remain available for
exploratory stress. They are active from process start (there is no arm or
restoration), their SNR/SIR are re-estimated from every message's desired
power, and results can depend on ZMQ message partitioning.

- **AWGN** (`--snr`): complex noise at the requested ratio to the message's
  post-channel desired power; silent messages receive no noise.
- **Flat Rician/Rayleigh fading** (`--fading`, `--k-factor`, `--rayleigh`,
  `--doppler`): a line-of-sight term plus an AR(1) scatter process stepped once
  per message with a Bessel-derived coefficient. This is not exact Jakes
  correlation, and a finite K-factor gives no guaranteed minimum gain.
- **EPA/EVA/ETU** (Python, `--profile`): TS 36.104 delay tables as a causal
  sparse FIR with per-tap 16-sinusoid Jakes approximations on an absolute
  sample clock, so trajectories are independent of message partitioning.
  These are LTE-era stress profiles, not TR 38.901 NR models. In the study, a
  5 Hz EPA run failed the functional gate on the retained NR receiver while
  its zero-Doppler control passed, so dynamic multipath remains unqualified.
- **CFO** (Python, `--cfo`), **whole-message erasure** (`--drop-prob`),
  **narrowband noise** (180 kHz filtered noise, DL only) and **scenarios**
  (joint wall-clock variation of SNR, Doppler and erasure) are exploratory and
  were not qualified in the study.
- **DL interference** (`--interference-type cw|narrowband`): superposed after
  the channel and before receiver noise, on the DL only. A CW tone is not an
  NR waveform.

[FAULT_COVERAGE.md](FAULT_COVERAGE.md) records which of these families have
full-stack evidence and which are implementation only.

## Troubleshooting

| Symptom | Cause and remedy |
|---|---|
| Broker exits at start with a bind error | Another process owns 2000/4001 (or your override). Stop the old broker or direct gNB/UE first. |
| `Unknown option` or range errors | The parsers are strict by design; see the bounds above and `--help`. |
| UE never attaches | The UE must connect to the broker's 2000 and the gNB to 4000/4001; start the broker first. Very low legacy SNR prevents initial access. |
| `--gui` fails | No display or no GNU Radio/PyQt5; use `--no-gui`. |
| Recipes refuse a gNB/UE config | Every recipe is defined at 23.04 Msps; both `srate` and `base_srate` must match. |
