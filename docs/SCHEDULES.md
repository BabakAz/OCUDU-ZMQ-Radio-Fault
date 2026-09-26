# Finite sample schedules, control and broker truth

The C and headless Python brokers can preload a finite schedule on top of a
fixed-reference, CFO or static-TDL profile. Each direction has its own sample
clock and starts its schedule at the first nonempty message after an
authenticated arm request. Every applied condition, the restoration and the
final sample accounting are written to a bounded truth log. The fault
recipes in [FAULT_RECIPES.md](FAULT_RECIPES.md) are schedules of this kind.

A [profile](../config/radio_broker/fixed_reference.fixture.json) defines the
initial settings, reference power, sample rate and seed. A
[schedule](../config/radio_broker/finite_schedule.fixture.json) defines
complete settings at integer sample offsets and ends with a restoration to
the initial settings. Structural contracts are the
[schedule schema](../config/radio_broker/schedule_schema.json) and the
[truth schema](../config/radio_broker/broker_truth_schema.json); the
[compiler](../scripts/radio_broker_schedule.py) additionally checks profile
identity, numeric ranges, ordered boundaries, unique event IDs, immutable
fields and restoration, which JSON Schema alone cannot express.

## Sample semantics

There are 1–32 events per direction, strictly ordered within a duration of
1 through `2^63−1` samples. The mutable settings are desired amplitude gain,
noise enable/SNR and CW enable/SIR/frequency (plus CFO or TDL enable for those
semantics). Modes, reference powers, sample rate and seed cannot change. An
identity direction accepts only no-op events. Decimal durations convert to
samples only when the sample rate yields an exact integer; nothing is rounded
silently.

The arm epoch is the number of samples that direction had already processed.
DL and UL epochs may differ. An empty frame receives an empty reply and
advances message accounting, but cannot arm or apply an event. An event at a
message's end applies when the next nonempty message starts. A message that
crosses an event is split internally for DSP and receives one reply of the
original length. No sample or transport reply is ever deleted.

Coefficient changes preserve the random-generator and CW phase state; a
frequency change alters the phase increment but keeps the accumulated phase.
Each `armed` record captures the state at arm, including any warm-up, so
reproducing a trial needs the same backend and environment and the full
input history; the master seed alone does not reproduce warm-up.

The final event restores the initial settings. Reaching the scheduled
duration does not stop the relay: forwarding continues until the owning
process stops the broker. Completion requires both directions to forward
their scheduled durations, process all events, observe the restoration and
reconcile input, processed and forwarded counts without errors. A clean stop
before completion produces an explicitly incomplete truth record and can
still exit zero, so consumers must check the final records, not only the exit
code. Processing, control or logging failures exit nonzero.

## Private preparation

Supply both `--radio-plan-file PATH --radio-control-dir DIR`. The directory
must be a canonical absolute path owned by the effective user with mode
`0700`, and `DIR/rb.sock` must fit Linux's 107-byte socket path limit.
Preparation requires an empty directory and exclusively creates `plan.wire`
and `control.token` (mode `0600`). It never starts a broker.

```bash
.venv/bin/python scripts/radio_broker_schedule.py validate \
  --profile config/radio_broker/fixed_reference.fixture.json \
  --schedule config/radio_broker/finite_schedule.fixture.json
```

`prepare` takes the same arguments plus `--directory /short/private/dir` and
prints the complete argument arrays for each backend and the input/plan
hashes, never the token. `scripts/radio_fault.py prepare` does the same for a
named recipe and records a preparation file alongside
([FAULT_RECIPES.md](FAULT_RECIPES.md)). Broker `--validate-config-only`
validates these inputs without creating sockets, truth files or threads.

## Control protocol

At startup a fresh instance ID binds the plan, profile, backend and build to
`broker_ready.json`. Readiness means the control socket and both relay
sockets are bound; it says nothing about NR attach or traffic. `rb.sock` is a
local `SOCK_SEQPACKET` socket; the
[client](../scripts/radio_broker_control.py) checks the server's PID and UID
through peer credentials against the ready record and sends the private
token. Callers can also require an expected PID, backend and build hash.

There are only `STATUS` and `ARM`. Sequence numbers start at 1 and increase
by one. Retrying the identical last packet returns its cached reply without
arming twice; the client exposes an explicit retry and never arms a
replacement process on its own. A stale identity, wrong token, malformed or
oversized packet, sequence violation or a second new ARM is a fatal control
error. Requests and replies are bounded at 512 and 4096 bytes, with at most
4096 requests per instance. A fresh run needs a fresh directory; startup
never overwrites retained files.

```bash
.venv/bin/python scripts/radio_broker_control.py --directory DIR --operation STATUS --sequence 1
```

## Truth records

`broker_events.jsonl` is the authoritative exposure record. It separates the
request time, the observed arm, successfully processed condition segments,
successfully forwarded progress and the final reconciliation. Each record
carries study/protocol/trial/pipeline identity, instance/build/config/plan
hashes, direction, a contiguous event sequence, monotonic and wall clocks,
sample bounds and scope. A condition timestamp marks the start of processing
its first affected segment and the record is written after that segment
succeeds, so a condition record alone does not prove forwarding.

One checked writer drains a queue of at most 256 records; each record is at
most 4096 bytes and the file at most 1 MiB. Queue overflow and serialization,
write, close or finalization errors are failures. Progress records are
emitted only when a send advances past an event or first completes a
direction's schedule, which bounds logging independently of message count.
These bounds do not establish storage latency, crash durability or NR
deadline compliance.

`scripts/radio_fault.py verify` replays the control receipts against the plan
and checks every truth record: the exact programmed transitions and settings,
the restoration, complete and equal input/processed/forwarded counts, the
arm-time DSP state and, for the Python broker, its fixed-profile, CFO or TDL
state records.

## Validation

The [validation protocol](../config/radio_broker/schedule_validation_protocol.yaml)
declares the compiler/core checks and 17 actual-process cases per backend:
exact event boundaries, warm-up, partition and no-op continuity, identity,
incomplete stops and authenticated control failures. Run it with:

```bash
.venv/bin/python scripts/validate_radio_broker_schedule.py --run-local \
  --backend both --output artifacts/schedule-check
```

It builds the C broker from source with the locked compiler, starts only the
brokers and private IPC peers, and requires a new directory under
`artifacts/`.
