/* Native finite schedules for fixed_reference_v1. This header is included only
 * after the production fixed DSP definitions; fixtures execute this same code.
 * Linux local control; no dependency beyond the broker's existing build. */
#ifndef RADIO_SCHEDULE_NATIVE_H
#define RADIO_SCHEDULE_NATIVE_H
#include "radio_schedule_sha256.h"
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdarg.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>
#define RB_WIRE_MAX 65536U
#define RB_EVENTS_MAX 32U
#define RB_RECORD_MAX 4096U
#define RB_QUEUE_MAX 256U
#define RB_TRUTH_MAX (1024U*1024U)

typedef struct { uint64_t offset; char id[65]; int restore; fixed_config_t config; } rb_event_t;
typedef struct {
    fixed_config_t base; uint64_t duration; size_t count; rb_event_t events[RB_EVENTS_MAX];
    int armed, complete, restored; size_t next_event, forwarded_event;
    uint64_t arm_sample, end_sample, processed, forwarded;
    uint64_t input_messages, output_messages, input_samples, output_samples;
} rb_direction_t;
typedef struct { size_t size; int metrics; char bytes[RM_RECORD_MAX]; } rb_record_t;
typedef struct {
    char study[65], protocol[65], trial[65], pipeline[65], plan_hash[65], config_hash[65];
    char instance[33], build_hash[65], token[65], directory[PATH_MAX], socket_path[108];
    uint32_t seed; double sample_rate; rb_direction_t directions[2];
    int directory_fd, socket_fd, truth_fd, metrics_fd, writer_started, writer_stop, initialized;
    uint64_t metrics_every, metrics_sequence, metrics_last_mono; size_t metrics_bytes; char metrics_hash[65];
    rm_metrics_t metrics[2];
    struct stat socket_stat; int socket_owned;
    pthread_mutex_t state_lock, queue_lock; pthread_cond_t queue_cond; pthread_t writer;
    rb_record_t queue[RB_QUEUE_MAX]; size_t queue_head, queue_count, total_bytes;
    uint64_t event_sequence, request_sequence, arm_sequence, request_wall, request_mono;
    int arm_requested, ready; char reason[65];
    char last_request[513], last_response[RB_RECORD_MAX]; size_t last_response_size;
    atomic_uint_fast64_t logging_errors; atomic_int local_error;
} rb_runtime_t;

static int rb_hex(const char *p, size_t n) {
    if (strlen(p)!=n) return 0;
    for(size_t i=0;i<n;i++) if(!((p[i]>='0'&&p[i]<='9')||(p[i]>='a'&&p[i]<='f')))return 0;
    return 1;
}
static int rb_id(const char *p) {
    size_t n=strlen(p);if(!n||n>64)return 0;
    for(size_t i=0;i<n;i++){char c=p[i]; if(!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||(i&&(c=='_'||c=='.'||c=='-'))))return 0;}
    return 1;
}
static int rb_uint(const char *s, uint64_t max, uint64_t *out) {
    if(!*s||(s[0]=='0'&&s[1]))return -1; uint64_t v=0;
    for(const char *p=s;*p;p++){if(*p<'0'||*p>'9')return -1;unsigned d=(unsigned)(*p-'0');if(v>max/10||(v==max/10&&d>max%10))return -1;v=v*10+d;}*out=v;return 0;
}
static int rb_float(const char *p, double *out) {
    if(strlen(p)>128)return -1;
    const char *s=p;if(*s=='-')s++;
    if(s[0]!='0'||s[1]!='x')return -1;s+=2;size_t before=0,after=0;
    while((*s>='0'&&*s<='9')||(*s>='a'&&*s<='f')){s++;before++;}
    if(!before)return -1;
    if(*s=='.'){s++;while((*s>='0'&&*s<='9')||(*s>='a'&&*s<='f')){s++;after++;}if(!after)return -1;}
    if(*s++!='p'||(*s!='+'&&*s!='-'))return -1;s++;
    if(*s<'0'||*s>'9')return -1;while(*s>='0'&&*s<='9')s++;if(*s)return -1;
    char *end;errno=0;double v=strtod(p,&end);if(*end||!isfinite(v))return -1;*out=v;return 0;
}
static int rb_config_equal(const fixed_config_t *a,const fixed_config_t *b) {
    return a->identity==b->identity&&a->ref_power==b->ref_power&&a->gain==b->gain&&
        (a->identity||a->noise_off==b->noise_off)&&a->noise_snr_db==b->noise_snr_db&&(a->identity||a->cw_enabled==b->cw_enabled)&&
        a->cw_sir_db==b->cw_sir_db&&a->cw_freq_hz==b->cw_freq_hz&&a->sample_rate_hz==b->sample_rate_hz;
}
/* Strict line lexer rejects repeated spaces, CR, tabs, missing LF and excess fields. */
static int rb_line(char **cursor, char *tokens[], size_t expected) {
    char *line=*cursor;char *end=strchr(line,'\n');if(!end||end==line||end-line>1024)return -1;*end='\0';*cursor=end+1;
    size_t count=0;char *start=line;
    for(char *p=line;;p++) {unsigned char c=(unsigned char)*p;
        if(c==' '||c==0){if(p==start||count>=expected)return -1;tokens[count++]=start;if(!c)break;*p='\0';start=p+1;}
        else if(c<33||c>126)return -1;
    }return count==expected?0:-1;
}
static int rb_settings(char **t, fixed_config_t *c) {
    uint64_t noise,cw;
    if(rb_float(t[0],&c->gain)||rb_uint(t[1],1,&noise)||rb_float(t[2],&c->noise_snr_db)||
       rb_uint(t[3],1,&cw)||rb_float(t[4],&c->cw_sir_db)||rb_float(t[5],&c->cw_freq_hz))return -1;
    c->noise_off=!noise;c->cw_enabled=(int)cw;return fixed_config_valid(c)?0:-1;
}
static int rb_parse_wire(rb_runtime_t *r,char *wire,size_t size,const fixed_config_t configs[2],uint32_t seed) {
    if(!size||size>RB_WIRE_MAX||wire[size-1]!='\n'||memchr(wire,0,size))return -1;
    rb_sha256_t sha;rb_sha_init(&sha);rb_sha_update(&sha,wire,size);rb_sha_final(&sha,r->plan_hash);
    wire[size]=0;char *cursor=wire,*t[14];uint64_t value;
    if(rb_line(&cursor,t,1)||strcmp(t[0],"RADIO_SCHEDULE_WIRE_V1"))return -1;
    if(rb_line(&cursor,t,5)||strcmp(t[0],"ids"))return -1;
    char *ids[]={r->study,r->protocol,r->trial,r->pipeline};
    for(size_t i=0;i<4;i++){if(!rb_id(t[i+1]))return -1;strcpy(ids[i],t[i+1]);}
    if(rb_line(&cursor,t,4)||strcmp(t[0],"profile")||!rb_hex(t[1],64)||rb_uint(t[2],UINT32_MAX,&value)||
       rb_float(t[3],&r->sample_rate))return -1;
    strcpy(r->config_hash,t[1]);r->seed=(uint32_t)value;if(r->seed!=seed)return -1;
    for(size_t d=0;d<2;d++) {
        rb_direction_t *p=&r->directions[d];
        if(rb_line(&cursor,t,12)||strcmp(t[0],"direction")||strcmp(t[1],d?"UL":"DL")||
           rb_uint(t[2],INT64_MAX,&p->duration)||!p->duration||rb_uint(t[3],RB_EVENTS_MAX,&value)||!value)return -1;
        p->count=(size_t)value;p->base=fixed_config_defaults();p->base.sample_rate_hz=r->sample_rate;
        if(!strcmp(t[4],"identity"))p->base.identity=1;else if(strcmp(t[4],"fixed"))return -1;
        if(rb_float(t[5],&p->base.ref_power)||rb_settings(t+6,&p->base)||
           (p->base.identity&&(!p->base.noise_off||p->base.cw_enabled)))return -1;
        /* Direction line has 12 tokens: keyword/name/duration/count/mode/ref + six settings. */
        fixed_config_t effective=configs[d];if(effective.identity){effective.noise_off=1;effective.cw_enabled=0;}
        if(!rb_config_equal(&p->base,&effective))return -1;
        for(size_t e=0;e<p->count;e++) {
            rb_event_t *v=&p->events[e];v->config=p->base;
            if(rb_line(&cursor,t,10)||strcmp(t[0],"event")||rb_uint(t[1],p->duration-1,&v->offset)||
               !rb_id(t[2])||(e&&v->offset<=p->events[e-1].offset))return -1;
            strcpy(v->id,t[2]);for(size_t j=0;j<e;j++)if(!strcmp(v->id,p->events[j].id))return -1;
            if(!strcmp(t[3],"restore"))v->restore=1;else if(strcmp(t[3],"set"))return -1;
            if(rb_settings(t+4,&v->config)||(p->base.identity&&(!v->config.noise_off||v->config.cw_enabled||!rb_config_equal(&v->config,&p->base))))return -1;
            if(v->restore&&!rb_config_equal(&v->config,&p->base))return -1;
        }
        rb_event_t *last=&p->events[p->count-1];if(!last->restore||!rb_config_equal(&last->config,&p->base))return -1;
    }
    return rb_line(&cursor,t,1)||strcmp(t[0],"end")||*cursor?-1:0;
}
static int rb_private_stat(const struct stat *s) {
    return S_ISREG(s->st_mode)&&s->st_uid==geteuid()&&(s->st_mode&07777)==0600&&s->st_nlink==1;
}
static int rb_read_private(int fd,char *out,size_t limit,size_t *size) {
    struct stat before,after;if(fstat(fd,&before)||!rb_private_stat(&before)||before.st_size<0||(uint64_t)before.st_size>limit)return -1;
    size_t total=0;while(total<=limit){ssize_t n=read(fd,out+total,limit+1-total);if(n<0){if(errno==EINTR)continue;return -1;}if(!n)break;total+=(size_t)n;}
    if(total>limit||fstat(fd,&after)||before.st_size!=after.st_size||before.st_mtim.tv_sec!=after.st_mtim.tv_sec||before.st_mtim.tv_nsec!=after.st_mtim.tv_nsec||before.st_ctim.tv_sec!=after.st_ctim.tv_sec||before.st_ctim.tv_nsec!=after.st_ctim.tv_nsec)return -1;
    out[total]=0;*size=total;return 0;
}
static void rb_dispose_loaded(rb_runtime_t *r) {
    if(!r)return;if(r->directory_fd>=0)close(r->directory_fd);
    if(r->initialized){pthread_cond_destroy(&r->queue_cond);pthread_mutex_destroy(&r->queue_lock);pthread_mutex_destroy(&r->state_lock);}free(r);
}
static rb_runtime_t *rb_load(const char *plan_path,const char *directory,const fixed_config_t configs[2],uint32_t seed) {
    char resolved[PATH_MAX];struct stat s;
    if(!realpath(directory,resolved)||strcmp(resolved,directory)||lstat(directory,&s)||!S_ISDIR(s.st_mode)||s.st_uid!=geteuid()||(s.st_mode&07777)!=0700)return NULL;
    rb_runtime_t *r=calloc(1,sizeof(*r));if(!r)return NULL;r->directory_fd=r->socket_fd=r->truth_fd=r->metrics_fd=-1;
    strcpy(r->directory,directory);
    if(snprintf(r->socket_path,sizeof(r->socket_path),"%s/rb.sock",directory)>=(int)sizeof(r->socket_path))goto fail;
    r->directory_fd=open(directory,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
    struct stat actual;if(r->directory_fd<0||fstat(r->directory_fd,&actual)||s.st_dev!=actual.st_dev||s.st_ino!=actual.st_ino||!S_ISDIR(actual.st_mode)||actual.st_uid!=geteuid()||(actual.st_mode&07777)!=0700)goto fail;
    int fd=open(plan_path,O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);if(fd<0)goto fail;
    char *wire=malloc(RB_WIRE_MAX+2);size_t size=0;int ok=wire?rb_read_private(fd,wire,RB_WIRE_MAX,&size):-1;if(close(fd))ok=-1;
    if(!ok)ok=rb_parse_wire(r,wire,size,configs,seed);free(wire);if(ok)goto fail;
    fd=openat(r->directory_fd,"control.token",O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);if(fd<0)goto fail;
    char token[66];ok=rb_read_private(fd,token,64,&size);if(close(fd))ok=-1;if(ok||!rb_hex(token,64))goto fail;strcpy(r->token,token);
    static const char *outputs[]={"rb.sock","broker_ready.json","broker_events.jsonl"};
    for(size_t i=0;i<3;i++){
        if(fstatat(r->directory_fd,outputs[i],&actual,AT_SYMLINK_NOFOLLOW)==0||errno!=ENOENT)goto fail;
    }
    if(pthread_mutex_init(&r->state_lock,NULL))goto fail;
    if(pthread_mutex_init(&r->queue_lock,NULL)){pthread_mutex_destroy(&r->state_lock);goto fail;}
    if(pthread_cond_init(&r->queue_cond,NULL)){pthread_mutex_destroy(&r->queue_lock);pthread_mutex_destroy(&r->state_lock);goto fail;}
    r->initialized=1;atomic_init(&r->logging_errors,0);atomic_init(&r->local_error,0);return r;
fail:rb_dispose_loaded(r);return NULL;
}
static int rb_clock(uint64_t *mono,uint64_t *wall) {
    struct timespec m,w;
    if(clock_gettime(CLOCK_MONOTONIC,&m)||clock_gettime(CLOCK_REALTIME,&w)||
       rm_timespec_ns(&m,mono)||rm_timespec_ns(&w,wall))return -1;
    return 0;
}
static int rb_format(char *out,const char *format,...) {
    va_list ap;va_start(ap,format);int n=vsnprintf(out,RB_RECORD_MAX,format,ap);va_end(ap);return n<0||n>=(int)RB_RECORD_MAX?-1:n;
}
static void rb_fail(rb_runtime_t *r) {
    atomic_store_explicit(&r->local_error,1,memory_order_release);atomic_store_explicit(&fatal_error,1,memory_order_release);
}
static int rb_emit_at(rb_runtime_t *r,const char *direction,const char *type,const char *scope,
                       uint64_t start,uint64_t end,uint64_t mono,uint64_t wall,const char *details) {
    char begin[32],finish[32];
    if(!strcmp(direction,"control")){strcpy(begin,"null");strcpy(finish,"null");}
    else {snprintf(begin,sizeof(begin),"%" PRIu64,start);snprintf(finish,sizeof(finish),"%" PRIu64,end);}
    char record[RB_RECORD_MAX];pthread_mutex_lock(&r->queue_lock);
    int n=rb_format(record,"{\"schema_version\":\"radio_broker_truth_v1\",\"study_id\":\"%s\",\"protocol_id\":\"%s\","
        "\"trial_id\":\"%s\",\"pipeline_id\":\"%s\",\"instance_id\":\"%s\",\"backend\":\"c\",\"build_sha256\":\"%s\","
        "\"config_sha256\":\"%s\",\"plan_sha256\":\"%s\",\"direction\":\"%s\",\"event_sequence\":%" PRIu64 ","
        "\"event_type\":\"%s\",\"monotonic_ns\":%" PRIu64 ",\"wall_ns\":%" PRIu64 ",\"sample_start\":%s,\"sample_end\":%s,"
        "\"scope\":\"%s\",\"details\":%s}\n",r->study,r->protocol,r->trial,r->pipeline,r->instance,r->build_hash,r->config_hash,
        r->plan_hash,direction,r->event_sequence+1,type,mono,wall,begin,finish,scope,details);
    if(n<0||r->queue_count==RB_QUEUE_MAX||r->writer_stop||r->total_bytes+(size_t)(n<0?0:n)>RB_TRUTH_MAX||atomic_load(&r->logging_errors)) {
        pthread_mutex_unlock(&r->queue_lock);atomic_fetch_add(&r->logging_errors,1);rb_fail(r);return -1;
    }
    size_t slot=(r->queue_head+r->queue_count)%RB_QUEUE_MAX;r->queue[slot].size=(size_t)n;r->queue[slot].metrics=0;memcpy(r->queue[slot].bytes,record,(size_t)n);
    r->queue_count++;r->total_bytes+=(size_t)n;r->event_sequence++;pthread_cond_signal(&r->queue_cond);pthread_mutex_unlock(&r->queue_lock);return 0;
}
static int rb_emit(rb_runtime_t *r,const char *direction,const char *type,const char *scope,uint64_t start,uint64_t end,const char *details) {
    uint64_t mono,wall;if(rb_clock(&mono,&wall)){rb_fail(r);return -1;}return rb_emit_at(r,direction,type,scope,start,end,mono,wall,details);
}
static void rb_error(rb_runtime_t *r,const char *reason) {
    if(!r)return;
    int first=!atomic_exchange_explicit(&r->local_error,1,memory_order_acq_rel);atomic_store(&fatal_error,1);
    if(first){char details[RB_RECORD_MAX];if(rb_format(details,"{\"reason\":\"%s\"}",reason)>=0)(void)rb_emit(r,"control","error","control",0,0,details);}
}
static int rb_write_all(int fd,const char *bytes,size_t size) {
    while(size){ssize_t n=write(fd,bytes,size);if(n<0&&errno==EINTR)continue;if(n<=0)return -1;bytes+=n;size-=(size_t)n;}return 0;
}
static void *rb_writer(void *arg) {
    rb_runtime_t *r=arg;rb_record_t record;
    for(;;){pthread_mutex_lock(&r->queue_lock);while(!r->queue_count&&!r->writer_stop)pthread_cond_wait(&r->queue_cond,&r->queue_lock);
        if(!r->queue_count&&r->writer_stop){pthread_mutex_unlock(&r->queue_lock);break;}
        rb_record_t *queued=&r->queue[r->queue_head];record.size=queued->size;record.metrics=queued->metrics;memcpy(record.bytes,queued->bytes,record.size);r->queue_head=(r->queue_head+1)%RB_QUEUE_MAX;r->queue_count--;pthread_mutex_unlock(&r->queue_lock);
        if(rb_write_all(record.metrics?r->metrics_fd:r->truth_fd,record.bytes,record.size)){atomic_fetch_add(&r->logging_errors,1);rb_fail(r);break;}
    }
    if(fsync(r->truth_fd)){atomic_fetch_add(&r->logging_errors,1);rb_fail(r);}
    if(close(r->truth_fd)){atomic_fetch_add(&r->logging_errors,1);rb_fail(r);}
    if(r->metrics_fd>=0){
        if(fsync(r->metrics_fd)){atomic_fetch_add(&r->logging_errors,1);rb_fail(r);}
        if(close(r->metrics_fd)){atomic_fetch_add(&r->logging_errors,1);rb_fail(r);}
    }return NULL;
}
static int rb_settings_json(char *out,const fixed_config_t *c) {
    return rb_format(out,"{\"gain\":%.17g,\"noise_enabled\":%s,\"noise_snr_db\":%.17g,\"cw_enabled\":%s,\"cw_sir_db\":%.17g,\"cw_freq_hz\":%.17g}",
        c->gain,c->noise_off?"false":"true",c->noise_snr_db,c->cw_enabled?"true":"false",c->cw_sir_db,c->cw_freq_hz);
}
static int rb_arm(rb_runtime_t *r,size_t direction,fixed_state_t *s) {
    rb_direction_t *p=&r->directions[direction];char details[RB_RECORD_MAX],settings[RB_RECORD_MAX];uint64_t mono,wall;
    fixed_config_t effective=s->config;if(effective.identity){effective.noise_off=1;effective.cw_enabled=0;}
    if(rb_settings_json(settings,&effective)<0)return -1;
    if(p->duration>UINT64_MAX-s->sample_clock||rb_clock(&mono,&wall))return -1;
    p->armed=1;p->arm_sample=s->sample_clock;p->end_sample=p->arm_sample+p->duration;
    int n=rb_format(details,"{\"request_sequence\":%" PRIu64 ",\"request_wall_ns\":%" PRIu64 ",\"request_monotonic_ns\":%" PRIu64 ","
        "\"arm_sample\":%" PRIu64 ",\"duration_samples\":%" PRIu64 ",\"state_at_arm\":{"
        "\"mode\":\"%s\",\"ref_power\":%.17g,\"sample_rate_hz\":%.17g,\"settings\":%s,\"noise_std\":%.17g,\"cw_amplitude\":%.17g,"
        "\"rng_version\":\"component_streams_v1\",\"rng_algorithm\":\"glibc_rand_r_box_muller_pair_f32\","
        "\"master_seed\":%" PRIu32 ",\"awgn_seed\":%" PRIu32 ",\"awgn_state\":%u,\"sample_clock\":%" PRIu64 ","
        "\"awgn_complex_draws\":%" PRIu64 ",\"awgn_normal_draws\":%" PRIu64 ",\"phase_u64\":%" PRIu64 ","
        "\"cw_step_u64\":%" PRIu64 ",\"masked_samples\":%" PRIu64 ",\"attenuated_samples\":%" PRIu64 ","
        "\"input_energy\":%.17g,\"desired_energy\":%.17g,\"noise_energy\":%.17g,\"cw_energy\":%.17g,\"output_energy\":%.17g}}",
        r->arm_sequence,r->request_wall,r->request_mono,p->arm_sample,p->duration,s->config.identity?"identity":"fixed",s->config.ref_power,s->config.sample_rate_hz,settings,s->noise_std,s->cw_amplitude,s->master_seed,s->awgn_seed,s->awgn_state,s->sample_clock,
        s->awgn_complex_draws,s->awgn_normal_draws,s->phase_u64,s->cw_step_u64,s->masked_samples,s->attenuated_samples,
        s->input_energy,s->desired_energy,s->noise_energy,s->cw_energy,s->output_energy);
    return n<0?-1:rb_emit_at(r,direction?"UL":"DL","armed","processed_samples",s->sample_clock,s->sample_clock,mono,wall,details);
}
static void rb_update_coefficients(fixed_state_t *s,const fixed_config_t *c) {
    s->config=*c;s->noise_std=sqrt(c->ref_power*pow(10.0,-c->noise_snr_db/10.0)/2);
    s->cw_amplitude=sqrt(c->ref_power*pow(10.0,-c->cw_sir_db/10.0));s->cw_step_u64=cw_phase_step(c->cw_freq_hz,c->sample_rate_hz);
}
/* Caller remains sole owner of fixed_state. Only snapshots/configuration events
 * hold the short state lock; neither DSP nor socket/filesystem IO holds it. */
static int rb_process(rb_runtime_t *r,size_t direction,fixed_state_t *s,float *iq,size_t count) {
    rb_direction_t *p=&r->directions[direction];
    if(!count)return 0;
    pthread_mutex_lock(&r->state_lock);int armed_status=0;
    if(r->arm_requested&&!p->armed)armed_status=rb_arm(r,direction,s);
    pthread_mutex_unlock(&r->state_lock);if(armed_status){rb_error(r,"arm_overflow_or_record");return -1;}
    size_t done=0;
    while(done<count) {
        rb_event_t *event=NULL;int changed=0;uint64_t mono=0,wall=0,start=s->sample_clock;size_t take=count-done;
        if(p->armed&&p->next_event<p->count){rb_event_t *next=&p->events[p->next_event];uint64_t boundary=p->arm_sample+next->offset;
            if(start>boundary){rb_error(r,"missed_event_boundary");return -1;}
            if(start==boundary){event=next;changed=!rb_config_equal(&s->config,&event->config);if(rb_clock(&mono,&wall)){rb_error(r,"clock_failure");return -1;}
                rb_update_coefficients(s,&event->config);
                if(p->next_event+1<p->count)boundary=p->arm_sample+p->events[p->next_event+1].offset;else boundary=UINT64_MAX;
            }
            if(boundary-start<(uint64_t)take)take=(size_t)(boundary-start);
        }
        if(!take||fixed_process(s,iq+2*done,take)){rb_error(r,"dsp_processing");return -1;}
        for(size_t i=0;i<2*take;i++)if(!isfinite(iq[2*done+i])){rb_error(r,"nonfinite_output");return -1;}
        pthread_mutex_lock(&r->state_lock);p->processed=s->sample_clock;
        if(event){char settings[RB_RECORD_MAX],details[RB_RECORD_MAX];int ok=rb_settings_json(settings,&event->config);
            if(ok>=0)ok=rb_format(details,"{\"event_id\":\"%s\",\"kind\":\"%s\",\"sample_offset\":%" PRIu64 ",\"changed\":%s,\"settings\":%s,\"processed_samples\":%" PRIu64 "}",
                event->id,event->restore?"restore":"set",event->offset,changed?"true":"false",settings,s->sample_clock);
            if(ok>=0)ok=rb_emit_at(r,direction?"UL":"DL",event->restore?"condition_restored":"condition_applied","processed_samples",start,s->sample_clock,mono,wall,details);
            if(ok<0){pthread_mutex_unlock(&r->state_lock);rb_error(r,"event_record");return -1;}
            p->next_event++;if(event->restore)p->restored=1;
        }
        pthread_mutex_unlock(&r->state_lock);done+=take;
    }
    return 0;
}
static int rb_forwarded(rb_runtime_t *r,size_t direction,uint64_t im,uint64_t is,uint64_t om,uint64_t os,uint64_t processed) {
    rb_direction_t *p=&r->directions[direction];pthread_mutex_lock(&r->state_lock);
    p->input_messages=im;p->input_samples=is;p->output_messages=om;p->output_samples=os;p->forwarded=os;p->processed=processed;
    int newly_complete=p->armed&&!p->complete&&os>=p->end_sample&&p->next_event==p->count&&p->restored;
    int publish=p->next_event>p->forwarded_event||newly_complete;uint64_t start=0;
    if(publish){start=p->armed?p->arm_sample:0;p->forwarded_event=p->next_event;p->complete|=newly_complete;}
    int status=0;
    if(publish){char details[RB_RECORD_MAX];int n=rb_format(details,"{\"input_messages\":%" PRIu64 ",\"input_samples\":%" PRIu64 ",\"output_messages\":%" PRIu64 ",\"output_samples\":%" PRIu64 ",\"scheduled_end_sample\":%" PRIu64 ",\"schedule_complete\":%s}",im,is,om,os,p->end_sample,p->complete?"true":"false");
        status=n<0?-1:rb_emit(r,direction?"UL":"DL","progress","successfully_forwarded_samples",start,os,details);}
    pthread_mutex_unlock(&r->state_lock);if(status)rb_error(r,"progress_record");return status;
}
/* Input receipt and failed/partial DSP are also reflected in status/finals. */
static void rb_snapshot(rb_runtime_t *r,size_t direction,uint64_t im,uint64_t is,uint64_t om,uint64_t os,uint64_t processed) {
    if(!r)return;pthread_mutex_lock(&r->state_lock);rb_direction_t *p=&r->directions[direction];
    p->input_messages=im;p->input_samples=is;p->output_messages=om;p->output_samples=os;p->processed=processed;p->forwarded=os;pthread_mutex_unlock(&r->state_lock);
}
static int rb_metrics_emit(void *context,size_t direction,const char *type,uint64_t start,uint64_t end,const char *details) {
    rb_runtime_t *r=context;
    uint64_t mono,wall;rm_json_t j={0};pthread_mutex_lock(&r->queue_lock);
    if(rb_clock(&mono,&wall)||mono<r->metrics_last_mono){pthread_mutex_unlock(&r->queue_lock);atomic_fetch_add(&r->logging_errors,1);rb_fail(r);return -1;}
    rm_json(&j,"{\"schema_version\":\"radio_broker_metrics_v1\",\"study_id\":\"%s\",\"protocol_id\":\"%s\","
        "\"trial_id\":\"%s\",\"pipeline_id\":\"%s\",\"instance_id\":\"%s\",\"backend\":\"c\",\"build_sha256\":\"%s\","
        "\"config_sha256\":\"%s\",\"plan_sha256\":\"%s\",\"metrics_config_sha256\":\"%s\",\"direction\":\"%s\","
        "\"event_sequence\":%" PRIu64 ",\"event_type\":\"%s\",\"monotonic_ns\":%" PRIu64 ",\"wall_ns\":%" PRIu64 ",",
        r->study,r->protocol,r->trial,r->pipeline,r->instance,r->build_hash,r->config_hash,r->plan_hash,r->metrics_hash,
        direction==2?"control":direction?"UL":"DL",r->metrics_sequence+1,type,mono,wall);
    if(direction==2)rm_json(&j,"\"sample_start\":null,\"sample_end\":null,\"scope\":\"control\",");
    else rm_json(&j,"\"sample_start\":%" PRIu64 ",\"sample_end\":%" PRIu64 ",\"scope\":\"direction\",",start,end);
    rm_json(&j,"\"details\":%s}\n",details);
    if(j.error||r->queue_count==RB_QUEUE_MAX||r->writer_stop||r->metrics_bytes+j.used>RM_FILE_MAX||atomic_load(&r->logging_errors)||r->metrics_sequence==UINT64_MAX){
        pthread_mutex_unlock(&r->queue_lock);atomic_fetch_add(&r->logging_errors,1);rb_fail(r);return -1;
    }
    size_t slot=(r->queue_head+r->queue_count)%RB_QUEUE_MAX;r->queue[slot].size=j.used;r->queue[slot].metrics=1;
    memcpy(r->queue[slot].bytes,j.text,j.used);r->queue_count++;r->metrics_bytes+=j.used;r->metrics_sequence++;r->metrics_last_mono=mono;
    pthread_cond_signal(&r->queue_cond);pthread_mutex_unlock(&r->queue_lock);return 0;
}
static int rb_metrics_enable(rb_runtime_t *r,uint64_t every) {
    struct stat existing;
    if(!every||every>1000000||fstatat(r->directory_fd,"broker_metrics.jsonl",&existing,AT_SYMLINK_NOFOLLOW)==0||errno!=ENOENT)return -1;
    char config[80];int n=snprintf(config,sizeof(config),"radio_broker_metrics_v1\n%" PRIu64 "\n",every);
    if(n<0||n>=(int)sizeof(config))return -1;
    rb_sha256_t sha;rb_sha_init(&sha);rb_sha_update(&sha,config,(size_t)n);rb_sha_final(&sha,r->metrics_hash);
    r->metrics_every=every;
    for(size_t d=0;d<2;d++)rm_init(&r->metrics[d],every,rb_metrics_emit,r,d);
    return 0;
}
static int rb_hash_executable(char out[65]) {
    int fd=open("/proc/self/exe",O_RDONLY|O_CLOEXEC);if(fd<0)return -1;
    rb_sha256_t sha;rb_sha_init(&sha);unsigned char bytes[16384];int status=0;
    for(;;){ssize_t n=read(fd,bytes,sizeof(bytes));if(n<0&&errno==EINTR)continue;if(n<0){status=-1;break;}if(!n)break;rb_sha_update(&sha,bytes,(size_t)n);}
    if(close(fd))status=-1;if(!status)rb_sha_final(&sha,out);return status;
}
static int rb_start(rb_runtime_t *r) {
    unsigned char nonce[16];size_t n=0;while(n<sizeof(nonce)){ssize_t got=getrandom(nonce+n,sizeof(nonce)-n,0);if(got<0&&errno==EINTR)continue;if(got<=0)return -1;n+=(size_t)got;}
    static const char hex[]="0123456789abcdef";for(size_t i=0;i<16;i++){r->instance[2*i]=hex[nonce[i]>>4];r->instance[2*i+1]=hex[nonce[i]&15];}
    if(rb_hash_executable(r->build_hash))return -1;
    r->truth_fd=openat(r->directory_fd,"broker_events.jsonl",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(r->truth_fd<0)return -1;
    if(r->metrics_every){
        r->metrics_fd=openat(r->directory_fd,"broker_metrics.jsonl",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
        if(r->metrics_fd<0||fchmod(r->metrics_fd,0600)){if(r->metrics_fd>=0)close(r->metrics_fd);close(r->truth_fd);r->truth_fd=r->metrics_fd=-1;return -1;}
    }
    if(fchmod(r->truth_fd,0600)||pthread_create(&r->writer,NULL,rb_writer,r)){close(r->truth_fd);if(r->metrics_fd>=0)close(r->metrics_fd);r->truth_fd=r->metrics_fd=-1;return -1;}r->writer_started=1;
    if(r->metrics_every){char details[RB_RECORD_MAX];
        if(rb_format(details,"{\"every_messages\":%" PRIu64 ",\"max_windows\":256,\"histogram\":\"uint64_bit_length\",\"sample_rate_hz\":%.17g,\"pid\":%ld}",r->metrics_every,r->sample_rate,(long)getpid())<0||rb_metrics_emit(r,2,"started",0,0,details))return -1;
    }
    if(rb_emit(r,"control","started","control",0,0,"{\"qualification\":\"development_only\",\"channel_semantics_version\":\"fixed_reference_v1\",\"timing_qualification\":\"RAD08_pending\"}"))return -1;
    r->socket_fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);if(r->socket_fd<0)return -1;
    struct sockaddr_un addr={.sun_family=AF_UNIX};strcpy(addr.sun_path,r->socket_path);
    /* Never unlink a preexisting pathname. Private directory prevents another uid
     * from racing pathname creation; metadata is checked again during cleanup. */
    if(bind(r->socket_fd,(struct sockaddr *)&addr,sizeof(addr)))return -1;
    if(fstatat(r->directory_fd,"rb.sock",&r->socket_stat,AT_SYMLINK_NOFOLLOW)||!S_ISSOCK(r->socket_stat.st_mode)||r->socket_stat.st_uid!=geteuid())return -1;
    r->socket_owned=1;
    if(fchmodat(r->directory_fd,"rb.sock",0600,0)||listen(r->socket_fd,8))return -1;
    return 0;
}
static int rb_json_string(char *out,size_t capacity,const char *text) {
    size_t pos=0;for(const unsigned char *p=(const unsigned char *)text;*p;p++) {
        if(*p<32||*p>126)return -1;if(*p=='"'||*p=='\\'){if(pos+1>=capacity)return -1;out[pos++]='\\';}
        if(pos+1>=capacity)return -1;out[pos++]=(char)*p;
    }out[pos]=0;return 0;
}
static int rb_ready(rb_runtime_t *r) {
    char path[217],record[RB_RECORD_MAX];if(rb_json_string(path,sizeof(path),r->socket_path))return -1;
    int n=rb_format(record,"{\"schema_version\":\"radio_broker_ready_v1\",\"instance_id\":\"%s\",\"pid\":%ld,\"plan_sha256\":\"%s\","
        "\"config_sha256\":\"%s\",\"backend\":\"c\",\"build_sha256\":\"%s\",\"control_socket\":\"%s\"}\n",r->instance,(long)getpid(),r->plan_hash,r->config_hash,r->build_hash,path);
    if(n<0)return -1;
    int fd=openat(r->directory_fd,"broker_ready.json",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);if(fd<0)return -1;
    int ok=fchmod(fd,0600)||rb_write_all(fd,record,(size_t)n)||fsync(fd);if(close(fd))ok=-1;if(ok)return -1;
    r->ready=1;return rb_emit(r,"control","ready","control",0,0,"{\"meaning\":\"local_control_and_both_relay_sockets_bound\"}");
}
static const char *rb_state(const rb_runtime_t *r) {
    if(atomic_load(&r->local_error)||atomic_load(&fatal_error))return "error";
    if(r->directions[0].complete&&r->directions[1].complete)return "completed";
    if(r->directions[0].armed&&r->directions[1].armed)return "armed";
    return r->arm_requested?"arm_pending":"ready";
}
static int rb_response(rb_runtime_t *r,char out[RB_RECORD_MAX],uint64_t sequence,const char *operation,const char *reason) {
    char direction_json[2][RB_RECORD_MAX];pthread_mutex_lock(&r->state_lock);
    for(size_t d=0;d<2;d++){rb_direction_t *p=&r->directions[d];char arm[32];if(p->armed)snprintf(arm,sizeof(arm),"%" PRIu64,p->arm_sample);else strcpy(arm,"null");
        if(rb_format(direction_json[d],"{\"armed\":%s,\"arm_sample\":%s,\"processed_samples\":%" PRIu64 ",\"forwarded_samples\":%" PRIu64 ",\"duration_samples\":%" PRIu64 ",\"schedule_complete\":%s,\"next_event\":%zu}",
            p->armed?"true":"false",arm,p->processed,p->forwarded,p->duration,p->complete?"true":"false",p->next_event)<0){pthread_mutex_unlock(&r->state_lock);return -1;}}
    char extra[96]="";if(reason)snprintf(extra,sizeof(extra),",\"reason\":\"%s\"",reason);
    int n=rb_format(out,"{\"schema_version\":\"radio_broker_control_v1\",\"ok\":%s,\"request_sequence\":%" PRIu64 ",\"operation\":\"%s\","
        "\"instance_id\":\"%s\",\"plan_sha256\":\"%s\",\"state\":\"%s\",\"directions\":{\"DL\":%s,\"UL\":%s}%s}\n",
        reason?"false":"true",sequence,operation,r->instance,r->plan_hash,rb_state(r),direction_json[0],direction_json[1],extra);
    pthread_mutex_unlock(&r->state_lock);return n;
}
/* Pure packet path also exercised by L1 without a control socket. Caller has
 * already authenticated SO_PEERCRED. Rejected bytes never enter diagnostics. */
static int rb_control_packet(rb_runtime_t *r,const char *packet,size_t size,char out[RB_RECORD_MAX]) {
    const char *reason=NULL;uint64_t sequence=0;char operation[7]="STATUS";char copy[513],*t[6];
    if(!size||size>512||packet[size-1]!='\n'||memchr(packet,0,size))reason="malformed_control";
    if(!reason){memcpy(copy,packet,size);copy[size]=0;char *cursor=copy;
        if(rb_line(&cursor,t,6)||*cursor||strcmp(t[0],"RBCTRL1")||rb_uint(t[1],UINT64_MAX,&sequence)||!sequence||
            (strcmp(t[2],"STATUS")&&strcmp(t[2],"ARM"))||!rb_hex(t[3],32)||!rb_hex(t[4],64)||!rb_hex(t[5],64))reason="malformed_control";
        else {strcpy(operation,t[2]);unsigned mismatch=0;for(size_t i=0;i<64;i++)mismatch|=(unsigned char)(t[5][i]^r->token[i]);
            if(mismatch)reason="unauthorized_control";else if(strcmp(t[3],r->instance))reason="stale_instance";else if(strcmp(t[4],r->plan_hash))reason="plan_mismatch";}
    }
    if(!reason&&sequence==r->request_sequence&&strlen(r->last_request)==size&&!memcmp(packet,r->last_request,size)) {
        memcpy(out,r->last_response,r->last_response_size);return (int)r->last_response_size;
    }
    if(!reason&&(r->request_sequence>=4096||sequence>4096))reason="control_budget";
    if(!reason&&sequence!=r->request_sequence+1)reason="control_sequence";
    if(!reason&&!strcmp(operation,"ARM")&&r->arm_requested)reason="repeated_arm";
    if(!reason&&!r->ready)reason="not_ready";
    if(reason){rb_error(r,reason);return rb_response(r,out,sequence,operation,reason);}
    if(!strcmp(operation,"ARM")){
        uint64_t mono,wall;if(rb_clock(&mono,&wall)){rb_error(r,"clock_failure");return rb_response(r,out,sequence,operation,"clock_failure");}
        pthread_mutex_lock(&r->state_lock);
        char details[RB_RECORD_MAX];int n=rb_format(details,"{\"request_sequence\":%" PRIu64 ",\"request_monotonic_ns\":%" PRIu64 ",\"request_wall_ns\":%" PRIu64 "}",sequence,mono,wall);
        if(n>=0)n=rb_emit_at(r,"control","arm_requested","control",0,0,mono,wall,details);
        if(n>=0){r->request_mono=mono;r->request_wall=wall;r->arm_sequence=sequence;r->arm_requested=1;}
        pthread_mutex_unlock(&r->state_lock);if(n<0){rb_error(r,"arm_request_record");return rb_response(r,out,sequence,operation,"arm_request_record");}
    }
    r->request_sequence=sequence;memcpy(r->last_request,packet,size);r->last_request[size]=0;
    int n=rb_response(r,out,sequence,operation,NULL);if(n<0){rb_error(r,"response_format");return -1;}
    memcpy(r->last_response,out,(size_t)n);r->last_response_size=(size_t)n;return n;
}
static int rb_poll_ready(int fd,short events,int timeout) {
    struct pollfd p={.fd=fd,.events=events};int result=poll(&p,1,timeout);
    if(result<0&&errno==EINTR)return 0;if(result<0)return -1;
    return result&&(p.revents&events)?1:result?-1:0;
}
static void rb_control_step(rb_runtime_t *r) {
    int poll_status=rb_poll_ready(r->socket_fd,POLLIN,50);if(poll_status<0){rb_error(r,"control_poll");return;}if(!poll_status)return;
    int fd=accept4(r->socket_fd,NULL,NULL,SOCK_NONBLOCK|SOCK_CLOEXEC);if(fd<0){if(errno!=EAGAIN&&errno!=EINTR)rb_error(r,"control_accept");return;}
    struct ucred peer;socklen_t length=sizeof(peer);char response[RB_RECORD_MAX];int n=-1;
    if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&length)||length!=sizeof(peer)||peer.uid!=geteuid()){
        rb_error(r,"unauthorized_peer");n=rb_response(r,response,0,"STATUS","unauthorized_peer");
    } else if(rb_poll_ready(fd,POLLIN,100)!=1){rb_error(r,"control_receive_timeout");n=rb_response(r,response,0,"STATUS","control_receive_timeout");}
    else {char packet[512];struct iovec iov={.iov_base=packet,.iov_len=sizeof(packet)};struct msghdr msg={.msg_iov=&iov,.msg_iovlen=1};ssize_t got=recvmsg(fd,&msg,MSG_DONTWAIT);
        if(got<=0||(msg.msg_flags&MSG_TRUNC)){rb_error(r,"malformed_control");n=rb_response(r,response,0,"STATUS","malformed_control");}
        else n=rb_control_packet(r,packet,(size_t)got,response);
    }
    if(n>0){if(rb_poll_ready(fd,POLLOUT,100)!=1||send(fd,response,(size_t)n,MSG_DONTWAIT|MSG_NOSIGNAL)!=n)rb_error(r,"control_send");}
    if(close(fd))rb_error(r,"control_close");
}
static int rb_join(pthread_t thread,int seconds) {
    struct timespec deadline;if(clock_gettime(CLOCK_REALTIME,&deadline))return -1;deadline.tv_sec+=seconds;
    return pthread_timedjoin_np(thread,NULL,&deadline);
}
static void rb_finish(rb_runtime_t *r) {
    if(!r)return;
    /* Relay threads must already be joined: snapshots are now stable. */
    if(r->socket_fd>=0&&close(r->socket_fd))rb_error(r,"control_close");r->socket_fd=-1;
    if(r->socket_owned){struct stat s;
        if(fstatat(r->directory_fd,"rb.sock",&s,AT_SYMLINK_NOFOLLOW)||s.st_dev!=r->socket_stat.st_dev||s.st_ino!=r->socket_stat.st_ino||!S_ISSOCK(s.st_mode)||s.st_uid!=geteuid())rb_error(r,"socket_ownership_changed");
        else if(unlinkat(r->directory_fd,"rb.sock",0))rb_error(r,"socket_cleanup");
    }
    if(r->directory_fd>=0&&close(r->directory_fd))rb_error(r,"directory_close");r->directory_fd=-1;
    int failed=atomic_load(&fatal_error)||atomic_load(&r->local_error)||atomic_load(&r->logging_errors);
    int all_complete=1;for(size_t d=0;d<2;d++){rb_direction_t *p=&r->directions[d];all_complete&=p->complete&&p->input_messages==p->output_messages&&p->input_samples==p->output_samples;}
    if(r->writer_started){for(size_t d=0;d<2;d++){rb_direction_t *p=&r->directions[d];char arm[32]="null",end[32]="null",details[RB_RECORD_MAX];
        if(p->armed){snprintf(arm,sizeof(arm),"%" PRIu64,p->arm_sample);snprintf(end,sizeof(end),"%" PRIu64,p->end_sample);}
        const char *status=failed?"error":all_complete?"complete":"incomplete";
        int n=rb_format(details,"{\"status\":\"%s\",\"reason\":\"%s\",\"armed_sample\":%s,\"scheduled_end_sample\":%s,\"processed_samples\":%" PRIu64 ",\"forwarded_samples\":%" PRIu64 ","
            "\"input_messages\":%" PRIu64 ",\"output_messages\":%" PRIu64 ",\"input_samples\":%" PRIu64 ",\"output_samples\":%" PRIu64 ",\"logging_errors\":%" PRIuFAST64 ","
            "\"all_events_processed\":%s,\"restoration_observed\":%s,\"schedule_complete\":%s}",status,failed?"runtime_error":all_complete?"completed":"unarmed_or_partial",arm,end,
            p->processed,p->forwarded,p->input_messages,p->output_messages,p->input_samples,p->output_samples,atomic_load(&r->logging_errors),p->next_event==p->count?"true":"false",p->restored?"true":"false",p->complete?"true":"false");
        if(n<0||rb_emit(r,d?"UL":"DL","final","processed_samples",0,p->processed,details))rb_fail(r);
    }
    if(r->metrics_every)for(size_t d=0;d<2;d++){
        rm_metrics_t *m=&r->metrics[d];
        if(rm_final(m,&m->current,atomic_load(&fatal_error)||atomic_load(&r->local_error),atomic_load(&r->logging_errors)))rb_fail(r);
    }
    pthread_mutex_lock(&r->queue_lock);r->writer_stop=1;pthread_cond_signal(&r->queue_cond);pthread_mutex_unlock(&r->queue_lock);
    if(rb_join(r->writer,5)){fprintf(stderr,"FATAL: bounded truth writer join failed\n");_Exit(EXIT_FAILURE);}r->truth_fd=-1;
    }
    rb_dispose_loaded(r);
}
#endif
