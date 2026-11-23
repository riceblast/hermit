/* SPDX-License-Identifier: GPL-2.0 */
/* Process observation plus target page_idle writes. Optional --placement
 * publishes snapshots to the Client; never configures swap or cgroups.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "rswap_hotness.h"
#include "../rswap_placement_uapi.h"

#define PAGE_BYTES UINT64_C(4096)
#define PM_PRESENT (UINT64_C(1) << 63)
#define PM_SWAPPED (UINT64_C(1) << 62)
#define PM_PFN ((UINT64_C(1) << 55) - 1)
#define FLAG(n) (UINT64_C(1) << (n))
#define IO_WORDS 512

enum page_state { RESIDENT, SWAPPED, CHANGED, SKIP };
static const char *const state_names[] = {"resident", "swapped", "changed", "skip"};

struct hot_page {
	uint64_t va, pfn, tag, previous_pfn;
	struct heat_window heat;
	uint64_t swap;
	unsigned char state, was_armed, armed, eligible, alias;
	signed char sample;
};

struct page_list {
	struct hot_page *v;
	size_t n, capacity;
};

struct pfn_ref { uint64_t pfn; size_t index; };
struct config {
	pid_t pid;
	double interval, warmup;
	unsigned int window;
	const char *out;
	bool pages, placement, manual;
	int upload_fd;
	const char *upload_path;
	FILE *placement_log;
};

static volatile sig_atomic_t interrupted;
static const char summary_header[] =
	"version\tpid\tgeneration\tstart_ns\tbegin_ns\tend_ns\tready_ns\tphase\tpages\tpresent\tswapped\tactive\tidle\tinvalid\tknown\tmean\tscan_ms\toverrun\tpages_file\n";

static void on_signal(int sig) { interrupted = sig; }

static uint64_t monotonic_ns(void)
{
	struct timespec t;
	if (clock_gettime(CLOCK_MONOTONIC, &t)) {
		perror("clock_gettime");
		exit(1);
	}
	return (uint64_t)t.tv_sec * UINT64_C(1000000000) + t.tv_nsec;
}

static int exact_io(int fd, void *buf, size_t size, uint64_t offset, bool write_it)
{
	size_t done = 0;
	if (offset > INT64_MAX || size > (uint64_t)INT64_MAX - offset) {
		errno = EOVERFLOW;
		return -1;
	}
	while (done < size) {
		ssize_t n = write_it ? pwrite(fd, (char *)buf + done, size - done, offset + done) :
			pread(fd, (char *)buf + done, size - done, offset + done);
		if (n < 0 && errno == EINTR && !interrupted)
			continue;
		if (n <= 0) {
			if (!n) errno = EIO;
			return -1;
		}
		done += n;
	}
	return 0;
}

static int append_page(struct page_list *list, const struct hot_page *page)
{
	if (list->n == list->capacity) {
		size_t next = list->capacity ? list->capacity * 2 : 1024;
		void *p;
		if (next < list->capacity || next > SIZE_MAX / sizeof(*list->v)) {
			errno = ENOMEM;
			return -1;
		}
		p = realloc(list->v, next * sizeof(*list->v));
		if (!p) return -1;
		list->v = p;
		list->capacity = next;
	}
	list->v[list->n++] = *page;
	return 0;
}

static uint64_t map_tag(const char *line)
{
	uint64_t h = UINT64_C(14695981039346656037);
	while (*line && *line != '\n') {
		h ^= (unsigned char)*line++;
		h *= UINT64_C(1099511628211);
	}
	return h;
}

/* Maps are ascending by VA. Histories survive PFN changes, but not detected
 * VMA descriptor changes or a nonpresent, nonswapped PTE. Same-address ABA
 * between scans cannot be detected and is an accepted association error.
 */
static int scan_maps(FILE *maps, int pm, const struct page_list *old,
		     struct page_list *next)
{
	char *line = NULL;
	size_t length = 0, old_i = 0;
	uint64_t entries[IO_WORDS];
	bool present = false, visible_pfn = false;
	int ret = -1;

	while (getline(&line, &length, maps) >= 0) {
		uint64_t begin, end, tag;
		char perms[5];
		if (sscanf(line, "%" SCNx64 "-%" SCNx64 " %4s", &begin, &end, perms) != 3 ||
		    begin >= end || begin % PAGE_BYTES || end % PAGE_BYTES) {
			errno = EINVAL;
			goto out;
		}
		if (perms[0] != 'r' || strstr(line, "[vsyscall]")) continue;
		tag = map_tag(line);
		for (uint64_t va = begin; va < end;) {
			size_t count = (end - va) / PAGE_BYTES;
			if (count > IO_WORDS) count = IO_WORDS;
			if (exact_io(pm, entries, count * 8, (va / PAGE_BYTES) * 8, false))
				goto out;
			for (size_t i = 0; i < count; i++, va += PAGE_BYTES) {
				uint64_t entry = entries[i];
				struct hot_page p = {.va = va, .tag = tag, .sample = -1};
				while (old_i < old->n && old->v[old_i].va < va) old_i++;
				if (!(entry & (PM_PRESENT | PM_SWAPPED))) continue;
				if ((entry & (PM_PRESENT | PM_SWAPPED)) == (PM_PRESENT | PM_SWAPPED)) {
					errno = EPROTO;
					goto out;
				}
				if (old_i < old->n && old->v[old_i].va == va && old->v[old_i].tag == tag) {
					const struct hot_page *prior = &old->v[old_i];
					p.heat = prior->heat;
					p.previous_pfn = prior->pfn ? prior->pfn : prior->previous_pfn;
					p.was_armed = prior->armed;
				}
				if (entry & PM_PRESENT) {
					present = true;
					p.pfn = entry & PM_PFN;
					visible_pfn |= p.pfn != 0;
					p.state = RESIDENT;
				} else {
					p.state = SWAPPED;
					p.swap = entry & PM_PFN;
				}
				if (append_page(next, &p)) goto out;
			}
			if (interrupted) { errno = EINTR; goto out; }
		}
	}
	if (ferror(maps)) goto out;
	if (present && !visible_pfn) {
		fprintf(stderr, "PFNs are hidden or unavailable; CAP_SYS_ADMIN is required.\n");
		errno = EACCES;
		goto out;
	}
	ret = 0;
out:
	free(line);
	return ret;
}

static int ref_compare(const void *a, const void *b)
{
	const struct pfn_ref *x = a, *y = b;
	if (x->pfn != y->pfn) return (x->pfn > y->pfn) - (x->pfn < y->pfn);
	return (x->index > y->index) - (x->index < y->index);
}

static struct pfn_ref *make_refs(const struct page_list *pages, size_t *n)
{
	struct pfn_ref *refs = malloc((pages->n ? pages->n : 1) * sizeof(*refs));
	*n = 0;
	if (!refs) return NULL;
	for (size_t i = 0; i < pages->n; i++)
		if (pages->v[i].state == RESIDENT && pages->v[i].pfn)
			refs[(*n)++] = (struct pfn_ref){pages->v[i].pfn, i};
	qsort(refs, *n, sizeof(*refs), ref_compare);
	return refs;
}

static bool target_flags(uint64_t flags)
{
	const uint64_t excluded = FLAG(15) | FLAG(16) | FLAG(17) | FLAG(18) |
		FLAG(19) | FLAG(20) | FLAG(22) | FLAG(23) | FLAG(24);
	/* SWAPBACKED includes ordinary anonymous and shmem/tmpfs pages. */
	return (flags & FLAG(14)) && !(flags & excluded);
}

static int classify_pages(struct page_list *pages, int flags_fd)
{
	size_t n;
	struct pfn_ref *refs = make_refs(pages, &n);
	uint64_t block[IO_WORDS], previous = UINT64_MAX;
	int ret = -1;
	if (!refs) return -1;
	for (size_t i = 0; i < n; i++) {
		uint64_t base = refs[i].pfn & ~(uint64_t)(IO_WORDS - 1);
		struct hot_page *p = &pages->v[refs[i].index];
		if (base != previous) {
			if (exact_io(flags_fd, block, sizeof(block), base * 8, false)) goto out;
			previous = base;
		}
		uint64_t flags = block[p->pfn - base];
		if (!target_flags(flags)) p->state = SKIP;
		else p->eligible = !!(flags & FLAG(5)); /* Only LRU pages can be armed. */
	}
	/* A zero PFN is never used as an idle target (e.g. the shared zero page). */
	size_t keep = 0;
	for (size_t i = 0; i < pages->n; i++) {
		struct hot_page *p = &pages->v[i];
		if (p->state != SKIP && (p->state != RESIDENT || p->pfn))
			pages->v[keep++] = *p;
	}
	pages->n = keep;
	ret = 0;
out:
	free(refs);
	return ret;
}

static int read_idle(struct page_list *pages, const struct pfn_ref *refs,
		     size_t n, int idle_fd)
{
	uint64_t previous = UINT64_MAX, bits = 0;
	for (size_t i = 0; i < n; i++) {
		struct hot_page *p = &pages->v[refs[i].index];
		uint64_t word = p->pfn / 64;
		if (!p->eligible || !p->was_armed || p->previous_pfn != p->pfn) continue;
		if (word != previous) {
			if (exact_io(idle_fd, &bits, 8, word * 8, false)) return -1;
			previous = word;
		}
		p->sample = (bits & FLAG(p->pfn % 64)) ? 0 : 1;
	}
	return 0;
}

/* Recheck pagemap after idle reads. A changed frame invalidates this round,
 * but does not erase the VA's earlier history. No page is pinned by this tool.
 */
static int verify_pages(struct page_list *pages, int pm)
{
	uint64_t block[IO_WORDS], previous = UINT64_MAX;
	for (size_t i = 0; i < pages->n; i++) {
		struct hot_page *p = &pages->v[i];
		uint64_t vpn = p->va / PAGE_BYTES, base = vpn & ~(uint64_t)(IO_WORDS - 1);
		if (base != previous) {
			if (exact_io(pm, block, sizeof(block), base * 8, false)) return -1;
			previous = base;
		}
		uint64_t entry = block[vpn - base];
		bool same = p->state == RESIDENT ?
			((entry & (PM_PRESENT | PM_SWAPPED)) == PM_PRESENT && (entry & PM_PFN) == p->pfn) :
			((entry & (PM_PRESENT | PM_SWAPPED)) == PM_SWAPPED && (entry & PM_PFN) == p->swap);
		if (!same) {
			if (p->pfn) p->previous_pfn = p->pfn;
			p->pfn = 0;
			p->state = CHANGED;
			p->sample = -1;
			p->eligible = 0;
		}
	}
	return 0;
}

static int arm_pages(struct page_list *pages, const struct pfn_ref *refs,
		     size_t n, int idle_fd)
{
	for (size_t i = 0; i < n;) {
		uint64_t word = refs[i].pfn / 64, bits = 0;
		size_t end = i;
		while (end < n && refs[end].pfn / 64 == word) {
			struct hot_page *p = &pages->v[refs[end].index];
			if (p->state == RESIDENT && p->eligible) bits |= FLAG(p->pfn % 64);
			end++;
		}
		if (bits && exact_io(idle_fd, &bits, 8, word * 8, true)) return -1;
		for (; i < end; i++) {
			struct hot_page *p = &pages->v[refs[i].index];
			p->armed = p->state == RESIDENT && p->eligible;
		}
	}
	return 0;
}

static int collect_round(FILE *maps, int pm, int flags_fd, int idle_fd,
			 const struct page_list *old, struct page_list *next, unsigned int window)
{
	size_t n;
	struct pfn_ref *refs;
	int ret = -1;
	if (scan_maps(maps, pm, old, next) || classify_pages(next, flags_fd)) return -1;
	refs = make_refs(next, &n);
	if (!refs) return -1;
	if (read_idle(next, refs, n, idle_fd) || verify_pages(next, pm)) goto out;
	for (size_t i = 0; i < next->n; i++) heat_push(&next->v[i].heat, next->v[i].sample, window);
	if (arm_pages(next, refs, n, idle_fd)) goto out;
	/* Count a resident PFN or swapped entry once in the mean. Keep all VA rows
	 * for diagnosis. Prefer a known history when an alias has just appeared.
	 */
	n = 0;
	for (size_t i = 0; i < next->n; i++) {
		struct hot_page *p = &next->v[i];
		if (p->state == RESIDENT || p->state == SWAPPED)
			refs[n++] = (struct pfn_ref){p->state == RESIDENT ? p->pfn : p->swap | PM_SWAPPED, i};
	}
	qsort(refs, n, sizeof(*refs), ref_compare);
	for (size_t i = 0; i < n;) {
		size_t end = i + 1, leader = refs[i].index;
		while (end < n && refs[end].pfn == refs[i].pfn) end++;
		for (size_t j = i; j < end; j++) {
			size_t index = refs[j].index;
			next->v[index].alias = 1;
			if (!heat_valid(&next->v[leader].heat) && heat_valid(&next->v[index].heat)) leader = index;
		}
		next->v[leader].alias = 0;
		i = end;
	}
	ret = 0;
out:
	free(refs);
	return ret;
}

/* Reopen proc files each round: a launch wrapper may exec the actual workload.
 * An exec racing the opens can leave one old mm fd at EOF. Retry EIO once;
 * invalidate the baseline because a partial attempt may already have armed
 * some frames. Persistent I/O/permission failures remain explicit errors.
 */
static int collect_process(pid_t pid, int flags_fd, int idle_fd,
			   struct page_list *old, struct page_list *next, unsigned int window)
{
	char path[80];
	for (int attempt = 0; attempt < 2; attempt++) {
		snprintf(path, sizeof(path), "/proc/%ld/maps", (long)pid);
		FILE *maps = fopen(path, "r");
		if (!maps) return -1;
		snprintf(path, sizeof(path), "/proc/%ld/pagemap", (long)pid);
		int pm = open(path, O_RDONLY | O_CLOEXEC);
		if (pm < 0) { int saved = errno; fclose(maps); errno = saved; return -1; }
		int ret = collect_round(maps, pm, flags_fd, idle_fd, old, next, window);
		int saved = errno;
		close(pm);
		fclose(maps);
		if (ret && saved == EIO && !attempt && !interrupted) {
			free(next->v);
			*next = (struct page_list){0};
			for (size_t i = 0; i < old->n; i++) old->v[i].armed = 0;
			continue;
		}
		errno = saved;
		return ret;
	}
	return -1;
}

/* /proc/PID/stat field 22; comm can contain spaces and parentheses. */
static int parse_identity(char *line, uint64_t *start, char *state)
{
	char *end = strrchr(line, ')'), *save, *token;
	if (!end || end[1] != ' ') return -1;
	for (int field = 3; (token = strtok_r(field == 3 ? end + 2 : NULL, " \n", &save)); field++) {
		if (field == 3) *state = token[0];
		if (field == 22) {
			char *tail;
			errno = 0;
			*start = strtoull(token, &tail, 10);
			return errno || *tail ? -1 : 0;
		}
	}
	return -1;
}

static int identity(pid_t pid, uint64_t *start)
{
	char path[80], *line = NULL, state = 0;
	size_t size = 0;
	FILE *f;
	int ret;
	snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
	f = fopen(path, "r");
	if (!f) return errno == ENOENT || errno == ESRCH ? 0 : -1;
	ret = getline(&line, &size, f) < 0 ? -1 : parse_identity(line, start, &state);
	fclose(f);
	free(line);
	if (ret) { errno = EPROTO; return -1; }
	return state == 'Z' || state == 'X' ? 0 : 1;
}

static int upload_command(int fd, struct rswap_upload_command *command,
		struct rswap_upload_ack *ack)
{
	ssize_t written;
	do { written = write(fd, command, sizeof(*command)); }
	while (written < 0 && errno == EINTR && !interrupted);
	if (written != sizeof(*command)) {
		if (written >= 0) errno = EIO;
		return -1;
	}
	return ack ? exact_io(fd, ack, sizeof(*ack), 0, false) : 0;
}

static int placement_start(const struct config *c, uint64_t *t0)
{
	struct rswap_upload_command command = {.version = RSWAP_PLACEMENT_VERSION,
		.op = RSWAP_UPLOAD_START, .count = (uint64_t)(c->warmup * 1e9),
		.value = c->manual};
	struct rswap_upload_ack ack;
	if (upload_command(c->upload_fd, &command, &ack)) return -1;
	if (!ack.run_id || !ack.start_ns || ack.generation) { errno = EPROTO; return -1; }
	*t0 = ack.start_ns;
	return 0;
}

static int heat_record_compare(const void *a, const void *b)
{
	const struct rswap_heat_record *x = a, *y = b;
	return (x->pfn > y->pfn) - (x->pfn < y->pfn);
}

static int placement_snapshot(const struct config *c, const struct page_list *pages,
		uint64_t generation, size_t known, int mean)
{
	size_t n = 0;
	struct rswap_heat_record *records = NULL;
	struct {
		struct rswap_upload_command command;
		struct rswap_heat_record records[RSWAP_UPLOAD_BATCH];
	} packet;
	struct rswap_upload_ack ack;
	struct rswap_upload_command command = {.version = RSWAP_PLACEMENT_VERSION,
		.op = RSWAP_UPLOAD_BEGIN, .generation = generation + 1,
		.value = known, .mean_ppm = mean};
	uint64_t begin = monotonic_ns();
	int ret = -1;

	for (size_t i = 0; i < pages->n; i++) {
		const struct hot_page *p = &pages->v[i];
		n += p->state == RESIDENT && !p->alias && p->pfn && heat_valid(&p->heat);
	}
	if (n) {
		records = malloc(n * sizeof(*records));
		if (!records) return -1;
	}
	n = 0;
	for (size_t i = 0; i < pages->n; i++) {
		const struct hot_page *p = &pages->v[i];
		if (p->state == RESIDENT && !p->alias && p->pfn && heat_valid(&p->heat))
			records[n++] = (struct rswap_heat_record){.pfn = p->pfn,
				.heat_ppm = heat_ppm(&p->heat)};
	}
	if (n > 1) qsort(records, n, sizeof(*records), heat_record_compare);
	command.count = n;
	if (upload_command(c->upload_fd, &command, NULL)) goto out;
	for (size_t i = 0; i < n;) {
		size_t count = n - i;
		if (count > RSWAP_UPLOAD_BATCH) count = RSWAP_UPLOAD_BATCH;
		packet.command = (struct rswap_upload_command){.version = RSWAP_PLACEMENT_VERSION,
			.op = RSWAP_UPLOAD_DATA, .generation = generation + 1, .count = count};
		memcpy(packet.records, records + i, count * sizeof(*records));
		size_t bytes = sizeof(packet.command) + count * sizeof(*records);
		ssize_t written;
		do { written = write(c->upload_fd, &packet, bytes); }
		while (written < 0 && errno == EINTR && !interrupted);
		if (written != (ssize_t)bytes) { if (written >= 0) errno = EIO; goto out; }
		i += count;
	}
	command = (struct rswap_upload_command){.version = RSWAP_PLACEMENT_VERSION,
		.op = RSWAP_UPLOAD_COMMIT, .generation = generation + 1};
	if (upload_command(c->upload_fd, &command, &ack)) goto out;
	if (ack.generation != generation + 1) { errno = EPROTO; goto out; }
	fprintf(c->placement_log, "1\t%" PRIu64 "\t%llu\t%llu\t%llu\t%llu\t%zu\t%zu\t%d\t%.3f\n",
		generation, (unsigned long long)ack.run_id, (unsigned long long)ack.start_ns,
		(unsigned long long)ack.generation, (unsigned long long)ack.publish_ns,
		n, known, mean, (monotonic_ns() - begin) / 1e6);
	if (fflush(c->placement_log) || ferror(c->placement_log)) goto out;
	ret = 0;
out:
	free(records);
	/* An explicit Stop or swapoff ends this run, not observation/the workload.
	 * Other upload failures retain Patch 8's explicit monitor-error behavior.
	 */
	if (ret && errno == ESTALE) return 1;
	return ret;
}

static int snapshot(struct config *c, FILE *summary, const struct page_list *pages,
		    uint64_t generation, uint64_t begin, uint64_t end, uint64_t t0)
{
	size_t known = 0, active = 0, idle = 0, present = 0, swapped = 0;
	long double sum = 0;
	char name[80] = "-", *path = NULL;
	FILE *detail = NULL;
	if (c->pages) {
		snprintf(name, sizeof(name), "pages-%06" PRIu64 ".tsv", generation);
		if (asprintf(&path, "%s/%s", c->out, name) < 0) return -1;
		detail = fopen(path, "wx");
		free(path);
		if (!detail) return -1;
		fprintf(detail, "va\tpfn\tlast_pfn\tstate\tactive\tvalid\theat_ppm\talias\tswap_type\tswap_offset\n");
	}
	for (size_t i = 0; i < pages->n; i++) {
		const struct hot_page *p = &pages->v[i];
		unsigned int valid = heat_valid(&p->heat);
		present += p->state == RESIDENT;
		swapped += p->state == SWAPPED;
		active += p->sample == 1;
		idle += p->sample == 0;
		if (valid && !p->alias) { known++; sum += (long double)heat_active(&p->heat) / valid; }
		if (detail) fprintf(detail, "0x%" PRIx64 "\t%" PRIu64 "\t%" PRIu64 "\t%s\t%u\t%u\t%d\t%u\t%d\t%" PRIu64 "\n",
			p->va, p->pfn, p->pfn ? p->pfn : p->previous_pfn, state_names[p->state],
			heat_active(&p->heat), valid, heat_ppm(&p->heat), p->alias,
			p->state == SWAPPED ? (int)(p->swap & 31) : -1,
			p->state == SWAPPED ? p->swap >> 5 : 0);
	}
	if (detail) {
		bool error = ferror(detail);
		if (fclose(detail)) error = true;
		if (error) { errno = EIO; return -1; }
	}
	/* A row commits a fully written snapshot. Only ready_ns <= Store time may
 	 * be used by the offline checker; end_ns alone would permit future data.
	 */
	double mean = known ? (double)(sum / known) : -1;
	/* Kernel uses integer ppm: floor both the page heat and the original mean.
	 * Equal fixed-point values are cold; no floating point in the Store path.
	 */
	if (c->placement && c->upload_fd >= 0) {
		int uploaded = placement_snapshot(c, pages, generation, known,
			known ? (int)(sum * RSWAP_HEAT_SCALE / known) : -1);
		if (uploaded < 0) return -1;
		if (uploaded > 0) {
			close(c->upload_fd);
			c->upload_fd = -1;
			printf("PLACEMENT stopped by control/swapoff; continuing observation only\n");
		}
	}
	uint64_t ready = monotonic_ns();
	const char *phase = ready - t0 < c->warmup * 1e9 ? "warmup" : "observe";
	fprintf(summary, "1\t%ld\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\t%s\t%zu\t%zu\t%zu\t%zu\t%zu\t%zu\t%zu\t%.9f\t%.3f\t%d\t%s\n",
		(long)c->pid, generation, t0, begin, end, ready, phase, pages->n, present, swapped,
		active, idle, pages->n - active - idle, known, mean, (end - begin) / 1e6,
		ready - begin > c->interval * 1e9, name);
	if (fflush(summary) || ferror(summary)) return -1;
	printf("generation=%" PRIu64 " elapsed=%.3f phase=%s pages=%zu known=%zu mean=%.6f active=%zu idle=%zu invalid=%zu scan_ms=%.3f\n",
		generation, (ready - t0) / 1e9, phase, pages->n, known, mean, active, idle,
		pages->n - active - idle, (end - begin) / 1e6);
	return fflush(stdout);
}

static void usage(FILE *f)
{
	fprintf(f, "Usage: rswap_hotness --out DIR [--interval SEC] [--window N] [--warmup SEC]\n"
		"                     [--pages] [--placement [--manual]] (--pid PID | -- COMMAND [ARGS...])\n"
		"Defaults: interval=1, window=20 (1..64 rounds), warmup=20.\n"
		"DIR must not exist. --pages writes per-round TSVs for offline Store matching.\n"
		"Requires 4-KiB pages, CAP_SYS_ADMIN/PFN visibility, and page_idle write access.\n"
		"--placement starts Client policy before launch (active swap required); --manual waits for adaptive command.\n"
		"Default is observation only. Ctrl-C stops; launched children receive the signal.\n");
}

static int run_metadata(const struct config *c, uint64_t token, uint64_t start,
			uint64_t end, int status, int error, int stop_signal)
{
	char *path;
	if (asprintf(&path, "%s/run.tsv", c->out) < 0) return -1;
	FILE *run = fopen(path, "wx");
	free(path);
	if (!run) return -1;
	fprintf(run, "version\tpid\tstart_ticks\tstart_ns\tend_ns\twindow\tinterval\twarmup\texit_status\tmonitor_error\tstop_signal\n"
		"1\t%ld\t%" PRIu64 "\t%" PRIu64 "\t%" PRIu64 "\t%u\t%.9f\t%.9f\t%d\t%d\t%d\n",
		(long)c->pid, token, start, end, c->window, c->interval, c->warmup, status, error, stop_signal);
	bool failed = ferror(run);
	if (fclose(run)) failed = true;
	if (failed) { errno = EIO; return -1; }
	return 0;
}

static double seconds_value(const char *s, bool zero_ok)
{
	char *end;
	double value;
	errno = 0;
	value = strtod(s, &end);
	if (errno || end == s || *end || !isfinite(value) || value < 0 || (!zero_ok && !value) || value > 86400) return -1;
	return value;
}

static long integer_value(const char *s, long max)
{
	char *end;
	long value;
	errno = 0;
	value = strtol(s, &end, 10);
	return errno || end == s || *end || value < 1 || value > max ? -1 : value;
}

static pid_t launch(char **command, uint64_t *t0, const struct config *c)
{
	int gate[2], result[2], error = 0;
	pid_t pid;
	if (pipe2(gate, O_CLOEXEC)) return -1;
	if (pipe2(result, O_CLOEXEC)) { close(gate[0]); close(gate[1]); return -1; }
	pid = fork();
	if (!pid) {
		char go;
		if (c->placement) close(c->upload_fd);
		close(gate[1]); close(result[0]);
		if (read(gate[0], &go, 1) != 1) _exit(127);
		close(gate[0]);
		signal(SIGPIPE, SIG_DFL);
		execvp(command[0], command);
		error = errno;
		if (write(result[1], &error, sizeof(error)) < 0) _exit(127);
		_exit(127);
	}
	close(gate[0]); close(result[1]);
	if (pid < 0) { close(gate[1]); close(result[0]); return -1; }
	*t0 = monotonic_ns();
	if (c->placement && placement_start(c, t0)) {
		int saved = errno;
		close(gate[1]); close(result[0]);
		while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) {}
		errno = saved;
		return -1;
	}
	ssize_t sent = write(gate[1], "x", 1);
	close(gate[1]);
	ssize_t got;
	do { got = read(result[0], &error, sizeof(error)); } while (got < 0 && errno == EINTR);
	close(result[0]);
	if (sent != 1 || got != 0) {
		kill(pid, SIGTERM);
		waitpid(pid, NULL, 0);
		errno = error ? error : EIO;
		return -1;
	}
	return pid;
}

int main(int argc, char **argv)
{
	struct config c = {.interval = 1, .window = 20, .warmup = 20, .upload_fd = -1,
		.upload_path = "/sys/kernel/debug/rswap/hotness"};
	const struct option options[] = {{"pid", 1, NULL, 'p'}, {"interval", 1, NULL, 'i'},
		{"window", 1, NULL, 'w'}, {"warmup", 1, NULL, 't'}, {"out", 1, NULL, 'o'},
		{"pages", 0, NULL, 'P'}, {"placement", 0, NULL, 'A'},
		{"manual", 0, NULL, 'M'}, {"help", 0, NULL, 'h'}, {NULL, 0, NULL, 0}};
	struct page_list old = {0};
	int option, flags_fd = -1, idle_fd = -1, status = 0, ret = 1, monitor_error;
	pid_t child = -1;
	uint64_t t0 = 0, token = 0, generation = 0, observation_end;
	FILE *summary = NULL;
	char path[PATH_MAX];
	while ((option = getopt_long(argc, argv, "+", options, NULL)) != -1) {
		long n;
		switch (option) {
		case 'p': n = integer_value(optarg, INT_MAX); if (n < 0) goto bad; c.pid = n; break;
		case 'i': c.interval = seconds_value(optarg, false); if (c.interval < 0.001) goto bad; break;
		case 'w': n = integer_value(optarg, 64); if (n < 0) goto bad; c.window = n; break;
		case 't': c.warmup = seconds_value(optarg, true); if (c.warmup < 0) goto bad; break;
		case 'o': c.out = optarg; break;
		case 'P': c.pages = true; break;
		case 'A': c.placement = true; break;
		case 'M': c.manual = true; break;
		case 'h': usage(stdout); return 0;
		default: goto bad;
		}
	}
	if (!c.out || (!!c.pid == (optind < argc)) || (c.manual && !c.placement)) goto bad;
	if (sysconf(_SC_PAGESIZE) != 4096) { fprintf(stderr, "4-KiB system pages required\n"); return 1; }
	flags_fd = open("/proc/kpageflags", O_RDONLY | O_CLOEXEC);
	idle_fd = open("/sys/kernel/mm/page_idle/bitmap", O_RDWR | O_CLOEXEC);
	if (flags_fd < 0 || idle_fd < 0) { perror("open kpageflags/page_idle (privileged access required)"); goto out; }
	if (mkdir(c.out, 0700)) { perror("create output directory (must be new)"); goto out; }
	if (snprintf(path, sizeof(path), "%s/summary.tsv", c.out) >= (int)sizeof(path)) { errno = ENAMETOOLONG; goto fail; }
	summary = fopen(path, "wx");
	if (!summary) goto fail;
	if (fcntl(fileno(summary), F_SETFD, FD_CLOEXEC)) goto fail;
	fputs(summary_header, summary);
	if (fflush(summary)) goto fail;
	if (c.placement) {
		c.upload_fd = open(c.upload_path, O_RDWR | O_CLOEXEC);
		if (c.upload_fd < 0) goto fail;
		if (snprintf(path, sizeof(path), "%s/placement.tsv", c.out) >= (int)sizeof(path)) {
			errno = ENAMETOOLONG; goto fail;
		}
		c.placement_log = fopen(path, "wx");
		if (!c.placement_log || fcntl(fileno(c.placement_log), F_SETFD, FD_CLOEXEC)) goto fail;
		fputs("version\tsample_generation\trun_id\tstart_ns\tgeneration\tpublish_ns\trecords\tknown\tmean_ppm\tupload_ms\n", c.placement_log);
	}
	struct sigaction action = {.sa_handler = on_signal};
	sigemptyset(&action.sa_mask);
	sigaction(SIGINT, &action, NULL); sigaction(SIGTERM, &action, NULL);
	signal(SIGPIPE, SIG_IGN);
	t0 = monotonic_ns();
	if (!c.pid) {
		child = launch(argv + optind, &t0, &c);
		if (child < 0) goto fail;
		c.pid = child;
	} else if (c.placement && placement_start(&c, &t0)) {
		goto fail;
	}
	int alive = identity(c.pid, &token);
	if (alive < 0) goto fail;
	if (!alive && child < 0) { errno = ESRCH; goto fail; }
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("START pid=%ld start_ns=%" PRIu64 " interval=%.6f window=%u warmup=%.3f mode=%s\n",
		(long)c.pid, t0, c.interval, c.window, c.warmup, child > 0 ? "launch" : "attach");
	while (alive && !interrupted) {
		uint64_t begin = monotonic_ns(), current;
		struct page_list next = {0};
		alive = identity(c.pid, &current);
		if (alive < 0) goto fail;
		if (!alive || current != token) break;
		int collected = collect_process(c.pid, flags_fd, idle_fd, &old, &next, c.window);
		int saved = errno;
		alive = identity(c.pid, &current);
		if (!alive || (alive > 0 && current != token) || interrupted) { free(next.v); break; }
		if (alive < 0 || collected) { free(next.v); errno = saved; goto fail; }
		uint64_t end = monotonic_ns();
		free(old.v);
		old = next;
		if (snapshot(&c, summary, &old, generation++, begin, end, t0)) goto fail;
		uint64_t deadline = begin + (uint64_t)(c.interval * 1e9);
		struct timespec until = {.tv_sec = deadline / 1000000000, .tv_nsec = deadline % 1000000000};
		int slept;
		do { slept = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &until, NULL); }
		while (slept == EINTR && !interrupted);
		if (slept && slept != EINTR) { errno = slept; goto fail; }
	}
	ret = 0;
	goto out;
bad:
	usage(stderr);
	return 2;
fail:
	perror("rswap hotness");
out:
	observation_end = monotonic_ns();
	/* Closing the owner fd stops policy even on errors, before waiting for a
	 * launched child. SIGKILL also closes it; inherited child copy was closed.
	 */
	if (c.upload_fd >= 0) close(c.upload_fd);
	if (c.placement_log && fclose(c.placement_log)) ret = 1;
	if (flags_fd >= 0) close(flags_fd);
	if (idle_fd >= 0) close(idle_fd);
	if (summary && fclose(summary)) ret = 1;
	free(old.v);
	monitor_error = ret;
	if (child > 0) {
		if (interrupted || ret) kill(child, interrupted ? interrupted : SIGTERM);
		pid_t waited;
		do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
		if (waited < 0) ret = 1;
		else if (!ret) ret = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
	}
	if (interrupted) ret = 128 + interrupted;
	if (summary && t0 && run_metadata(&c, token, t0, observation_end, ret, monitor_error, interrupted)) {
		perror("write run metadata");
		ret = 1;
	}
	return ret;
}
