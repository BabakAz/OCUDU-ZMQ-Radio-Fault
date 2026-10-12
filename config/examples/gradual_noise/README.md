# Gradual-noise configuration example

These two schedules use the same explicit fixed digital reference power
of **10,000,000**, a sample rate of **23.04 Msps** and master seed **137**.
`G0` forwards both directions unchanged. `G3` adds whole-uplink complex AWGN
at reference levels **20, 10 and 0 dB**, each for 500 ms, starting 4 s after
arming. At 5.5 s the noise is explicitly disabled. Both schedules run for
10 s, have matching event markers and leave the downlink unchanged.

The resulting complex noise powers are 100,000, 1,000,000 and 10,000,000;
each real component has half that variance. These are ratios to the fixed
digital reference, not receiver SINR or calibrated RF measurements. The
identifiers in the JSON files are illustrative placeholders. Replace them
with your own identifiers when preparing a new run.

`manifest.json` pins the released files and compiled plan identities. Check
the unchanged example from the repository root with:

```bash
.venv/bin/python scripts/verify_gradual_noise.py
make broker
.venv/bin/python scripts/verify_gradual_noise.py --c-binary build/zmq_channel_broker
```

The optional C check loads both complete plans and exits before opening
sockets. For preparation and control commands, see
[the gradual-noise guide](../../../docs/GRADUAL_NOISE.md).
