/* SPDX-License-Identifier: GPL-2.0 */
/* Small operator-controlled anonymous-page workload. No host configuration. */
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

int main(int argc, char **argv)
{
	char *end, command[32];
	unsigned long mib;
	unsigned int generation = 0;
	uint64_t seed = (uint64_t)getpid();
	if (argc != 2 || sysconf(_SC_PAGESIZE) != 4096)
		return 2;
	errno = 0;
	mib = strtoul(argv[1], &end, 10);
	if (errno || *end || mib < 2 || mib > 64 || mib % 2)
		return 2;
	size_t bytes = mib * 1024UL * 1024, words = bytes / 8;
	uint64_t *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
			     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED || madvise(data, bytes, MADV_NOHUGEPAGE)) {
		perror("anonymous mapping");
		return 1;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	for (size_t w = 0; w < words; w++)
		data[w] = pattern(seed, generation, w);
	printf("{\"event\":\"ready\",\"pid\":%ld,\"base\":%" PRIuPTR
	       ",\"bytes\":%zu,\"generation\":0}\n", (long)getpid(), (uintptr_t)data, bytes);
	while (fgets(command, sizeof(command), stdin)) {
		command[strcspn(command, "\n")] = 0;
		if (!strcmp(command, "rewrite")) {
			generation++;
			for (size_t w = 0; w < words; w++)
				data[w] = pattern(seed, generation, w);
		} else if (!strncmp(command, "out ", 4)) {
			size_t start = 0, length = bytes;
			if (!strcmp(command + 4, "a"))
				length /= 2;
			else if (!strcmp(command + 4, "b")) {
				start = bytes / 2;
				length /= 2;
			} else if (strcmp(command + 4, "all"))
				return 2;
			if (madvise((char *)data + start, length, MADV_PAGEOUT)) {
				perror("MADV_PAGEOUT");
				return 1;
			}
		} else if (!strcmp(command, "verify") || !strcmp(command, "finish")) {
			for (size_t w = 0; w < words; w++) {
				uint64_t actual = ((volatile uint64_t *)data)[w];
				uint64_t expected = pattern(seed, generation, w);
				if (actual != expected) {
					fprintf(stderr, "FAIL generation=%u word=%zu actual=%016" PRIx64
						" expected=%016" PRIx64 "\n", generation, w, actual, expected);
					return 1;
				}
			}
		} else
			return 2;
		printf("{\"event\":\"done\",\"command\":\"%s\",\"generation\":%u}\n", command, generation);
		if (!strcmp(command, "finish")) {
			munmap(data, bytes);
			return 0;
		}
	}
	fprintf(stderr, "STOP: controller closed before finish; no PASS\n");
	return 1;
}
