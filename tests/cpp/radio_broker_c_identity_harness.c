/* L1 production relay fixture: every ZeroMQ transport operation is replaced
 * below with bounded in-memory I/O. No socket, peer, or NR stack is started.
 * The production channel_thread and DSP implementation execute unmodified. */
#define _GNU_SOURCE
#include <zmq.h>
#include <stddef.h>
#include <stdint.h>

#ifndef RADIO_BROKER_SOURCE
#define RADIO_BROKER_SOURCE "../../scripts/zmq_channel_broker.c"
#endif
#define main ocudu_broker_program_main
#include RADIO_BROKER_SOURCE
#undef main

typedef struct {
    int type;
    int closed;
} fixture_socket_t;

static fixture_socket_t fixture_rep, fixture_req;
static int fixture_errno;
static const char *fixture_mode;
static size_t fixture_limit;
static size_t fixture_received;
static size_t fixture_forwarded;
static size_t fixture_sample_offset;
static size_t fixture_forwarded_samples;
static size_t fixture_message_size;
static int fixture_more;
static const void *fixture_message_data;
static uint8_t fixture_request = 0;
static float fixture_iq[8192];
static uint8_t fixture_original[sizeof(fixture_iq)];
static uint64_t fixture_output_hash = UINT64_C(14695981039346656037);
static int fixture_identity_mismatch;
static int fixture_request_mismatch;

static int mode_is(const char *value) {
    return strcmp(fixture_mode, value) == 0;
}

static size_t fixture_samples_in_message(void) {
    static const size_t irregular[] = {1, 3, 17, 257, 1024, 2048, 746};
    if (mode_is("counter-baseline")) return 11;
    if (mode_is("empty")) return 0;
    if (mode_is("identity-irregular")) return irregular[fixture_received];
    if (mode_is("identity-fixed")) return fixture_received == 15 ? 241 : 257;
    if (mode_is("identity-contiguous")) return 4096;
    return 11;
}

static void make_fixture_iq(size_t count) {
    static const uint32_t finite_bits[] = {
        UINT32_C(0x00000000), UINT32_C(0x80000000), /* signed zero */
        UINT32_C(0x00000001), UINT32_C(0x80000001), /* subnormal */
        UINT32_C(0x3f800000), UINT32_C(0xbf800000),
        UINT32_C(0x3e800000), UINT32_C(0xbe800000),
        UINT32_C(0x7f7fffff), UINT32_C(0xff7fffff), /* finite extrema */
    };
    for (size_t i = 0; i < count * 2; i++) {
        if (mode_is("identity-zeros")) {
            fixture_iq[i] = 0.0f;
        } else if (strncmp(fixture_mode, "identity-", 9) == 0) {
            uint32_t bits = finite_bits[(fixture_sample_offset * 2 + i) %
                                       (sizeof(finite_bits) / sizeof(finite_bits[0]))];
            memcpy(&fixture_iq[i], &bits, sizeof(bits));
        } else if (mode_is("counter-baseline") || mode_is("zeros")) {
            fixture_iq[i] = 0.0f;
        } else if (mode_is("overflow-output")) {
            fixture_iq[i] = 1e20f;
        } else {
            fixture_iq[i] = i % 2 == 0 ? 1.0f : 0.0f;
        }
    }
    if (mode_is("nan")) fixture_iq[0] = NAN;
    if (mode_is("infinity")) fixture_iq[1] = INFINITY;
    memcpy(fixture_original, fixture_iq, count * 2 * sizeof(float));
}

/* These definitions resolve the production translation unit's actual ZMQ
 * calls. Linking libzmq only supplies unused entry-point references; no real
 * ZMQ context or socket constructor is called by this fixture. */
void *zmq_socket(void *context, int type) {
    (void)context;
    fixture_socket_t *socket = type == ZMQ_REP ? &fixture_rep : &fixture_req;
    socket->type = type;
    socket->closed = 0;
    return socket;
}

int zmq_setsockopt(void *socket, int option, const void *value, size_t size) {
    (void)socket; (void)option; (void)value; (void)size;
    return 0;
}

int zmq_bind(void *socket, const char *endpoint) {
    (void)socket; (void)endpoint;
    return 0;
}

int zmq_connect(void *socket, const char *endpoint) {
    (void)socket; (void)endpoint;
    return 0;
}

int zmq_close(void *socket) {
    ((fixture_socket_t *)socket)->closed = 1;
    return 0;
}

int zmq_msg_init(zmq_msg_t *message) {
    (void)message;
    return 0;
}

int zmq_msg_close(zmq_msg_t *message) {
    (void)message;
    return 0;
}

int zmq_msg_recv(zmq_msg_t *message, void *socket, int flags) {
    (void)message; (void)flags;
    fixture_more = 0;
    if (socket == &fixture_rep) {
        fixture_message_data = &fixture_request;
        fixture_message_size = sizeof(fixture_request);
        fixture_more = mode_is("multipart-request");
    } else {
        size_t count = fixture_samples_in_message();
        make_fixture_iq(count);
        fixture_message_data = fixture_iq;
        fixture_message_size = count * 2 * sizeof(float);
        if (mode_is("misaligned")) fixture_message_size -= sizeof(float);
        if (mode_is("oversized")) fixture_message_size = MAX_MESSAGE_SIZE + 1U;
        fixture_more = mode_is("multipart-iq");
        fixture_received++;
        fixture_sample_offset += count;
    }
    return (int)fixture_message_size;
}

size_t zmq_msg_size(const zmq_msg_t *message) {
    (void)message;
    return fixture_message_size;
}

void *zmq_msg_data(zmq_msg_t *message) {
    (void)message;
    return (void *)fixture_message_data;
}

int zmq_msg_more(const zmq_msg_t *message) {
    (void)message;
    return fixture_more;
}

int zmq_send(void *socket, const void *buffer, size_t size, int flags) {
    (void)flags;
    if (socket == &fixture_req) {
        if (size != 1 || memcmp(buffer, &fixture_request, 1) != 0) {
            fixture_request_mismatch = 1;
        }
        return (int)size;
    }
    if (mode_is("failed-send")) {
        fixture_errno = EIO;
        return -1;
    }
    if (mode_is("interrupted-send") || mode_is("sibling-failed-send")) {
        if (mode_is("sibling-failed-send")) {
            atomic_store_explicit(&fatal_error, 1, memory_order_release);
        } else {
            stop_requested = 1;
        }
        fixture_errno = EAGAIN;
        return -1;
    }
    if (memcmp(buffer, fixture_original, size) != 0) fixture_identity_mismatch = 1;
    const uint8_t *bytes = buffer;
    for (size_t i = 0; i < size; i++) {
        fixture_output_hash ^= bytes[i];
        fixture_output_hash *= UINT64_C(1099511628211);
    }
    fixture_forwarded++;
    fixture_forwarded_samples += size / (2 * sizeof(float));
    if (fixture_forwarded >= fixture_limit) stop_requested = 1;
    return (int)size;
}

int zmq_errno(void) {
    return fixture_errno;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    fixture_mode = argv[1];
    fixture_limit = mode_is("counter-baseline") ? 10000 : 1;
    if (mode_is("identity-irregular")) fixture_limit = 7;
    if (mode_is("identity-fixed")) fixture_limit = 16;
    atomic_init(&fatal_error, 0);
    stop_requested = 0;
    channel_args_t channel = {
        .name = "DL",
        .rep_bind_addr = "fixture-only-rep",
        .req_connect_addr = "fixture-only-req",
        .snr_db = 20.0f,
        .doppler_hz = 5.0f,
        .k_factor_db = 3.0f,
        .sample_rate = 23040000.0f,
        .rng_seed = 424242,
        .sir_linear = 100.0f,
        .fading_enabled = mode_is("legacy-fading-cw"),
        .interference_enabled = mode_is("legacy-fading-cw"),
        .interference_freq = 1000000.0f,
    };
#ifdef RADIO_BROKER_ACCOUNTING_SCHEMA
    _Static_assert(sizeof(channel.input_messages) == 8, "64-bit message counter");
    _Static_assert(sizeof(channel.input_samples) == 8, "64-bit sample counter");
    channel.identity_mode = !mode_is("counter-baseline") && !mode_is("zeros") &&
                            strncmp(fixture_mode, "legacy-", 7) != 0 &&
                            !mode_is("overflow-output");
    /* Identity must override all transforms, including initialization state. */
    if (channel.identity_mode) {
        channel.fading_enabled = 1;
        channel.interference_enabled = 1;
        channel.interference_freq = 1000000.0f;
    }
#endif
    atomic_init(&channel.startup_state, CHANNEL_START_PENDING);
    channel_thread(&channel);
    printf("HARNESS_RESULT: {\"transport\":\"stubbed-in-memory\","
           "\"received_iq_messages\":%zu,\"forwarded_messages\":%zu,"
           "\"forwarded_samples\":%zu,\"byte_mismatch\":%d,"
           "\"request_mismatch\":%d,\"sockets_closed\":%d,"
           "\"fatal\":%d,\"output_hash_fnv1a64\":\"%016llx\"}\n",
           fixture_received, fixture_forwarded, fixture_forwarded_samples,
           fixture_identity_mismatch, fixture_request_mismatch,
           fixture_rep.closed && fixture_req.closed,
           atomic_load_explicit(&fatal_error, memory_order_relaxed),
           (unsigned long long)fixture_output_hash);
    return 0;
}
