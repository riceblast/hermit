/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM rswap

#if !defined(_RSWAP_TRACE_H) || defined(TRACE_HEADER_MULTI_READ)
#define _RSWAP_TRACE_H

#include <linux/tracepoint.h>
#include "rswap_placement.h"

/* Emitted immediately after the allocation attempt. alloc_seq joins the
 * serialized mapping event even if CPUs emit decision events out of order.
 */
TRACE_EVENT(decision,
	TP_PROTO(u64 offset, u64 pfn, const struct rswap_decision *d,
		 const struct rswap_location *loc, int ret),
	TP_ARGS(offset, pfn, d, loc, ret),
	TP_STRUCT__entry(
		__field(u64, offset) __field(u64, pfn) __field(u64, alloc_seq)
		__field(u64, run) __field(u64, generation)
		__field(u64, decision_ns) __field(u64, publish_ns) __field(u64, rr)
		__field(unsigned int, state) __field(unsigned int, reason)
		__field(int, heat) __field(int, mean)
		__field(unsigned int, preferred_server) __field(unsigned int, preferred_tier)
		__field(unsigned int, server) __field(unsigned int, tier)
		__field(u64, slot) __field(unsigned int, fallback) __field(int, ret)
	),
	TP_fast_assign(
		__entry->offset = offset; __entry->pfn = pfn;
		__entry->alloc_seq = ret ? 0 : loc->alloc_seq;
		__entry->run = d->run_id; __entry->generation = d->generation;
		__entry->decision_ns = d->decision_ns; __entry->publish_ns = d->publish_ns;
		__entry->rr = d->rr; __entry->state = d->state; __entry->reason = d->reason;
		__entry->heat = d->heat; __entry->mean = d->mean;
		__entry->preferred_server = d->server; __entry->preferred_tier = d->tier;
		__entry->server = loc->server; __entry->tier = loc->tier; __entry->slot = loc->slot;
		__entry->fallback = ret ? 0 : (loc->server ^ d->server) + 2 * (loc->tier ^ d->tier);
		__entry->ret = ret;
	),
	TP_printk("alloc_seq=%llu offset=%llu pfn=%llu run=%llu generation=%llu decision_ns=%llu publish_ns=%llu state=%u reason=%u heat_ppm=%d mean_ppm=%d preferred_server=%u preferred_tier=%u server=%u tier=%u slot=%llu fallback=%u rr=%llu alloc_ret=%d",
		__entry->alloc_seq, __entry->offset, __entry->pfn, __entry->run,
		__entry->generation, __entry->decision_ns, __entry->publish_ns,
		__entry->state, __entry->reason, __entry->heat, __entry->mean,
		__entry->preferred_server, __entry->preferred_tier, __entry->server,
		__entry->tier, __entry->slot, __entry->fallback, __entry->rr, __entry->ret)
);

/* Serialized allocator lifecycle; users excludes index ownership. */
TRACE_EVENT(mapping,
	TP_PROTO(u64 seq, unsigned int action, u64 offset, unsigned int server,
		 unsigned int tier, u64 slot, unsigned int users, unsigned int live,
		 unsigned int fallback),
	TP_ARGS(seq, action, offset, server, tier, slot, users, live, fallback),
	TP_STRUCT__entry(
		__field(u64, seq)
		__field(unsigned int, action)
		__field(u64, offset)
		__field(unsigned int, server)
		__field(unsigned int, tier)
		__field(u64, slot)
		__field(unsigned int, users)
		__field(unsigned int, live)
		__field(unsigned int, fallback)
	),
	TP_fast_assign(
		__entry->seq = seq;
		__entry->action = action;
		__entry->offset = offset;
		__entry->server = server;
		__entry->tier = tier;
		__entry->slot = slot;
		__entry->users = users;
		__entry->live = live;
		__entry->fallback = fallback;
	),
	TP_printk("seq=%llu action=%s offset=%llu server=%u tier=%s slot=%llu users=%u live=%u fallback=%u",
		(unsigned long long)__entry->seq,
		__print_symbolic(__entry->action, {0, "alloc"}, {1, "get"},
			{2, "put"}, {3, "invalidate"}, {4, "free"}),
		(unsigned long long)__entry->offset, __entry->server,
		__print_symbolic(__entry->tier, {0, "dram"}, {1, "pmem"}),
		(unsigned long long)__entry->slot, __entry->users,
		__entry->live, __entry->fallback)
);

/* Observation only: ret is submission status, not terminal WC status.
 * start_ns uses CLOCK_MONOTONIC, independent of the configured ftrace clock.
 */
TRACE_EVENT(store_sample,
	TP_PROTO(u64 start_ns, u64 pfn, unsigned int swap_type, u64 offset, int ret),
	TP_ARGS(start_ns, pfn, swap_type, offset, ret),
	TP_STRUCT__entry(
		__field(u64, start_ns)
		__field(u64, pfn)
		__field(unsigned int, swap_type)
		__field(u64, offset)
		__field(int, ret)
	),
	TP_fast_assign(
		__entry->start_ns = start_ns;
		__entry->pfn = pfn;
		__entry->swap_type = swap_type;
		__entry->offset = offset;
		__entry->ret = ret;
	),
	TP_printk("start_ns=%llu pfn=%llu swap_type=%u offset=%llu submit_ret=%d",
		(unsigned long long)__entry->start_ns, (unsigned long long)__entry->pfn,
		__entry->swap_type, (unsigned long long)__entry->offset, __entry->ret)
);

/* One event per terminal data WC, before request retirement. Disabled by default. */
TRACE_EVENT(placement,
	TP_PROTO(unsigned int swap_type, u64 offset, unsigned int server,
		 unsigned int tier, int op, int status),
	TP_ARGS(swap_type, offset, server, tier, op, status),
	TP_STRUCT__entry(
		__field(unsigned int, swap_type)
		__field(u64, offset)
		__field(unsigned int, server)
		__field(unsigned int, tier)
		__field(int, op)
		__field(int, status)
	),
	TP_fast_assign(
		__entry->swap_type = swap_type;
		__entry->offset = offset;
		__entry->server = server;
		__entry->tier = tier;
		__entry->op = op;
		__entry->status = status;
	),
	TP_printk("swap_type=%u offset=%llu server=%u tier=%s op=%s status=%d",
		__entry->swap_type, (unsigned long long)__entry->offset,
		__entry->server,
		__print_symbolic(__entry->tier, {0, "dram"}, {1, "pmem"}),
		__print_symbolic(__entry->op, {0, "write"}, {1, "read_sync"},
				 {2, "read_async"}),
		__entry->status)
);

#endif

#undef TRACE_INCLUDE_PATH
#define TRACE_INCLUDE_PATH .
#undef TRACE_INCLUDE_FILE
#define TRACE_INCLUDE_FILE rswap_trace
#include <trace/define_trace.h>
