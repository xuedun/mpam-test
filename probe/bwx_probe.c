/* bwx_probe -- multi-threaded read-modify-write memory bandwidth load generator.
 *
 * Purpose: give `bwx calibrate` a load whose *logical* traffic is exactly known,
 * so the ddrc uncore counters can be converted to bytes and the MPAM MB cap can be
 * shown to be binding. Each thread streams `data[i] += 1` over a buffer larger
 * than the LLC slice; one pass moves 2 * buffer bytes logically (read + write).
 * With enough threads the demand exceeds any cap, so the cap -- not compute --
 * becomes the binding constraint.
 *
 * Deliberately static: it must run unchanged on hosts with a different glibc.
 * Build: cc -static -O2 -pthread bwx_probe.c -o bwx_probe
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t stopped = 0;
static pthread_barrier_t barrier;

typedef struct {
    int index;
    int cpu;              /* -1 = no pinning (caller uses numactl instead) */
    uint64_t *data;
    size_t elems;
    uint64_t passes;
    uint64_t checksum;
} thread_ctx;

static void on_stop(int sig) { (void)sig; stopped = 1; }

static double now(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) { perror("clock_gettime"); exit(2); }
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void phase(const char *name)
{
    printf("{\"event\":\"phase\",\"phase\":\"%s\",\"monotonic_sec\":%.6f}\n", name, now());
}

static void *worker(void *arg)
{
    thread_ctx *c = (thread_ctx *)arg;
    if (c->cpu >= 0) {
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(c->cpu, &set);
        if (sched_setaffinity(0, sizeof(set), &set) != 0)
            fprintf(stderr, "thread %d: affinity(cpu=%d) failed: %s\n",
                    c->index, c->cpu, strerror(errno));
    }
    /* Prefault so the measured window is steady-state traffic, not first touch. */
    for (size_t i = 0; i < c->elems; ++i)
        c->data[i] = (uint64_t)i;
    pthread_barrier_wait(&barrier);          /* all threads enter the window together */
    uint64_t passes = 0;
    while (!stopped) {
        for (size_t i = 0; i < c->elems; ++i)
            c->data[i] += 1;
        ++passes;
    }
    c->passes = passes;
    c->checksum = c->elems ? c->data[c->elems - 1] : 0;
    return NULL;
}

static int parse_cpu_list(const char *s, int *dst, int max)
{
    int n = 0;
    const char *p = s;
    while (*p && n < max) {
        char *end;
        long lo = strtol(p, &end, 10), hi = lo;
        if (*end == '-') { p = end + 1; hi = strtol(p, &end, 10); }
        for (long c = lo; c <= hi && n < max; ++c)
            dst[n++] = (int)c;
        p = end;
        if (*p == ',') ++p;
        else if (*p != '\0') { fprintf(stderr, "bad cpu-list near '%s'\n", p); exit(2); }
    }
    return n;
}

static void usage(const char *argv0)
{
    fprintf(stderr, "usage: %s [--threads N] [--buffer-mib N] "
                    "{--duration|--duration-sec} S [--cpu-list 0,2,4]\n", argv0);
}

int main(int argc, char **argv)
{
    int threads = 8;
    long buffer_mib = 256;
    double duration = 6.0;
    char cpu_list_str[4096] = "";
    int cpu_list[1024], cpu_count = 0;

    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--threads") && i + 1 < argc) threads = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--buffer-mib") && i + 1 < argc) buffer_mib = atol(argv[++i]);
        else if ((!strcmp(argv[i], "--duration") || !strcmp(argv[i], "--duration-sec"))
                 && i + 1 < argc) duration = atof(argv[++i]);
        else if (!strcmp(argv[i], "--cpu-list") && i + 1 < argc) {
            strncpy(cpu_list_str, argv[++i], sizeof(cpu_list_str) - 1);
            cpu_list_str[sizeof(cpu_list_str) - 1] = '\0';
        } else { usage(argv[0]); return 2; }
    }
    if (threads <= 0 || buffer_mib <= 0 || duration <= 0) {
        fprintf(stderr, "threads/buffer/duration must be positive\n");
        return 2;
    }
    if (cpu_list_str[0])
        cpu_count = parse_cpu_list(cpu_list_str, cpu_list,
                                   (int)(sizeof(cpu_list) / sizeof(int)));

    setvbuf(stdout, NULL, _IOLBF, 0);        /* line buffered: host parses incrementally */
    signal(SIGTERM, on_stop);
    signal(SIGINT, on_stop);
    signal(SIGALRM, on_stop);
    alarm((unsigned)(duration + 30));        /* hard ceiling against a stuck window */

    size_t buffer_bytes = (size_t)buffer_mib * 1024UL * 1024UL;
    size_t elems = buffer_bytes / sizeof(uint64_t);
    pthread_t *tids = calloc(threads, sizeof(pthread_t));
    thread_ctx *ctx = calloc(threads, sizeof(thread_ctx));
    if (!tids || !ctx) { perror("calloc"); return 2; }
    pthread_barrier_init(&barrier, NULL, threads + 1);

    phase("alloc");
    for (int i = 0; i < threads; ++i) {
        if (posix_memalign((void **)&ctx[i].data, 64, buffer_bytes) != 0 || !ctx[i].data) {
            fprintf(stderr, "posix_memalign failed (%ld MiB)\n", buffer_mib);
            return 2;
        }
        ctx[i].index = i;
        ctx[i].cpu = cpu_count > 0 ? cpu_list[i % cpu_count] : -1;
        ctx[i].elems = elems;
    }

    phase("spawn");
    for (int i = 0; i < threads; ++i)
        if (pthread_create(&tids[i], NULL, worker, &ctx[i]) != 0) {
            fprintf(stderr, "pthread_create failed for thread %d\n", i);
            stopped = 1;
        }

    phase("load");
    double begin = now();
    pthread_barrier_wait(&barrier);
    struct timespec pause = {0, 100000000};
    while (now() < begin + duration)
        nanosleep(&pause, NULL);
    stopped = 1;
    double elapsed = now() - begin;
    for (int i = 0; i < threads; ++i)
        pthread_join(tids[i], NULL);

    uint64_t total_passes = 0, checksum = 0;
    for (int i = 0; i < threads; ++i) {
        total_passes += ctx[i].passes;
        checksum ^= ctx[i].checksum + (uint64_t)i;
    }
    uint64_t logical_bytes = total_passes * (uint64_t)buffer_bytes * 2ULL;
    printf("{\"event\":\"result\",\"threads\":%d,\"buffer_bytes_per_thread\":%lu,"
           "\"passes_total\":%" PRIu64 ",\"elapsed_sec\":%.6f,"
           "\"logical_read_write_bytes\":%" PRIu64 ",\"checksum\":%" PRIu64 "}\n",
           threads, (unsigned long)buffer_bytes, total_passes, elapsed,
           logical_bytes, checksum);

    for (int i = 0; i < threads; ++i)
        free(ctx[i].data);
    free(ctx);
    free(tids);
    pthread_barrier_destroy(&barrier);
    phase("done");
    return 0;
}
