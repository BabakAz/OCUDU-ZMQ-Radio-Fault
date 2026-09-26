# Running a recipe with a gNB, UE and core

The brokers need nothing from the RAN beyond its ZMQ virtual radio. This page
describes how the study connected them; adapt paths and addresses to your
setup. Everything runs on one host over loopback.

## Components used in the study

| Component | Version in the study |
|---|---|
| gNB | [OCUDU](https://gitlab.com/ocudu/ocudu) commit `eb335616a67c4c145861515f25dbea6281d7e4a4`, built with ZMQ |
| UE | srsUE from [srsRAN 4G](https://github.com/srsran/srsRAN_4G) 23.04.0 (`ec29b0c1ff79cebcbe66caa6d6b90778261c42b8`), built with ZMQ |
| Core | [Open5GS](https://github.com/open5gs/open5gs) v2.8.0 |
| Host | Ubuntu 24.04 x86_64 VM, libzmq 4.3.5 |

The study's gNB also carried jBPF telemetry instrumentation and its srsUE two
small fixes for SA-mode switch-off and RRC release; neither affects the radio
path, and neither is needed by the brokers. Other srsRAN-family gNBs with the
same ZMQ driver should work, but only this combination was exercised.

## Configuration

- **gNB:** [`config/examples/gnb_zmq_broker.yml`](../config/examples/gnb_zmq_broker.yml),
  the study's cell (band 3, 20 MHz, 15 kHz SCS, ARFCN 368500, PLMN 99970,
  TAC 1, AMF at 127.0.0.5) with the radio pointed at the broker:
  `tx_port=tcp://127.0.0.1:4000,rx_port=tcp://127.0.0.1:4001,base_srate=23.04e6`
  and `srate: 23.04`. OCUDU's ZMQ driver accepts only non-positive gains; keep
  `tx_gain`/`rx_gain` at `0.0`.
- **UE:** [`config/examples/ue_zmq.conf`](../config/examples/ue_zmq.conf),
  unchanged from a direct setup (`tx_port=...:2001`, `rx_port=...:2000`,
  23.04 Msps). It places the UE's TUN device in network namespace `ue1`; create
  it with `sudo ip netns add ue1` before starting the UE.
- **Core:** subscribe the UE's IMSI/K/OPc (public srsRAN test values in the
  example) in Open5GS with the same PLMN and TAC.

The example gNB file keeps the study's deliberately tolerant radio-link
settings (`max_consecutive_kos: 10000`, longer `t310`/`t311`, larger
`n310`/`n311`) so that short injected faults do not immediately release the
software UE. Radio-link-failure incidence and timing measured with it are not
representative of default OCUDU behavior.

## Order of operations

```bash
# Once: build the C broker and the Python environment.
make broker && uv sync --frozen

# 1. Freeze the trial (checks both radio sample rates).
.venv/bin/python scripts/radio_fault.py prepare B1 /tmp/rf/t1 \
    --gnb-config config/examples/gnb_zmq_broker.yml --ue-config config/examples/ue_zmq.conf

# 2. Core first, then the broker, which binds 2000 (to the UE) and 4001 (to the gNB).
.venv/bin/python scripts/radio_fault.py launch /tmp/rf/t1 --c-binary build/zmq_channel_broker

# 3. gNB, then UE (both typically need root for real-time threads and the TUN device).
sudo ./gnb -c config/examples/gnb_zmq_broker.yml
sudo ./srsue config/examples/ue_zmq.conf

# 4. Wait for the UE to attach and for your traffic to reach steady state
#    (the study waited for iperf3 and a probe flow), then arm.
.venv/bin/python scripts/radio_fault.py arm /tmp/rf/t1

# 5. Keep observing through recovery, then stop traffic, UE and gNB, then the broker.
.venv/bin/python scripts/radio_fault.py stop /tmp/rf/t1
.venv/bin/python scripts/radio_fault.py verify /tmp/rf/t1 --write
```

`arm` returns once both directions have completed their 10 s schedules; the
broker keeps forwarding unchanged samples afterwards. The truth records give
the exact programmed exposure in each direction's sample coordinates, and
`control.jsonl` brackets the ARM request in host monotonic time. Sample
positions are not NR slot numbers, and host time is not radio time; the
paper treats these as separate clocks and so should your analysis.

Stop the broker last. If it stops while the gNB or UE still exchange
samples, the final records honestly report the in-flight message, but a
clean stop keeps the accounting simplest. Use a fresh trial directory for
every run.

## Python-broker recipes

CFO and static TDL recipes (and the Python variants of the additive recipes)
run the headless Python broker with the same ports:

```bash
.venv/bin/python scripts/radio_fault.py prepare grc_ul_tdl_c_500ms /tmp/rf/t2
.venv/bin/python scripts/radio_fault.py launch /tmp/rf/t2
```

Load matters more for the Python broker. In the study, a Python-broker
fixed-reference control at 5 Mbit/s per direction lost 36–42% of its UL
datagrams without any fault, so the Python additive and static TDL runs used
2 Mbit/s per direction. Every exchange waits for the broker's processing, so
check on your host that controls are clean at your traffic level before
comparing a fault with them.

## Brokerless control

For a no-broker baseline, point the gNB back at `tx_port=...:2000,rx_port=...:2001`.
The paper's repeated comparison instead used a C broker running the `N0`
recipe as its control, so that controls and faults share the same relay path.
