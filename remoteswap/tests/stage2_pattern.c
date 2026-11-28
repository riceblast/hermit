/* SPDX-License-Identifier: GPL-2.0 */
/* Operator-driven hot/cold, swap and empty-PTE checks. No system configuration. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define PAGES 8192UL
static uint64_t *data;
static atomic_int active, quitting;
static pthread_mutex_t access_lock = PTHREAD_MUTEX_INITIALIZER;
static int discarded;
static uint64_t value(size_t word) { return UINT64_C(0xa573ec198572ab01) ^ (word * UINT64_C(0x9e3779b97f4a7c15)); }
static void verify(void)
{
	for (size_t word = 0; word < PAGES * 512; word++) {
		uint64_t expected = discarded && word < PAGES * 256 ? 0 : value(word);
		if (((volatile uint64_t *)data)[word] != expected) {
			fprintf(stderr, "DATA FAIL word=%zu\n", word); exit(1);
		}
	}
}
static void *worker(void *unused)
{
	volatile uint64_t sum = 0;
	(void)unused;
	while (!atomic_load(&quitting)) {
		pthread_mutex_lock(&access_lock);
		if (atomic_load(&active))
			for (size_t p = 0; p < PAGES / 2; p++) sum += ((volatile uint64_t *)data)[p * 512];
		pthread_mutex_unlock(&access_lock);
		struct timespec t = {.tv_nsec = 10000000}; nanosleep(&t, NULL);
	}
	return NULL;
}
int main(void)
{
	char command[80];
	data = mmap(NULL, PAGES * 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED || madvise(data, PAGES * 4096, MADV_NOHUGEPAGE)) { perror("mmap"); return 1; }
	for (size_t w = 0; w < PAGES * 512; w++) data[w] = value(w);
	void *reserved = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (reserved == MAP_FAILED) return 1;
	atomic_store(&active, 1);
	pthread_t thread;
	int e = pthread_create(&thread, NULL, worker, NULL);
	if (e) { errno = e; perror("pthread_create"); return 1; }
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("{\"event\":\"ready\",\"pid\":%ld,\"base\":%" PRIuPTR ",\"pages\":%lu,\"reserved\":%" PRIuPTR "}\n",
	       (long)getpid(), (uintptr_t)data, PAGES, (uintptr_t)reserved);
	while (fgets(command, sizeof(command), stdin)) {
		command[strcspn(command, "\n")] = 0;
		if (!strcmp(command, "out")) {
			atomic_store(&active, 0);
			pthread_mutex_lock(&access_lock);
			if (madvise(data, PAGES / 2 * 4096, MADV_PAGEOUT)) { perror("pageout"); return 1; }
			pthread_mutex_unlock(&access_lock);
		} else if (!strcmp(command, "dirty")) {
			/* A read-only swap-in can retain a valid clean swap copy. Force
			 * the next pageout through Store, without changing any contents. */
			pthread_mutex_lock(&access_lock);
			for (size_t p = 0; p < PAGES; p++)
				((volatile uint64_t *)data)[p * 512] = discarded && p < PAGES / 2 ? 0 : value(p * 512);
			pthread_mutex_unlock(&access_lock);
		} else if (!strcmp(command, "outcold")) {
			if (madvise(data + PAGES * 256, PAGES / 2 * 4096, MADV_PAGEOUT)) { perror("pageout cold"); return 1; }
		} else if (!strcmp(command, "in")) {
			verify(); /* swap-in must preserve every word */
			atomic_store(&active, 1);
		} else if (!strcmp(command, "discard")) {
			atomic_store(&active, 0);
			pthread_mutex_lock(&access_lock);
			if (madvise(data, PAGES / 2 * 4096, MADV_DONTNEED)) { perror("dontneed"); return 1; }
			discarded = 1;
			pthread_mutex_unlock(&access_lock);
		} else if (!strcmp(command, "refault")) {
			verify(); /* DONTNEED contents zero; cold half still has its pattern */
			/* Write one word per page so anonymous zero-page PTEs become private. */
			for (size_t p = 0; p < PAGES / 2; p++) data[p * 512] = 0;
			atomic_store(&active, 1);
		} else if (!strcmp(command, "finish")) {
			atomic_store(&active, 0); atomic_store(&quitting, 1);
			pthread_join(thread, NULL); verify();
		} else return 2;
		printf("{\"event\":\"done\",\"command\":\"%s\"}\n", command);
		if (!strcmp(command, "finish")) { munmap(data, PAGES * 4096); munmap(reserved, 4096); return 0; }
	}
	return 1;
}
