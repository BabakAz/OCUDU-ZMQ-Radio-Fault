# Broker metrics contract

Optional per-window accounting for finite broker schedules. This contract was
frozen before the study's local process qualification. It defines engineering
accounting; it is not a cost benchmark or a radio measurement.

## Interface and bounds

Both backends accept `--radio-metrics-every-messages N` only with the existing
finite schedule/control interface and headless fixed-reference channel. Absent
means disabled; an explicit N must be a decimal integer in [1,1000000]. No new
timing reads or histograms occur when disabled. Receive materialization uses
the same implementation in enabled and disabled runs.

Enabled runs write private `broker_metrics.jsonl` through the existing checked
writer thread and shared queue (256 records). Truth remains capped at 4096
bytes/record and 1 MiB/file. Metrics are capped at 16384 bytes/record and 16
MiB/file, with at most 256 windows per direction including the final partial.
Exhausted budgets, queue loss, clock regression, integer overflow, nonfinite
values, malformed records, and write/close failures invalidate the run and
cause a nonzero exit. No overwrite or silently dropped window is allowed.

Metrics use the truth envelope with `schema_version=radio_broker_metrics_v1`,
an independent contiguous global `event_sequence` starting at 1, and an added
`metrics_config_sha256`: SHA256 of ASCII `radio_broker_metrics_v1\nN\n`, where N
is the canonical decimal interval. Event types are `started`, `window`, and
`final`; control scope is used for started, direction scope for windows/finals.
Envelope sample bounds cover processed samples. Direction, trial, pipeline,
instance, build, profile and plan identities must match checked truth. Python
build identity is the composite over ocudu_channel_broker.py,
radio_broker_metrics.py, radio_broker_profile.py, radio_broker_schedule.py,
radio_schedule_runtime.py and radio_static_tdl.py.

## Windows and populations

Each relay starts a window before its first request receive. A full window
closes after N successfully forwarded messages, including empty messages.
Windows are contiguous: next start equals previous end. Final partial closes
exactly once before socket teardown; it includes any active stopped/error
phase and may have zero output after an exactly full previous window. There
is no periodic heartbeat while an API call is blocked. Final records follow
checked relay/control cleanup and precede checked writer completion. Exit and
complete final capture are required in addition to record contents.

`started.details` contains `every_messages`, `max_windows=256`,
`histogram=uint64_bit_length`, `sample_rate_hz`, and `pid`.

`window.details` contains:

- `window_id` (one-based per direction), `final_partial` (boolean),
  `start_ns`, `end_ns`, `wall_ns=end_ns-start_ns` (strictly positive), and
  `loop_overhead_ns`.
- `input_messages`, `output_messages`, `input_samples`, `output_samples`,
  `input_bytes`, `output_bytes`: window deltas of validated received or
  successfully forwarded frames. Empty frames count as messages.
- `input_sample_start`, `input_sample_end`, `output_sample_start`,
  `output_sample_end`, `processed_sample_start`, `processed_sample_end`:
  cumulative frontiers; input/output deltas equal their window sample counts.
- `completed_processing_samples`: samples of messages whose entire processing
  phase completed, including messages subsequently failing to send. Partially
  processed failed messages remain in processed frontiers/energy, outside this
  denominator. Processing counts are messages; core counts below are segments.
- `intentional_mask_samples`: delta of the existing core masked-sample counter
  for desired gain zero; receiver noise/CW may remain. `rejected_messages`:
  complete observed IQ frames rejected for multipart, size, alignment or
  nonfinite input. Requests and system errors remain runtime errors; frames
  filtered before the application sees them are not fabricated observations.
- `energies`: finite window deltas named `input`, `desired`, `noise`, `cw`,
  `output`, covering exactly the processed sample interval, from the existing
  fixed-reference accumulator. These are sums of squared complex magnitudes,
  not calibrated receiver powers or complete RF interference measurements.
- `phases`: keys `request_receive`, `request_send`, `upstream_receive`,
  `processing`, `downstream_send`. Each maps outcomes `completed`, `stopped`,
  `error` to `[count,sum_ns,max_ns]`, including explicit zero triplets.
- `processing_parts`: `input_prepare`, `channel_chain`, `output_prepare`,
  each with the same outcome triplets. These partition processing: input
  materialization/validation/accounting; channel/schedule work; output
  serialization/validation/capacity checks. C output serialization itself
  involves no additional copy; output preparation still includes validation.
- `dsp_core`: the same outcome triplets, nested inside channel_chain, timing
  each actual fixed-reference core invocation (including its existing energy
  and state accounting). Do not call channel_chain exclusively DSP time.
  C skips its core for empty frames; Python invokes an empty core. These are
  zero versus one actual core calls, while both count one processing message.
- `message_samples_histogram` and `processing_ns_histogram`: sorted sparse
  `[bin,count]` arrays. Bin is uint64 bit_length: bin 0 is value 0, bin b>0
  covers [2^(b-1),2^b-1], up to bin 64. Message histogram covers validated
  received frames; processing histogram covers completed processing phases.

Ratios are derived by the independent validator, not serialized as redundant
broker fields. RTF = output_samples * 1e9 / (Fs * wall_ns), without clipping.
Processing-budget ratio = completed processing sum_ns * Fs /
(completed_processing_samples * 1e9); zero samples gives null with a reason.
Histogram quantiles are interval bounds, not exact percentiles.
Effective channel parameters and reference powers come from the identified
plan/profile and checked condition records joined by exact processed sample
interval. A window may span several settings and has no single coefficient
label. Retain component energies and their sample denominator when deriving
realized power/ratio; zero ratio denominators are undefined. These joins and
final logging/exit evidence are required along with individual metric rows.

`final.details` contains `window_count`, cumulative `input_messages`,
`output_messages`, `input_samples`, `output_samples`, `processed_samples`,
`completed_processing_samples`, `status` (`complete`, `incomplete`, `error`),
and `logging_errors`. Complete means clean metrics accounting; treatment
completion remains the separate truth schedule status. A clean stopped receive
with no valid IQ outstanding is complete, even before treatment completion.
A requested stop leaving a valid input/processing/output gap is incomplete;
an actual runtime, timing or logging failure is error. Do not label an
intentional stopped receive as an instrumentation error.

## Matched timing boundaries

For each exchange: t0 precedes downstream request receive; t1 follows request
materialization/release; t2 follows upstream request send; t3 is immediately
after complete upstream IQ receipt **before application copy/allocation**;
t4 follows processing and output validation, before downstream send; t5
follows downstream send. Five disjoint elapsed phases are successive
differences. Python uses recv(copy=False) and explicit materialization after
t3; C captures t3 inside its receive helper before copying. API elapsed
includes library execution and retries, not exclusively sleeping.

An active phase is closed with stopped/error outcome when interrupted. Error
means a local phase failure; a clean interruption caused by a sibling failure
is stopped for that phase while the global final status remains error. Nested
processing parts inherit their parent processing outcome when that phase
ends, including an earlier part that completed before a later part failed.
Part durations therefore partition processing exactly for each outcome; part
counts count entered parts. DSP core retains each invocation's actual outcome;
its total duration is nested in channel_chain and is not added again.
All phase sums plus loop_overhead_ns equal window
wall_ns. Accounting/enqueue work after a full-window end belongs to the next
window. No fabricated one-nanosecond denominator or negative-duration clamp.

## Whole-process resources

External resource observations bind boot ID, PID, start-time ticks, UID and
executable identity, checking identity before and after reads. CPU uses the
process CPU clock (all threads), with /proc stat ticks as a coarse cross-check;
never RUSAGE_CHILDREN or one thread's schedstat. Retain monotonic brackets
around observations and SC_CLK_TCK/page size. Reject missing endpoints or
counter regression. VmRSS and VmHWM are explicitly approximate; sampled RSS
maximum is not an exact peak, and VmHWM covers lifetime including startup.

Baseline follows warmup/ARM and quiescent peers; endpoint follows the last
measured reply and authenticated quiescent STATUS, before termination.
CPU cores = delta_cpu_ns/delta_wall_ns; CPU seconds/million samples uses the
exact measured DL+UL successfully forwarded sample delta. Zero samples gives
null with reason. Never allocate shared-process CPU separately to directions.
Broker progress windows and external resource intervals have distinct support.
Retain harness process CPU and introduced pacing separately.
Peer quiescence and STATUS do not prove the asynchronous writer has drained.
The resource interval excludes final-partial emission and any deferred writer
work after its endpoint, as well as startup/teardown. The development on/off
pair therefore reports observed in-interval CPU, not the total instrumentation
overhead of a completed trial. A later cost benchmark must choose and qualify
its writer-drain or steady-state amortization boundary explicitly.

## Acceptance

Deterministic clock fixtures must exercise first/final windows, empty frames,
RTF below/above one, unsent work, interrupted phases, nested conservation,
histogram boundaries and invalid data. Actual bounded processes verify exact
counts/capture/identity, timing conservation, final coverage, resources and
enabled/disabled output/state identity. On/off costs are descriptive single
development observations with no ranking or overhead acceptance threshold.
Run the actual-process checks with
`scripts/validate_radio_broker_metrics.py --run-local --backend both --output artifacts/NEW`.
