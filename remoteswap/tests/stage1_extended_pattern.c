/* SPDX-License-Identifier: GPL-2.0 */
/* Controlled base-page workload for entry reuse, exec, and direct reclaim. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static uint64_t pattern(uint64_t seed, unsigned int generation, size_t word)
{
	uint64_t x = seed ^ ((uint64_t)(generation + 1) * UINT64_C(0x9e3779b97f4a7c15)) ^ word;
	x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
	return x ^ (x >> 31);
}

static uint64_t *allocate(size_t bytes)
{
	uint64_t *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED || madvise(data, bytes, MADV_NOHUGEPAGE)) {
		perror("anonymous mapping");
		exit(1);
	}
	return data;
}

static void fill(uint64_t *data, size_t words, uint64_t seed, unsigned int generation)
{
	for (size_t w = 0; w < words; w++)
		data[w] = pattern(seed, generation, w);
}

static void verify(uint64_t *data, size_t words, uint64_t seed, unsigned int generation)
{
	for (size_t w = 0; w < words; w++) {
		uint64_t actual = ((volatile uint64_t *)data)[w];
		uint64_t expected = pattern(seed, generation, w);
		if (actual != expected) {
			fprintf(stderr, "FAIL generation=%u word=%zu actual=%016" PRIx64
				" expected=%016" PRIx64 "\n", generation, w, actual, expected);
			exit(1);
		}
	}
}

static void join_cgroup(void)
{
	const char *group = getenv("RSWAP_STAGE1_CGROUP");
	char path[4096];
	if (!group)
		return;
	if (snprintf(path, sizeof(path), "%s/cgroup.procs", group) >= (int)sizeof(path))
		exit(2);
	FILE *tasks = fopen(path, "w");
	if (!tasks || fprintf(tasks, "%ld\n", (long)getpid()) < 0 || fclose(tasks)) {
		perror("join test cgroup");
		exit(1);
	}
}

int main(int argc, char **argv)
{
	char *end, command[32];
	unsigned int generation = 0, reserved = 0;
	uint64_t seed = (uint64_t)getpid();
	void *old_ranges[32];
	if (argc != 2 || sysconf(_SC_PAGESIZE) != 4096)
		return 2;
	errno = 0;
	unsigned long mib = strtoul(argv[1], &end, 10);
	if (errno || *end || mib < 2 || mib > 64 || mib % 2)
		return 2;
	join_cgroup();
	size_t bytes = mib * 1024UL * 1024, words = bytes / 8;
	uint64_t *data = allocate(bytes);
	setvbuf(stdout, NULL, _IOLBF, 0);
	fill(data, words, seed, generation);
	printf("{\"event\":\"ready\",\"pid\":%ld,\"base\":%" PRIuPTR
	       ",\"bytes\":%zu,\"generation\":0}\n", (long)getpid(), (uintptr_t)data, bytes);
	while (fgets(command, sizeof(command), stdin)) {
		command[strcspn(command, "\n")] = 0;
		if (!strcmp(command, "rewrite")) {
			fill(data, words, seed, ++generation);
		} else if (!strcmp(command, "remap")) {
			if (reserved == sizeof(old_ranges) / sizeof(old_ranges[0]))
				return 2;
			verify(data, words, seed, generation);
			void *old = data;
			if (munmap(old, bytes))
				return 1;
			/* Keep each old VA unavailable, ensuring a new logical page range. */
			void *guard = mmap(old, bytes, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS |
					   MAP_FIXED_NOREPLACE, -1, 0);
			if (guard == MAP_FAILED || guard != old) {
				perror("reserve old VA");
				return 1;
			}
			old_ranges[reserved++] = guard;
			data = allocate(bytes);
			fill(data, words, seed, ++generation);
		} else if (!strcmp(command, "exec")) {
			execl("/proc/self/exe", argv[0], argv[1], (char *)NULL);
			perror("exec");
			return 1;
		} else if (!strcmp(command, "out all")) {
			if (madvise(data, bytes, MADV_PAGEOUT)) {
				perror("MADV_PAGEOUT");
				return 1;
			}
		} else if (!strcmp(command, "verify") || !strcmp(command, "finish")) {
			verify(data, words, seed, generation);
		} else
			return 2;
		printf("{\"event\":\"done\",\"command\":\"%s\",\"generation\":%u,"
		       "\"base\":%" PRIuPTR ",\"bytes\":%zu}\n", command, generation, (uintptr_t)data, bytes);
		if (!strcmp(command, "finish")) {
			munmap(data, bytes);
			for (unsigned int i = 0; i < reserved; i++)
				munmap(old_ranges[i], bytes);
			return 0;
		}
	}
	fprintf(stderr, "STOP: controller closed before finish; no PASS\n");
	return 1;
}
