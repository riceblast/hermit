/* SPDX-License-Identifier: GPL-2.0 */
/* VPN histories for one fixed mm. Observation only unless --publish is set.
 * No swap/cgroup configuration or placement-policy changes are performed.
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

#define PAGE_BYTES UINT64_C(4096)
#define PM_PRESENT (UINT64_C(1) << 63)
#define PM_SWAPPED (UINT64_C(1) << 62)
#define PM_FILE (UINT64_C(1) << 61)
#define PM_PFN ((UINT64_C(1) << 55) - 1)
#define FLAG(n) (UINT64_C(1) << (n))
#define IO_WORDS 512
/* This repository's CONFIG_MIGRATION + CONFIG_MEMORY_FAILURE kernel reserves
 * types 29..31 for migration/hwpoison; CONFIG_DEVICE_PRIVATE is disabled. */
#define NORMAL_SWAP_TYPES 29
#define RESIDENT 1
#define IN_SCOPE 2
#define ELIGIBLE 4
#define ARMED 8
#define WAS_ARMED 16
/* /proc/PID/stat field 9, paired kernel include/linux/sched.h PF_EXITING.
 * exit_mm() can clear task->mm long before the task becomes a zombie. */
#define TASK_EXITING UINT64_C(0x00000004)

/* A missing/empty PTE leaves the record in the history with flags=0. */
struct hot_page {
	uint64_t vpn, heat, pfn;
	unsigned int hits;
	unsigned char flags, sample;
};
struct page_list { struct hot_page *v; size_t n, capacity; };
struct pfn_ref { uint64_t pfn; size_t index; };
struct totals { uint64_t pages, sum, bins[5]; };
struct publisher { int identity_fd, heat_fd; uint64_t binding; };
struct config { pid_t pid; double interval; unsigned int window; const char *out; bool publish; };
static volatile sig_atomic_t interrupted;
static const char summary_header[] =
	"pid\tEWMA_window\tmean_hotness\tpages\tdist[0 - 0.2]\tdist[0.2 - 0.4]\t"
	"dist[0.4 - 0.6]\tdist[0.6 - 0.8]\tdist[0.8 - 1.0]\n";

static void on_signal(int sig) { interrupted = sig; }
static uint64_t monotonic_ns(void)
{
	struct timespec t;
	if (clock_gettime(CLOCK_MONOTONIC, &t)) { perror("clock_gettime"); exit(1); }
	return (uint64_t)t.tv_sec * UINT64_C(1000000000) + t.tv_nsec;
}

static int exact_io(int fd, void *buf, size_t size, uint64_t offset, bool write_it)
{
	size_t done = 0;
	if (offset > INT64_MAX || size > (uint64_t)INT64_MAX - offset) {
		errno = EOVERFLOW; return -1;
	}
	while (done < size) {
		ssize_t n = write_it ? pwrite(fd, (char *)buf + done, size - done, offset + done) :
			pread(fd, (char *)buf + done, size - done, offset + done);
		if (n < 0 && errno == EINTR && !interrupted) continue;
		if (n <= 0) { if (!n) errno = EIO; return -1; }
		done += n;
	}
	return 0;
}

static int append_page(struct page_list *list, struct hot_page page)
{
	if (list->n == list->capacity) {
		size_t next = list->capacity ? list->capacity * 2 : 1024;
		if (next < list->capacity || next > SIZE_MAX / sizeof(page)) { errno = ENOMEM; return -1; }
		void *p = realloc(list->v, next * sizeof(page));
		if (!p) return -1;
		list->v = p; list->capacity = next;
	}
	list->v[list->n++] = page;
	return 0;
}

/* Private mappings only: resident COW pages must additionally have KPF_ANON.
 * Ordinary swapped PTEs in private mappings are anonymous, including file COW.
 * Empty PTEs, shmem, PFNMAP and huge/compound pages are outside the population.
 */
static int scan_maps(FILE *maps, int pm, struct page_list *pages)
{
	char *line = NULL;
	size_t length = 0, vmas = 0;
	uint64_t entries[IO_WORDS] = {0};
	bool present = false, visible = false;
	int ret = -1;
	clearerr(maps);
	if (fseek(maps, 0, SEEK_SET)) return -1;
	while (getline(&line, &length, maps) >= 0) {
		uint64_t begin, end;
		char perms[5];
		if (sscanf(line, "%" SCNx64 "-%" SCNx64 " %4s", &begin, &end, perms) != 3 ||
		    begin >= end || begin % PAGE_BYTES || end % PAGE_BYTES) { errno = EPROTO; goto out; }
		vmas++;
		if (perms[3] != 'p' || strstr(line, "[vsyscall]")) continue;
		for (uint64_t va = begin; va < end;) {
			size_t count = (end - va) / PAGE_BYTES;
			if (count > IO_WORDS) count = IO_WORDS;
			if (exact_io(pm, entries, count * 8, (va / PAGE_BYTES) * 8, false)) goto out;
			for (size_t i = 0; i < count; i++, va += PAGE_BYTES) {
				uint64_t e = entries[i], state = e & (PM_PRESENT | PM_SWAPPED);
				struct hot_page p = {.vpn = va / PAGE_BYTES, .flags = IN_SCOPE};
				if (!state) continue;
				if (state == (PM_PRESENT | PM_SWAPPED)) { errno = EPROTO; goto out; }
				if ((e & PM_FILE) || (state == PM_SWAPPED && (e & 31) >= NORMAL_SWAP_TYPES)) continue;
				if (state == PM_PRESENT) {
					present = true;
					p.pfn = e & PM_PFN;
					visible |= p.pfn != 0;
					p.flags |= RESIDENT;
				}
				if (append_page(pages, p)) goto out;
			}
			if (interrupted) { errno = EINTR; goto out; }
		}
	}
	if (ferror(maps)) goto out;
	/* Proc fds pin mm_count, not mm_users. EOF after exec is never a new run. */
	if (!vmas) { errno = ESTALE; goto out; }
	if (present && !visible) { errno = EACCES; goto out; }
	ret = 0;
out:
	free(line); return ret;
}

static int ref_compare(const void *a, const void *b)
{
	const struct pfn_ref *x = a, *y = b;
	if (x->pfn != y->pfn) return (x->pfn > y->pfn) - (x->pfn < y->pfn);
	return (x->index > y->index) - (x->index < y->index);
}
static struct pfn_ref *make_refs(const struct page_list *pages, size_t *n)
{
	if (pages->n > SIZE_MAX / sizeof(struct pfn_ref)) { errno = ENOMEM; return NULL; }
	struct pfn_ref *refs = malloc((pages->n ? pages->n : 1) * sizeof(*refs));
	*n = 0;
	if (!refs) return NULL;
	for (size_t i = 0; i < pages->n; i++)
		if ((pages->v[i].flags & (IN_SCOPE | RESIDENT)) == (IN_SCOPE | RESIDENT) && pages->v[i].pfn)
			refs[(*n)++] = (struct pfn_ref){pages->v[i].pfn, i};
	qsort(refs, *n, sizeof(*refs), ref_compare);
	return refs;
}
static bool target_flags(uint64_t f)
{
	const uint64_t excluded = FLAG(15) | FLAG(16) | FLAG(17) | FLAG(18) |
		FLAG(19) | FLAG(20) | FLAG(22) | FLAG(23) | FLAG(24);
	return (f & FLAG(12)) && (f & FLAG(14)) && !(f & excluded);
}
static int classify_pages(struct page_list *pages, int flags_fd)
{
	size_t n, keep = 0;
	struct pfn_ref *refs = make_refs(pages, &n);
	uint64_t block[IO_WORDS], previous = UINT64_MAX;
	if (!refs) return -1;
	for (size_t i = 0; i < n; i++) {
		struct hot_page *p = &pages->v[refs[i].index];
		uint64_t base = p->pfn & ~(uint64_t)(IO_WORDS - 1);
		if (base != previous) {
			if (exact_io(flags_fd, block, sizeof(block), base * 8, false)) { free(refs); return -1; }
			previous = base;
		}
		uint64_t f = block[p->pfn - base];
		if (!target_flags(f)) p->flags = 0;
		else if (f & FLAG(5)) p->flags |= ELIGIBLE;
	}
	free(refs);
	for (size_t i = 0; i < pages->n; i++) {
		struct hot_page p = pages->v[i];
		if ((p.flags & IN_SCOPE) && (!(p.flags & RESIDENT) || p.pfn)) pages->v[keep++] = p;
	}
	pages->n = keep;
	return 0;
}

/* Both inputs sorted by VPN. Never key history by PFN, VMA text or swap entry. */
static int merge_history(const struct page_list *old, const struct page_list *current,
		struct page_list *next)
{
	size_t i = 0, j = 0;
	while (i < old->n || j < current->n) {
		struct hot_page p;
		if (j == current->n || (i < old->n && old->v[i].vpn < current->v[j].vpn)) {
			p = old->v[i++]; p.flags = 0; p.pfn = 0; p.sample = 0;
		} else {
			p = current->v[j++];
			if (i < old->n && old->v[i].vpn == p.vpn) {
				const struct hot_page *prior = &old->v[i++];
				p.heat = prior->heat; p.hits = prior->hits;
				if ((p.flags & RESIDENT) && (prior->flags & ARMED) && prior->pfn == p.pfn)
					p.flags |= WAS_ARMED;
			}
		}
		if (append_page(next, p)) return -1;
	}
	return 0;
}
static int read_idle(struct page_list *pages, const struct pfn_ref *refs, size_t n, int fd)
{
	uint64_t previous = UINT64_MAX, bits = 0;
	if (n && !pages->v) { errno = EINVAL; return -1; }
	for (size_t i = 0; i < n; i++) {
		struct hot_page *p = &pages->v[refs[i].index];
		uint64_t word = p->pfn / 64;
		if ((p->flags & (ELIGIBLE | WAS_ARMED)) != (ELIGIBLE | WAS_ARMED)) continue;
		if (word != previous) {
			if (exact_io(fd, &bits, 8, word * 8, false)) return -1;
			previous = word;
		}
		p->sample = !(bits & FLAG(p->pfn % 64));
	}
	return 0;
}

/* Batched reread, only for sampling validation. Mid-window same-PFN ABA is
 * deliberately not tracked. A changed resident frame contributes sample=0.
 */
static int verify_pages(struct page_list *pages, int pm, int flags_fd)
{
	uint64_t block[IO_WORDS], previous = UINT64_MAX;
	for (size_t i = 0; i < pages->n; i++) {
		struct hot_page *p = &pages->v[i];
		if (!(p->flags & IN_SCOPE)) continue;
		uint64_t base = p->vpn & ~(uint64_t)(IO_WORDS - 1);
		if (base != previous) {
			if (exact_io(pm, block, sizeof(block), base * 8, false)) return -1;
			previous = base;
		}
		uint64_t e = block[p->vpn - base];
		uint64_t state = e & (PM_PRESENT | PM_SWAPPED);
		if ((p->flags & RESIDENT) && state == PM_PRESENT && (e & PM_PFN) == p->pfn && !(e & PM_FILE)) continue;
		p->sample = 0; p->pfn = 0; p->flags = 0;
		if (e & PM_FILE) continue;
		if (state == PM_SWAPPED && (e & 31) < NORMAL_SWAP_TYPES) p->flags = IN_SCOPE;
		else if (state == PM_PRESENT && (e & PM_PFN)) {
			uint64_t flags, pfn = e & PM_PFN;
			if (exact_io(flags_fd, &flags, 8, pfn * 8, false)) return -1;
			if (target_flags(flags)) {
				p->pfn = pfn; p->flags = IN_SCOPE | RESIDENT;
				/* Changed PFNs get a fresh baseline next round. */
			}
		}
	}
	return 0;
}
static int arm_pages(struct page_list *pages, const struct pfn_ref *refs, size_t n, int fd)
{
	for (size_t i = 0; i < n;) {
		uint64_t word = refs[i].pfn / 64, bits = 0;
		size_t end = i;
		while (end < n && refs[end].pfn / 64 == word) {
			struct hot_page *p = &pages->v[refs[end].index];
			if (p->pfn == refs[end].pfn && (p->flags & (RESIDENT | ELIGIBLE)) == (RESIDENT | ELIGIBLE)) bits |= FLAG(p->pfn % 64);
			end++;
		}
		if (bits && exact_io(fd, &bits, 8, word * 8, true)) return -1;
		for (; i < end; i++) {
			struct hot_page *p = &pages->v[refs[i].index];
			if (p->pfn == refs[i].pfn && (p->flags & (RESIDENT | ELIGIBLE)) == (RESIDENT | ELIGIBLE)) p->flags |= ARMED;
		}
	}
	return 0;
}
static int collect_round(FILE *maps, int pm, int flags_fd, int idle_fd,
		const struct page_list *old, struct page_list *next)
{
	struct page_list current = {0};
	struct pfn_ref *refs = NULL;
	size_t n;
	int ret = -1;
	if (scan_maps(maps, pm, &current) || classify_pages(&current, flags_fd) ||
	    merge_history(old, &current, next)) goto out;
	/* Release current before sorting the full history's resident subset. */
	free(current.v); current.v = NULL;
	refs = make_refs(next, &n);
	if (!refs || read_idle(next, refs, n, idle_fd) || verify_pages(next, pm, flags_fd) ||
	    arm_pages(next, refs, n, idle_fd)) goto out;
	ret = 0;
out:
	free(refs); free(current.v); return ret;
}
static void finish_round(struct page_list *pages, unsigned int rounds, bool update)
{
	for (size_t i = 0; i < pages->n; i++) {
		struct hot_page *p = &pages->v[i];
		p->hits += p->sample == 1;
		if (update) { p->heat = heat_update(p->heat, p->hits, rounds); p->hits = 0; }
	}
}
static int count_pages(const struct page_list *pages, struct totals *t)
{
	memset(t, 0, sizeof(*t));
	for (size_t i = 0; i < pages->n; i++) if (pages->v[i].flags & IN_SCOPE) {
		if (t->pages == UINT32_MAX) { errno = EOVERFLOW; return -1; }
		t->pages++; t->sum += pages->v[i].heat;
		t->bins[heat_bin(pages->v[i].heat)]++;
	}
	return 0;
}
static int snapshot(FILE *out, pid_t pid, uint64_t window, uint64_t elapsed_ns,
		const struct totals *t)
{
	char label[80];
	if (!window) snprintf(label, sizeof(label), "0(0s)");
	else snprintf(label, sizeof(label), "%" PRIu64 "(%.3fs)", window, elapsed_ns / 1e9);
	long double mean = t->pages ? (long double)t->sum / RSWAP_VPN_HEAT_SCALE / t->pages : 0;
	if (fprintf(out, "%-8ld\t%-22s\t%-12.2Lf\t%-12" PRIu64, (long)pid, label, mean, t->pages) < 0) return -1;
	for (unsigned int i = 0; i < 5; i++) {
		char cell[80];
		snprintf(cell, sizeof(cell), "%" PRIu64 "(%.1Lf%%)", t->bins[i],
			t->pages ? (long double)t->bins[i] * 100 / t->pages : 0);
		if (fprintf(out, "\t%-18s", cell) < 0) return -1;
	}
	return fputc('\n', out) == EOF || fflush(out) ? -1 : 0;
}

static int command_write(int fd, const struct rswap_vpn_heat_command *cmd,
		const struct rswap_vpn_heat_record *rows)
{
	unsigned char buffer[sizeof(*cmd) + RSWAP_VPN_HEAT_BATCH * sizeof(*rows)];
	size_t bytes = cmd->op == RSWAP_VPN_HEAT_DATA ? cmd->count * sizeof(*rows) : 0;
	if (bytes > sizeof(buffer) - sizeof(*cmd)) { errno = EINVAL; return -1; }
	memcpy(buffer, cmd, sizeof(*cmd));
	if (bytes) memcpy(buffer + sizeof(*cmd), rows, bytes);
	ssize_t n;
	do { n = write(fd, buffer, sizeof(*cmd) + bytes); } while (n < 0 && errno == EINTR && !interrupted);
	if (n != (ssize_t)(sizeof(*cmd) + bytes)) { if (n >= 0) errno = EIO; return -1; }
	return 0;
}
static int publisher_current(const struct publisher *p)
{
	char status[512];
	ssize_t n = pread(p->identity_fd, status, sizeof(status) - 1, 0);
	if (n < 0) return -1;
	status[n] = 0;
	char *id = strstr(status, "binding_id=");
	uint64_t binding = 0;
	if (!id || sscanf(id, "binding_id=%" SCNu64, &binding) != 1 || binding != p->binding ||
	    !strstr(status, "attached=1") || !strstr(status, "mm_current=1")) { errno = ESTALE; return -1; }
	return 0;
}
static int publisher_start(struct publisher *p, pid_t pid)
{
	char bind[80], status[512];
	p->identity_fd = open("/sys/kernel/debug/rswap/identity", O_RDWR | O_CLOEXEC);
	if (p->identity_fd < 0) return -1;
	int len = snprintf(bind, sizeof(bind), "bind %ld", (long)pid);
	if (write(p->identity_fd, bind, len) != len) return -1;
	ssize_t n = pread(p->identity_fd, status, sizeof(status) - 1, 0);
	if (n < 0) return -1;
	status[n] = 0;
	char *id = strstr(status, "binding_id=");
	if (!id || sscanf(id, "binding_id=%" SCNu64, &p->binding) != 1 || publisher_current(p)) {
		errno = ESTALE; return -1;
	}
	p->heat_fd = open("/sys/kernel/debug/rswap/vpn_heat", O_RDWR | O_CLOEXEC);
	if (p->heat_fd < 0) return -1;
	struct rswap_vpn_heat_command cmd = {.version = RSWAP_VPN_HEAT_VERSION,
		.op = RSWAP_VPN_HEAT_BIND, .binding_id = p->binding};
	return command_write(p->heat_fd, &cmd, NULL);
}
static int publish_snapshot(const struct publisher *p, const struct page_list *pages,
		uint64_t window, const struct totals *t)
{
	struct rswap_vpn_heat_record rows[RSWAP_VPN_HEAT_BATCH];
	struct rswap_vpn_heat_command cmd = {.version = RSWAP_VPN_HEAT_VERSION,
		.op = RSWAP_VPN_HEAT_BEGIN, .binding_id = p->binding, .window = window,
		.count = pages->n, .pages = t->pages, .sum_heat = t->sum};
	struct rswap_vpn_heat_ack ack;
	if (publisher_current(p) || command_write(p->heat_fd, &cmd, NULL)) return -1;
	cmd.op = RSWAP_VPN_HEAT_DATA; cmd.pages = cmd.sum_heat = 0;
	for (size_t i = 0; i < pages->n;) {
		size_t n = pages->n - i;
		if (n > RSWAP_VPN_HEAT_BATCH) n = RSWAP_VPN_HEAT_BATCH;
		for (size_t j = 0; j < n; j++) rows[j] = (struct rswap_vpn_heat_record){
			.vpn = pages->v[i+j].vpn, .heat = pages->v[i+j].heat,
			.in_scope = !!(pages->v[i+j].flags & IN_SCOPE)};
		cmd.count = n;
		if (command_write(p->heat_fd, &cmd, rows)) return -1;
		i += n;
	}
	cmd.op = RSWAP_VPN_HEAT_COMMIT; cmd.count = 0;
	if (command_write(p->heat_fd, &cmd, NULL) || exact_io(p->heat_fd, &ack, sizeof(ack), 0, false)) return -1;
	if (!ack.has_snapshot || ack.binding_id != p->binding || ack.window != window ||
	    ack.records != pages->n || ack.pages != t->pages || ack.sum_heat != t->sum) { errno = EPROTO; return -1; }
	return 0;
}

/* /proc/PID/stat field 22; comm can contain spaces and parentheses. */
static int identity(pid_t pid, uint64_t *start)
{
	char path[80], *line = NULL, *save, *token, state = 0;
	size_t size = 0;
	int ret = -1;
	uint64_t flags = 0;
	snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
	FILE *f = fopen(path, "r");
	if (!f) return errno == ENOENT || errno == ESRCH ? 0 : -1;
	if (getline(&line, &size, f) < 0) { if (!ferror(f)) errno = EPROTO; goto out; }
	char *end = strrchr(line, ')');
	if (!end || end[1] != ' ') { errno = EPROTO; goto out; }
	errno = EPROTO;
	for (int field = 3; (token = strtok_r(field == 3 ? end + 2 : NULL, " \n", &save)); field++) {
		if (field == 3) state = token[0];
		if (field == 9) {
			char *tail;
			errno = 0; flags = strtoull(token, &tail, 10);
			if (errno || *tail || *token == '-') { errno = EPROTO; goto out; }
		}
		if (field == 22) {
			char *tail;
			errno = 0; *start = strtoull(token, &tail, 10);
			if (!errno && !*tail && *token != '-')
				ret = state == 'Z' || state == 'X' || (flags & TASK_EXITING) ? 0 : 1;
			else errno = EPROTO;
			break;
		}
	}
 out:
	if (ret < 0 && !errno) errno = EPROTO;
	fclose(f); free(line); return ret;
}
/* Recheck only lifecycle errors, preserving the original diagnostic. A live
 * target with a new mm (exec), revoked binding or swapoff is still an error.
 * Never follow a replacement PID or turn an unfinished group into output.
 */
static bool target_finished(pid_t pid, uint64_t token)
{
	uint64_t current = 0;
	int saved = errno, alive = identity(pid, &current);
	errno = saved;
	return !alive || (alive > 0 && current != token);
}
static pid_t launch(char **command)
{
	int result[2], error = 0;
	if (pipe2(result, O_CLOEXEC)) return -1;
	pid_t pid = fork();
	if (!pid) {
		close(result[0]); signal(SIGPIPE, SIG_DFL);
		execvp(command[0], command);
		error = errno;
		if (write(result[1], &error, sizeof(error)) < 0) _exit(127);
		_exit(127);
	}
	close(result[1]);
	if (pid < 0) { close(result[0]); return -1; }
	ssize_t n;
	do { n = read(result[0], &error, sizeof(error)); } while (n < 0 && errno == EINTR);
	close(result[0]);
	if (n) {
		kill(pid, SIGTERM);
		while (waitpid(pid, NULL, 0) < 0 && errno == EINTR) {}
		errno = error ? error : EIO; return -1;
	}
	return pid;
}
static double seconds_value(const char *s)
{
	char *end;
	errno = 0; double n = strtod(s, &end);
	return errno || end == s || *end || !isfinite(n) || n < .001 || n > 86400 ? -1 : n;
}
static long integer_value(const char *s, long max)
{
	char *end;
	errno = 0; long n = strtol(s, &end, 10);
	return errno || end == s || *end || n < 1 || n > max ? -1 : n;
}
static void usage(FILE *f)
{
	fputs("Usage: rswap_hotness --out NEW_DIR [--a-bit-window SECONDS] [--ewma-window N]\n"
	      "                     [--publish] (--pid PID | -- COMMAND...)\n"
	      "Defaults: wait 5 seconds AFTER each scan; EWMA every 4 completed rounds.\n"
	      "--interval/--window are aliases. --publish needs Client 1.18 identity/vpn_heat.\n"
	      "Only summary.tsv is written. This phase does not enable placement policy.\n", f);
}

int main(int argc, char **argv)
{
	struct config c = {.interval = 5, .window = 4};
	struct publisher p = {.identity_fd = -1, .heat_fd = -1};
	const struct option options[] = {{"pid", 1, NULL, 'p'}, {"out", 1, NULL, 'o'},
		{"a-bit-window", 1, NULL, 'i'}, {"interval", 1, NULL, 'i'},
		{"ewma-window", 1, NULL, 'w'}, {"window", 1, NULL, 'w'},
		{"publish", 0, NULL, 'U'}, {"placement", 0, NULL, 'A'},
		{"help", 0, NULL, 'h'}, {NULL, 0, NULL, 0}};
	struct page_list old = {0};
	int option, flags_fd = -1, idle_fd = -1, pm = -1, ret = 1, status = 0;
	pid_t child = -1;
	FILE *maps = NULL, *summary = NULL;
	uint64_t token = 0, baseline = 0, window = 0;
	unsigned int rounds = 0;
	char path[PATH_MAX];
	bool observing = false;
	while ((option = getopt_long(argc, argv, "+", options, NULL)) != -1) {
		long n;
		switch (option) {
		case 'p': n = integer_value(optarg, INT_MAX); if (n < 0) goto bad; c.pid = n; break;
		case 'o': c.out = optarg; break;
		case 'i': c.interval = seconds_value(optarg); if (c.interval < 0) goto bad; break;
		case 'w': n = integer_value(optarg, 64); if (n < 0) goto bad; c.window = n; break;
		case 'U': c.publish = true; break;
		case 'A': fputs("--placement belongs to the previous PFN policy; use --publish for VPN observation.\n", stderr); goto bad;
		case 'h': usage(stdout); return 0;
		default: goto bad;
		}
	}
	if (!c.out || (!!c.pid == (optind < argc))) goto bad;
	if (sysconf(_SC_PAGESIZE) != 4096) { fputs("4-KiB system pages required\n", stderr); return 1; }
	flags_fd = open("/proc/kpageflags", O_RDONLY | O_CLOEXEC);
	idle_fd = open("/sys/kernel/mm/page_idle/bitmap", O_RDWR | O_CLOEXEC);
	if (flags_fd < 0 || idle_fd < 0) { perror("open kpageflags/page_idle (privileged access required)"); goto out; }
	if (mkdir(c.out, 0700)) { perror("create output directory (must be new)"); goto out; }
	if (snprintf(path, sizeof(path), "%s/summary.tsv", c.out) >= (int)sizeof(path)) { errno = ENAMETOOLONG; goto fail; }
	summary = fopen(path, "wxe");
	if (!summary || fputs(summary_header, summary) == EOF || fflush(summary)) goto fail;
	struct sigaction action = {.sa_handler = on_signal};
	sigemptyset(&action.sa_mask);
	sigaction(SIGINT, &action, NULL); sigaction(SIGTERM, &action, NULL); signal(SIGPIPE, SIG_IGN);
	if (!c.pid) { child = launch(argv + optind); if (child < 0) goto fail; c.pid = child; }
	int alive = identity(c.pid, &token);
	if (alive < 0) goto fail;
	if (!alive) { if (child > 0) { ret = 0; goto out; } errno = ESRCH; goto fail; }
	observing = true;
	/* Bind after successful exec, then hold the same mm's proc fds throughout. */
	if (c.publish && publisher_start(&p, c.pid)) goto fail;
	snprintf(path, sizeof(path), "/proc/%ld/maps", (long)c.pid);
	maps = fopen(path, "re");
	snprintf(path, sizeof(path), "/proc/%ld/pagemap", (long)c.pid);
	pm = open(path, O_RDONLY | O_CLOEXEC);
	if (!maps || pm < 0) {
		if ((errno == ENOENT || errno == ESRCH) && target_finished(c.pid, token)) { ret = 0; goto out; }
		goto fail;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	printf("START pid=%ld a_bit_window=%.3f EWMA_window=%u publish=%u\n",
		(long)c.pid, c.interval, c.window, c.publish);
	while (!interrupted) {
		uint64_t current;
		alive = identity(c.pid, &current);
		if (alive < 0) goto fail;
		if (!alive || current != token) break;
		if (c.publish && publisher_current(&p)) goto fail;
		struct page_list next = {0};
		int collected = collect_round(maps, pm, flags_fd, idle_fd, &old, &next), saved = errno;
		alive = identity(c.pid, &current);
		if (!alive || (alive > 0 && current != token) || interrupted) { free(next.v); break; }
		if (alive < 0) { free(next.v); goto fail; }
		if (collected) { free(next.v); errno = saved; goto fail; }
		free(old.v); old = next;
		bool output = !baseline;
		if (!baseline) baseline = monotonic_ns();
		else {
			rounds++; output = rounds == c.window;
			finish_round(&old, c.window, output);
			if (output) { window++; rounds = 0; }
		}
		if (output) {
			struct totals t;
			if (count_pages(&old, &t) || (c.publish && publish_snapshot(&p, &old, window, &t)) ||
			    snapshot(summary, c.pid, window, monotonic_ns() - baseline, &t)) goto fail;
		}
		/* KISS: scan/output finish, then wait. Never manufacture missed rounds. */
		struct timespec wait = {.tv_sec = (time_t)c.interval,
			.tv_nsec = (long)((c.interval - (time_t)c.interval) * 1e9)};
		int slept;
		do { slept = nanosleep(&wait, &wait); } while (slept && errno == EINTR && !interrupted);
		if (slept && errno != EINTR) goto fail;
	}
	ret = 0; goto out;
bad:
	usage(stderr); return 2;
fail:
	if (observing && (errno == ESTALE || errno == ESRCH) && target_finished(c.pid, token)) {
		ret = 0;
		goto out;
	}
	perror("rswap hotness");
out:
	if (p.heat_fd >= 0) close(p.heat_fd);
	if (p.identity_fd >= 0) close(p.identity_fd);
	if (pm >= 0) close(pm);
	if (maps) fclose(maps);
	if (flags_fd >= 0) close(flags_fd);
	if (idle_fd >= 0) close(idle_fd);
	if (summary && fclose(summary)) ret = 1;
	free(old.v);
	if (child > 0) {
		if (ret || interrupted) kill(child, interrupted ? interrupted : SIGTERM);
		pid_t waited;
		do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
		if (waited < 0) ret = 1;
		else if (!ret) ret = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
	}
	return interrupted ? 128 + interrupted : ret;
}
