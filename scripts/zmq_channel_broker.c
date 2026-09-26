/*
 * ZMQ Channel Broker — RF Channel Impairment Simulator for OCUDU ZMQ Radio
 *
 * Sits between gNB and UE ZMQ sockets, forwarding IQ samples while
 * applying channel impairments:
 *   - AWGN (Additive White Gaussian Noise) — legacy message-local mode
 *   - Rician/Rayleigh flat fading — optional, enabled with --fading/--rayleigh
 *   - Explicit --identity mode — byte-exact relay of finite cf32 inputs
 *
 * Port topology:
 *   gNB TX (REP bind :4000) → Broker DL (REQ→4000, impair, REP bind :2000) → UE RX (REQ→2000)
 *   UE  TX (REP bind :2001) → Broker UL (REQ→2001, impair, REP bind :4001) → gNB RX (REQ→4001)
 *
 * Build:  clang-18 -std=c17 -O2 -Wall -Wextra -Wpedantic -Werror \
 *           -o zmq_channel_broker zmq_channel_broker.c -lzmq -lm -pthread
 * Usage:  ./zmq_channel_broker [--snr <dB>] [--fading] [--doppler <Hz>] [--seed <uint32>]
 */

#define _GNU_SOURCE
#include <errno.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <inttypes.h>
#include <sys/un.h>
#include <time.h>
#include <zmq.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define INITIAL_BUF_SIZE (4U * 1024U * 1024U)
#define MAX_MESSAGE_SIZE (64U * 1024U * 1024U)
#define DEFAULT_SRATE   23.04e6f          /* OCUDU ZMQ sample rate */
#define MIN_DB          (-100.0f)
#define MAX_DB          100.0f
#define MAX_DOPPLER_HZ  5000.0f
#define MIN_SRATE_HZ    1000.0f
#define MAX_SRATE_HZ    250.0e6f
#define RADIO_BROKER_ACCOUNTING_SCHEMA "radio_broker_accounting_v1"
#define FIXED_SEMANTICS "fixed_reference_v1"
#define LEGACY_SEMANTICS "legacy_message_local_v1"

static volatile sig_atomic_t stop_requested = 0;
static atomic_int fatal_error;

static void sig_handler(int sig) {
    (void)sig;
    stop_requested = 1;
}

static int broker_should_run(void) {
    return stop_requested == 0 &&
           atomic_load_explicit(&fatal_error, memory_order_relaxed) == 0;
}

static int parse_bounded_float(const char *option,
                               const char *text,
                               float minimum,
                               float maximum,
                               float *result) {
    char *end = NULL;
    errno = 0;
    float value = strtof(text, &end);
    if (text == end || end == NULL || *end != '\0' || errno == ERANGE || !isfinite(value)) {
        fprintf(stderr, "ERROR: %s requires a finite number, got '%s'\n", option, text);
        return -1;
    }
    if (value < minimum || value > maximum) {
        fprintf(stderr, "ERROR: %s must be in [%g, %g], got %g\n",
                option, (double)minimum, (double)maximum, (double)value);
        return -1;
    }
    *result = value;
    return 0;
}

static int read_bounded_float(int argc,
                              char *argv[],
                              int *index,
                              float minimum,
                              float maximum,
                              float *result) {
    const char *option = argv[*index];
    if (*index + 1 >= argc) {
        fprintf(stderr, "ERROR: %s requires a value\n", option);
        return -1;
    }
    *index += 1;
    return parse_bounded_float(option, argv[*index], minimum, maximum, result);
}

static int read_uint32(int argc,
                       char *argv[],
                       int *index,
                       uint32_t *result) {
    const char *option = argv[*index];
    char *end = NULL;
    unsigned long value;
    if (*index + 1 >= argc) {
        fprintf(stderr, "ERROR: %s requires a value\n", option);
        return -1;
    }
    *index += 1;
    if (argv[*index][0] == '\0' || argv[*index][0] == '-' || argv[*index][0] == '+') {
        fprintf(stderr, "ERROR: %s requires an unsigned 32-bit decimal integer\n", option);
        return -1;
    }
    errno = 0;
    value = strtoul(argv[*index], &end, 10);
    if (errno == ERANGE || end == argv[*index] || end == NULL || *end != '\0' ||
        value > UINT32_MAX) {
        fprintf(stderr, "ERROR: %s requires an unsigned 32-bit decimal integer\n", option);
        return -1;
    }
    *result = (uint32_t)value;
    return 0;
}

static int read_bounded_double(int argc, char *argv[], int *index,
                                double minimum, double maximum, double *result) {
    const char *option = argv[*index];
    if (*index + 1 >= argc) {
        fprintf(stderr, "ERROR: %s requires a value\n", option);
        return -1;
    }
    const char *text = argv[++*index];
    char *end = NULL;
    errno = 0;
    double value = strtod(text, &end);
    if (text == end || *end != '\0' || errno == ERANGE || !isfinite(value) ||
        value < minimum || value > maximum) {
        fprintf(stderr, "ERROR: %s requires a finite number in [%.17g, %.17g]\n",
                option, minimum, maximum);
        return -1;
    }
    *result = value;
    return 0;
}

/* Syntax-only local endpoints. IPC path ownership and private-directory
 * lifecycle belong to the launcher/harness; this parser opens no files. */
static int validate_local_endpoint(const char *option, const char *endpoint) {
    static const char tcp_prefix[] = "tcp://127.0.0.1:";
    if (strncmp(endpoint, tcp_prefix, sizeof(tcp_prefix) - 1) == 0) {
        const char *digits = endpoint + sizeof(tcp_prefix) - 1;
        unsigned int port = 0;
        if (digits[0] < '1' || digits[0] > '9') goto invalid;
        for (const char *p = digits; *p != '\0'; p++) {
            if (*p < '0' || *p > '9') goto invalid;
            port = port * 10U + (unsigned int)(*p - '0');
            if (port > 65535U) goto invalid;
        }
        return 0;
    }
    if (strncmp(endpoint, "ipc://", 6) == 0) {
        const char *path = endpoint + 6;
        size_t length = strlen(path);
        if (length < 2 || path[0] != '/' || path[length - 1] == '/' ||
            length >= sizeof(((struct sockaddr_un *)0)->sun_path)) goto invalid;
        const char *component = path + 1;
        for (const char *p = component; ; p++) {
            unsigned char ch = (unsigned char)*p;
            if (ch != '\0' && (ch < 32 || ch == 127 || ch == '*')) goto invalid;
            if (ch == '/' || ch == '\0') {
                size_t component_length = (size_t)(p - component);
                if (component_length == 0 ||
                    (component_length == 1 && component[0] == '.') ||
                    (component_length == 2 && component[0] == '.' && component[1] == '.')) {
                    goto invalid;
                }
                if (ch == '\0') break;
                component = p + 1;
            }
        }
        return 0;
    }
invalid:
    fprintf(stderr,
            "ERROR: %s requires tcp://127.0.0.1:<port 1..65535 without leading zeros> "
            "or ipc://<absolute pathname shorter than %zu bytes without empty/dot components, "
            "wildcards, or controls>\n", option,
            sizeof(((struct sockaddr_un *)0)->sun_path));
    return -1;
}

static int read_local_endpoint(int argc,
                                char *argv[],
                                int *index,
                                const char **result) {
    const char *option = argv[*index];
    if (*result != NULL) {
        fprintf(stderr, "ERROR: duplicate endpoint option %s\n", option);
        return -1;
    }
    if (*index + 1 >= argc) {
        fprintf(stderr, "ERROR: %s requires a value\n", option);
        return -1;
    }
    *index += 1;
    if (validate_local_endpoint(option, argv[*index]) != 0) return -1;
    *result = argv[*index];
    return 0;
}

static int validate_distinct_endpoints(const char *const endpoints[4]) {
    for (size_t i = 0; i < 4; i++) {
        for (size_t j = i + 1; j < 4; j++) {
            if (strcmp(endpoints[i], endpoints[j]) == 0) {
                fprintf(stderr, "ERROR: all four relay endpoints must be distinct\n");
                return -1;
            }
        }
    }
    return 0;
}

/* ── Random Number Generation ─────────────────────────────────────────────── */

/* Box-Muller: generate two independent N(0,1) samples (for I and Q) */
static inline void randn_pair(unsigned int *seed, float *n1, float *n2) {
    float u1 = ((float)rand_r(seed) + 1.0f) / ((float)RAND_MAX + 1.0f);
    float u2 = ((float)rand_r(seed) + 1.0f) / ((float)RAND_MAX + 1.0f);
    float r  = sqrtf(-2.0f * logf(u1));
    *n1 = r * cosf(2.0f * (float)M_PI * u2);
    *n2 = r * sinf(2.0f * (float)M_PI * u2);
}

typedef struct {
    int identity, noise_off, cw_enabled;
    unsigned int seen;
    double ref_power, gain, noise_snr_db, cw_sir_db, cw_freq_hz, sample_rate_hz;
} fixed_config_t;

#include "radio_metrics_native.h"

typedef struct {
    rm_metrics_t *metrics;
    fixed_config_t config;
    uint32_t master_seed, awgn_seed;
    unsigned int awgn_state;
    uint64_t sample_clock, awgn_complex_draws, awgn_normal_draws;
    uint64_t phase_u64, cw_step_u64, masked_samples, attenuated_samples;
    double noise_std, cw_amplitude;
    double input_energy, desired_energy, noise_energy, cw_energy, output_energy;
} fixed_state_t;

static uint32_t fixed_component_seed(uint32_t master, uint32_t direction,
                                      uint32_t component) {
    uint32_t value = master ^ direction ^ component;
    value ^= value >> 16;
    value *= UINT32_C(0x7feb352d);
    value ^= value >> 15;
    value *= UINT32_C(0x846ca68b);
    return value ^ (value >> 16);
}

static fixed_config_t fixed_config_defaults(void) {
    return (fixed_config_t){.gain = 1.0, .noise_snr_db = 28.0,
                            .cw_sir_db = 20.0, .sample_rate_hz = DEFAULT_SRATE};
}

static uint64_t cw_phase_step(double frequency, double sample_rate) {
    double scaled = ldexp(fabs(frequency) / sample_rate, 64);
    uint64_t magnitude = (uint64_t)floor(scaled + 0.5);
    return frequency < 0 ? UINT64_C(0) - magnitude : magnitude;
}

static int fixed_config_valid(const fixed_config_t *c) {
    return isfinite(c->ref_power) && c->ref_power >= 1e-20 && c->ref_power <= 1e10 &&
           isfinite(c->gain) && c->gain >= 0 && c->gain <= 1 &&
           isfinite(c->noise_snr_db) && fabs(c->noise_snr_db) <= 100 &&
           isfinite(c->cw_sir_db) && fabs(c->cw_sir_db) <= 100 &&
           isfinite(c->sample_rate_hz) && c->sample_rate_hz >= MIN_SRATE_HZ &&
           c->sample_rate_hz <= MAX_SRATE_HZ && isfinite(c->cw_freq_hz) &&
           fabs(c->cw_freq_hz) <= c->sample_rate_hz / 2;
}

static int fixed_init(fixed_state_t *state, const fixed_config_t *config,
                       uint32_t master, uint32_t direction) {
    if (!fixed_config_valid(config)) return -1;
    *state = (fixed_state_t){.config = *config, .master_seed = master};
    state->awgn_seed = fixed_component_seed(master, direction, UINT32_C(0x4157474e));
    state->awgn_state = state->awgn_seed;
    state->noise_std = sqrt(config->ref_power * pow(10.0, -config->noise_snr_db / 10.0) / 2);
    state->cw_amplitude = sqrt(config->ref_power * pow(10.0, -config->cw_sir_db / 10.0));
    /* Versioned binary64 quantization; Python uses the identical operations.
     * Nyquist is 2^63 and safely representable as uint64. */
    state->cw_step_u64 = cw_phase_step(config->cw_freq_hz, config->sample_rate_hz);
    return 0;
}

/* Production fixed-profile DSP, also called by bounded file-only L1 fixtures.
 * Counters and energies cover processed samples, not successful socket sends.
 * Component values round to cf32 before a binary64 sum and final cf32 cast. */
static int fixed_process_core(fixed_state_t *state, float *samples, size_t count) {
    const fixed_config_t *c = &state->config;
    if (count > MAX_MESSAGE_SIZE / 8 || count > UINT64_MAX - state->sample_clock ||
        (!c->identity && (count > UINT64_MAX - state->awgn_complex_draws ||
                         count > (UINT64_MAX - state->awgn_normal_draws) / 2 ||
                         count > UINT64_MAX - state->masked_samples ||
                         count > UINT64_MAX - state->attenuated_samples))) return -1;
    for (size_t i = 0; i < count; i++) {
        if (!isfinite(samples[2 * i]) || !isfinite(samples[2 * i + 1])) return -1;
    }
    for (size_t i = 0; i < count; i++) {
        double xi = samples[2 * i], xq = samples[2 * i + 1];
        state->input_energy += xi * xi + xq * xq;
        if (c->identity) {
            state->desired_energy += xi * xi + xq * xq;
            state->output_energy += xi * xi + xq * xq;
            continue;
        }
        float di = (float)(c->gain * xi), dq = (float)(c->gain * xq);
        float ni = 0, nq = 0, ci = 0, cq = 0, n1, n2;
        randn_pair(&state->awgn_state, &n1, &n2);
        if (!c->noise_off) {
            ni = (float)(state->noise_std * (double)n1);
            nq = (float)(state->noise_std * (double)n2);
        }
        if (c->cw_enabled) {
            double phase = ldexp((double)state->phase_u64, -64) * (2.0 * M_PI);
            ci = (float)(state->cw_amplitude * cos(phase));
            cq = (float)(state->cw_amplitude * sin(phase));
        }
        state->phase_u64 += state->cw_step_u64;
        float yi = (float)((double)di + (double)ci + (double)ni);
        float yq = (float)((double)dq + (double)cq + (double)nq);
        samples[2 * i] = yi;
        samples[2 * i + 1] = yq;
        state->desired_energy += (double)di * di + (double)dq * dq;
        state->noise_energy += (double)ni * ni + (double)nq * nq;
        state->cw_energy += (double)ci * ci + (double)cq * cq;
        state->output_energy += (double)yi * yi + (double)yq * yq;
    }
    state->sample_clock += (uint64_t)count;
    if (!c->identity) {
        state->awgn_complex_draws += (uint64_t)count;
        state->awgn_normal_draws += (uint64_t)count * 2;
        if (c->gain == 0) state->masked_samples += (uint64_t)count;
        else if (c->gain < 1) state->attenuated_samples += (uint64_t)count;
    }
    return 0;
}

/* Optional nested core timer: disabled paths perform no timing reads. */
static int fixed_process(fixed_state_t *state, float *samples, size_t count) {
    rm_metrics_t *m = state->metrics;
    if (!m) return fixed_process_core(state, samples, count);
    uint64_t begin, end;
    if (rm_now(m, &begin) || rm_core_begin(m, begin)) return -1;
    int result = fixed_process_core(state, samples, count);
    if (rm_now(m, &end) || rm_core_end(m, end, result ? RM_ERROR : RM_COMPLETED)) return -1;
    return result;
}

static void fixed_record(const fixed_state_t *s, const char *direction, const char *kind) {
    const fixed_config_t *c = &s->config;
    printf("RADIO_FIXED_PROFILE: {\"schema_version\":\"radio_fixed_profile_v1\","
           "\"record_type\":\"%s\",\"backend\":\"c\",\"direction\":\"%s\","
           "\"channel_semantics_version\":\"fixed_reference_v1\","
           "\"rng_version\":\"component_streams_v1\","
           "\"rng_algorithm\":\"glibc_rand_r_box_muller_pair_f32\","
           "\"master_seed\":%" PRIu32 ",\"awgn_seed\":%" PRIu32 ",\"awgn_state\":%" PRIu32 ","
           "\"sample_rate_hz\":%.17g,\"mode\":\"%s\",\"ref_power\":%.17g,"
           "\"gain\":%.17g,\"noise_enabled\":%s,\"noise_snr_db\":%.17g,"
           "\"cw_enabled\":%s,\"cw_sir_db\":%.17g,\"cw_freq_hz\":%.17g,"
           "\"cw_step_u64\":%" PRIu64 ",\"sample_clock\":%" PRIu64 ","
           "\"awgn_complex_draws\":%" PRIu64 ",\"awgn_normal_draws\":%" PRIu64 ","
           "\"phase_u64\":%" PRIu64 ",\"masked_samples\":%" PRIu64 ","
           "\"attenuated_samples\":%" PRIu64 ",\"input_energy\":%.17g,"
           "\"desired_energy\":%.17g,\"noise_energy\":%.17g,\"cw_energy\":%.17g,"
           "\"output_energy\":%.17g,\"units\":\"relative_digital_complex_power\","
           "\"scope\":\"cumulative_processed_samples\"}\n",
           kind, direction, s->master_seed, s->awgn_seed, (uint32_t)s->awgn_state, c->sample_rate_hz,
           c->identity ? "identity" : "fixed", c->ref_power, c->gain,
           !c->identity && !c->noise_off ? "true" : "false", c->noise_snr_db,
           !c->identity && c->cw_enabled ? "true" : "false", c->cw_sir_db, c->cw_freq_hz,
           s->cw_step_u64, s->sample_clock, s->awgn_complex_draws, s->awgn_normal_draws,
           s->phase_u64, s->masked_samples, s->attenuated_samples, s->input_energy,
           s->desired_energy, s->noise_energy, s->cw_energy, s->output_energy);
    fflush(stdout);
}

static int read_fixed_option(int argc, char *argv[], int *index, fixed_config_t configs[2]) {
    const char *option = argv[*index];
    int direction;
    if (strncmp(option, "--dl-", 5) == 0) direction = 0;
    else if (strncmp(option, "--ul-", 5) == 0) direction = 1;
    else return 0;
    const char *name = option + 5;
    static const char *names[] = {"mode", "ref-power", "gain", "noise-snr", "noise-off",
                                  "cw", "cw-sir", "cw-freq"};
    size_t field;
    for (field = 0; field < sizeof(names) / sizeof(names[0]); field++) {
        if (strcmp(name, names[field]) == 0) break;
    }
    if (field == sizeof(names) / sizeof(names[0])) return 0;
    fixed_config_t *c = &configs[direction];
    if (c->seen & (1U << field)) {
        fprintf(stderr, "ERROR: duplicate fixed-profile option %s\n", option);
        return -1;
    }
    c->seen |= 1U << field;
    if (field == 4) c->noise_off = 1;
    else if (field == 5) c->cw_enabled = 1;
    else if (field == 0) {
        if (*index + 1 >= argc) {
            fprintf(stderr, "ERROR: %s requires identity or fixed\n", option);
            return -1;
        }
        const char *mode = argv[++*index];
        if (strcmp(mode, "identity") == 0) c->identity = 1;
        else if (strcmp(mode, "fixed") == 0) c->identity = 0;
        else {
            fprintf(stderr, "ERROR: %s requires identity or fixed\n", option);
            return -1;
        }
    } else {
        double *values[] = {NULL, &c->ref_power, &c->gain, &c->noise_snr_db, NULL, NULL,
                            &c->cw_sir_db, &c->cw_freq_hz};
        double minimum = field == 1 ? 1e-20 : field == 2 ? 0 : field == 7 ? -125e6 : -100;
        double maximum = field == 1 ? 1e10 : field == 2 ? 1 : field == 7 ? 125e6 : 100;
        if (read_bounded_double(argc, argv, index, minimum, maximum, values[field]) != 0) return -1;
    }
    return 1;
}

#include "radio_schedule_native.h"

/* ── Rician / Rayleigh Flat Fading Model ───────────────────────────────────── *
 *
 * First-order autoregressive (AR1) model for time-correlated flat fading,
 * extended with an optional Line-of-Sight (LoS) component (Rician fading).
 *
 * Scatter (NLOS) component, updated once per received nonempty message:
 *   h_I[n] = α · h_I[n-1] + σ_inn · N(0,1)
 *   h_Q[n] = α · h_Q[n-1] + σ_inn · N(0,1)
 *
 * Total channel coefficient:
 *   h = √(K/(K+1)) · 1.0  +  √(1/(K+1)) · (h_I + j·h_Q)
 *       └── LoS (fixed) ──┘   └── scattered (fading) ──┘
 *
 * where:
 *   K       = Rician K-factor (linear). K=0 → pure Rayleigh, K→∞ → AWGN-like.
 *   α       = J₀(2π · f_d · T)            (one-step Bessel-derived AR1 coefficient)
 *   σ_inn   = √((1 - α²) · 0.5)           (innovation std, preserves E[|h_s|²]=1)
 *   T       = num_iq_pairs / sample_rate   (received-message sample duration)
 *   f_d     = max Doppler frequency (Hz)
 *
 * Properties:
 *   - E[|h|²] = 1.0  (unit mean power, no long-term gain/loss)
 *   - Finite K has no deterministic positive fade floor: Gaussian scatter
 *     can approach cancellation of the fixed LoS component.
 *   - K = 0 (linear) → Rayleigh; K = 0 dB means equal LoS/scatter power.
 *   - This message-stepped AR1 approximation does not reproduce the complete
 *     Jakes autocorrelation and depends on the received message partition.
 * ─────────────────────────────────────────────────────────────────────────── */

typedef struct {
    float h_I;            /* real part of scatter component */
    float h_Q;            /* imaginary part of scatter component */
    int   enabled;        /* fading active? */
    float doppler_hz;     /* max Doppler frequency */
    float sample_rate;    /* IQ sample rate (Hz) */
    float k_factor;       /* Rician K-factor (linear, 0 = pure Rayleigh) */
    float los_amp;        /* √(K/(K+1)) — LoS amplitude */
    float scatter_amp;    /* √(1/(K+1)) — scatter amplitude */
} fading_state_t;

static void fading_init(fading_state_t *f, int enabled, float doppler_hz,
                        float sample_rate, float k_factor_db,
                        unsigned int *seed) {
    f->enabled     = enabled;
    f->doppler_hz  = doppler_hz;
    f->sample_rate = sample_rate;
    /* Convert K-factor from dB to linear */
    f->k_factor    = powf(10.0f, k_factor_db / 10.0f);
    float Kp1      = f->k_factor + 1.0f;
    f->los_amp     = sqrtf(f->k_factor / Kp1);
    f->scatter_amp = sqrtf(1.0f / Kp1);
    if (enabled) {
        /* Start at a random point on the distribution */
        float n1, n2;
        randn_pair(seed, &n1, &n2);
        f->h_I = n1 * 0.7071f;  /* √0.5 for unit mean |h_scatter|² */
        f->h_Q = n2 * 0.7071f;
    } else {
        f->h_I = 1.0f;
        f->h_Q = 0.0f;
    }
}

/* Advance legacy fading once per message of num_iq_pairs complex samples. */
static void fading_update(fading_state_t *f, int num_iq_pairs, unsigned int *seed) {
    if (!f->enabled) return;

    double T     = (double)num_iq_pairs / (double)f->sample_rate;
    float  alpha = (float)j0(2.0 * M_PI * (double)f->doppler_hz * T);
    float  a2    = alpha * alpha;
    float  sigma = sqrtf(fmaxf(0.0f, (1.0f - a2) * 0.5f));

    float n1, n2;
    randn_pair(seed, &n1, &n2);
    f->h_I = alpha * f->h_I + sigma * n1;
    f->h_Q = alpha * f->h_Q + sigma * n2;
}

/* Apply fading: complex-multiply each IQ pair by h_total
 * h_total = los_amp * 1.0  +  scatter_amp * (h_I + j*h_Q)  */
static void fading_apply(const fading_state_t *f, float *samples, int nfloats) {
    if (!f->enabled) return;
    /* Combine LoS (real-only, phase=0) and scatter components */
    float hI = f->los_amp + f->scatter_amp * f->h_I;
    float hQ =              f->scatter_amp * f->h_Q;
    for (int i = 0; i + 1 < nfloats; i += 2) {
        float I = samples[i];
        float Q = samples[i + 1];
        samples[i]     = hI * I - hQ * Q;   /* Re{h · x} */
        samples[i + 1] = hI * Q + hQ * I;   /* Im{h · x} */
    }
}

/* Instantaneous channel gain in dB: 10·log10(|h_total|²) */
static inline float fading_gain_db(const fading_state_t *f) {
    float hI = f->los_amp + f->scatter_amp * f->h_I;
    float hQ =              f->scatter_amp * f->h_Q;
    float g  = hI * hI + hQ * hQ;
    return (g > 1e-30f) ? 10.0f * log10f(g) : -300.0f;
}

/* ── CW Interference ──────────────────────────────────────────────────────── *
 *
 * Injects a continuous-wave (CW) tone into the IQ stream to model a
 * co-channel or adjacent-channel interferer.  Applied on the DL path only
 * (interference_enabled = 0 in ul_args).
 *
 * I(n) = A · exp(j · (2π · f_int · n / fs + φ))
 *   where A  = √(P_signal / SIR_linear)
 *         φ  = uint64 sample-clocked NCO phase; advances through silence
 *
 * ─────────────────────────────────────────────────────────────────────────── */

typedef struct {
    int   enabled;
    float freq_hz;     /* centre frequency offset from DC (Hz); may be negative */
    float sir_linear;  /* Signal-to-Interference Ratio (linear) */
    uint64_t phase_u64;       /* cw_clock_version=u64_nco_v1 */
    uint64_t phase_step_u64;
    float sample_rate;
} interference_state_t;

static void interference_init(interference_state_t *intf, int enabled,
                              float freq_hz, float sir_linear, float srate) {
    intf->enabled     = enabled;
    intf->freq_hz     = freq_hz;
    intf->sir_linear  = sir_linear;
    intf->phase_u64   = 0;
    intf->phase_step_u64 = cw_phase_step(freq_hz, srate);
    intf->sample_rate = srate;
}

/* Add CW tone to interleaved float32 I/Q samples. */
static void interference_apply(interference_state_t *intf,
                               float *samples, int nfloats) {
    if (nfloats < 2) return;
    uint64_t count = (uint64_t)(nfloats / 2);
    if (!intf->enabled || intf->sir_linear <= 0.0f) {
        intf->phase_u64 += intf->phase_step_u64 * count;
        return;
    }

    /* Measure signal power (pre-noise, matched to AWGN reference) */
    float sig_power = 0.0f;
    for (int i = 0; i < nfloats; i++) sig_power += samples[i] * samples[i];
    sig_power /= (float)nfloats;
    if (sig_power < 1e-20f) {
        intf->phase_u64 += intf->phase_step_u64 * count;
        return;
    }

    /* sig_power is averaged over interleaved scalar I/Q components. A complex
     * CW has power A^2, while the complex input power is E[I^2 + Q^2], so the
     * scalar-component average must be doubled to honour the requested SIR. */
    float int_amp   = sqrtf((2.0f * sig_power) / intf->sir_linear);
    for (int i = 0; i + 1 < nfloats; i += 2) {
        double angle = ldexp((double)intf->phase_u64, -64) * (2.0 * M_PI);
        samples[i]     += int_amp * (float)cos(angle);
        samples[i + 1] += int_amp * (float)sin(angle);
        intf->phase_u64 += intf->phase_step_u64;
    }
}

/* Optional legacy input-power display. This reports E[|I+jQ|^2] over
 * every validated input sample, including silence; it does not alter DSP. */
typedef struct { double energy; uint64_t samples, start_ns, last_ns; int started; } power_display_t;
static int power_clock(uint64_t *now) {
    struct timespec t;
    return clock_gettime(CLOCK_MONOTONIC,&t)||rm_timespec_ns(&t,now)?-1:0;
}
static int power_accumulate(power_display_t *p,const float *iq,size_t count) {
    if(count>UINT64_MAX-p->samples)return -1;
    double energy=0;
    for(size_t i=0;i<2*count;i++){double v=iq[i];if(!isfinite(v))return -1;energy+=v*v;}
    if(!isfinite(energy)||!isfinite(p->energy+energy))return -1;
    p->energy+=energy;p->samples+=(uint64_t)count;return 0;
}
static int power_report(power_display_t *p,const char *direction,uint64_t now,int final) {
    if(!p->started||now<p->start_ns||now<p->last_ns)return -1;
    p->last_ns=now;
    if(!p->samples||(!final&&now-p->start_ns<UINT64_C(1000000000)))return 0;
    double power=p->energy/(double)p->samples;if(!isfinite(power))return -1;
    if(printf("power %s=%.17g samples=%" PRIu64 " statistic=mean_squared_complex_magnitude units=relative_digital_complex_power final=%s\n",
              direction,power,p->samples,final?"true":"false")<0||fflush(stdout))return -1;
    p->energy=0;p->samples=0;p->start_ns=now;return 0;
}

/* ── Channel Thread ───────────────────────────────────────────────────────── */

typedef struct {
    const char *name;
    const char *rep_bind_addr;    /* broker binds REP here (downstream endpoint) */
    const char *req_connect_addr; /* broker connects REQ here (upstream endpoint) */
    float       snr_db;
    int         fading_enabled;
    float       doppler_hz;
    float       k_factor_db;     /* Rician K-factor in dB (0 = Rayleigh) */
    float       sample_rate;
    void       *zmq_ctx;
    unsigned int rng_seed;
    int         interference_enabled;
    float       interference_freq;   /* Hz */
    float       sir_linear;
    int         print_power;         /* all-input mean squared complex magnitude */
    int         identity_mode;       /* bypass all DSP; retain format checks */
    uint64_t    input_messages;      /* validated finite, single-frame cf32 */
    uint64_t    input_samples;
    uint64_t    output_messages;     /* complete successful downstream sends */
    uint64_t    output_samples;
    uint64_t    rejected_messages;
    uint64_t    error_count;          /* local failures, not sibling failures */
    int         fixed_enabled;
    fixed_state_t fixed;
    rb_runtime_t *schedule;
    size_t schedule_direction;
    atomic_int  startup_state;
} channel_args_t;

enum {
    CHANNEL_START_PENDING = 0,
    CHANNEL_START_READY = 1,
    CHANNEL_START_FAILED = 2,
};

static void channel_mark_failed(channel_args_t *ch) {
    rb_error(ch->schedule, "relay_failure");
    if (ch->error_count != UINT64_MAX) ch->error_count++;
    atomic_store_explicit(&ch->startup_state, CHANNEL_START_FAILED,
                          memory_order_release);
    atomic_store_explicit(&fatal_error, 1, memory_order_release);
}

static rm_frontier_t channel_frontier(const channel_args_t *ch) {
    return (rm_frontier_t){.input_messages=ch->input_messages,.output_messages=ch->output_messages,
        .input_samples=ch->input_samples,.output_samples=ch->output_samples,.processed_samples=ch->fixed.sample_clock,
        .masked_samples=ch->fixed.masked_samples,.rejected_messages=ch->rejected_messages,
        .energies={ch->fixed.input_energy,ch->fixed.desired_energy,ch->fixed.noise_energy,ch->fixed.cw_energy,ch->fixed.output_energy}};
}
static void channel_reject_iq(channel_args_t *ch) {
    if(ch->rejected_messages==UINT64_MAX)channel_mark_failed(ch);
    else ch->rejected_messages++;
}

static int channel_count_message(channel_args_t *ch,
                                 uint64_t *messages,
                                 uint64_t *samples,
                                 size_t sample_count) {
    if (*messages == UINT64_MAX || sample_count > UINT64_MAX - *samples) {
        fprintf(stderr, "[%s] FATAL: relay accounting counter overflow\n", ch->name);
        channel_mark_failed(ch);
        return -1;
    }
    *messages += 1;
    *samples += (uint64_t)sample_count;
    return 0;
}

static int validate_finite_iq(channel_args_t *ch,
                              const uint8_t *buffer,
                              size_t byte_count,
                              const char *stage) {
    const float *samples = (const float *)buffer;
    for (size_t i = 0; i < byte_count / sizeof(float); i++) {
        if (!isfinite(samples[i])) {
            fprintf(stderr, "[%s] FATAL: nonfinite IQ %s at scalar component %zu\n",
                    ch->name, stage, i);
            if(!strcmp(stage,"input"))channel_reject_iq(ch);
            channel_mark_failed(ch);
            return -1;
        }
    }
    return 0;
}

static void print_channel_accounting(const channel_args_t *ch) {
    const char *status = "stopped";
    if (atomic_load_explicit(&fatal_error, memory_order_acquire) != 0) {
        status = "error";
    } else if (ch->input_messages != ch->output_messages ||
               ch->input_samples != ch->output_samples) {
        status = "incomplete";
    }
    /* This is relay accounting only, not the campaign truth/event schema.
     * A clean externally requested stop does not establish a finite trial. */
    printf("C_RELAY_ACCOUNTING: {\"schema_version\":\"%s\","
           "\"record_type\":\"final\",\"backend\":\"c\",\"direction\":\"%s\","
           "\"identity\":%s,\"input_messages\":%" PRIu64 ","
           "\"input_samples\":%" PRIu64 ",\"output_messages\":%" PRIu64 ","
           "\"output_samples\":%" PRIu64 ",\"error_count\":%" PRIu64 ","
           "\"status\":\"%s\"}\n",
           RADIO_BROKER_ACCOUNTING_SCHEMA, ch->name,
           ch->identity_mode ? "true" : "false", ch->input_messages,
           ch->input_samples, ch->output_messages, ch->output_samples,
           ch->error_count, status);
    fflush(stdout);
}

static int set_socket_option(channel_args_t *ch,
                             void *socket,
                             int option,
                             const void *value,
                             size_t value_size,
                             const char *description) {
    if (zmq_setsockopt(socket, option, value, value_size) == 0) {
        return 0;
    }
    fprintf(stderr, "[%s] FATAL: set %s: %s\n",
            ch->name, description, zmq_strerror(zmq_errno()));
    channel_mark_failed(ch);
    return -1;
}

static int receive_message(channel_args_t *ch,
                           void *socket,
                           const char *operation,
                           uint8_t **buffer,
                           size_t *capacity,
                           size_t *message_size, int iq_reply) {
    zmq_msg_t message;
    if (zmq_msg_init(&message) != 0) {
        fprintf(stderr, "[%s] FATAL: %s message init: %s\n",
                ch->name, operation, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
        return -1;
    }

    while (broker_should_run()) {
        if (zmq_msg_recv(&message, socket, 0) >= 0) {
            if(iq_reply && ch->fixed.metrics){
                uint64_t received_ns;
                if(rm_now(ch->fixed.metrics,&received_ns)||rm_transition(ch->fixed.metrics,RM_PROCESSING,received_ns)){
                    channel_mark_failed(ch);(void)zmq_msg_close(&message);return -1;
                }
            }
            size_t received_size = zmq_msg_size(&message);
            /* Both deployed RF drivers use single-frame REQ/REP messages.
             * Do not forward the first part of an unsupported multipart. */
            if (zmq_msg_more(&message)) {
                if(iq_reply)channel_reject_iq(ch);
                fprintf(stderr, "[%s] FATAL: %s is multipart; only single-frame messages are supported\n",
                        ch->name, operation);
                channel_mark_failed(ch);
                (void)zmq_msg_close(&message);
                return -1;
            }
            if (received_size > MAX_MESSAGE_SIZE) {
                if(iq_reply)channel_reject_iq(ch);
                fprintf(stderr,
                        "[%s] FATAL: %s message is %zu bytes; limit is %u bytes\n",
                        ch->name, operation, received_size,
                        (unsigned int)MAX_MESSAGE_SIZE);
                channel_mark_failed(ch);
                (void)zmq_msg_close(&message);
                return -1;
            }
            if (received_size > *capacity) {
                uint8_t *resized = realloc(*buffer, received_size);
                if (resized == NULL) {
                    fprintf(stderr,
                            "[%s] FATAL: cannot allocate %zu bytes for %s message\n",
                            ch->name, received_size, operation);
                    channel_mark_failed(ch);
                    (void)zmq_msg_close(&message);
                    return -1;
                }
                *buffer = resized;
                *capacity = received_size;
            }
            if (received_size > 0) {
                memcpy(*buffer, zmq_msg_data(&message), received_size);
            }
            *message_size = received_size;
            if (zmq_msg_close(&message) != 0) {
                fprintf(stderr, "[%s] FATAL: close %s message: %s\n",
                        ch->name, operation, zmq_strerror(zmq_errno()));
                channel_mark_failed(ch);
                return -1;
            }
            return 1;
        }

        int error_number = zmq_errno();
        if (error_number == EAGAIN || error_number == EINTR) {
            continue;
        }
        fprintf(stderr, "[%s] FATAL: %s: %s\n",
                ch->name, operation, zmq_strerror(error_number));
        channel_mark_failed(ch);
        (void)zmq_msg_close(&message);
        return -1;
    }

    if (zmq_msg_close(&message) != 0) {
        fprintf(stderr, "[%s] FATAL: close interrupted %s message: %s\n",
                ch->name, operation, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
        return -1;
    }
    return 0;
}

static int send_message(channel_args_t *ch,
                        void *socket,
                        const char *operation,
                        const uint8_t *buffer,
                        size_t message_size) {
    while (broker_should_run()) {
        int sent = zmq_send(socket, buffer, message_size, 0);
        if (sent >= 0) {
            if ((size_t)sent != message_size) {
                fprintf(stderr,
                        "[%s] FATAL: %s sent %d of %zu bytes\n",
                        ch->name, operation, sent, message_size);
                channel_mark_failed(ch);
                return -1;
            }
            return 1;
        }
        int error_number = zmq_errno();
        if (error_number == EAGAIN || error_number == EINTR) {
            continue;
        }
        fprintf(stderr, "[%s] FATAL: %s: %s\n",
                ch->name, operation, zmq_strerror(error_number));
        channel_mark_failed(ch);
        return -1;
    }
    return 0;
}

static void print_channel_progress(const channel_args_t *ch,
                                   uint64_t msg_count,
                                   uint64_t total_samples,
                                   const fading_state_t *fading,
                                   float min_gain_db,
                                   float max_gain_db) {
    if (fading->enabled) {
        printf("[%s] %" PRIu64 " msgs, %.1f M IQ | fade: %.1f..%.1f dB (now %.1f dB)\n",
               ch->name, msg_count, (double)total_samples / 1e6,
               min_gain_db, max_gain_db, fading_gain_db(fading));
    } else {
        printf("[%s] %" PRIu64 " msgs, %.1f M samples processed\n",
               ch->name, msg_count, (double)total_samples / 1e6);
    }

    /* The launcher redirects stdout to a retained run log. Flush each
     * periodic record so live acceptance can observe channel progress rather
     * than receiving the records only when the broker exits. */
    fflush(stdout);
}

static void *channel_thread(void *arg) {
    channel_args_t *ch = (channel_args_t *)arg;
    void *rep = NULL;
    void *req = NULL;
    uint8_t *buf = NULL;
    uint8_t *req_buf = NULL;
    size_t buf_capacity = INITIAL_BUF_SIZE;
    size_t req_buf_capacity = INITIAL_BUF_SIZE;
    int startup_complete = 0;
    rm_metrics_t *metrics = ch->fixed.metrics;
    power_display_t power = {0};
    uint64_t timing_ns = 0;

    float snr_linear = powf(10.0f, ch->snr_db / 10.0f);
    unsigned int seed = ch->rng_seed;

    /* Initialize fading model */
    fading_state_t fading = {0};
    if (!ch->fixed_enabled) {
        fading_init(&fading, ch->fading_enabled && !ch->identity_mode, ch->doppler_hz,
                    ch->sample_rate, ch->k_factor_db, &seed);
    }

    /* Initialize CW interference */
    interference_state_t intf = {0};
    if (!ch->fixed_enabled) {
        interference_init(&intf, ch->interference_enabled && !ch->identity_mode,
                          ch->interference_freq, ch->sir_linear, ch->sample_rate);
    }

    buf = malloc(buf_capacity);
    req_buf = malloc(req_buf_capacity);
    if (buf == NULL || req_buf == NULL) {
        fprintf(stderr,
                "[%s] FATAL: cannot allocate initial relay buffers (%u bytes each)\n",
                ch->name, (unsigned int)INITIAL_BUF_SIZE);
        channel_mark_failed(ch);
        goto done;
    }

    /* Create sockets. All options are set before bind/connect. */
    rep = zmq_socket(ch->zmq_ctx, ZMQ_REP);
    if (rep == NULL) {
        fprintf(stderr, "[%s] FATAL: create REP socket: %s\n",
                ch->name, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
        goto done;
    }
    req = zmq_socket(ch->zmq_ctx, ZMQ_REQ);
    if (req == NULL) {
        fprintf(stderr, "[%s] FATAL: create REQ socket: %s\n",
                ch->name, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
        goto done;
    }

    /* Socket timeout lets each relay observe a signal or sibling failure. */
    int timeout_ms = 500;
    int linger_ms = 0;
    int64_t max_message_size = (int64_t)MAX_MESSAGE_SIZE;
    if (set_socket_option(ch, rep, ZMQ_RCVTIMEO, &timeout_ms,
                          sizeof(timeout_ms), "REP receive timeout") != 0 ||
        set_socket_option(ch, rep, ZMQ_SNDTIMEO, &timeout_ms,
                          sizeof(timeout_ms), "REP send timeout") != 0 ||
        set_socket_option(ch, req, ZMQ_RCVTIMEO, &timeout_ms,
                          sizeof(timeout_ms), "REQ receive timeout") != 0 ||
        set_socket_option(ch, req, ZMQ_SNDTIMEO, &timeout_ms,
                          sizeof(timeout_ms), "REQ send timeout") != 0 ||
        set_socket_option(ch, rep, ZMQ_LINGER, &linger_ms,
                          sizeof(linger_ms), "REP linger") != 0 ||
        set_socket_option(ch, req, ZMQ_LINGER, &linger_ms,
                          sizeof(linger_ms), "REQ linger") != 0 ||
        set_socket_option(ch, rep, ZMQ_MAXMSGSIZE, &max_message_size,
                          sizeof(max_message_size), "REP maximum message size") != 0 ||
        set_socket_option(ch, req, ZMQ_MAXMSGSIZE, &max_message_size,
                          sizeof(max_message_size), "REQ maximum message size") != 0) {
        goto done;
    }

    if (zmq_bind(rep, ch->rep_bind_addr) != 0) {
        fprintf(stderr, "[%s] FATAL: bind REP on %s: %s\n",
                ch->name, ch->rep_bind_addr, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
        goto done;
    }
    if (zmq_connect(req, ch->req_connect_addr) != 0) {
        fprintf(stderr, "[%s] FATAL: connect REQ to %s: %s\n",
                ch->name, ch->req_connect_addr, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
        goto done;
    }

    if (ch->fixed_enabled) {
        printf("[%s] Active: upstream %s → fixed_reference_v1 (%s) → downstream %s\n",
               ch->name, ch->req_connect_addr,
               ch->fixed.config.identity ? "identity" : "gain + CW + receiver noise",
               ch->rep_bind_addr);
        fixed_record(&ch->fixed, ch->name, "started");
    } else if (ch->identity_mode) {
        printf("[%s] Active: upstream %s → identity_cf32_v1 (all DSP bypassed) → downstream %s\n",
               ch->name, ch->req_connect_addr, ch->rep_bind_addr);
    } else if (ch->fading_enabled) {
        printf("[%s] Active: upstream %s → Rician(K=%.1f dB, fd=%.0f Hz)+AWGN(SNR=%.1f dB) → downstream %s\n",
               ch->name, ch->req_connect_addr, ch->k_factor_db, ch->doppler_hz, ch->snr_db, ch->rep_bind_addr);
    } else {
        printf("[%s] Active: upstream %s → AWGN(SNR=%.1f dB) → downstream %s\n",
               ch->name, ch->req_connect_addr, ch->snr_db, ch->rep_bind_addr);
    }
    fflush(stdout);
    atomic_store_explicit(&ch->startup_state, CHANNEL_START_READY,
                          memory_order_release);
    startup_complete = 1;

    float min_gain_db = 0.0f, max_gain_db = 0.0f;
    if(ch->print_power){
        if(power_clock(&power.start_ns)){channel_mark_failed(ch);goto done;}
        power.started=1;power.last_ns=power.start_ns;
    }

    while (broker_should_run()) {
        if(metrics){
            rm_frontier_t f=channel_frontier(ch);
            if(rm_now(metrics,&timing_ns)||(!metrics->started&&rm_begin(metrics,timing_ns,&f))||rm_phase_begin(metrics,RM_REQUEST_RECEIVE,timing_ns)){
                channel_mark_failed(ch);break;
            }
        }
        /* 1. Receive request from downstream (UE-RX or gNB-RX)
         *    REP is in "recv" state — EAGAIN/EINTR just mean retry. */
        size_t rlen = 0;
        int io_status = receive_message(ch, rep, "receive downstream request",
                                        &req_buf, &req_buf_capacity, &rlen, 0);
        if (io_status <= 0) break;

        if(metrics&&(rm_now(metrics,&timing_ns)||rm_transition(metrics,RM_REQUEST_SEND,timing_ns))){channel_mark_failed(ch);break;}
        /* 2. Forward request to upstream (gNB-TX or UE-TX)
         *    REQ send — retry on EAGAIN/EINTR. */
        io_status = send_message(ch, req, "send upstream request", req_buf, rlen);
        if (io_status <= 0) break;

        if(metrics&&(rm_now(metrics,&timing_ns)||rm_transition(metrics,RM_UPSTREAM_RECEIVE,timing_ns))){channel_mark_failed(ch);break;}
        /* 3. Receive IQ data from upstream
         *    REQ is now in "recv" state — MUST stay here until we get
         *    the reply or running goes to 0.  Jumping back to step 1
         *    would violate the REQ-REP state machine. */
        size_t dlen = 0;
        io_status = receive_message(ch, req, "receive upstream IQ reply",
                                    &buf, &buf_capacity, &dlen, 1);
        if (io_status <= 0) break;
        if (dlen % (2U * sizeof(float)) != 0) {
            channel_reject_iq(ch);
            fprintf(stderr,
                    "[%s] FATAL: IQ reply length %zu is not cf32-aligned "
                    "(whole interleaved I/Q pairs require multiples of 8 bytes)\n",
                    ch->name, dlen);
            channel_mark_failed(ch);
            break;
        }
        if (validate_finite_iq(ch, buf, dlen, "input") != 0) break;
        size_t sample_count = dlen / (2U * sizeof(float));
        if (channel_count_message(ch, &ch->input_messages, &ch->input_samples,
                                  sample_count) != 0) break;

        if(ch->print_power){uint64_t power_now;
            if(power_accumulate(&power,(const float *)buf,sample_count)||power_clock(&power_now)||power_report(&power,ch->name,power_now,0)){
                channel_mark_failed(ch);break;
            }
        }
        if(metrics&&rm_input(metrics,sample_count)){channel_mark_failed(ch);break;}
        rb_snapshot(ch->schedule, ch->schedule_direction, ch->input_messages, ch->input_samples,
                    ch->output_messages, ch->output_samples, ch->fixed.sample_clock);

        /* 4. Apply channel impairments to interleaved float32 I/Q samples.
         *    SIR and SNR both use the desired signal after fading as their
         *    reference. Interference is superposed before receiver noise so
         *    neither configured ratio accidentally includes the other. */
        if(metrics&&(rm_now(metrics,&timing_ns)||rm_part_next(metrics,RM_CHANNEL_CHAIN,timing_ns))){channel_mark_failed(ch);break;}
        int nfloats = (int)(dlen / sizeof(float));
        if (ch->fixed_enabled) {
            int dsp_status = ch->schedule ?
                rb_process(ch->schedule, ch->schedule_direction, &ch->fixed, (float *)buf, sample_count) :
                fixed_process(&ch->fixed, (float *)buf, sample_count);
            if (dsp_status != 0) {
                fprintf(stderr, "[%s] FATAL: invalid fixed-profile input or sample/draw counter overflow\n",
                        ch->name);
                channel_mark_failed(ch);
                break;
            }
        } else if (!ch->identity_mode && nfloats >= 2) {
            float *samples = (float *)buf;
            int num_iq = nfloats / 2;

            /* (b) Apply configured Rician/Rayleigh flat fading. */
            fading_update(&fading, num_iq, &seed);
            fading_apply(&fading, samples, nfloats);

            if (fading.enabled) {
                float gdb = fading_gain_db(&fading);
                if (ch->output_messages == 0) { min_gain_db = max_gain_db = gdb; }
                if (gdb < min_gain_db) min_gain_db = gdb;
                if (gdb > max_gain_db) max_gain_db = gdb;
            }

            float desired_power = 0.0f;
            for (int i = 0; i < nfloats; i++)
                desired_power += samples[i] * samples[i];
            desired_power /= (float)nfloats;

            /* (c) Superpose synthetic CW interference before receiver noise. */
            interference_apply(&intf, samples, nfloats);

            /* (d) Add AWGN relative only to the post-channel desired signal. */
            if (desired_power > 1e-20f) {
                float noise_std = sqrtf(desired_power / snr_linear);
                float n1, n2;

                /* Process I/Q pairs */
                int i;
                for (i = 0; i + 1 < nfloats; i += 2) {
                    randn_pair(&seed, &n1, &n2);
                    samples[i]     += noise_std * n1;
                    samples[i + 1] += noise_std * n2;
                }
            }
        }
        if(metrics&&(rm_now(metrics,&timing_ns)||rm_part_next(metrics,RM_OUTPUT_PREPARE,timing_ns))){channel_mark_failed(ch);break;}
        /* Identity was already checked and has not written any sample bytes.
         * Legacy DSP may overflow even for finite input; fail before sending. */
        if (!ch->identity_mode && validate_finite_iq(ch, buf, dlen, "output") != 0) break;

        /* 5. Send impaired IQ data to downstream
         *    REP is in "send" state — retry on EAGAIN/EINTR. */
        /* Capacity must be established before the irreversible successful send. */
        if (ch->output_messages == UINT64_MAX || sample_count > UINT64_MAX - ch->output_samples) {
            channel_mark_failed(ch);
            break;
        }
        if(metrics&&(rm_now(metrics,&timing_ns)||rm_phase_end(metrics,timing_ns,RM_COMPLETED,sample_count)||rm_phase_begin(metrics,RM_DOWNSTREAM_SEND,timing_ns))){channel_mark_failed(ch);break;}
        io_status = send_message(ch, rep, "send downstream IQ reply", buf, dlen);
        if (io_status <= 0) break;
        /* A timing failure cannot erase an already successful wire send. */
        int output_timing_error=metrics&&(rm_now(metrics,&timing_ns)||rm_phase_end(metrics,timing_ns,RM_COMPLETED,0));
        if (channel_count_message(ch, &ch->output_messages, &ch->output_samples,
                                  sample_count) != 0) break;
        if(output_timing_error){channel_mark_failed(ch);break;}

        if(metrics){rm_frontier_t f=channel_frontier(ch);if(rm_maybe_flush(metrics,timing_ns,&f)){channel_mark_failed(ch);break;}}
        if (ch->schedule && rb_forwarded(ch->schedule, ch->schedule_direction,
                ch->input_messages, ch->input_samples, ch->output_messages, ch->output_samples,
                ch->fixed.sample_clock) != 0) { channel_mark_failed(ch); break; }

        if (!ch->schedule && ch->output_messages % 10000 == 0) {
            print_channel_progress(ch, ch->output_messages, ch->output_samples, &fading,
                                   min_gain_db, max_gain_db);
        }
    }
done:
    if(ch->print_power&&power.started){uint64_t power_now;if(power_clock(&power_now)||power_report(&power,ch->name,power_now,1))channel_mark_failed(ch);}
    if(metrics){
        rm_frontier_t f=channel_frontier(ch);metrics->current=f;
        if(rm_now(metrics,&timing_ns)||rm_close_window(metrics,timing_ns,
                ch->error_count?RM_ERROR:RM_STOPPED,&f))channel_mark_failed(ch);
    }
    free(buf);
    free(req_buf);
    if (rep != NULL && zmq_close(rep) != 0) {
        fprintf(stderr, "[%s] FATAL: close REP socket: %s\n",
                ch->name, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
    }
    if (req != NULL && zmq_close(req) != 0) {
        fprintf(stderr, "[%s] FATAL: close REQ socket: %s\n",
                ch->name, zmq_strerror(zmq_errno()));
        channel_mark_failed(ch);
    }
    if (!startup_complete &&
        atomic_load_explicit(&ch->startup_state, memory_order_acquire) ==
            CHANNEL_START_PENDING &&
        atomic_load_explicit(&fatal_error, memory_order_acquire) != 0) {
        atomic_store_explicit(&ch->startup_state, CHANNEL_START_FAILED,
                              memory_order_release);
    }

    rb_snapshot(ch->schedule, ch->schedule_direction, ch->input_messages, ch->input_samples,
                ch->output_messages, ch->output_samples, ch->fixed.sample_clock);
    printf("[%s] Stopped after %" PRIu64 " messages\n", ch->name, ch->output_messages);
    print_channel_accounting(ch);
    if (ch->fixed_enabled) fixed_record(&ch->fixed, ch->name, "final");
    return NULL;
}

int main(int argc, char *argv[]) {
    float dl_snr_db   = 28.0f;   /* canonical launcher baseline */
    float ul_snr_db   = 28.0f;
    int   fading      = 0;       /* off by default (backward compatible) */
    float dl_doppler  = 5.0f;    /* Hz — stationary / very slow fades */
    float ul_doppler  = 5.0f;
    float k_factor_db = 3.0f;    /* canonical Rician baseline */
    float srate       = DEFAULT_SRATE;
    int   intf_enabled = 0;       /* DL CW interference (off by default) */
    float intf_freq   = 1.0e6f;  /* Hz — default 1 MHz from DC */
    float sir_db      = 20.0f;   /* Signal-to-Interference Ratio (dB) */
    float sir_linear  = 100.0f;  /* 10^(20/10) */
    int   print_power = 0;
    int   identity_mode = 0;
    const char *dl_bind = NULL;
    const char *dl_connect = NULL;
    const char *ul_bind = NULL;
    const char *ul_connect = NULL;
    uint32_t master_seed = 1U;   /* deterministic unless explicitly changed */
    int fixed_enabled = 0, semantics_seen = 0, legacy_options_seen = 0;
    int validate_config_only = 0;
    const char *radio_plan_file = NULL, *radio_control_dir = NULL;
    rb_runtime_t *schedule = NULL;
    uint64_t metrics_every = 0; int metrics_seen = 0;
    double fixed_sample_rate = DEFAULT_SRATE;
    fixed_config_t fixed_configs[2] = {fixed_config_defaults(), fixed_config_defaults()};

    for (int i = 1; i < argc; i++) {
        static const char *legacy_options[] = {
            "--identity", "--print-power", "--snr", "--dl-snr", "--ul-snr", "--fading",
            "--doppler", "--dl-doppler", "--ul-doppler", "--k-factor", "--rayleigh",
            "--interference-type", "--interference-freq", "--sir"};
        for (size_t j = 0; j < sizeof(legacy_options) / sizeof(legacy_options[0]); j++) {
            if (strcmp(argv[i], legacy_options[j]) == 0) legacy_options_seen = 1;
        }
        int fixed_option = read_fixed_option(argc, argv, &i, fixed_configs);
        if (fixed_option < 0) return 2;
        if (fixed_option > 0) continue;
        if (strcmp(argv[i], "--radio-metrics-every-messages") == 0) {
            if(metrics_seen||i+1>=argc||rb_uint(argv[++i],1000000,&metrics_every)||!metrics_every){
                fprintf(stderr,"ERROR: metrics interval requires one decimal integer in [1,1000000]\n");return 2;
            }
            metrics_seen=1;
        } else if (strcmp(argv[i], "--radio-plan-file") == 0 || strcmp(argv[i], "--radio-control-dir") == 0) {
            const char **target = strcmp(argv[i], "--radio-plan-file") == 0 ? &radio_plan_file : &radio_control_dir;
            if (*target || i + 1 >= argc || !argv[i + 1][0]) {
                fprintf(stderr, "ERROR: schedule option requires exactly one nonempty value\n"); return 2;
            }
            *target = argv[++i];
        } else if (strcmp(argv[i], "--validate-config-only") == 0) {
            validate_config_only = 1;
        } else if (strcmp(argv[i], "--channel-semantics") == 0) {
            if (semantics_seen || i + 1 >= argc) {
                fprintf(stderr, "ERROR: --channel-semantics requires one explicit version\n");
                return 2;
            }
            semantics_seen = 1;
            const char *version = argv[++i];
            if (strcmp(version, FIXED_SEMANTICS) == 0) fixed_enabled = 1;
            else if (strcmp(version, LEGACY_SEMANTICS) != 0) {
                fprintf(stderr, "ERROR: unsupported channel semantics version\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--identity") == 0) {
            identity_mode = 1;
        } else if (strcmp(argv[i], "--dl-bind") == 0) {
            if (read_local_endpoint(argc, argv, &i, &dl_bind) != 0) return 2;
        } else if (strcmp(argv[i], "--dl-connect") == 0) {
            if (read_local_endpoint(argc, argv, &i, &dl_connect) != 0) return 2;
        } else if (strcmp(argv[i], "--ul-bind") == 0) {
            if (read_local_endpoint(argc, argv, &i, &ul_bind) != 0) return 2;
        } else if (strcmp(argv[i], "--ul-connect") == 0) {
            if (read_local_endpoint(argc, argv, &i, &ul_connect) != 0) return 2;
        } else if (strcmp(argv[i], "--dl-snr") == 0) {
            if (read_bounded_float(argc, argv, &i, MIN_DB, MAX_DB, &dl_snr_db) != 0) return 2;
        } else if (strcmp(argv[i], "--ul-snr") == 0) {
            if (read_bounded_float(argc, argv, &i, MIN_DB, MAX_DB, &ul_snr_db) != 0) return 2;
        } else if (strcmp(argv[i], "--snr") == 0) {
            float snr_db;
            if (read_bounded_float(argc, argv, &i, MIN_DB, MAX_DB, &snr_db) != 0) return 2;
            dl_snr_db = ul_snr_db = snr_db;
        } else if (strcmp(argv[i], "--fading") == 0) {
            fading = 1;
        } else if (strcmp(argv[i], "--doppler") == 0) {
            float doppler_hz;
            if (read_bounded_float(argc, argv, &i, 0.0f, MAX_DOPPLER_HZ, &doppler_hz) != 0) return 2;
            dl_doppler = ul_doppler = doppler_hz;
            fading = 1;  /* --doppler implies --fading */
        } else if (strcmp(argv[i], "--dl-doppler") == 0) {
            if (read_bounded_float(argc, argv, &i, 0.0f, MAX_DOPPLER_HZ, &dl_doppler) != 0) return 2;
            fading = 1;
        } else if (strcmp(argv[i], "--ul-doppler") == 0) {
            if (read_bounded_float(argc, argv, &i, 0.0f, MAX_DOPPLER_HZ, &ul_doppler) != 0) return 2;
            fading = 1;
        } else if (strcmp(argv[i], "--k-factor") == 0) {
            if (read_bounded_float(argc, argv, &i, MIN_DB, MAX_DB, &k_factor_db) != 0) return 2;
        } else if (strcmp(argv[i], "--rayleigh") == 0) {
            k_factor_db = -100.0f;  /* effectively K=0 (pure Rayleigh) */
            fading = 1;
        } else if (strcmp(argv[i], "--srate") == 0) {
            if (read_bounded_float(argc, argv, &i, MIN_SRATE_HZ, MAX_SRATE_HZ, &srate) != 0) return 2;
            fixed_sample_rate = strtod(argv[i], NULL);
        } else if (strcmp(argv[i], "--interference-type") == 0) {
            if (i + 1 >= argc) {
                fprintf(stderr, "ERROR: --interference-type requires a value\n");
                return 2;
            }
            const char *type = argv[++i];
            if (strcmp(type, "none") == 0) {
                intf_enabled = 0;
            } else if (strcmp(type, "cw") == 0) {
                intf_enabled = 1;
            } else {
                fprintf(stderr,
                        "ERROR: --interference-type must be 'none' or 'cw'; use the GNU Radio broker for narrowband\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--interference-freq") == 0) {
            if (read_bounded_float(argc, argv, &i, -125.0e6f, 125.0e6f, &intf_freq) != 0) return 2;
        } else if (strcmp(argv[i], "--sir") == 0) {
            if (read_bounded_float(argc, argv, &i, MIN_DB, MAX_DB, &sir_db) != 0) return 2;
            sir_linear = powf(10.0f, sir_db / 10.0f);
        } else if (strcmp(argv[i], "--print-power") == 0) {
            print_power = 1;
        } else if (strcmp(argv[i], "--seed") == 0) {
            if (read_uint32(argc, argv, &i, &master_seed) != 0) return 2;
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            printf("Usage: %s [OPTIONS]\n\n", argv[0]);
            printf("Identity relay:\n");
            printf("  --identity          Byte-exact finite cf32 relay in both directions\n");
            printf("                      Overrides all DSP options; format checks remain active\n\n");
            printf("Versioned fixed-reference profile:\n");
            printf("  --channel-semantics <version>  legacy_message_local_v1 (default) | fixed_reference_v1\n");
            printf("  --dl-mode / --ul-mode <mode>  identity | fixed (default fixed)\n");
            printf("  --dl-ref-power / --ul-ref-power <power>  Both required; complex digital units [1e-20,1e10]\n");
            printf("  --dl-gain / --ul-gain <gain>  Desired amplitude [0,1], default 1\n");
            printf("  --dl-noise-snr / --ul-noise-snr <dB>  Reference SNR [-100,100], default 28\n");
            printf("  --dl-noise-off / --ul-noise-off  Disable addition; still advance AWGN stream\n");
            printf("  --dl-cw / --ul-cw  Enable independent fixed-reference CW\n");
            printf("  --dl-cw-sir / --ul-cw-sir <dB>  Reference SIR [-100,100], default 20\n");
            printf("  --dl-cw-freq / --ul-cw-freq <Hz>  Within +/-Fs/2, default 0\n");
            printf("    Fixed profile rejects legacy DSP/identity/print-power options.\n");
            printf("    Fixed mode advances two real AWGN variates and the uint64 CW NCO per sample,\n");
            printf("    including silence/disabled additions. Directional identity bypasses both.\n\n");
            printf("  --radio-metrics-every-messages N  Optional finite-schedule timing windows [1,1000000]\n");
            printf("  --radio-plan-file PATH --radio-control-dir DIR  Finite development schedule, authenticated local arm\n");
            printf("  --validate-config-only  Validate effective configuration and exit without sockets\n\n");
            printf("AWGN options:\n");
            printf("  --snr <dB>          Set both DL and UL SNR (default: 28)\n");
            printf("  --dl-snr <dB>       Override DL SNR\n");
            printf("  --ul-snr <dB>       Override UL SNR\n");
            printf("\nFading options:\n");
            printf("  --fading            Enable Rician fading (default K=3 dB)\n");
            printf("  --k-factor <dB>     Rician K-factor in dB (default: 3)\n");
            printf("                       0 dB = equal LoS and scatter (moderate fading)\n");
            printf("                       6 dB = LoS power about four times scatter power\n");
            printf("                      10 dB = LoS power ten times scatter power\n");
            printf("  --rayleigh          Approximate zero-LoS limit (K=-100 dB)\n");
            printf("  --doppler <Hz>      Max Doppler frequency, implies --fading (default: 5)\n");
            printf("  --dl-doppler <Hz>   Override DL Doppler, implies --fading\n");
            printf("  --ul-doppler <Hz>   Override UL Doppler, implies --fading\n");
            printf("\nInterference options (DL only):\n");
            printf("  --interference-type <type>  none | cw (default: none)\n");
            printf("                               Use the GNU Radio broker for narrowband\n");
            printf("  --interference-freq <Hz>    Centre freq offset from DC (default: 1e6)\n");
            printf("  --sir <dB>                  Signal-to-Interference Ratio dB (default: 20)\n");
            printf("                               0 dB = equal power, -10 dB = jammer-dominated\n");
            printf("\nOther:\n");
            printf("  --dl-bind <endpoint>     Downstream DL REP (default tcp://127.0.0.1:2000)\n");
            printf("  --dl-connect <endpoint>  Upstream DL REQ (default tcp://127.0.0.1:4000)\n");
            printf("  --ul-bind <endpoint>     Downstream UL REP (default tcp://127.0.0.1:4001)\n");
            printf("  --ul-connect <endpoint>  Upstream UL REQ (default tcp://127.0.0.1:2001)\n");
            printf("    Endpoints must be distinct canonical loopback TCP or absolute IPC paths.\n");
            printf("    The caller must own and manage IPC paths inside its private directory.\n");
            printf("  --srate <Hz>        IQ sample rate (default: 23.04e6)\n");
            printf("  --seed <uint32>     Deterministic master RNG seed (default: 1)\n");
            printf("  --print-power       Print all-input 1 s mean squared complex magnitude and final partial\n");
            printf("  -h, --help          Show this help\n");
            printf("\nDoppler guidelines:\n");
            printf("    5 Hz    Stationary / very slow fades\n");
            printf("   10 Hz    Pedestrian   (~3 km/h at 1.8 GHz)\n");
            printf("   70 Hz    Vehicular    (~50 km/h)\n");
            printf("  300 Hz    Highway      (~200 km/h)\n");
            printf("  900 Hz    High-speed train (~600 km/h)\n");
            printf("\nModel limits:\n");
            printf("    Finite K does not impose a positive minimum gain or guarantee service.\n");
            printf("    Flat fading is a message-stepped AR1 approximation, not exact Jakes fading.\n");
            printf("\nExamples:\n");
            printf("  %s --snr 30 --fading --doppler 5    # Legacy Rician AR1 fading\n", argv[0]);
            printf("  %s --snr 15 --fading --doppler 70   # Vehicular fading\n", argv[0]);
            printf("  %s --snr 10                         # AWGN only (no fading)\n", argv[0]);
            printf("  %s --rayleigh --doppler 10 --snr 30 # Legacy approximate Rayleigh limit\n", argv[0]);
            return 0;
        } else {
            fprintf(stderr, "ERROR: Unknown option '%s' (use --help)\n", argv[i]);
            return 2;
        }
    }

    if (dl_bind == NULL) dl_bind = "tcp://127.0.0.1:2000";
    if (dl_connect == NULL) dl_connect = "tcp://127.0.0.1:4000";
    if (ul_bind == NULL) ul_bind = "tcp://127.0.0.1:4001";
    if (ul_connect == NULL) ul_connect = "tcp://127.0.0.1:2001";
    const char *endpoints[4] = {dl_bind, dl_connect, ul_bind, ul_connect};
    if (validate_distinct_endpoints(endpoints) != 0) return 2;

    if (fixed_enabled) {
        if (legacy_options_seen) {
            fprintf(stderr, "ERROR: fixed_reference_v1 cannot mix legacy impairment/identity/print-power options\n");
            return 2;
        }
        for (size_t i = 0; i < 2; i++) {
            fixed_configs[i].sample_rate_hz = fixed_sample_rate;
            if (!(fixed_configs[i].seen & (1U << 1)) || !fixed_config_valid(&fixed_configs[i])) {
                fprintf(stderr, "ERROR: fixed_reference_v1 requires both directional reference powers and valid per-direction parameters (CW within Nyquist)\n");
                return 2;
            }
        }
    } else if (fixed_configs[0].seen || fixed_configs[1].seen) {
        fprintf(stderr, "ERROR: directional fixed-profile fields require --channel-semantics fixed_reference_v1\n");
        return 2;
    }

    if (!fixed_enabled && fabsf(intf_freq) > srate * 0.5f) {
        fprintf(stderr,
                "ERROR: --interference-freq magnitude must not exceed Nyquist (%g Hz for sample rate %g Hz)\n",
                (double)(srate * 0.5f), (double)srate);
        return 2;
    }

    if (radio_plan_file || radio_control_dir) {
        if (!radio_plan_file || !radio_control_dir || !fixed_enabled ||
            !(schedule = rb_load(radio_plan_file, radio_control_dir, fixed_configs, master_seed))) {
            fprintf(stderr, "ERROR: invalid schedule/private control inputs or profile mismatch\n"); return 2;
        }
    }

    if(metrics_seen&&(!schedule||rb_metrics_enable(schedule,metrics_every))){
        fprintf(stderr,"ERROR: metrics require a valid schedule and absent private metrics output\n");rb_dispose_loaded(schedule);return 2;
    }
    if (validate_config_only) {
        printf("RADIO_CONFIG_VALIDATED: {\"schema_version\":\"radio_config_validated_v1\","
               "\"backend\":\"c\",\"channel_semantics_version\":\"%s\","
               "\"rng_version\":\"%s\",\"master_seed\":%" PRIu32 ","
               "\"sample_rate_hz\":%.17g,\"directions\":{",
               fixed_enabled ? FIXED_SEMANTICS : LEGACY_SEMANTICS,
               fixed_enabled ? "component_streams_v1" : "legacy_direction_streams_v1",
               master_seed, fixed_enabled ? fixed_sample_rate : (double)srate);
        for (size_t i = 0; i < 2; i++) {
            printf("%s\"%s\":{", i ? "," : "", i ? "UL" : "DL");
            if (fixed_enabled) {
                const fixed_config_t *c = &fixed_configs[i];
                printf("\"mode\":\"%s\",\"ref_power\":%.17g,\"gain\":%.17g,"
                       "\"noise_enabled\":%s,\"noise_snr_db\":%.17g,"
                       "\"cw_enabled\":%s,\"cw_sir_db\":%.17g,\"cw_freq_hz\":%.17g}",
                       c->identity ? "identity" : "fixed", c->ref_power, c->gain,
                       !c->identity && !c->noise_off ? "true" : "false", c->noise_snr_db,
                       !c->identity && c->cw_enabled ? "true" : "false", c->cw_sir_db, c->cw_freq_hz);
            } else {
                printf("\"mode\":\"%s\",\"snr_db\":%.9g,\"fading_enabled\":%s,"
                       "\"doppler_hz\":%.9g,\"k_factor_db\":%.9g,\"cw_enabled\":%s,"
                       "\"cw_sir_db\":%.9g,\"cw_freq_hz\":%.9g,\"cw_clock_version\":\"u64_nco_v1\"}",
                       identity_mode ? "identity" : "legacy", (double)(i ? ul_snr_db : dl_snr_db),
                       fading && !identity_mode ? "true" : "false",
                       (double)(i ? ul_doppler : dl_doppler), (double)k_factor_db,
                       !i && intf_enabled && !identity_mode ? "true" : "false",
                       (double)sir_db, i ? 0.0 : (double)intf_freq);
            }
        }
        printf("}}\n");
        rb_dispose_loaded(schedule);
        return 0;
    }

    /* ── Banner ──────────────────────────────────────────────────────────── */
    printf("╔═════════════════════════════════════════════════════════╗\n");
    if (fixed_enabled) {
        printf("║     ZMQ Channel Broker  (fixed_reference_v1)         ║\n");
    } else if (identity_mode) {
        printf("║     ZMQ Channel Broker  (identity_cf32_v1)          ║\n");
    } else if (fading) {
        if (k_factor_db > -50.0f)
            printf("║     ZMQ Channel Broker  (AWGN + Rician Fading)       ║\n");
        else
            printf("║     ZMQ Channel Broker  (AWGN + Rayleigh Fading)     ║\n");
    } else {
        printf("║     ZMQ Channel Broker  (AWGN only)                   ║\n");
    }
    printf("╠═════════════════════════════════════════════════════════╣\n");
    if (fixed_enabled) {
        printf("║  Independent directional gain, CW and receiver noise ║\n");
    } else if (identity_mode) {
        printf("║  DL/UL: all DSP bypassed; finite cf32 validation     ║\n");
    } else if (fading) {
        printf("║  DL: SNR %5.1f dB  Doppler %4.0f Hz  K=%5.1f dB     ║\n", dl_snr_db, dl_doppler, k_factor_db);
        printf("║  UL: SNR %5.1f dB  Doppler %4.0f Hz  K=%5.1f dB     ║\n", ul_snr_db, ul_doppler, k_factor_db);
    } else {
        printf("║  DL: SNR %5.1f dB                                     ║\n", dl_snr_db);
        printf("║  UL: SNR %5.1f dB                                     ║\n", ul_snr_db);
    }
    if (intf_enabled && !identity_mode) {
    printf("║  DL interference: CW  freq=%.3f MHz  SIR=%.1f dB        ║\n",
           intf_freq / 1e6f, sir_db);
    printf("║  CW clock: u64_nco_v1 (advances through silence)      ║\n");
    }
    if (fixed_enabled) {
        printf("Fixed AWGN component seeds: master=%" PRIu32 " DL=%" PRIu32 " UL=%" PRIu32 "\n",
               master_seed, fixed_component_seed(master_seed, UINT32_C(0x0d1a5eed), UINT32_C(0x4157474e)),
               fixed_component_seed(master_seed, UINT32_C(0x00a17eed), UINT32_C(0x4157474e)));
    } else {
        printf("║  RNG master seed: %-10u  (DL=%-10u UL=%-10u) ║\n",
               master_seed, master_seed ^ UINT32_C(0xD1A5EED),
               master_seed ^ UINT32_C(0xA17EED));
    }
    printf("╠═════════════════════════════════════════════════════════╣\n");
    printf("╚═════════════════════════════════════════════════════════╝\n\n");
    printf("DL endpoints: upstream %s → downstream %s\n", dl_connect, dl_bind);
    printf("UL endpoints: upstream %s → downstream %s\n", ul_connect, ul_bind);

    if (signal(SIGINT, sig_handler) == SIG_ERR ||
        signal(SIGTERM, sig_handler) == SIG_ERR ||
        signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        fprintf(stderr, "FATAL: cannot install signal handlers: %s\n",
                strerror(errno));
        rb_dispose_loaded(schedule);
        return EXIT_FAILURE;
    }

    if (schedule && rb_start(schedule) != 0) {
        rb_error(schedule, "schedule_startup"); rb_finish(schedule); return EXIT_FAILURE;
    }

    void *ctx = zmq_ctx_new();
    if (ctx == NULL) {
        fprintf(stderr, "FATAL: cannot create ZeroMQ context: %s\n",
                zmq_strerror(zmq_errno()));
        rb_error(schedule, "context_create"); rb_finish(schedule);
        return EXIT_FAILURE;
    }

    channel_args_t dl_args = {
        .name                 = "DL",
        .rep_bind_addr        = dl_bind,       /* UE connects here by default */
        .req_connect_addr     = dl_connect,    /* gNB binds here by default   */
        .snr_db               = dl_snr_db,
        .fading_enabled       = fading,
        .doppler_hz           = dl_doppler,
        .sample_rate          = srate,
        .zmq_ctx              = ctx,
        .rng_seed             = (unsigned int)(master_seed ^ UINT32_C(0xD1A5EED)),
        .k_factor_db          = k_factor_db,
        .interference_enabled = intf_enabled,
        .interference_freq    = intf_freq,
        .sir_linear           = sir_linear,
        .print_power          = print_power,
        .identity_mode        = identity_mode,
    };

    channel_args_t ul_args = {
        .name                 = "UL",
        .rep_bind_addr        = ul_bind,       /* gNB connects here by default */
        .req_connect_addr     = ul_connect,    /* UE binds here by default     */
        .snr_db               = ul_snr_db,
        .fading_enabled       = fading,
        .doppler_hz           = ul_doppler,
        .sample_rate          = srate,
        .zmq_ctx              = ctx,
        .rng_seed             = (unsigned int)(master_seed ^ UINT32_C(0xA17EED)),
        .k_factor_db          = k_factor_db,
        .interference_enabled = 0,   /* no interference on UL */
        .interference_freq    = 0.0f,
        .sir_linear           = 1e30f,
        .print_power          = print_power,
        .identity_mode        = identity_mode,
    };
    dl_args.schedule = ul_args.schedule = schedule;
    ul_args.schedule_direction = 1;
    if (fixed_enabled) {
        dl_args.fixed_enabled = ul_args.fixed_enabled = 1;
        dl_args.identity_mode = fixed_configs[0].identity;
        ul_args.identity_mode = fixed_configs[1].identity;
        if (fixed_init(&dl_args.fixed, &fixed_configs[0], master_seed, UINT32_C(0x0d1a5eed)) != 0 ||
            fixed_init(&ul_args.fixed, &fixed_configs[1], master_seed, UINT32_C(0x00a17eed)) != 0) {
            fprintf(stderr, "ERROR: invalid fixed profile\n");
            (void)zmq_ctx_destroy(ctx);
            rb_error(schedule, "fixed_initialization"); rb_finish(schedule);
            return 2;
        }
    }
    if(schedule&&schedule->metrics_every){
        dl_args.fixed.metrics=&schedule->metrics[0];ul_args.fixed.metrics=&schedule->metrics[1];
    }
    atomic_init(&dl_args.startup_state, CHANNEL_START_PENDING);
    atomic_init(&ul_args.startup_state, CHANNEL_START_PENDING);

    pthread_t dl_thread, ul_thread;
    int dl_thread_started = 0;
    int ul_thread_started = 0;
    int thread_error = pthread_create(&dl_thread, NULL, channel_thread, &dl_args);
    if (thread_error != 0) {
        rb_error(schedule, "relay_thread_create");
        fprintf(stderr, "FATAL: cannot create DL relay thread: %s\n",
                strerror(thread_error));
        atomic_store_explicit(&fatal_error, 1, memory_order_release);
    } else {
        dl_thread_started = 1;
    }
    if (atomic_load_explicit(&fatal_error, memory_order_acquire) == 0) {
        thread_error = pthread_create(&ul_thread, NULL, channel_thread, &ul_args);
        if (thread_error != 0) {
            rb_error(schedule, "relay_thread_create");
            fprintf(stderr, "FATAL: cannot create UL relay thread: %s\n",
                    strerror(thread_error));
            atomic_store_explicit(&fatal_error, 1, memory_order_release);
        } else {
            ul_thread_started = 1;
        }
    }

    if (dl_thread_started && ul_thread_started) {
        uint64_t startup_mono = 0, startup_wall = 0;
        if (schedule && rb_clock(&startup_mono, &startup_wall)) rb_error(schedule, "clock_failure");
        struct timespec startup_poll = {.tv_sec = 0, .tv_nsec = 10000000L};
        while (broker_should_run()) {
            if (schedule) {
                uint64_t now_mono, now_wall;
                if (rb_clock(&now_mono, &now_wall) || now_mono - startup_mono > UINT64_C(10000000000)) {
                    rb_error(schedule, "relay_startup_timeout"); break;
                }
            }
            int dl_state = atomic_load_explicit(&dl_args.startup_state,
                                                memory_order_acquire);
            int ul_state = atomic_load_explicit(&ul_args.startup_state,
                                                memory_order_acquire);
            if (dl_state == CHANNEL_START_READY &&
                ul_state == CHANNEL_START_READY) {
                if (schedule && rb_ready(schedule) != 0) { rb_error(schedule, "ready_publication"); break; }
                printf("Broker running — both relay paths ready; Ctrl+C to stop\n\n");
                fflush(stdout);
                break;
            }
            if (dl_state == CHANNEL_START_FAILED ||
                ul_state == CHANNEL_START_FAILED) {
                atomic_store_explicit(&fatal_error, 1, memory_order_release);
                break;
            }
            if (nanosleep(&startup_poll, NULL) != 0 && errno != EINTR) {
                fprintf(stderr, "FATAL: startup wait failed: %s\n",
                        strerror(errno));
                atomic_store_explicit(&fatal_error, 1, memory_order_release);
                break;
            }
        }
    }

    if (schedule) {
        while (broker_should_run()) rb_control_step(schedule);
    }

    if (dl_thread_started) {
        thread_error = schedule ? rb_join(dl_thread, 5) : pthread_join(dl_thread, NULL);
        if (thread_error != 0) {
            if (schedule) { fprintf(stderr, "FATAL: bounded DL join failed\n"); _Exit(EXIT_FAILURE); }
            fprintf(stderr, "FATAL: cannot join DL relay thread: %s\n",
                    strerror(thread_error));
            atomic_store_explicit(&fatal_error, 1, memory_order_release);
        }
    }
    if (ul_thread_started) {
        thread_error = schedule ? rb_join(ul_thread, 5) : pthread_join(ul_thread, NULL);
        if (thread_error != 0) {
            if (schedule) { fprintf(stderr, "FATAL: bounded UL join failed\n"); _Exit(EXIT_FAILURE); }
            fprintf(stderr, "FATAL: cannot join UL relay thread: %s\n",
                    strerror(thread_error));
            atomic_store_explicit(&fatal_error, 1, memory_order_release);
        }
    }

    if (!dl_thread_started) print_channel_accounting(&dl_args);
    if (!ul_thread_started) print_channel_accounting(&ul_args);
    if (zmq_ctx_destroy(ctx) != 0) {
        rb_error(schedule, "context_destroy");
        fprintf(stderr, "FATAL: cannot destroy ZeroMQ context: %s\n",
                zmq_strerror(zmq_errno()));
        atomic_store_explicit(&fatal_error, 1, memory_order_release);
    }
    rb_finish(schedule);
    if (atomic_load_explicit(&fatal_error, memory_order_acquire) != 0) {
        fprintf(stderr, "\nBroker stopped after a relay failure.\n");
        return EXIT_FAILURE;
    }
    printf("\nBroker shut down.\n");
    return EXIT_SUCCESS;
}
