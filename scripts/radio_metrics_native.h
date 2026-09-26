/* RAD-08 bounded native timing accumulator. All operations accept explicit
 * timestamps for deterministic L1 tests; only rm_now() reads the real clock.
 * Elapsed API time is not a CPU measurement. DSP populations are nested. */
#ifndef RADIO_METRICS_NATIVE_H
#define RADIO_METRICS_NATIVE_H
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#define RM_RECORD_MAX 16384U
#define RM_FILE_MAX (16U * 1024U * 1024U)
#define RM_WINDOWS_MAX 256U
#define RM_BINS 65U

enum { RM_REQUEST_RECEIVE, RM_REQUEST_SEND, RM_UPSTREAM_RECEIVE,
       RM_PROCESSING, RM_DOWNSTREAM_SEND, RM_PHASES };
enum { RM_INPUT_PREPARE, RM_CHANNEL_CHAIN, RM_OUTPUT_PREPARE, RM_PARTS };
enum { RM_COMPLETED, RM_STOPPED, RM_ERROR, RM_OUTCOMES };
typedef struct { uint64_t count, sum, maximum; } rm_stat_t;
typedef struct {
    uint64_t input_messages, output_messages, input_samples, output_samples, processed_samples;
    uint64_t masked_samples, rejected_messages;
    double energies[5];
} rm_frontier_t;
typedef int (*rm_emit_fn)(void *, size_t, const char *, uint64_t, uint64_t, const char *);
typedef struct {
    uint64_t every, window_count, start_ns, last_ns, completed_samples, completed_start;
    int started, finished, window_closed, failed, phase, part, core_active;
    uint64_t phase_start, part_start, core_start;
    uint64_t part_durations[RM_PARTS]; int part_seen[RM_PARTS];
    rm_frontier_t baseline, current;
    rm_stat_t phases[RM_PHASES][RM_OUTCOMES], parts[RM_PARTS][RM_OUTCOMES], core[RM_OUTCOMES];
    uint64_t message_hist[RM_BINS], processing_hist[RM_BINS];
    rm_emit_fn emit; void *context; size_t direction;
} rm_metrics_t;

static int rm_add(uint64_t *value, uint64_t increment) {
    if (increment > UINT64_MAX - *value) return -1;
    *value += increment;
    return 0;
}
static unsigned rm_bin(uint64_t value) {
    unsigned bin = 0;
    while (value) { value >>= 1; bin++; }
    return bin;
}
static int rm_stat_add(rm_stat_t *s, uint64_t elapsed) {
    if (s->count == UINT64_MAX || elapsed > UINT64_MAX - s->sum) return -1;
    s->count++; s->sum += elapsed;
    if (elapsed > s->maximum) s->maximum = elapsed;
    return 0;
}
static void rm_init(rm_metrics_t *m, uint64_t every, rm_emit_fn emit, void *context, size_t direction) {
    *m = (rm_metrics_t){.every=every, .phase=-1, .part=-1, .emit=emit,
                        .context=context, .direction=direction};
}
static int rm_invalid(rm_metrics_t *m) { m->failed = 1; return -1; }
static int rm_at(rm_metrics_t *m, uint64_t now) {
    if (m->failed || m->finished || m->window_closed || (m->started && now < m->last_ns)) return rm_invalid(m);
    m->last_ns = now;
    return 0;
}
static int rm_timespec_ns(const struct timespec *t,uint64_t *value) {
    if(t->tv_sec<0||t->tv_nsec<0||t->tv_nsec>=1000000000L||
       (uint64_t)t->tv_sec>(UINT64_MAX-(uint64_t)t->tv_nsec)/UINT64_C(1000000000))return -1;
    *value=(uint64_t)t->tv_sec*UINT64_C(1000000000)+(uint64_t)t->tv_nsec;return 0;
}
static int rm_now(rm_metrics_t *m, uint64_t *now) {
    struct timespec t;
    if(clock_gettime(CLOCK_MONOTONIC,&t)||rm_timespec_ns(&t,now))return rm_invalid(m);
    return rm_at(m,*now);
}
static int rm_begin(rm_metrics_t *m, uint64_t now, const rm_frontier_t *frontier) {
    if (!m->every || m->every > 1000000 || m->started || rm_at(m, now)) return rm_invalid(m);
    m->started = 1; m->start_ns = now; m->baseline = m->current = *frontier;
    return 0;
}
static int rm_phase_begin(rm_metrics_t *m, int phase, uint64_t now) {
    if (!m->started || phase < 0 || phase >= RM_PHASES || m->phase != -1 || rm_at(m, now)) return rm_invalid(m);
    m->phase = phase; m->phase_start = now;
    if (phase == RM_PROCESSING) {
        memset(m->part_durations, 0, sizeof(m->part_durations));
        memset(m->part_seen, 0, sizeof(m->part_seen));
        m->part = RM_INPUT_PREPARE; m->part_start = now; m->part_seen[m->part] = 1;
    }
    return 0;
}
static int rm_part_next(rm_metrics_t *m, int part, uint64_t now) {
    if (m->phase != RM_PROCESSING || part != m->part + 1 || part >= RM_PARTS || m->core_active || rm_at(m, now)) return rm_invalid(m);
    m->part_durations[m->part] = now - m->part_start;
    m->part = part; m->part_start = now; m->part_seen[part] = 1;
    return 0;
}
static int rm_core_begin(rm_metrics_t *m, uint64_t now) {
    if (m->phase != RM_PROCESSING || m->part != RM_CHANNEL_CHAIN || m->core_active || rm_at(m, now)) return rm_invalid(m);
    m->core_active = 1; m->core_start = now;
    return 0;
}
static int rm_core_end(rm_metrics_t *m, uint64_t now, int outcome) {
    if (!m->core_active || outcome < 0 || outcome >= RM_OUTCOMES || rm_at(m, now) ||
        rm_stat_add(&m->core[outcome], now - m->core_start)) return rm_invalid(m);
    m->core_active = 0;
    return 0;
}
static int rm_phase_end(rm_metrics_t *m, uint64_t now, int outcome, uint64_t completed_samples) {
    if (m->phase < 0 || outcome < 0 || outcome >= RM_OUTCOMES || m->core_active || rm_at(m, now)) return rm_invalid(m);
    uint64_t elapsed = now - m->phase_start;
    if (rm_stat_add(&m->phases[m->phase][outcome], elapsed)) return rm_invalid(m);
    if (m->phase == RM_PROCESSING) {
        if (m->part < 0 || (outcome == RM_COMPLETED && m->part != RM_OUTPUT_PREPARE)) return rm_invalid(m);
        m->part_durations[m->part] = now - m->part_start;
        for (int i=0; i<RM_PARTS; i++) {
            if (m->part_seen[i] && rm_stat_add(&m->parts[i][outcome], m->part_durations[i])) return rm_invalid(m);
        }
        if (outcome == RM_COMPLETED && (rm_add(&m->completed_samples, completed_samples) ||
            rm_add(&m->processing_hist[rm_bin(elapsed)], 1))) return rm_invalid(m);
        m->part = -1;
    }
    m->phase = -1;
    return 0;
}
static int rm_transition(rm_metrics_t *m, int next, uint64_t now) {
    return rm_phase_end(m, now, RM_COMPLETED, 0) || rm_phase_begin(m, next, now) ? -1 : 0;
}
static int rm_input(rm_metrics_t *m, uint64_t samples) {
    if (m->window_closed || m->phase != RM_PROCESSING || m->part != RM_INPUT_PREPARE || rm_add(&m->message_hist[rm_bin(samples)], 1)) return rm_invalid(m);
    return 0;
}
/* Bounded string builder; no partial or truncated JSON reaches the writer. */
typedef struct { char text[RM_RECORD_MAX]; size_t used; int error; } rm_json_t;
static void rm_json(rm_json_t *j, const char *format, ...) {
    if (j->error) return;
    va_list ap; va_start(ap, format);
    int n = vsnprintf(j->text + j->used, sizeof(j->text) - j->used, format, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= sizeof(j->text) - j->used) j->error = 1;
    else j->used += (size_t)n;
}
static void rm_population_json(rm_json_t *j, const rm_stat_t population[RM_OUTCOMES]) {
    static const char *names[] = {"completed", "stopped", "error"};
    rm_json(j, "{");
    for (size_t i=0; i<RM_OUTCOMES; i++) {
        rm_json(j, "%s\"%s\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 "]", i ? "," : "", names[i],
                population[i].count, population[i].sum, population[i].maximum);
    }
    rm_json(j, "}");
}
static void rm_hist_json(rm_json_t *j, const uint64_t bins[RM_BINS]) {
    int first = 1; rm_json(j, "[");
    for (unsigned i=0; i<RM_BINS; i++) if (bins[i]) {
        rm_json(j, "%s[%u,%" PRIu64 "]", first ? "" : ",", i, bins[i]); first = 0;
    }
    rm_json(j, "]");
}
static int rm_flush(rm_metrics_t *m, uint64_t now, int final_partial, const rm_frontier_t *f) {
    if (!m->started || m->phase != -1 || m->core_active || m->window_count >= RM_WINDOWS_MAX || rm_at(m, now) || now <= m->start_ns) return rm_invalid(m);
    const rm_frontier_t *b = &m->baseline;
    if (f->input_messages < b->input_messages || f->output_messages < b->output_messages ||
        f->input_samples < b->input_samples || f->output_samples < b->output_samples ||
        f->processed_samples < b->processed_samples || f->masked_samples < b->masked_samples || f->rejected_messages < b->rejected_messages || m->completed_samples < m->completed_start) return rm_invalid(m);
    uint64_t im=f->input_messages-b->input_messages, om=f->output_messages-b->output_messages;
    uint64_t in=f->input_samples-b->input_samples, on=f->output_samples-b->output_samples;
    if (in>UINT64_MAX/8 || on>UINT64_MAX/8 || (!final_partial && om!=m->every)) return rm_invalid(m);
    uint64_t phase_sum=0, messages=0, processing=0, core_sum=0, chain_sum=0;
    for (size_t p=0; p<RM_PHASES; p++) for (size_t o=0; o<RM_OUTCOMES; o++) {
        if (rm_add(&phase_sum,m->phases[p][o].sum)) return rm_invalid(m);
    }
    for (size_t o=0; o<RM_OUTCOMES; o++) {
        uint64_t parts=0;
        for (size_t p=0; p<RM_PARTS; p++) if (rm_add(&parts,m->parts[p][o].sum)) return rm_invalid(m);
        if (parts!=m->phases[RM_PROCESSING][o].sum || rm_add(&core_sum,m->core[o].sum) || rm_add(&chain_sum,m->parts[RM_CHANNEL_CHAIN][o].sum)) return rm_invalid(m);
    }
    for (size_t i=0; i<RM_BINS; i++) {
        if (rm_add(&messages,m->message_hist[i]) || rm_add(&processing,m->processing_hist[i])) return rm_invalid(m);
    }
    uint64_t wall=now-m->start_ns;
    if (phase_sum>wall || core_sum>chain_sum || messages!=im || processing!=m->phases[RM_PROCESSING][RM_COMPLETED].count) return rm_invalid(m);
    double energy[5];
    for (size_t i=0; i<5; i++) {
        energy[i]=f->energies[i]-b->energies[i];
        if (!isfinite(f->energies[i]) || !isfinite(b->energies[i]) || !isfinite(energy[i]) || energy[i]<0) return rm_invalid(m);
    }
    rm_json_t j={0};
    rm_json(&j,"{\"window_id\":%" PRIu64 ",\"final_partial\":%s,\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ",\"wall_ns\":%" PRIu64 ",\"loop_overhead_ns\":%" PRIu64 ",",
            m->window_count+1,final_partial?"true":"false",m->start_ns,now,wall,wall-phase_sum);
    rm_json(&j,"\"input_messages\":%" PRIu64 ",\"output_messages\":%" PRIu64 ",\"input_samples\":%" PRIu64 ",\"output_samples\":%" PRIu64 ",\"input_bytes\":%" PRIu64 ",\"output_bytes\":%" PRIu64 ",",im,om,in,on,in*8,on*8);
    rm_json(&j,"\"input_sample_start\":%" PRIu64 ",\"input_sample_end\":%" PRIu64 ",\"output_sample_start\":%" PRIu64 ",\"output_sample_end\":%" PRIu64 ",\"processed_sample_start\":%" PRIu64 ",\"processed_sample_end\":%" PRIu64 ",\"completed_processing_samples\":%" PRIu64 ",",
            b->input_samples,f->input_samples,b->output_samples,f->output_samples,b->processed_samples,f->processed_samples,m->completed_samples-m->completed_start);
    rm_json(&j,"\"intentional_mask_samples\":%" PRIu64 ",\"rejected_messages\":%" PRIu64 ",",f->masked_samples-b->masked_samples,f->rejected_messages-b->rejected_messages);
    rm_json(&j,"\"energies\":{\"input\":%.17g,\"desired\":%.17g,\"noise\":%.17g,\"cw\":%.17g,\"output\":%.17g},\"phases\":{",energy[0],energy[1],energy[2],energy[3],energy[4]);
    static const char *phase_names[]={"request_receive","request_send","upstream_receive","processing","downstream_send"};
    for(size_t i=0;i<RM_PHASES;i++){rm_json(&j,"%s\"%s\":",i?",":"",phase_names[i]);rm_population_json(&j,m->phases[i]);}
    rm_json(&j,"},\"processing_parts\":{");
    static const char *part_names[]={"input_prepare","channel_chain","output_prepare"};
    for(size_t i=0;i<RM_PARTS;i++){rm_json(&j,"%s\"%s\":",i?",":"",part_names[i]);rm_population_json(&j,m->parts[i]);}
    rm_json(&j,"},\"dsp_core\":");rm_population_json(&j,m->core);
    rm_json(&j,",\"message_samples_histogram\":");rm_hist_json(&j,m->message_hist);
    rm_json(&j,",\"processing_ns_histogram\":");rm_hist_json(&j,m->processing_hist);rm_json(&j,"}");
    if (j.error || !m->emit || m->emit(m->context,m->direction,"window",b->processed_samples,f->processed_samples,j.text)) return rm_invalid(m);
    m->window_count++;m->start_ns=now;m->baseline=m->current=*f;m->completed_start=m->completed_samples;
    memset(m->phases,0,sizeof(m->phases));memset(m->parts,0,sizeof(m->parts));memset(m->core,0,sizeof(m->core));
    memset(m->message_hist,0,sizeof(m->message_hist));memset(m->processing_hist,0,sizeof(m->processing_hist));
    return 0;
}
static int rm_maybe_flush(rm_metrics_t *m,uint64_t now,const rm_frontier_t *f) {
    if(m->window_closed || m->failed || m->finished)return rm_invalid(m);
    m->current=*f;
    if(f->output_messages<m->baseline.output_messages || f->output_messages-m->baseline.output_messages>m->every)return rm_invalid(m);
    return f->output_messages-m->baseline.output_messages==m->every?rm_flush(m,now,0,f):0;
}
static int rm_close_window(rm_metrics_t *m,uint64_t now,int outcome,const rm_frontier_t *f) {
    if(m->window_closed)return 0;
    m->current=*f;
    if(!m->started){m->window_closed=1;return 0;}
    if(m->failed)return -1;
    if(m->phase!=-1 && rm_phase_end(m,now,outcome,0))return -1;
    int status=rm_flush(m,now,1,f);if(!status)m->window_closed=1;return status;
}
static int rm_final(rm_metrics_t *m,const rm_frontier_t *f,int runtime_error,uint64_t logging_errors) {
    if(m->finished)return rm_invalid(m);m->finished=1;m->current=*f;
    const char *status=(runtime_error||m->failed||logging_errors)?"error":
        (!m->started||!m->window_closed||m->completed_samples!=f->processed_samples||f->input_messages!=f->output_messages||f->input_samples!=f->output_samples||f->processed_samples!=f->output_samples)?"incomplete":"complete";
    rm_json_t j={0};
    rm_json(&j,"{\"window_count\":%" PRIu64 ",\"input_messages\":%" PRIu64 ",\"output_messages\":%" PRIu64 ",\"input_samples\":%" PRIu64 ",\"output_samples\":%" PRIu64 ",\"processed_samples\":%" PRIu64 ",\"completed_processing_samples\":%" PRIu64 ",\"status\":\"%s\",\"logging_errors\":%" PRIu64 "}",
            m->window_count,f->input_messages,f->output_messages,f->input_samples,f->output_samples,f->processed_samples,m->completed_samples,status,logging_errors);
    return j.error||!m->emit||m->emit(m->context,m->direction,"final",0,f->processed_samples,j.text)?rm_invalid(m):0;
}
#endif
