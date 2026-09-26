# Fault families: implementation and evidence

Twelve fault families were implemented before the study, and two NR profiles
were added during it. This table separates what the code can do from what
the paper's study actually exercised. L1 means isolated numerical or
component checks, L2 an actual broker between independent local peers, and
L3 a full OCUDU/srsUE/Open5GS stack. "Finite" means the condition is a
recipe or schedule with authenticated arming and verified restoration;
"legacy" means a continuous mode active from process start.

| Family | C broker | Python broker | Evidence in the study |
|---|---|---|---|
| Gain / sample blanking / attenuation | Finite | Finite | Both L1/L2. C: single and dispersed blanks and attenuation at L3, and the repeated N0/B1/B5 comparison. No Python full-stack blanking comparison. |
| Additive AWGN | Finite | Finite | Both L1/L2; one 500 ms UL L3 exposure per broker with matched controls. |
| Additive CW tone | Finite | Finite | Both L1/L2; one 500 ms UL L3 exposure per broker. |
| Carrier-frequency offset | Rejected | Finite | Python L1/L2 and one +500 Hz / 500 ms L3 control/exposure pair. |
| Static TR 38.901 TDL-A (100 ns) | Rejected | Finite | Python L2 with independent full trajectory; one L3 control/exposure pair. |
| Static TR 38.901 TDL-C (300 ns) | Rejected | Finite | Python L2; one L3 control/exposure pair (the first control failed and is retained with its retry). |
| Flat Rician fading | Legacy | Legacy | Functional L3 runs before the study; no study comparison. |
| Flat Rayleigh limit (K = −100 dB) | Legacy | Legacy | Implementation only. |
| EPA multipath | — | Legacy | Isolated L1; a static run passed and a 5 Hz run failed the functional gate. |
| EVA, ETU multipath | — | Legacy | Implementation only. |
| Narrowband (180 kHz) noise | — | Legacy, DL | Implementation only. |
| Whole-message erasure | — | Legacy | Isolated fixture only. |
| Joint scenarios (drive-by, urban walk, edge of cell) | — | Legacy | Implementation only. |

Beyond the table: TDL-B/D/E are in the catalog but not selectable, and
dynamic (Doppler) NR TDL and CDL/MIMO are outside this repository.

## Reading the table

- An L3 exposure in the study is one control/fault pair in its own
  configuration epoch, not a repeated experiment. Only the N0/B1/B5 blanking
  comparison was repeated (five randomized blocks, 15 trials).
- Evidence applies to the source revision that produced it. The released
  C broker is the one used for the C additive pilots and the V3 comparison,
  and the released Python broker is the one used for the static TDL-C pair.
  Earlier Python experiments ran earlier revisions ([PROVENANCE.md](../PROVENANCE.md)).
- Noise, fading, frequency offset and interference are normal channel
  effects; injecting them tests receiver and protocol behavior, not an
  equipment failure. Blanking and erasure are abstract signal operations.
  They do not model a specific blockage, beam failure or mobility pattern.
  A CW tone does not reproduce a neighboring NR cell.
- Component SNR/SIR values are digital ratios to a declared reference, not
  calibrated receiver measurements. EPA/EVA/ETU are LTE-era profiles (TS
  36.101/36.104), not TR 38.901 NR models.
