/* L1 lifecycle regression using actual channel_thread with finite stub I/O.
 * No native control socket or ZeroMQ context/socket is constructed. */
#define main ocudu_broker_program_main
#include "../../scripts/zmq_channel_broker.c"
#undef main
static int rep_stub,req_stub,fixture_error,forwarded,closed,close_failure,invalid_input,start_failure,create_calls;
static float samples[22];static unsigned char request;static void *message_data;static size_t message_size;
void *zmq_socket(void *ctx,int type){(void)ctx;return type==ZMQ_REP?&rep_stub:&req_stub;}
int zmq_setsockopt(void *socket,int option,const void *value,size_t size){(void)socket;(void)option;(void)value;(void)size;return 0;}
int zmq_bind(void *socket,const char *endpoint){(void)socket;(void)endpoint;return 0;}
int zmq_connect(void *socket,const char *endpoint){(void)socket;(void)endpoint;return 0;}
int zmq_close(void *socket){(void)socket;closed++;if(close_failure){fixture_error=EIO;return -1;}return 0;}
int zmq_msg_init(zmq_msg_t *message){(void)message;return 0;}
int zmq_msg_close(zmq_msg_t *message){(void)message;return 0;}
int zmq_msg_recv(zmq_msg_t *message,void *socket,int flags){(void)message;(void)flags;
    if(start_failure){struct timespec pause={.tv_nsec=1000000};nanosleep(&pause,NULL);fixture_error=EAGAIN;return -1;}
    if(socket==&rep_stub){message_data=&request;message_size=1;}else{if(invalid_input)samples[0]=NAN;message_data=samples;message_size=sizeof(samples);}return (int)message_size;}
int zmq_msg_more(const zmq_msg_t *message){(void)message;return 0;}
size_t zmq_msg_size(const zmq_msg_t *message){(void)message;return message_size;}
void *zmq_msg_data(zmq_msg_t *message){(void)message;return message_data;}
int zmq_send(void *socket,const void *data,size_t size,int flags){(void)data;(void)flags;if(socket==&rep_stub){forwarded++;stop_requested=1;}return (int)size;}
int zmq_errno(void){return fixture_error;}
const char *zmq_strerror(int error){return strerror(error);}
void *zmq_ctx_new(void){return &rep_stub;}
int zmq_ctx_destroy(void *context){(void)context;return 0;}
int __real_pthread_create(pthread_t *,const pthread_attr_t *,void *(*)(void *),void *);
int __wrap_pthread_create(pthread_t *thread,const pthread_attr_t *attr,void *(*entry)(void *),void *arg){
    if(start_failure&&++create_calls==2)return EAGAIN;
    return __real_pthread_create(thread,attr,entry,arg);
}
int main(int argc,char **argv){
    if(argc!=2)return 2;
    if(!strcmp(argv[1],"second-thread-failure")){
        start_failure=1;
        char *args[]={"broker","--identity"};
        return ocudu_broker_program_main(2,args)==EXIT_FAILURE?0:3;
    }
    channel_args_t ch={.name="DL",.identity_mode=1,.sample_rate=23040000,.rep_bind_addr="stub",.req_connect_addr="stub"};
    atomic_init(&ch.startup_state,CHANNEL_START_PENDING);
    if(!strcmp(argv[1],"sample-overflow"))ch.output_samples=UINT64_MAX-5;
    else if(!strcmp(argv[1],"message-overflow"))ch.output_messages=UINT64_MAX;
    else if(!strcmp(argv[1],"close-after-stop"))close_failure=1;
    else if(!strcmp(argv[1],"close-after-fatal")){close_failure=1;invalid_input=1;}
    else return 2;
    channel_thread(&ch);
    printf("LIFECYCLE: {\"forwarded\":%d,\"closed\":%d,\"errors\":%" PRIu64 ",\"fatal\":%d,\"transport\":\"L1_stub\"}\n",forwarded,closed,ch.error_count,atomic_load(&fatal_error));
    return 0;
}
