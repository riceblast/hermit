/* SPDX-License-Identifier: GPL-2.0 */
/* Compile the unchanged production CLI with write/close observation hooks.
 * Queries use the very same held owner fd; no second uploader or idle writer.
 * Evidence goes to stdout, not extra sampler files. Audit errors SIGSTOP this
 * process, retaining its owner fds until the operator decides how to recover.
 */
#define _GNU_SOURCE
#include <sys/types.h>
#include <unistd.h>
static ssize_t audited_write(int, const void *, size_t);
static int audited_close(int);
#define write audited_write
#define close audited_close
#define main production_sampler_main
#include "../tools/rswap_hotness.c"
#undef main
#undef close
#undef write
#include <assert.h>

#define WATCH_MAX 256
static struct rswap_vpn_heat_command begun;
static struct rswap_vpn_heat_record selected[WATCH_MAX], watched[WATCH_MAX];
static uint64_t watch_vpn[WATCH_MAX], seen, current_pages, heat_sum, previous_vpn;
static size_t selected_count, watched_count, watch_count, watch_cursor;
static int heat_owner = -1, heat_hold = -1, identity_owner = -1;
static pid_t target_pid;
static uint64_t target_token;
static bool transaction;
static uint64_t range_begin, range_count, range_pages, range_records, range_sum, bins[5];
static void fail(const char *reason)
{
	printf("AUDIT_ERROR %s errno=%d\n", reason, errno); fflush(stdout);
	raise(SIGSTOP);
	_exit(1);
}
static void check(bool ok, const char *why) { if (!ok) fail(why); }
static int watch_compare(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return (x > y) - (x < y);
}
static bool query(int fd, uint64_t binding, uint64_t vpn, struct rswap_vpn_heat_ack *a)
{
	struct rswap_vpn_heat_command q = {.version=1,.op=RSWAP_VPN_HEAT_QUERY,.binding_id=binding,.vpn=vpn};
	ssize_t n = write(fd,&q,sizeof(q));
	if (n < 0 && errno == ESTALE && target_pid && target_finished(target_pid,target_token)) return false;
	check(n == sizeof(q), "query-write");
	n = pread(fd,a,sizeof(*a),0);
	if (n < 0 && errno == ESTALE && target_pid && target_finished(target_pid,target_token)) return false;
	check(n == sizeof(*a), "query-read");
	return true;
}
static bool audit_record(int fd, const struct rswap_vpn_heat_record *r, bool watched_record)
{
	struct rswap_vpn_heat_ack a;
	if (!query(fd,begun.binding_id,r->vpn,&a)) return false;
	check(a.binding_id==begun.binding_id && a.has_snapshot && a.found && a.window==begun.window &&
	      a.records==begun.count && a.pages==begun.pages && a.sum_heat==begun.sum_heat && a.heat==r->heat,
	      "known-VPN-query-mismatch-or-unknown");
	if (watched_record)
		printf("WATCH {\"window\":%llu,\"vpn\":%llu,\"heat\":%llu,\"in_scope\":%u}\n",
		       begun.window,r->vpn,r->heat,r->in_scope);
	return true;
}
static ssize_t audited_write(int fd, const void *buffer, size_t size)
{
	struct rswap_vpn_heat_command c;
	/* identity bind is the only small text write issued by the sampler. */
	if (size < sizeof(c)) {
		if (size >= 5 && !memcmp(buffer,"bind ",5)) {
			char text[sizeof(c)]; long pid;
			memcpy(text,buffer,size); text[size]=0;
			check(sscanf(text,"bind %ld",&pid)==1 && pid>0 && pid<=INT_MAX,"target-PID");
			target_pid=pid;
			(void)identity(target_pid,&target_token);
			identity_owner=fd;
		}
		return write(fd,buffer,size);
	}
	memcpy(&c,buffer,sizeof(c));
	if (c.version!=RSWAP_VPN_HEAT_VERSION || c.op<1 || c.op>5) return write(fd,buffer,size);
	ssize_t ret=write(fd,buffer,size);
	if (ret!=(ssize_t)size) return ret; /* production CLI reports syscall errors */
	if (c.op==RSWAP_VPN_HEAT_BIND) {
		heat_owner=fd; heat_hold=dup(fd); check(heat_hold>=0,"dup-owner");
	} else if (c.op==RSWAP_VPN_HEAT_BEGIN) {
		begun=c; seen=current_pages=heat_sum=previous_vpn=0;
		selected_count=watched_count=watch_cursor=0; transaction=true;
		range_pages=range_records=range_sum=0; memset(bins,0,sizeof(bins));
	} else if (c.op==RSWAP_VPN_HEAT_DATA) {
		check(transaction && c.window==begun.window && size==sizeof(c)+c.count*sizeof(struct rswap_vpn_heat_record),"data-frame");
		const struct rswap_vpn_heat_record *records=(const void *)((const char *)buffer+sizeof(c));
		for (size_t i=0;i<c.count;i++) {
			const struct rswap_vpn_heat_record *r=&records[i];
			check(!r->reserved && r->in_scope<=1 && r->heat<=RSWAP_VPN_HEAT_SCALE &&
			      (!seen || r->vpn>previous_vpn),"invalid-heat-record");
			if (r->in_scope) { current_pages++; heat_sum+=r->heat; bins[heat_bin(r->heat)]++; }
			if (r->vpn>=range_begin && r->vpn-range_begin<range_count) {
				range_records++; if (r->in_scope) { range_pages++; range_sum+=r->heat; }
			}
			while (watch_cursor<watch_count && watch_vpn[watch_cursor]<r->vpn) watch_cursor++;
			if (watch_cursor<watch_count && watch_vpn[watch_cursor]==r->vpn) {
				check(watched_count<WATCH_MAX,"watch-capacity"); watched[watched_count++]=*r; watch_cursor++;
			}
			if (selected_count<32 && seen==(uint64_t)selected_count*begun.count/32)
				selected[selected_count++]=*r;
			previous_vpn=r->vpn; seen++;
		}
	} else if (c.op==RSWAP_VPN_HEAT_COMMIT) {
		check(transaction && seen==begun.count && current_pages==begun.pages && heat_sum==begun.sum_heat,"snapshot-total-mismatch");
		for (size_t i=0;i<selected_count;i++) if (!audit_record(fd,&selected[i],false)) goto exited;
		for (size_t i=0;i<watched_count;i++) if (!audit_record(fd,&watched[i],true)) goto exited;
		for (size_t i=0;i<watch_count;i++) {
			bool found=false;
			for (size_t j=0;j<watched_count;j++) if (watched[j].vpn==watch_vpn[i]) found=true;
			if (!found) {
				struct rswap_vpn_heat_ack a;
				if (!query(fd,c.binding_id,watch_vpn[i],&a)) goto exited;
				check(a.has_snapshot && !a.found && !a.heat,"new-VPN-must-be-zero");
				printf("MISSING_ZERO {\"window\":%llu,\"vpn\":%" PRIu64 "}\n",c.window,watch_vpn[i]);
			}
		}
		struct rswap_vpn_heat_command outside={.version=1,.op=5,.binding_id=c.binding_id+1,.vpn=1};
		errno=0; check(write(fd,&outside,sizeof(outside))==-1 && errno==ESTALE,"outside-binding-not-rejected");
		/* Restore query to VPN 0 for the production COMMIT acknowledgement. */
		struct rswap_vpn_heat_ack ack;
		if (!query(fd,c.binding_id,0,&ack)) goto exited;
		printf("AUDIT {\"window\":%llu,\"records\":%llu,\"pages\":%llu,\"sum_heat\":%llu,"
		       "\"checked_queries\":%zu,\"watched\":%zu,\"unknown\":0,\"binding_id\":%llu,\"mono_ns\":%" PRIu64 ","
		       "\"range_records\":%" PRIu64 ",\"range_pages\":%" PRIu64 ",\"range_sum\":%" PRIu64 ","
		       "\"bins\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "]}\n",
		       c.window,begun.count,begun.pages,begun.sum_heat,selected_count+watched_count,watched_count,c.binding_id,monotonic_ns(),
		       range_records,range_pages,range_sum,bins[0],bins[1],bins[2],bins[3],bins[4]);
		fflush(stdout); transaction=false;
	}
	return ret;
exited:
	/* COMMIT can succeed just before the target exits. Stop the observation
	 * without certifying an audit/summary that could not be read back. */
	puts("AUDIT_TARGET_EXIT unfinished_observation_discarded=1"); fflush(stdout);
	errno=ESTALE;
	return -1;
}
static int audited_close(int fd)
{
	int ret=close(fd);
	if (fd==identity_owner && heat_hold>=0) {
		struct rswap_vpn_heat_command c={.version=1,.op=5,.binding_id=begun.binding_id,.vpn=1};
		errno=0; check(write(heat_hold,&c,sizeof(c))==-1 && errno==ESTALE,"closed-identity-still-valid");
		check(!close(heat_hold),"close-held-owner"); heat_hold=-1;
		puts("LIFECYCLE closed_identity_rejected=1"); fflush(stdout);
	}
	return ret;
}
int main(int argc,char **argv)
{
	const char *range=getenv("RSWAP_STAGE2_RANGE");
	if (range) check(sscanf(range,"%" SCNu64 ":%" SCNu64,&range_begin,&range_count)==2 && range_count,"invalid-range");
	const char *path=getenv("RSWAP_STAGE2_WATCH");
	if (path) {
		FILE *f=fopen(path,"r"); check(f!=NULL,"open-watch-list");
		while (watch_count<WATCH_MAX && fscanf(f,"%" SCNu64,&watch_vpn[watch_count])==1) watch_count++;
		check(feof(f),"watch-list-overflow-or-malformed"); fclose(f);
		qsort(watch_vpn,watch_count,sizeof(watch_vpn[0]),watch_compare);
		for (size_t i=1;i<watch_count;i++) check(watch_vpn[i]!=watch_vpn[i-1],"duplicate-watch-VPN");
	}
	return production_sampler_main(argc,argv);
}
