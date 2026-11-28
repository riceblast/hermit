/* SPDX-License-Identifier: GPL-2.0 */
/* Explicit MADV_PAGEOUT and per-word verification; no host configuration. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static uint64_t value(size_t word)
{
	return UINT64_C(0xb41326d58e792c01) ^ (word * UINT64_C(0x9e3779b97f4a7c15));
}

int main(int argc, char **argv)
{
	char *end, command[80];
	if (argc != 2 || sysconf(_SC_PAGESIZE) != 4096) return 2;
	errno = 0;
	unsigned long mib = strtoul(argv[1], &end, 10);
	if (errno || *end || !mib || mib > 40960) return 2;
	size_t bytes = mib * 1024UL * 1024, words = bytes / 8;
	uint64_t *data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED || madvise(data, bytes, MADV_NOHUGEPAGE)) { perror("mmap"); return 1; }
	for (size_t i = 0; i < words; i++) data[i] = value(i);
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("{\"event\":\"ready\",\"pid\":%ld,\"base\":%" PRIuPTR ",\"bytes\":%zu}\n",
	       (long)getpid(), (uintptr_t)data, bytes);
	while (fgets(command, sizeof(command), stdin)) {
		command[strcspn(command, "\n")] = 0;
		unsigned long page;
		char extra;
		if (!strcmp(command, "out all")) {
			/* Repeat pageout must create a new Store even when verify only
			 * read a still-valid swap copy. Volatile preserves same-value writes. */
			for (size_t p = 0; p < bytes / 4096; p++)
				((volatile uint64_t *)data)[p * 512] = value(p * 512);
			if (madvise(data, bytes, MADV_PAGEOUT)) { perror("pageout"); return 1; }
		} else if (sscanf(command, "drop %lu %c", &page, &extra) == 1) {
			if (page >= bytes / 4096 || madvise(data + page * 512, 4096, MADV_DONTNEED)) return 1;
		} else if (sscanf(command, "refault %lu %c", &page, &extra) == 1) {
			if (page >= bytes / 4096) return 2;
			for (size_t i = page * 512; i < (page + 1) * 512; i++) data[i] = value(i);
		} else if (sscanf(command, "out %lu %c", &page, &extra) == 1) {
			if (page >= bytes / 4096) return 2;
			((volatile uint64_t *)data)[page * 512] = value(page * 512);
			if (madvise(data + page * 512, 4096, MADV_PAGEOUT)) return 1;
		} else if (!strcmp(command, "verify") || !strcmp(command, "finish")) {
			for (size_t i = 0; i < words; i++)
				if (((volatile uint64_t *)data)[i] != value(i)) {
					fprintf(stderr, "DATA FAIL word=%zu\n", i); return 1;
				}
		} else return 2;
		printf("{\"event\":\"done\",\"command\":\"%s\"}\n", command);
		if (!strcmp(command, "finish")) { munmap(data, bytes); return 0; }
	}
	return 1;
}
