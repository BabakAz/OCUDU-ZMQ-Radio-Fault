/* Bounded L1 file fixture invoking the production fixed_profile core.
 * No transport calls, context, socket, or NR stack are started. */
#define main ocudu_broker_program_main
#include "../../scripts/zmq_channel_broker.c"
#undef main

int main(int argc, char **argv) {
    if (argc != 17) return 2;
    FILE *input = fopen(argv[1], "rb");
    if (input == NULL || fseek(input, 0, SEEK_END) != 0) return 3;
    long bytes = ftell(input);
    if (bytes < 0 || (unsigned long)bytes > MAX_MESSAGE_SIZE || bytes % 8 != 0 ||
        fseek(input, 0, SEEK_SET) != 0) return 4;
    float *iq = malloc((size_t)bytes + 8);
    if (iq == NULL || fread(iq, 1, (size_t)bytes, input) != (size_t)bytes) return 5;
    fclose(input);
    fixed_config_t config = fixed_config_defaults();
    config.ref_power = strtod(argv[5], NULL);
    config.gain = strtod(argv[6], NULL);
    config.noise_snr_db = strtod(argv[7], NULL);
    config.noise_off = atoi(argv[8]);
    config.cw_enabled = atoi(argv[9]);
    config.cw_sir_db = strtod(argv[10], NULL);
    config.cw_freq_hz = strtod(argv[11], NULL);
    config.sample_rate_hz = strtod(argv[12], NULL);
    config.identity = atoi(argv[13]);
    fixed_state_t state;
    const char *direction = argv[3];
    uint32_t tag = strcmp(direction, "DL") == 0 ? UINT32_C(0x0d1a5eed) : UINT32_C(0x00a17eed);
    if (fixed_init(&state, &config, (uint32_t)strtoul(argv[4], NULL, 10), tag) != 0) return 6;
    if (strcmp(argv[16], "sample-overflow") == 0) state.sample_clock = UINT64_MAX;
    if (strcmp(argv[16], "draw-overflow") == 0) state.awgn_normal_draws = UINT64_MAX - 1;
    int legacy_cw = strncmp(argv[16], "legacy-cw", 9) == 0;
    interference_state_t legacy;
    interference_init(&legacy, config.cw_enabled, (float)config.cw_freq_hz,
                      (float)pow(10.0, config.cw_sir_db / 10), (float)config.sample_rate_hz);
    if (!legacy_cw) fixed_record(&state, direction, "started");
    size_t total = (size_t)bytes / 8, offset = 0, part = 0;
    size_t chunk = (size_t)strtoul(argv[14], NULL, 10);
    size_t switch_at = (size_t)strtoul(argv[15], NULL, 10);
    static const size_t irregular[] = {1, 2, 3, 257, 1024, 4096, 17};
    int status = 0;
    while (offset < total) {
        size_t count = chunk == 0 ? total : chunk == 1 ? irregular[part++ % 7] : chunk;
        if (count > total - offset) count = total - offset;
        if (switch_at > offset && switch_at < offset + count) count = switch_at - offset;
        if (switch_at && offset == switch_at) {
            if (strcmp(argv[16], "noise-on") == 0) state.config.noise_off = 0;
            if (strcmp(argv[16], "cw-on") == 0) state.config.cw_enabled = 1;
            if (strcmp(argv[16], "legacy-cw-on") == 0) legacy.enabled = 1;
        }
        if (legacy_cw) interference_apply(&legacy, iq + 2 * offset, (int)(2 * count));
        else if (fixed_process(&state, iq + 2 * offset, count) != 0) { status = 7; break; }
        offset += count;
    }
    if (legacy_cw) {
        printf("LEGACY_CW_FIXTURE: {\"scope\":\"L1_file_fixture\",\"cw_clock_version\":\"u64_nco_v1\","
               "\"phase_u64\":%" PRIu64 ",\"step_u64\":%" PRIu64 "}\n",
               legacy.phase_u64, legacy.phase_step_u64);
    } else fixed_record(&state, direction, "final");
    FILE *output = fopen(argv[2], "wb");
    if (output == NULL || fwrite(iq, 1, (size_t)bytes, output) != (size_t)bytes ||
        fclose(output) != 0) status = 8;
    free(iq);
    return status;
}
