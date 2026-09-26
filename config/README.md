# Configuration

| Path | Content |
|---|---|
| `examples/gnb_zmq_broker.yml` | OCUDU gNB configuration of the study, with its ZMQ radio on the broker ports 4000/4001 |
| `examples/ue_zmq.conf` | srsUE configuration of the study (direct ports 2000/2001; public test credentials) |
| `channel_broker/tr38901_tdl_v19.4.0.json` | TR 38.901 V19.4.0 TDL catalog; its SHA-256 is checked by the static TDL core |
| `radio_broker/fixed_reference.fixture.json` | Development fixed-reference profile with a synthetic unit reference power |
| `radio_broker/finite_schedule.fixture.json` | Development finite schedule with separate DL/UL events and restoration |
| `radio_broker/schedule_schema.json`, `broker_truth_schema.json`, `broker_metrics_schema.json` | Structural contracts of schedules, truth and metrics records |
| `radio_broker/*_validation_protocol.yaml`, `metrics_validation_fixture.json` | Prospective protocols and fixtures of the actual-process validators |

Everything except `examples/` is part of the paper's released files and must
stay byte-identical (see [PROVENANCE.md](../PROVENANCE.md)). The fixtures are
engineering examples, not calibrated operating points; the recipes in
`scripts/radio_fault.py` define the paper's conditions.
