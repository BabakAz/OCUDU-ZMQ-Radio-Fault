/* L1 deterministic native timing/core/writer fixtures. No radio/control/ZMQ
 * sockets are opened. Synthetic timestamps describe accounting, not CPU cost. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>

/* These fixtures exercise record accounting and writer error handling, not
 * filesystem durability latency. Keep descriptor validation and real writes/
 * closes; substitute only fsync, before including the production writer. */
static int fixture_fsync_failure;
static unsigned fixture_fsync_calls, fixture_fsync_errors;
static int fixture_fsync(int fd) {
    fixture_fsync_calls++;
    if(fcntl(fd,F_GETFD)<0){fixture_fsync_errors++;return -1;}
    if(fixture_fsync_failure){fixture_fsync_errors++;errno=EIO;return -1;}
    return 0;
}
#define fsync fixture_fsync
#define main ocudu_broker_program_main
#include "../../scripts/zmq_channel_broker.c"
#undef main
#undef fsync

static uint_fast64_t fixture_logging_errors;
static void *fixture_writer(void *arg) {
    void *result=rb_writer(arg);
    /* rb_finish joins this thread before freeing the runtime. */
    fixture_logging_errors=atomic_load(&((rb_runtime_t *)arg)->logging_errors);
    return result;
}

static int capture(void *context,size_t direction,const char *type,uint64_t start,uint64_t end,const char *details) {
    (void)context;
    printf("METRIC: {\"direction\":\"%s\",\"event_type\":\"%s\",\"sample_start\":%" PRIu64 ",\"sample_end\":%" PRIu64 ",\"details\":%s}\n",
           direction?"UL":"DL",type,start,end,details);return 0;
}
static uint64_t send_delay;
static int complete(rm_metrics_t *m,rm_frontier_t *f,uint64_t start,uint64_t samples,int mask) {
    if(rm_phase_begin(m,RM_REQUEST_RECEIVE,start)||rm_transition(m,RM_REQUEST_SEND,start+10)||
       rm_transition(m,RM_UPSTREAM_RECEIVE,start+20)||rm_transition(m,RM_PROCESSING,start+30))return -1;
    f->input_messages++;f->input_samples+=samples;
    if(rm_input(m,samples)||rm_part_next(m,RM_CHANNEL_CHAIN,start+40))return -1;
    if(samples&&(rm_core_begin(m,start+45)||rm_core_end(m,start+55,RM_COMPLETED)))return -1;
    f->processed_samples+=samples;f->energies[0]+=samples;f->energies[mask?2:1]+=samples;f->energies[4]+=samples;
    if(mask)f->masked_samples+=samples;
    if(rm_part_next(m,RM_OUTPUT_PREPARE,start+60)||rm_phase_end(m,start+70,RM_COMPLETED,samples)||
       rm_phase_begin(m,RM_DOWNSTREAM_SEND,start+70)||rm_phase_end(m,start+80+send_delay,RM_COMPLETED,0))return -1;
    f->output_messages++;f->output_samples+=samples;
    return rm_maybe_flush(m,start+80+send_delay,f);
}
static int writer_fixture(const char *directory,const char *mode) {
    fixture_fsync_failure=!strcmp(mode,"fsync-error");
    fixed_config_t configs[2]={fixed_config_defaults(),fixed_config_defaults()};
    for(size_t d=0;d<2;d++)configs[d].ref_power=1;
    configs[0].noise_snr_db=20;configs[0].cw_sir_db=10;configs[0].cw_freq_hz=1440000;
    char plan[PATH_MAX];snprintf(plan,sizeof(plan),"%s/plan.wire",directory);
    rb_runtime_t *r=rb_load(plan,directory,configs,41);if(!r||rb_metrics_enable(r,2))return 3;
    strcpy(r->instance,"11111111111111111111111111111111");if(rb_hash_executable(r->build_hash))return 3;
    r->truth_fd=openat(r->directory_fd,"broker_events.jsonl",O_WRONLY|O_CREAT|O_EXCL,0600);
    r->metrics_fd=openat(r->directory_fd,"broker_metrics.jsonl",O_WRONLY|O_CREAT|O_EXCL,0600);
    if(r->truth_fd<0||r->metrics_fd<0||pthread_create(&r->writer,NULL,fixture_writer,r))return 3;r->writer_started=1;
    if(rb_emit(r,"control","started","control",0,0,"{\"layer\":\"L1_file_writer_fixture\"}"))return 3;
    if(!strcmp(mode,"writer-error"))close(r->metrics_fd);
    if(!strcmp(mode,"metrics-budget")){pthread_mutex_lock(&r->queue_lock);r->metrics_bytes=RM_FILE_MAX;pthread_mutex_unlock(&r->queue_lock);}
    char details[10000];memset(details,'b',sizeof(details));memcpy(details,"{\"padding\":\"",12);details[9000]='"';details[9001]='}';details[9002]=0;
    int emitted=rb_metrics_emit(r,2,"started",0,0,details);
    rb_finish(r);
    printf("WRITER: {\"fatal_error\":%d,\"logging_errors\":%" PRIuFAST64 ",\"fsync_calls\":%u,\"fsync_errors\":%u}\n",
           atomic_load(&fatal_error),fixture_logging_errors,fixture_fsync_calls,fixture_fsync_errors);
    if(!strcmp(mode,"writer"))return emitted||atomic_load(&fatal_error)?4:0;
    return atomic_load(&fatal_error)?0:4;
}
int main(int argc,char **argv) {
    if(argc==3)return writer_fixture(argv[2],argv[1]);
    if(argc!=2)return 2;const char *mode=argv[1];
    if(!strcmp(mode,"power")) {
        power_display_t p={.started=1,.start_ns=100};float iq[]={3,4,0,0,0,0,0,0};
        if(power_accumulate(&p,iq,4)||power_report(&p,"DL",200,1))return 3;
        if(power_report(&p,"DL",300,1))return 3;
        if(power_accumulate(&p,iq+2,3)||power_report(&p,"DL",UINT64_C(1000000400),0))return 3;
        p.samples=UINT64_MAX;if(power_accumulate(&p,iq,1)==0||power_report(&p,"DL",0,1)==0)return 3;return 0;
    }
    if(!strcmp(mode,"timespec")) {
        uint64_t value=0;struct timespec t={.tv_sec=(time_t)(UINT64_MAX/UINT64_C(1000000000)),.tv_nsec=(long)(UINT64_MAX%UINT64_C(1000000000))};
        if(rm_timespec_ns(&t,&value)||value!=UINT64_MAX)return 3;
        t.tv_nsec++;if(rm_timespec_ns(&t,&value)==0)return 3;
        t.tv_sec=0;t.tv_nsec=-1;if(rm_timespec_ns(&t,&value)==0)return 3;
        t.tv_nsec=1000000000L;if(rm_timespec_ns(&t,&value)==0)return 3;
        t.tv_nsec=0;t.tv_sec=-1;if(rm_timespec_ns(&t,&value)==0)return 3;
        puts("CHECKED_TIMESPEC: true");return 0;
    }
    if(!strcmp(mode,"bins")){uint64_t values[]={0,1,2,3,4,7,8,UINT64_C(1)<<63,UINT64_MAX};
        for(size_t i=0;i<sizeof(values)/sizeof(values[0]);i++)printf("%" PRIu64 " %u\n",values[i],rm_bin(values[i]));return 0;}
    rm_metrics_t m;rm_frontier_t f={0};rm_init(&m,2,capture,NULL,0);
    if(rm_begin(&m,100,&f))return 3;
    if(!strcmp(mode,"normal")||!strcmp(mode,"partial")||!strcmp(mode,"duplicate-close")) {
        uint64_t sizes[]={0,8,256,1};size_t count=!strcmp(mode,"partial")?3:4;uint64_t end=100;
        for(size_t i=0;i<count;i++){if(complete(&m,&f,end,sizes[i],i==2))return 3;end+=90;}
        if(rm_phase_begin(&m,RM_REQUEST_RECEIVE,end)||rm_close_window(&m,end+50,RM_STOPPED,&f))return 3;
        if(!strcmp(mode,"duplicate-close")){
            uint64_t windows=m.window_count;if(rm_close_window(&m,end+100,RM_STOPPED,&f)||m.window_count!=windows)return 3;
            if(rm_phase_begin(&m,RM_REQUEST_RECEIVE,end+100)==0)return 3;
        }
        if(rm_final(&m,&f,0,0))return 3;return 0;
    }
    if(!strcmp(mode,"slow")) {
        send_delay=UINT64_C(1000000000);
        if(complete(&m,&f,100,8,0)||rm_phase_begin(&m,RM_REQUEST_RECEIVE,send_delay+190)||
           rm_close_window(&m,send_delay+200,RM_STOPPED,&f)||rm_final(&m,&f,0,0))return 3;
        return 0;
    }
    if(!strcmp(mode,"stall")) {
        if(rm_phase_begin(&m,RM_REQUEST_RECEIVE,100)||rm_transition(&m,RM_REQUEST_SEND,110)||
           rm_transition(&m,RM_UPSTREAM_RECEIVE,120)||rm_close_window(&m,1000000120,RM_STOPPED,&f)||rm_final(&m,&f,0,0))return 3;
        return 0;
    }
    if(!strcmp(mode,"unsent")||!strcmp(mode,"processing-error")) {
        if(rm_phase_begin(&m,RM_REQUEST_RECEIVE,100)||rm_transition(&m,RM_REQUEST_SEND,110)||rm_transition(&m,RM_UPSTREAM_RECEIVE,120)||rm_transition(&m,RM_PROCESSING,130))return 3;
        f.input_messages=1;f.input_samples=8;
        if(rm_input(&m,8)||rm_part_next(&m,RM_CHANNEL_CHAIN,140)||rm_core_begin(&m,145)||rm_core_end(&m,155,RM_COMPLETED))return 3;
        if(!strcmp(mode,"processing-error")){
            f.processed_samples=4;f.energies[0]=f.energies[1]=f.energies[4]=4;
            if(rm_core_begin(&m,160)||rm_core_end(&m,170,RM_ERROR)||rm_close_window(&m,180,RM_ERROR,&f)||rm_final(&m,&f,1,0))return 3;
        }else{
            f.processed_samples=8;f.energies[0]=f.energies[1]=f.energies[4]=8;
            if(rm_part_next(&m,RM_OUTPUT_PREPARE,160)||rm_phase_end(&m,170,RM_COMPLETED,8)||rm_phase_begin(&m,RM_DOWNSTREAM_SEND,170)||rm_close_window(&m,220,RM_STOPPED,&f)||rm_final(&m,&f,0,0))return 3;
        }return 0;
    }
    if(!strcmp(mode,"regression")){if(rm_phase_begin(&m,RM_REQUEST_RECEIVE,100)||rm_phase_end(&m,99,RM_COMPLETED,0)==0)return 3;}
    else if(!strcmp(mode,"zero-window")){if(rm_flush(&m,100,1,&f)==0)return 3;}
    else if(!strcmp(mode,"overflow")){rm_stat_t s={.count=1,.sum=UINT64_MAX};if(rm_stat_add(&s,1)==0)return 3;}
    else if(!strcmp(mode,"nan-energy")){f.energies[0]=NAN;if(rm_flush(&m,101,1,&f)==0)return 3;}
    else if(!strcmp(mode,"histogram-mismatch")){f.input_messages=1;if(rm_flush(&m,101,1,&f)==0)return 3;}
    else if(!strcmp(mode,"window-budget")){m.window_count=256;if(rm_flush(&m,101,1,&f)==0)return 3;}
    else if(!strcmp(mode,"denominator")){f.input_samples=f.output_samples=f.processed_samples=1;m.current=f;m.window_closed=1;if(rm_final(&m,&f,0,0))return 3;}
    else return 2;
    puts("CHECKED_INVALID: true");return 0;
}
