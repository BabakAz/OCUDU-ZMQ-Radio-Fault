# G0/G3 gradual-noise example

This example pairs a neutral control (`G0`) with a three-stage uplink AWGN
staircase (`G3`). The [example files](../config/examples/gradual_noise/)
provide a custom schedule for the C broker. The `radio_fault.py` recipes and
their pinned identities
remain available; `G0` and `G3` are not names accepted by that recipe CLI.

## Programmed condition

Both arms use the same profile: 23,040,000 samples/s, master seed 137, unit
desired gain, no CW and an identity downlink. Each direction has a ten-second
schedule (230,400,000 samples) and five events: baseline, three level markers
and restore. G0 retains every marker with noise disabled throughout.

| UL sample interval after ARM | Sample-seconds | G3 digital reference SNR | Complex noise power / reference |
|---|---|---:|---:|
| [92,160,000, 103,680,000) | [4.0, 4.5) | 20 dB | 0.01 |
| [103,680,000, 115,200,000) | [4.5, 5.0) | 10 dB | 0.1 |
| [115,200,000, 126,720,000) | [5.0, 5.5) | 0 dB | 1.0 |

At sample offset 126,720,000, both arms explicitly restore their baseline
settings. G3 adds noise over 34,560,000 UL samples; neither arm blanks,
attenuates or deletes samples. DL stays unchanged, including at its own
level markers. Directions have independent sample clocks; these offsets do
not imply a physical mapping to host timestamps or NR slots.

The fixed digital reference power is **10,000,000**. This is an explicit
example parameter; choose and document an appropriate reference for your own
input signal using [FIXED_REFERENCE.md](FIXED_REFERENCE.md).

For reference SNR `S`, complex noise power is `P_ref * 10**(-S/10)`;
each real component has half this variance. The broker does not normalize
noise to each message's input power. Digital reference SNR is distinct from
reported receiver SINR and calibrated RF SNR. The seed alone does not reproduce
an input stream or warm-up state; see [SCHEDULES.md](SCHEDULES.md).

## Check and prepare

From the repository root, after the [usual setup](../README.md#quick-start):

```bash
make broker
.venv/bin/python scripts/verify_gradual_noise.py --c-binary build/zmq_channel_broker

.venv/bin/python scripts/radio_broker_schedule.py validate \
  --profile config/examples/gradual_noise/profile.json \
  --schedule config/examples/gradual_noise/G3.schedule.json
```

The example uses illustrative study/trial/pipeline identifiers. To prepare a
new experiment, copy the schedule, assign your own identifiers and retain that
copy with the resulting plan hash. Keep the profile and timing fixed when
comparing G0 with G3.

For a preparation-only example, use a new, short private directory:

```bash
trial_dir=$(mktemp -d /tmp/rf-g3-XXXXXX)
.venv/bin/python scripts/radio_broker_schedule.py prepare \
  --profile config/examples/gradual_noise/profile.json \
  --schedule config/examples/gradual_noise/G3.schedule.json \
  --directory "$trial_dir"
```

Preparation prints the broker argument arrays and hashes and creates a
private plan and token. It does not launch or arm anything. Substitute
`G0.schedule.json` and a fresh directory for the matched control.
Use the printed C arguments with `build/zmq_channel_broker` and your own
testbed controller; [SCHEDULES.md](SCHEDULES.md) describes authenticated
STATUS/ARM control, completion and truth accounting. These lower-level
preparations are not `radio_fault.py` trial directories. Record control
receipts and reconcile exact transitions, restoration and final forwarded
counts before claiming an exposure completed. Exclude control tokens and
sockets from shared evidence.

## Scope

The verifier checks file and compiled-plan hashes, and can ask the C broker
to validate the configuration without creating sockets or processing I/Q.
It does not measure receiver behavior or service performance. See the
[paper and citation](../README.md#citing) for the research evaluation, and
please cite the paper when using these tools in research.
