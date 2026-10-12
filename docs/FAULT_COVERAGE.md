# Fault support and software validation

This page describes implemented mechanisms and the checks available in this
repository. “Finite” means a profile or schedule supports authenticated
arming and explicit restoration. “Legacy” means a continuous mode active
from process start. Support does not imply identical numerical realizations
or equivalent validation of both backends.

| Mechanism | C broker | Python broker |
|---|---|---|
| Gain / sample blanking / attenuation | Finite | Finite |
| Additive AWGN | Finite and legacy | Finite and legacy |
| Additive CW tone | Finite; legacy DL | Finite; legacy DL |
| Carrier-frequency offset | Rejected | Finite and legacy |
| Static TR 38.901 TDL-A (100 ns) | Rejected | Finite |
| Static TR 38.901 TDL-C (300 ns) | Rejected | Finite |
| Flat Rician fading / Rayleigh limit | Legacy | Legacy |
| EPA / EVA / ETU multipath | — | Legacy |
| Narrowband (180 kHz) noise | — | Legacy, DL |
| Whole-message erasure | — | Legacy |
| Joint scenarios (drive-by, urban walk, edge of cell) | — | Legacy |

TDL-B/D/E appear in the numerical catalog but are not selectable. Dynamic
(Doppler) NR TDL and CDL/MIMO are outside the implemented interface.

## Available checks

- **Source/build checks** verify preserved file hashes, the Python source
  composite and, when the reference toolchain is available, the C executable
  hash. See [PROVENANCE.md](../PROVENANCE.md).
- **Unit and fixture tests** exercise numerical cores, parsers, scheduling,
  authentication, accounting and truth reconciliation. CFO and static TDL
  have Python-specific numerical and process tests.
- **Actual-process validators** exercise identity, fixed-reference DSP,
  schedules and metrics on both brokers between independent local peers.
- **Broker-only demos** run the recipe lifecycle with synthetic endpoints
  and an independent I/Q-level oracle.
- **Stepped-noise examples** have their own profile, schedule and numerical
  checks; see [GRADUAL_NOISE.md](GRADUAL_NOISE.md).

See [VALIDATION.md](VALIDATION.md) for commands and exact coverage. Legacy
modes do not have the same finite-schedule validation contract. Passing a
software check establishes only the behavior checked; it does not measure
a full RAN's radio, protocol or service response.

## Interpretation limits

Noise, fading, frequency offset and interference are channel effects;
blanking and erasure are abstract signal operations. They do not identify a
specific equipment failure, blockage, beam failure or mobility pattern, and
a CW tone does not reproduce a neighboring NR cell.

Component SNR/SIR values are digital ratios to a declared reference, not
calibrated receiver measurements. EPA/EVA/ETU are LTE-era profiles
(TS 36.101/36.104), not TR 38.901 NR models. The static TDL implementation is
a scalar realization, not a certified 3GPP channel emulator.

Research evaluation is described in the [paper](../README.md#citing).
