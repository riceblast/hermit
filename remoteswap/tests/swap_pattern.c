/* Userspace hardware-test workload. Does not configure swap or cgroups. */
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>

#define PAGE_BYTES 4096
#define WORDS_PER_PAGE (PAGE_BYTES / sizeof(uint64_t))

static uint64_t number(const char *s)
{
	char *end;
	unsigned long long value;

	errno = 0;
	value = strtoull(s, &end, 10);
	if (errno || end == s || *end || *s == '-' || !value) {
		fprintf(stderr, "Invalid positive integer: %s\n", s);
		exit(1);
	}
	return value;
}

static uint64_t pattern(uint64_t seed, uint64_t generation,
			uint64_t page, uint64_t word)
{
	uint64_t x = seed ^ (generation * UINT64_C(0x9e3779b97f4a7c15)) ^
		((page + 1) * UINT64_C(0xd1b54a32d192ed03)) ^ word;

	x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
	return x ^ (x >> 31);
}

static int wait_for_enter(void)
{
	int c = getchar();

	if (c == '\n')
		return 0;
	fprintf(stderr, "STOP: expected ENTER; no final PASS was reached.\n");
	return -1;
}

int main(int argc, char **argv)
{
	uint64_t mib, rounds, seed, generation;
	size_t bytes, pages, p, w;
	uint64_t *data;
	int result = 1;

	if (argc < 3 || argc > 4) {
		fprintf(stderr, "Usage: %s <MiB> <rounds> [positive seed]\n", argv[0]);
		return 1;
	}
	mib = number(argv[1]);
	rounds = number(argv[2]);
	seed = argc == 4 ? number(argv[3]) : (uint64_t)getpid();
	if (mib > SIZE_MAX / (1024 * 1024) || rounds > 1000 ||
	    sysconf(_SC_PAGESIZE) != PAGE_BYTES) {
		fprintf(stderr, "Unsupported size, rounds (1..1000), or page size.\n");
		return 1;
	}
	bytes = (size_t)mib * 1024 * 1024;
	pages = bytes / PAGE_BYTES;
	data = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (data == MAP_FAILED) {
		perror("mmap");
		return 1;
	}
	if (madvise(data, bytes, MADV_NOHUGEPAGE)) {
		perror("MADV_NOHUGEPAGE");
		goto out;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("START pid=%ld MiB=%" PRIu64 " rounds=%" PRIu64
	       " seed=%" PRIu64 " THP=disabled\n", (long)getpid(), mib, rounds, seed);
	for (generation = 0; generation < rounds; generation++) {
		struct rusage usage;

		for (p = 0; p < pages; p++)
			for (w = 0; w < WORDS_PER_PAGE; w++)
				data[p * WORDS_PER_PAGE + w] = pattern(seed, generation, p, w);
		printf("READY generation=%" PRIu64
		       " pid=%ld; press ENTER to verify (keep this process alive).\n",
		       generation, (long)getpid());
		if (wait_for_enter())
			goto out;
		/* Alternate traversal direction; read every word through volatile. */
		for (size_t i = 0; i < pages; i++) {
			p = generation & 1 ? pages - 1 - i : i;
			for (w = 0; w < WORDS_PER_PAGE; w++) {
				uint64_t expected = pattern(seed, generation, p, w);
				uint64_t actual = ((volatile uint64_t *)data)[p * WORDS_PER_PAGE + w];

				if (actual != expected) {
					fprintf(stderr, "FAIL generation=%" PRIu64
						" page=%zu word=%zu expected=%016" PRIx64
						" actual=%016" PRIx64 "\n",
						generation, p, w, expected, actual);
					goto out;
				}
			}
		}
		if (getrusage(RUSAGE_SELF, &usage)) {
			perror("getrusage");
			goto out;
		}
		printf("VERIFIED generation=%" PRIu64 " pages=%zu major_faults=%ld\n",
		       generation, pages, usage.ru_majflt);
	}
	printf("PASS: all %" PRIu64 " generations verified, every 4-KiB page and word.\n", rounds);
	result = 0;
out:
	munmap(data, bytes);
	return result;
}
