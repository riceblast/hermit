/* SPDX-License-Identifier: GPL-2.0 */
/* Two threads share three prefaulted anonymous regions. No swap/cgroup setup. */
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

static atomic_int phase;
struct worker { volatile uint64_t *data; size_t pages; unsigned int id; uint64_t sum; };

static uint64_t now_ns(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}

static void *access_pages(void *arg)
{
	struct worker *w = arg;
	uint64_t sum = 0;
	int current;
	while ((current = atomic_load(&phase)) < 2) {
		size_t third = w->pages / 3;
		size_t begin = current == 0 ? 0 : 2 * third;
		for (size_t i = begin + w->id; i < begin + third; i += 2)
			sum += w->data[i * 512];
		/* Medium region is touched only during a short part of each 3s cycle. */
		if (now_ns() % 3000000000ULL < 200000000ULL)
			for (size_t i = third + w->id; i < 2 * third; i += 2)
				sum += w->data[i * 512];
	}
	w->sum = sum;
	return NULL;
}

static long positive(const char *s, long max)
{
	char *tail;
	errno = 0;
	long value = strtol(s, &tail, 10);
	return errno || tail == s || *tail || value < 1 || value > max ? -1 : value;
}

int main(int argc, char **argv)
{
	long mib = argc == 3 ? positive(argv[1], 65536) : -1;
	long seconds = argc == 3 ? positive(argv[2], 3600) : -1;
	if (mib < 0 || seconds < 0 || sysconf(_SC_PAGESIZE) != 4096) {
		fprintf(stderr, "Usage: hotness_pattern MiB seconds_per_phase (4-KiB pages)\n");
		return 2;
	}
	size_t bytes = (size_t)mib * 1024 * 1024, pages = bytes / 4096;
	uint64_t *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED) { perror("mmap"); return 1; }
	if (madvise(data, bytes, MADV_NOHUGEPAGE)) { perror("madvise"); munmap(data, bytes); return 1; }
	for (size_t i = 0; i < pages; i++) data[i * 512] = i ^ UINT64_C(0x123456789abcdef);
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("READY pid=%ld begin=%p end=%p pages=%zu threads=2\n", (long)getpid(), (void *)data, (void *)((char *)data + bytes), pages);
	struct worker workers[2] = {{data, pages, 0, 0}, {data, pages, 1, 0}};
	pthread_t threads[2];
	unsigned int created = 0;
	for (; created < 2; created++) {
		int error = pthread_create(&threads[created], NULL, access_pages, &workers[created]);
		if (error) {
			errno = error; perror("pthread_create"); atomic_store(&phase, 2);
			for (unsigned int i = 0; i < created; i++) pthread_join(threads[i], NULL);
			munmap(data, bytes); return 1;
		}
	}
	for (int p = 0; p < 2; p++) {
		atomic_store(&phase, p);
		printf("PHASE=%d mono_ns=%" PRIu64 " hot_third=%d medium_third=1 cold_third=%d\n", p, now_ns(), p ? 2 : 0, p ? 0 : 2);
		struct timespec remaining = {.tv_sec = seconds};
		while (nanosleep(&remaining, &remaining) && errno == EINTR) {}
	}
	atomic_store(&phase, 2);
	for (unsigned int i = 0; i < created; i++) pthread_join(threads[i], NULL);
	for (size_t i = 0; i < pages; i++)
		if (data[i * 512] != (i ^ UINT64_C(0x123456789abcdef))) {
			fprintf(stderr, "FAIL page=%zu\n", i); munmap(data, bytes); return 1;
		}
	printf("PASS pages=%zu checksum=%" PRIu64 "\n", pages, workers[0].sum + workers[1].sum);
	munmap(data, bytes);
	return 0;
}
