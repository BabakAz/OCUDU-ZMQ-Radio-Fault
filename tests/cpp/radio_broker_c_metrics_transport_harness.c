/* L1: actual production channel_thread and core, finite stubbed ZMQ transport.
 * A deterministic monotonic clock attributes synthetic API/materialization
 * elapsed costs. It establishes timing boundaries, not measured performance. */
#define main ocudu_broker_program_main
#include "../../scripts/zmq_channel_broker.c"
#undef main
static int rep_socket,req_socket,error_number,message_iq,fail_clock,clock_reads;
static int forwarded,received,closed;static const char *mode;
static uint64_t virtual_ns=100,hash=UINT64_C(14695981039346656037);
static size_t message_size;static unsigned char request;static float samples[10];static void *message_data;
int clock_gettime(clockid_t id,struct timespec *value){
    clock_reads++;if(fail_clock){fail_clock=0;errno=EIO;return -1;}
    value->tv_sec=id==CLOCK_REALTIME?1700000000:0;value->tv_nsec=(long)virtual_ns++;return 0;
}
void *zmq_socket(void *ctx,int type){(void)ctx;return type==ZMQ_REP?&rep_socket:&req_socket;}
int zmq_setsockopt(void *socket,int option,const void *value,size_t size){(void)socket;(void)option;(void)value;(void)size;return 0;}
int zmq_bind(void *socket,const char *address){(void)socket;(void)address;return 0;}
int zmq_connect(void *socket,const char *address){(void)socket;(void)address;return 0;}
int zmq_close(void *socket){(void)socket;closed++;return 0;}
int zmq_msg_init(zmq_msg_t *message){(void)message;return 0;}
int zmq_msg_close(zmq_msg_t *message){(void)message;virtual_ns+=5;
    if(message_iq&&!strcmp(mode,"copy-error")){error_number=EIO;return -1;}return 0;}
int zmq_msg_recv(zmq_msg_t *message,void *socket,int flags){(void)message;(void)flags;
    message_iq=socket==&req_socket;
    if(!strcmp(mode,"sibling-stop")){atomic_store(&fatal_error,1);error_number=EAGAIN;return -1;}
    if(!message_iq){virtual_ns+=7;message_size=1;message_data=&request;}
    else {virtual_ns+=13;size_t sizes[]={0,3,5};size_t n=sizes[received%3];
        if(strcmp(mode,"enabled")&&strcmp(mode,"disabled")&&strcmp(mode,"power-identity"))n=3;
        for(size_t i=0;i<2*n;i++)samples[i]=i%2?0:1;
        if(!strcmp(mode,"invalid-iq"))samples[0]=NAN;
        if(!strcmp(mode,"power-identity")&&n){samples[0]=3;samples[1]=4;}
        message_size=n*8;message_data=samples;received++;}
    return (int)message_size;
}
int zmq_msg_more(const zmq_msg_t *message){(void)message;return 0;}
size_t zmq_msg_size(const zmq_msg_t *message){(void)message;if(message_iq)virtual_ns+=19;return message_size;}
void *zmq_msg_data(zmq_msg_t *message){(void)message;if(message_iq)virtual_ns+=17;return message_data;}
int zmq_send(void *socket,const void *data,size_t size,int flags){(void)flags;
    if(socket==&req_socket){virtual_ns+=11;return (int)size;}
    virtual_ns+=23;
    if(!strcmp(mode,"send-stop")){stop_requested=1;error_number=EAGAIN;return -1;}
    forwarded++;const unsigned char *p=data;for(size_t i=0;i<size;i++){hash^=p[i];hash*=UINT64_C(1099511628211);}
    if(!strcmp(mode,"send-clock-error")){fail_clock=1;stop_requested=1;}
    if(forwarded==3)stop_requested=1;
    return (int)size;
}
int zmq_errno(void){return error_number;}
const char *zmq_strerror(int code){return strerror(code);}
static int capture(void *ctx,size_t direction,const char *type,uint64_t start,uint64_t end,const char *details){(void)ctx;(void)direction;
    printf("METRIC: {\"event_type\":\"%s\",\"sample_start\":%" PRIu64 ",\"sample_end\":%" PRIu64 ",\"details\":%s}\n",type,start,end,details);return 0;}
int main(int argc,char **argv){
    if(argc!=2)return 2;mode=argv[1];
    rb_runtime_t *runtime=calloc(1,sizeof(*runtime));if(!runtime)return 3;
    pthread_mutex_init(&runtime->state_lock,NULL);pthread_mutex_init(&runtime->queue_lock,NULL);pthread_cond_init(&runtime->queue_cond,NULL);
    channel_args_t ch={.name="DL",.rep_bind_addr="stub",.req_connect_addr="stub",.sample_rate=23040000,.fixed_enabled=1,.schedule=runtime};
    fixed_config_t config=fixed_config_defaults();config.ref_power=1;config.cw_enabled=1;config.cw_freq_hz=1000;
    if(fixed_init(&ch.fixed,&config,41,0x0d1a5eed))return 3;
    rm_metrics_t metrics;rm_init(&metrics,2,capture,NULL,0);
    int enabled=strcmp(mode,"disabled")&&strcmp(mode,"power-identity");
    if(enabled)ch.fixed.metrics=&metrics;
    if(!strcmp(mode,"power-identity")){ch.fixed_enabled=0;ch.identity_mode=1;ch.schedule=NULL;ch.print_power=1;}
    atomic_init(&ch.startup_state,CHANNEL_START_PENDING);channel_thread(&ch);
    if(enabled){rm_frontier_t f=channel_frontier(&ch);if(rm_final(&metrics,&f,atomic_load(&fatal_error),0))return 4;}
    printf("TRANSPORT: {\"forwarded\":%d,\"output_messages\":%" PRIu64 ",\"output_samples\":%" PRIu64 ",\"clock_reads\":%d,\"closed\":%d,\"fatal\":%d,\"output_hash\":\"%016" PRIx64 "\",\"draws\":%" PRIu64 ",\"phase\":%" PRIu64 "}\n",
        forwarded,ch.output_messages,ch.output_samples,clock_reads,closed,atomic_load(&fatal_error),hash,ch.fixed.awgn_normal_draws,ch.fixed.phase_u64);
    pthread_cond_destroy(&runtime->queue_cond);pthread_mutex_destroy(&runtime->queue_lock);pthread_mutex_destroy(&runtime->state_lock);free(runtime);return 0;
}
