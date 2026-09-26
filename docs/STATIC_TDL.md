# Static TR 38.901 TDL-A and TDL-C

The Python broker implements two static, single-antenna tapped-delay-line
channels from 3GPP TR 38.901 V19.4.0 (Release 19) clause 7.7.2: **TDL-A with
a 100 ns delay spread** (23 diffuse paths) and **TDL-C with a 300 ns delay
spread** (24 diffuse paths). They are selected with
`--channel-semantics grc_static_tdl_a_v1` or `grc_static_tdl_c_v1` and used by
the `grc_*tdl*` recipes. The numerical core is
[`scripts/radio_static_tdl.py`](../scripts/radio_static_tdl.py).

## Model

- **Catalog.** Normalized delays and path powers come from
  [`config/channel_broker/tr38901_tdl_v19.4.0.json`](../config/channel_broker/tr38901_tdl_v19.4.0.json),
  a transcription of the TR 38.901 tables (TDL-A through TDL-E). The core
  checks the file's SHA-256 before use, so it must stay byte-identical (the
  repository forces LF line endings for this reason). Delays scale with the
  requested delay spread; path powers are normalized to unit total.
- **Realization.** For each direction a seed is derived from the uint32
  master seed with the same mixer as the fixed-reference noise streams (tag
  `0x54415053`). NumPy PCG64 draws one complex Gaussian coefficient per path,
  once, scaled by the path's power. Doppler is zero: the realization never
  changes during a run.
- **Filtering.** Each path uses a 32-tap Kaiser-windowed (β = 6) sinc
  fractional-delay filter centered at 15 samples. The per-path filters and
  coefficients are summed into one complex FIR kernel, applied as a causal
  convolution with a history carried across messages, so the output does not
  depend on ZMQ message partitioning.
- **Common delay.** Only the UL is processed; the DL is identity. The UL is
  delayed by exactly 15 samples at all times: in the control recipes, before
  and after the TDL pulse, and (through the filter center) during it. Only the
  channel changes at the pulse boundaries, never the delay.
- **Records.** The realization (delays, powers, coefficients, kernel, seed,
  catalog hash) is exported before arm and bound into `preparation.json`;
  the broker's arm and final records carry the realization hash, the filter
  history hash and the exact count of samples that went through the channel.
  `radio_fault.py verify` checks all of them.

At 23.04 Msps the TDL-A path delays span up to 22.25 samples (combined
kernel of 54 taps) and the TDL-C delays up to 59.80 samples (91 taps). Path
powers are normalized in the ensemble mean, so a single seed-41 realization
does not have unit power gain.

## Scope

This is a static scalar realization of the selected catalog entries. It is
**not** a time-varying 3GPP channel, a CDL/MIMO model, or evidence of RF
conformance, and the paper does not claim otherwise: the TDL-A and TDL-C
results are single control/exposure pairs with model-specific scope. TDL-B,
TDL-D and TDL-E are present in the catalog but are not selectable here.
The legacy EPA/EVA/ETU profiles of the Python broker are LTE-era stress models
and are not TR 38.901 profiles.
