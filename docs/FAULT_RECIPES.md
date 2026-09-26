# Fault recipes and the trial lifecycle

A **recipe** is a named, fixed radio condition: a broker profile plus a finite
sample schedule for both directions. `scripts/radio_fault.py` freezes a recipe
into a private control directory, arms a running broker through its
authenticated control socket, and afterwards reconciles the broker's own truth
records against the plan. The recipes are exactly those of the paper's study:
each one compiles to the same plan bytes as in the study
([test](../tests/test_radio_fault_recipes.py), [provenance](../PROVENANCE.md)).

## Common timing

Every recipe runs at **23.04 Msps** (20 MHz NR carrier, 15 kHz SCS) and
schedules **10 s = 230,400,000 samples** per direction from the arm epoch.
Faults start **4 s (92,160,000 samples) after arm** and affect the **whole
uplink**; the downlink is forwarded unchanged (identity mode). Every schedule
starts with a `baseline` event and ends each pulse with an explicit restore.
The master seed defaults to 41. After ARM the broker must complete the whole
schedule within 60 s of wall time. The broker keeps forwarding after the
schedule completes, so a testbed can observe recovery for as long as it likes.

Because the whole UL leg is disturbed, a fault can also disturb feedback for
DL transmissions (HARQ acknowledgements, CSI) as well as UL data.

## Recipes

| Recipe | Broker | UL condition after arm |
|---|---|---|
| `normal` | C | No fault: UL fixed mode at unit gain, additions disabled |
| `ul_blank_50ms` | C | One whole-UL blank (gain 0) over [4.00, 4.05) s |
| `ul_blank_disperse_5x10ms` | C | Five 10 ms blanks starting at 4.0, 4.5, 5.0, 5.5 and 6.0 s |
| `ul_attenuation_500ms` | C | Amplitude gain 0.125 (−18.06 dB) over [4.0, 4.5) s |
| `c_fixed_normal` | C | Control for C additive recipes: reference power 1×10⁷, components disabled |
| `ul_awgn_500ms` | C | AWGN at 10 dB below the 1×10⁷ reference over [4.0, 4.5) s |
| `ul_cw_500ms` | C | CW tone at +1.44 MHz, 10 dB below reference, over [4.0, 4.5) s |
| `grc_normal` | Python | Control for CFO (`grc_cfo_v1`, zero offset) |
| `ul_cfo_500ms` | Python | +500 Hz carrier-frequency offset over [4.0, 4.5) s (250 whole cycles) |
| `grc_fixed_normal` | Python | Control for Python additive recipes (reference 1×10⁷) |
| `grc_ul_awgn_500ms` | Python | AWGN as `ul_awgn_500ms`, Python broker |
| `grc_ul_cw_500ms` | Python | CW as `ul_cw_500ms`, Python broker |
| `grc_tdl_a_normal` | Python | Static TDL-A control: 15-sample UL delay, channel disabled |
| `grc_ul_tdl_a_500ms` | Python | Static TR 38.901 TDL-A, 100 ns, over [4.0, 4.5) s |
| `grc_tdl_c_normal` | Python | Static TDL-C control: 15-sample UL delay, channel disabled |
| `grc_ul_tdl_c_500ms` | Python | Static TR 38.901 TDL-C, 300 ns, over [4.0, 4.5) s |
| `N0`, `B1`, `B5` | C | The paper's repeated-comparison arms: `normal`, `ul_blank_50ms` and `ul_blank_disperse_5x10ms` |

Compare a fault only with the control on the same broker: C and Python use
different random generators, and the TDL controls carry the same common delay
as their exposures. `B1` and `B5` program the same total of 1,152,000 blanked
samples (50 ms). The V3 arms differ from their recipes only in their schema
and protocol identity and in the 2 Mbit/s traffic metadata that the study's
testbed used. Specifications also carry `ul_bitrate`/`dl_bitrate` (5M or 2M)
and `settle_seconds` for the testbed's traffic generator; the brokers ignore
them.

The additive reference power 1×10⁷ is the rounded mean UL input power of a
no-fault capture in the study's OCUDU/srsUE setup; it is a digital
reference, not a calibrated receiver SNR ([FIXED_REFERENCE.md](FIXED_REFERENCE.md)).

```bash
.venv/bin/python scripts/radio_fault.py list
.venv/bin/python scripts/radio_fault.py show B5        # exact pulses in samples and seconds
```

## Lifecycle

The paper describes a finite **freeze, arm, apply, restore, qualify**
lifecycle. With a real gNB and UE ([INTEGRATION.md](INTEGRATION.md)):

```bash
# 1. Freeze: compile the recipe into a new private control directory.
.venv/bin/python scripts/radio_fault.py prepare B1 /tmp/rf/t1 \
    --gnb-config config/examples/gnb_zmq_broker.yml --ue-config config/examples/ue_zmq.conf

# 2. Start the broker on the standard ZMQ ports (before the gNB and UE).
.venv/bin/python scripts/radio_fault.py launch /tmp/rf/t1 --c-binary build/zmq_channel_broker

# 3. Start the core, gNB and UE; wait for attach and steady traffic.

# 4. Arm: STATUS, ARM, then STATUS until both directions complete (about 10 s).
.venv/bin/python scripts/radio_fault.py arm /tmp/rf/t1

# 5. Keep observing as long as the experiment needs, stop the gNB/UE, then the broker.
.venv/bin/python scripts/radio_fault.py stop /tmp/rf/t1

# 6. Qualify: reconcile the plan, control receipts and broker truth.
.venv/bin/python scripts/radio_fault.py verify /tmp/rf/t1 --write
```

`prepare` refuses an existing directory and a path whose `rb.sock` would
exceed Linux's 107-byte limit; keep trial directories short. `--gnb-config`
and `--ue-config` are optional; when given, both radios must use 23.04 Msps
and their SHA-256 values are recorded. Identities default to fresh UUIDs;
pass `--study-id`, `--trial-id` and `--pipeline-id` to bind a trial to your
own experiment records. For Python-broker recipes omit `--c-binary`; `launch`
uses the current interpreter, so run it from the environment that has the
dependencies.

You may start the broker yourself instead of using `launch`: `prepare` prints
the exact `argv` (append `--no-gui` for the Python broker). `arm` takes the
expected PID from `--pid`, else from `launch.json`, else from the broker's own
ready record. It takes the expected build identity from
`--expected-build-sha256`, else `--c-binary`, else `launch.json`, else (Python
broker) the current source composite, else the ready record.
`execution.json` records which source each value came from; prefer
independent values, because the ready record is the broker's self-report.

`stop` sends SIGTERM only after re-checking the recorded boot ID, start time
and executable of the launched process, waits for it to exit, and falls back
to SIGKILL only after the timeout. The broker writes its final truth and
accounting records during a clean SIGTERM shutdown, so stop it that way.

## Trial directory

| File | Written by | Content |
|---|---|---|
| `recipe.json`, `profile.json`, `schedule.json` | prepare | Canonical specification, profile and schedule |
| `plan.wire`, `control.token` | prepare | Compiled plan and 256-bit control token (mode 0600, never printed) |
| `preparation.json` | prepare | Identities, plan/profile hashes, exact broker argv, Python source identity, TDL realizations |
| `launch.json`, `broker.log` | launch | Process identity, command, expected build; broker stdout/stderr |
| `broker_ready.json`, `rb.sock` | broker | Ready identity and control socket |
| `broker_events.jsonl` | broker | Truth: arm, each condition applied/restored, forwarded progress, finals |
| `control.jsonl`, `execution.json` | arm | Every control receipt with monotonic brackets; the arm outcome |
| `stop.json` | stop | Signals sent and outcome |
| `verification.json` | verify `--write` | The reconciliation result |

Archive the directory without `control.token` and `rb.sock` if you share it.

## What `verify` establishes

`verify` recomputes the recipe's profile, schedule and plan from
`recipe.json` and the identities, and requires every archived file to match.
It then replays the control receipts (sequence, timing, monotone frontiers,
one ARM, completion within the wall bound) and checks each truth record:
trial and runtime identity, the arm epoch and the DSP state at arm, every
programmed transition at its exact sample with its exact settings, the
restoration, forwarded progress, and final records in which input, processed
and forwarded samples and messages are equal and complete. For Python-broker
recipes it also checks the fixed-profile records in `broker.log`, the CFO
phase and exposure count, or the TDL realization, history and exposure count.

A qualified result means **the broker applied and restored exactly the
programmed condition, by its own complete accounting**. It does not claim a
matched RAN observation window, service impact, calibrated SNR/SIR or channel
conformance; `whole_trial_qualified` and `matched_radio_windows_qualified`
stay false for that reason. Those belong to the testbed that observes the
RAN.

## Broker-only demo and IQ oracle

```bash
make demo                                                    # B1 through the C broker
.venv/bin/python scripts/radio_fault.py demo ul_cfo_500ms    # any recipe; Python broker
```

`demo` runs the complete lifecycle with synthetic gNB and UE endpoints on
private IPC sockets instead of a RAN, then verifies the trial. It also
performs an **independent IQ-level check** at the synthetic receivers: each
source sends known constant-envelope samples (mean power 1×10⁷), and each sink
compares every received sample with what was sent.

| Recipe family | Oracle requirement |
|---|---|
| gain, blank, attenuation | Samples differ exactly on the programmed pulses, with received/sent power equal to gain² exactly |
| AWGN, CW | Samples differ exactly on the pulse; added mean power within 5% of 1×10⁶ |
| static TDL | Against the input delayed by 15 samples, samples differ exactly on the TDL pulse |
| CFO | Differences only inside the pulse, power preserved, full coherence with a 500 Hz rotation starting at the pulse |
| controls, and the DL of every recipe | Every sample reproduced exactly |

All 16 recipes and the three V3 arms pass both `verify` and this oracle
through the real brokers. A demo takes about 10–40 s. `--frame-samples`
changes the message size (default 23,040 samples, one subframe); larger
messages force the brokers to split events inside messages.

## Custom schedules

The recipes are fixed on purpose. For a new condition, write a profile and a
schedule JSON (see the [fixtures](../config/radio_broker/) and
[SCHEDULES.md](SCHEDULES.md)), prepare it with
`scripts/radio_broker_schedule.py prepare`, and drive the broker with
`scripts/radio_broker_control.py`. The reconciliation functions
`radio_fault.validate_control` and `radio_fault.validate_truth` take any
compiled plan, so a custom harness can reuse them directly.
