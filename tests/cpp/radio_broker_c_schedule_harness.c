/* L1: production schedule/parser/control-packet/DSP code, regular-file fixture.
 * No ZMQ or AF_UNIX socket is created. Build hash is this actual fixture binary. */
#define main ocudu_broker_program_main
#include "../../scripts/zmq_channel_broker.c"
#undef main

static int fixture_writer(rb_runtime_t *r) {
    unsigned char nonce[16];if(getrandom(nonce,16,0)!=16)return -1;
    for(size_t i=0;i<16;i++)snprintf(r->instance+2*i,3,"%02x",nonce[i]);
    if(rb_hash_executable(r->build_hash))return -1;
    r->truth_fd=openat(r->directory_fd,"broker_events.jsonl",O_WRONLY|O_CREAT|O_EXCL,0600);
    if(r->truth_fd<0||pthread_create(&r->writer,NULL,rb_writer,r))return -1;r->writer_started=1;r->ready=1;
    return rb_emit(r,"control","started","control",0,0,"{\"layer\":\"L1_file_fixture_no_transport\"}");
}
static int fixture_control(rb_runtime_t *r,const char *action) {
    char packet[513],response[RB_RECORD_MAX];
    int n=snprintf(packet,sizeof(packet),"RBCTRL1 1 ARM %s %s %s\n",r->instance,r->plan_hash,r->token);
    if(!strcmp(action,"wrong-token"))packet[n-2]=packet[n-2]=='0'?'1':'0';
    int result=rb_control_packet(r,packet,(size_t)n,response);if(result<0)return -1;
    printf("CONTROL: %.*s",result,response);
    if(!strcmp(action,"retry")){char retry[RB_RECORD_MAX];int second=rb_control_packet(r,packet,(size_t)n,retry);if(second!=result||memcmp(response,retry,(size_t)result))return -1;}
    if(!strcmp(action,"rearm")){packet[8]='2';result=rb_control_packet(r,packet,(size_t)n,response);if(result>0)printf("CONTROL: %.*s",result,response);}
    return atomic_load(&fatal_error)?-1:0;
}
int main(int argc,char **argv) {
    if(argc==4&&!strcmp(argv[1],"hash")) {
        FILE *file=fopen(argv[2],"rb");if(!file)return 2;size_t chunk=(size_t)strtoul(argv[3],NULL,10);if(!chunk||chunk>65536)return 2;
        unsigned char buffer[65536];rb_sha256_t sha;rb_sha_init(&sha);size_t got;
        while((got=fread(buffer,1,chunk,file)))rb_sha_update(&sha,buffer,got);
        if(ferror(file)||fclose(file))return 3;char hex[65];rb_sha_final(&sha,hex);puts(hex);return 0;
    }
    if(argc!=8)return 2;
    const char *action=argv[7];fixed_config_t configs[2]={fixed_config_defaults(),fixed_config_defaults()};
    for(size_t d=0;d<2;d++){configs[d].ref_power=1;configs[d].sample_rate_hz=23040000;configs[d].identity=!strcmp(action,"identity");}
    configs[0].noise_snr_db=20;configs[0].cw_sir_db=10;configs[0].cw_freq_hz=1440000;
    rb_runtime_t *r=rb_load(argv[1],argv[2],configs,41);if(!r)return 3;
    if(fixture_writer(r)){rb_dispose_loaded(r);return 4;}
    if(!strcmp(action,"writer-failure")){close(r->truth_fd);rb_finish(r);return atomic_load(&fatal_error)?0:5;}
    if(!strcmp(action,"queue-overflow")){
        pthread_mutex_lock(&r->queue_lock);r->total_bytes=RB_TRUTH_MAX;pthread_mutex_unlock(&r->queue_lock);
        int result=rb_emit(r,"control","ready","control",0,0,"{}");rb_finish(r);return result<0&&atomic_load(&fatal_error)?0:5;
    }
    if(!strcmp(action,"wrong-token")||!strcmp(action,"rearm")) {int result=fixture_control(r,action);rb_finish(r);return result<0?0:5;}
    FILE *in=fopen(argv[3],"rb");if(!in||fseek(in,0,SEEK_END))return 6;long bytes=ftell(in);rewind(in);
    if(bytes<31*8||bytes>65536*8||bytes%8)return 6;float *source=malloc((size_t)bytes),*iq=malloc((size_t)bytes);
    if(!source||!iq||fread(source,1,(size_t)bytes,in)!=(size_t)bytes||fclose(in))return 6;
    fixed_state_t states[2];size_t warmup[2]={19,31};
    if(!strcmp(action,"no-warmup"))warmup[0]=warmup[1]=0;
    for(size_t d=0;d<2;d++) {
        if(fixed_init(&states[d],&configs[d],41,d?0x00a17eed:0x0d1a5eed))return 7;
        memcpy(iq,source,warmup[d]*8);if(rb_process(r,d,&states[d],iq,warmup[d]))return 7;
        if(rb_forwarded(r,d,1,warmup[d],1,warmup[d],states[d].sample_clock))return 7;
    }
    if(!strcmp(action,"arm-overflow"))states[0].sample_clock=UINT64_MAX-3;
    if(fixture_control(r,action))return 7;
    size_t partition=(size_t)strtoul(argv[5],NULL,10);size_t total=(size_t)bytes/8-31;
    if(!strcmp(action,"empty-only"))total=0;
    for(size_t d=0;d<2;d++) {
        if(rb_process(r,d,&states[d],iq,0)||r->directions[d].armed)return 7;
        memcpy(iq,source+62,total*8);size_t done=0,part=0;uint64_t messages=1;
        const size_t irregular[]={1,2,3,17,257,1024,4096};
        while(done<total) {
            size_t n=partition==0?total:partition==1?irregular[part++%7]:partition;if(n>total-done)n=total-done;
            messages++;rb_snapshot(r,d,messages,warmup[d]+done+n,messages-1,warmup[d]+done,states[d].sample_clock);
            if(rb_process(r,d,&states[d],iq+2*done,n)) {
                rb_snapshot(r,d,messages,warmup[d]+done+n,messages-1,warmup[d]+done,states[d].sample_clock);
                if(!strcmp(action,"arm-overflow")){free(source);free(iq);rb_finish(r);return atomic_load(&fatal_error)?0:7;}return 7;
            }
            if(!strcmp(action,"unsent")){rb_snapshot(r,d,messages,warmup[d]+done+n,messages-1,warmup[d]+done,states[d].sample_clock);break;}
            done+=n;if(rb_forwarded(r,d,messages,warmup[d]+done,messages,warmup[d]+done,states[d].sample_clock))return 7;
        }
        fixed_record(&states[d],d?"UL":"DL","fixture_final");char path[PATH_MAX];snprintf(path,sizeof(path),"%s%s",argv[4],d?".ul":"");
        FILE *out=fopen(path,"wb");if(!out||fwrite(iq,8,total,out)!=total||fclose(out))return 8;
    }
    free(source);free(iq);rb_finish(r);return atomic_load(&fatal_error)?9:0;
}
