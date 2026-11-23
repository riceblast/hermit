/* Shared-address-space swap readback. Does not configure cgroups or swap. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <unistd.h>

#define PAGE_BYTES 4096
#define WORDS_PER_PAGE (PAGE_BYTES / sizeof(uint64_t))
#define BATCH_PAGES 64

struct verification {
	uint64_t *data;
	size_t pages;
	uint64_t seed, generation;
	pthread_barrier_t batch;
	pthread_mutex_t start;
	bool abort_start;
	atomic_int failed;
};

static uint64_t number(const char *s)
{
	char *end;
	unsigned long long n;

	errno = 0;
	n = strtoull(s, &end, 10);
	if (errno || end == s || *end || *s == '-' || !n) {
		fprintf(stderr, "Invalid positive integer: %s\n", s);
		exit(1);
	}
	return n;
}

static uint64_t pattern(uint64_t seed, uint64_t generation, size_t page, size_t word)
{
	uint64_t x = seed ^ (generation * UINT64_C(0x9e3779b97f4a7c15)) ^
		((page + 1) * UINT64_C(0xd1b54a32d192ed03)) ^ word;

	x = (x ^ (x >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
	x = (x ^ (x >> 27)) * UINT64_C(0x94d049bb133111eb);
	return x ^ (x >> 31);
}

static void *verify(void *arg)
{
	struct verification *v = arg;
	size_t base, i, w;
	bool abort_start;

	/* A failed pthread_create must not strand earlier workers at a barrier. */
	pthread_mutex_lock(&v->start);
	abort_start = v->abort_start;
	pthread_mutex_unlock(&v->start);
	if (abort_start)
		return NULL;
	for (base = 0; base < v->pages; base += BATCH_PAGES) {
		pthread_barrier_wait(&v->batch);
		if (atomic_load(&v->failed))
			break;
		/* All threads read the same batch, including the same swapped VAs.
		 * Direction changes only between generations, never between workers.
		 */
		for (i = base; i < v->pages && i < base + BATCH_PAGES; i++) {
			size_t p = v->generation & 1 ? v->pages - 1 - i : i;

			for (w = 0; w < WORDS_PER_PAGE; w++) {
				uint64_t expected = pattern(v->seed, v->generation, p, w);
				uint64_t actual = ((volatile uint64_t *)v->data)[p * WORDS_PER_PAGE + w];

				if (actual != expected) {
					if (!atomic_exchange(&v->failed, 1))
						fprintf(stderr, "FAIL generation=%" PRIu64
							" page=%zu word=%zu expected=%016" PRIx64
							" actual=%016" PRIx64 "\n",
							v->generation, p, w, expected, actual);
					/* Everyone still reaches the next batch barrier. */
					goto next_batch;
				}
			}
		}
next_batch:
		;
	}
	return NULL;
}

static int verify_shared(struct verification *v, unsigned int nr_threads)
{
	pthread_t threads[32];
	unsigned int created = 0, i;
	int ret;

	atomic_store(&v->failed, 0);
	v->abort_start = false;
	ret = pthread_mutex_init(&v->start, NULL);
	if (ret) {
		errno = ret;
		perror("pthread_mutex_init");
		return -1;
	}
	ret = pthread_barrier_init(&v->batch, NULL, nr_threads);
	if (ret) {
		errno = ret;
		perror("pthread_barrier_init");
		pthread_mutex_destroy(&v->start);
		return -1;
	}
	pthread_mutex_lock(&v->start);
	for (i = 0; i < nr_threads; i++) {
		ret = pthread_create(&threads[i], NULL, verify, v);
		if (ret) {
			errno = ret;
			perror("pthread_create");
			v->abort_start = true;
			break;
		}
		created++;
	}
	pthread_mutex_unlock(&v->start);
	for (i = 0; i < created; i++)
		pthread_join(threads[i], NULL);
	pthread_barrier_destroy(&v->batch);
	pthread_mutex_destroy(&v->start);
	return v->abort_start || atomic_load(&v->failed) ? -1 : 0;
}

int main(int argc, char **argv)
{
	struct verification v = {0};
	uint64_t mib, rounds, nr_threads;
	size_t bytes, p, w;
	int result = 1;

	if (argc < 4 || argc > 5) {
		fprintf(stderr, "Usage: %s <MiB> <rounds> <threads:1..32> [seed]\n", argv[0]);
		return 1;
	}
	mib = number(argv[1]);
	rounds = number(argv[2]);
	nr_threads = number(argv[3]);
	v.seed = argc == 5 ? number(argv[4]) : (uint64_t)getpid();
	if (mib > SIZE_MAX / (1024 * 1024) || rounds > 1000 || nr_threads > 32 ||
	    sysconf(_SC_PAGESIZE) != PAGE_BYTES) {
		fprintf(stderr, "Unsupported size, rounds, thread count or page size.\n");
		return 1;
	}
	bytes = (size_t)mib * 1024 * 1024;
	v.pages = bytes / PAGE_BYTES;
	v.data = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (v.data == MAP_FAILED) {
		perror("mmap");
		return 1;
	}
	if (madvise(v.data, bytes, MADV_NOHUGEPAGE)) {
		perror("MADV_NOHUGEPAGE");
		goto out;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("START pid=%ld MiB=%" PRIu64 " rounds=%" PRIu64
	       " threads=%" PRIu64 " seed=%" PRIu64 " THP=disabled\n",
	       (long)getpid(), mib, rounds, nr_threads, v.seed);
	for (v.generation = 0; v.generation < rounds; v.generation++) {
		struct rusage usage;

		for (p = 0; p < v.pages; p++)
			for (w = 0; w < WORDS_PER_PAGE; w++)
				v.data[p * WORDS_PER_PAGE + w] = pattern(v.seed, v.generation, p, w);
		printf("READY generation=%" PRIu64 " pid=%ld; press ENTER to verify.\n",
		       v.generation, (long)getpid());
		if (getchar() != '\n') {
			fprintf(stderr, "STOP: expected ENTER; no final PASS.\n");
			goto out;
		}
		if (verify_shared(&v, (unsigned int)nr_threads))
			goto out;
		if (getrusage(RUSAGE_SELF, &usage)) {
			perror("getrusage");
			goto out;
		}
		printf("VERIFIED generation=%" PRIu64 " pages=%zu threads=%" PRIu64
		       " major_faults=%ld\n", v.generation, v.pages, nr_threads, usage.ru_majflt);
	}
	printf("PASS: all %" PRIu64 " generations verified by all %" PRIu64 " threads.\n",
	       rounds, nr_threads);
	result = 0;
out:
	munmap(v.data, bytes);
	return result;
}
