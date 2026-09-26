# Directional fixed-reference profiles

`fixed_reference_v1` gives each direction an independent desired gain,
additive Gaussian noise and continuous-wave (CW) tone, all defined relative to
a declared reference power. Both the C and the headless Python broker
implement it. The finite gain, blank, attenuation, AWGN and CW recipes are
schedules over this profile ([SCHEDULES.md](SCHEDULES.md)).

## Power and clock contract

For one direction, let `x[n]` be finite native CF32 input, `g` the desired
amplitude gain and `P_ref` the declared reference complex power:

```text
d[n] = g * x[n]
P_noise = P_ref * 10^(-noise_snr_db / 10)
P_cw    = P_ref * 10^(-cw_sir_db / 10)
y[n] = d[n] + cw[n] + noise[n]
```

Each independent noise I/Q component has variance `P_noise / 2`. Noise and
tone power stay fixed through silence, attenuation and a desired-signal mask
(`g = 0`): muting the desired signal leaves receiver noise and the configured
interferer present. The configured SNR/SIR is relative to `P_ref`, not to the
instantaneous input or measured output. Gain scales desired **amplitude**, so
desired power scales by `g²`. All powers are relative digital `I² + Q²`, not
watts, dBm or a calibrated receiver noise figure.

Each desired/noise/tone component rounds to CF32 before a binary64 sum and a
final CF32 output rounding. Consequently gain 1 with both additions disabled
reproduces the input exactly, and gains 0 and 1/8 are exact scalings; the
demo's IQ oracle relies on this. A direction in `identity` mode preserves
finite input bytes exactly, including signed zero, advances its processed
sample count, and draws no noise or tone phase. Identity still validates
message format and reconciles successful sends. Empty frames advance message
counts but no DSP sample state.

Fixed-mode CW uses a modulo-2^64 phase accumulator. Given binary64 input
frequency and sample rate, its step magnitude is
`floor(ldexp(abs(frequency) / sample_rate, 64) + 0.5)`; a negative frequency
negates that integer modulo 2^64. Both implementations use the same binary64
operations. Phase advances on every fixed-mode sample, including zero input
and disabled CW, and does not depend on message boundaries or a large
floating absolute sample index. Within a finite schedule only the predeclared
coefficients may change; sample rate, reference power, mode and seed are
immutable.

## Random streams and reproducibility

`component_streams_v1` derives an AWGN seed from the uint32 master seed, a
direction tag (DL `0x0D1A5EED`, UL `0x00A17EED`) and the component tag
`0x4157474E`. The mixer input is the XOR of master, direction and component
tags; it then applies XOR-shift 16, multiply `0x7FEB352D`, XOR-shift 15,
multiply `0x846CA68B`, XOR-shift 16, with uint32 multiplication.

C uses `rand_r` and float32 Box–Muller pairs; Python uses NumPy PCG64 and
float32 `standard_normal`. The two backends do not produce equal noise bytes.
Within one backend and recorded environment, fixed-mode sample streams are
repeatable across message partitions. Every fixed-mode sample consumes two
real normal variates even with noise disabled. CW consumes no random draws,
and changing DL settings never consumes UL draws. Same-seed repeatability is
an engineering regression, not a promise of identical random bytes across a
different generator, libc, NumPy, compiler or architecture.

## Profile files and CLI mapping

[`radio_broker_profile.py`](../scripts/radio_broker_profile.py) accepts a
regular JSON file of at most 16 KiB. Required versions, every field, finite
ranges and direction names are checked; unknown or duplicate fields fail and
no executable or free-form argument can be expressed. `reference_provenance`
names a reference record but does not attest that a calibration happened, and
`qualification` must be `development_only`. The adapter maps a profile to
either broker's CLI without starting anything:

```bash
.venv/bin/python scripts/radio_broker_profile.py \
  --profile config/radio_broker/fixed_reference.fixture.json --backend c
```

With `--gnb-config` and `--ue-config` it also checks that both ZMQ radios use
the profile's sample rate in `srate` and in exactly one `base_srate` token.
The [fixture](../config/radio_broker/fixed_reference.fixture.json) uses a
synthetic unit reference power; it is a development example, not a
calibrated operating point.

The paper's additive recipes use a fixed UL reference of 1×10⁷, the rounded
mean input power of a retained no-fault UL capture in the study. To choose a
reference for another setup, define a clean identity interval per direction,
reconcile its input/output counts with the peers, compute
`P_ref = sum(I² + Q²) / N` over that interval (including zero samples), record
the bounds and conditions, and freeze that value for all treatments. Never
re-estimate it from a treated message.

## Records

`RADIO_FIXED_PROFILE: ` prefixes started/final JSON records for each
direction on stdout (schema `radio_fixed_profile_v1`). Records identify the
backend, semantics, RNG, reference power, effective settings, sample clock,
normal-draw counts, CW step/phase and cumulative input/desired/noise/CW/output
energies. `masked_samples` counts `g = 0` and `attenuated_samples` counts
`0 < g < 1`; the categories are disjoint. Energies are sums over processed
samples; divide by the matching sample count only when it is nonzero.
Component energies do not in general sum to the output energy because of
cross terms. Python additionally records its PCG64 state and NumPy version.

These records cover **processed** samples. The `radio_broker_accounting_v1`
records cover validated inputs and successful sends; a send failure can leave
processed counts ahead of forwarded counts. Neither record alone certifies
delivered service or schedule completion.

## Relation to the legacy power model

`legacy_message_local_v1` scales additions from each message's desired power.
That is a different model and cannot represent fixed receiver noise. The
fixed-reference path also corrects two legacy behaviors: CW phase now
advances through silent input in both brokers, and C replaces its float32
phase accumulator with the uint64 oscillator, so legacy CW trajectories differ
from those of the study's earliest runs.

## Validation scope

The offline tests exercise the production numerical cores with fixed seeds
and independent power/phase oracles, including silent and masked input,
message partitions, directional isolation, counter overflow, nonfinite input
and CLI rejection. `scripts/validate_radio_broker_fixed.py --run-local` runs
the actual production processes against finite private IPC peers. Neither is
a radio or throughput experiment.
