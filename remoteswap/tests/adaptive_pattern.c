/* SPDX-License-Identifier: GPL-2.0 */
/* Controlled hot/cold Store workload; owns only its mappings, no host setup.
 * A controller waits for a committed heat snapshot at each READY before ENTER.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static atomic_int stop_worker;
struct region { volatile uint64_t *data; size_t begin, end; uint64_t sum; };

static uint64_t pattern(size_t word, unsigned int generation)
{
	uint64_t x = word ^ ((uint64_t)(generation + 1) * UINT64_C(0x9e3779b97f4a7c15));
	x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
	return x ^ (x >> 31);
}

static void *touch(void *arg)
{
	struct region *r = arg;
	uint64_t sum = 0;
	while (!atomic_load(&stop_worker)) {
		for (size_t p = r->begin; p < r->end; p++) sum += r->data[p * 512];
		struct timespec delay = {.tv_nsec = 1000000};
		nanosleep(&delay, NULL);
	}
	r->sum = sum;
	return NULL;
}

static int wait_enter(void)
{
	if (getchar() == '\n') return 0;
	fprintf(stderr, "STOP: controller ENTER required; no PASS reached\n");
	return -1;
}

static int verify(volatile uint64_t *data, size_t words, unsigned int generation)
{
	for (size_t w = 0; w < words; w++) {
		uint64_t actual = data[w], expected = pattern(w, generation);
		if (actual != expected) {
			fprintf(stderr, "FAIL generation=%u word=%zu actual=%016" PRIx64 " expected=%016" PRIx64 "\n",
				generation, w, actual, expected);
			return -1;
		}
	}
	return 0;
}

int main(int argc, char **argv)
{
	char *tail = NULL;
	errno = 0;
	unsigned long mib = argc == 2 ? strtoul(argv[1], &tail, 10) : 0;
	if (argc != 2 || errno || !tail || *tail || mib < 1 || mib > 4096 || sysconf(_SC_PAGESIZE) != 4096) {
		fprintf(stderr, "Usage: adaptive_pattern MiB (1..4096); three controller ENTERs required\n");
		return 2;
	}
	size_t bytes = mib * 1024UL * 1024, pages = bytes / 4096;
	uint64_t *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED) { perror("mmap"); return 1; }
	int ret = 1;
	if (madvise(data, bytes, MADV_NOHUGEPAGE)) { perror("MADV_NOHUGEPAGE"); goto out; }
	setvbuf(stdout, NULL, _IOLBF, 0);
	for (unsigned int generation = 0; generation < 2; generation++) {
		/* Dirty every word again so round two requires fresh Store decisions. */
		for (size_t w = 0; w < bytes / 8; w++) data[w] = pattern(w, generation);
		struct region r = {.data = data, .begin = generation ? pages / 2 : 0,
			.end = generation ? pages : pages / 2};
		pthread_t worker;
		atomic_store(&stop_worker, 0);
		int error = pthread_create(&worker, NULL, touch, &r);
		if (error) { errno = error; perror("pthread_create"); goto out; }
		printf("READY generation=%u pid=%ld begin=%p end=%p hot_half=%u pages=%zu; wait for published heat, then ENTER\n",
			generation, (long)getpid(), (void *)data, (void *)((char *)data + bytes), generation, pages);
		int stopped = wait_enter();
		atomic_store(&stop_worker, 1);
		pthread_join(worker, NULL);
		if (stopped) goto out;
		/* This alters referenced bits. Only the snapshot committed BEFORE this
		 * call describes the intended hot/cold input for these Stores.
		 */
		if (madvise(data, bytes, MADV_PAGEOUT)) { perror("MADV_PAGEOUT"); goto out; }
		if (verify(data, bytes / 8, generation)) goto out;
		printf("VERIFIED generation=%u pages=%zu checksum=%" PRIu64 "\n", generation, pages, r.sum);
	}
	printf("READY_FINAL pid=%ld; live swapoff, then ENTER for final content check\n", (long)getpid());
	if (wait_enter() || verify(data, bytes / 8, 1)) goto out;
	printf("PASS: both hot/cold phases and final content verified\n");
	ret = 0;
out:
	munmap(data, bytes);
	return ret;
}
