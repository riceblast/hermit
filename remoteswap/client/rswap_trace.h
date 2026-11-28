/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM rswap

#if !defined(_RSWAP_TRACE_H) || defined(TRACE_HEADER_MULTI_READ)
#define _RSWAP_TRACE_H

#include <linux/tracepoint.h>
#include "rswap_placement.h"
#include "rswap_app.h"
#include "../rswap_vpn_heat_uapi.h"

/* Detailed fixed-policy evidence remains independent of the adaptive raw
 * format. Both policies also use the shared human-readable decision event.
 */
TRACE_EVENT(app_decision,
	TP_PROTO(u64 offset, u64 pfn, const struct rswap_app_decision *d,
		 const struct rswap_location *loc, int ret),
	TP_ARGS(offset, pfn, d, loc, ret),
	TP_STRUCT__entry(
		__field(u64, binding) __field(u64, offset) __field(u64, pfn)
		__field(u64, rr) __field(u64, alloc_seq)
		__field(unsigned int, policy) __field(unsigned int, preferred_server)
		__field(unsigned int, preferred_tier) __field(unsigned int, server)
		__field(unsigned int, tier) __field(int, ret)
	),
	TP_fast_assign(
		__entry->binding = d->binding_id; __entry->offset = offset; __entry->pfn = pfn;
		__entry->rr = d->rr; __entry->alloc_seq = ret ? 0 : loc->alloc_seq;
		__entry->policy = d->policy; __entry->preferred_server = d->server;
		__entry->preferred_tier = d->tier; __entry->server = loc->server;
		__entry->tier = loc->tier; __entry->ret = ret;
	),
	TP_printk("binding=%llu offset=%llu pfn=%llu policy=%u rr=%llu preferred_server=%u preferred_tier=%u server=%u tier=%u alloc_seq=%llu alloc_ret=%d",
		__entry->binding, __entry->offset, __entry->pfn, __entry->policy,
		__entry->rr, __entry->preferred_server, __entry->preferred_tier,
		__entry->server, __entry->tier, __entry->alloc_seq, __entry->ret)
);

/* Store-only results. stage 0 = post accepted/pre-post error, stage 1 = WC.
 * A fast WC can precede stage 0 in the log. Key by alloc_seq, never offset alone.
 */
TRACE_EVENT(store_result,
	TP_PROTO(u64 alloc_seq, u64 offset, unsigned int server, unsigned int tier,
		 unsigned int stage, int status),
	TP_ARGS(alloc_seq, offset, server, tier, stage, status),
	TP_STRUCT__entry(
		__field(u64, alloc_seq) __field(u64, offset)
		__field(unsigned int, server) __field(unsigned int, tier)
		__field(unsigned int, stage) __field(int, status)
	),
	TP_fast_assign(
		__entry->alloc_seq = alloc_seq; __entry->offset = offset;
		__entry->server = server; __entry->tier = tier;
		__entry->stage = stage; __entry->status = status;
	),
	TP_printk("alloc_seq=%llu offset=%llu server=%u tier=%u stage=%u status=%d",
		__entry->alloc_seq, __entry->offset, __entry->server,
		__entry->tier, __entry->stage, __entry->status)
);

/* Formatting is done into event-owned storage before trace_seq sees it. A
 * single leading %s avoids this kernel's full-seq mixed-va_list verifier bug.
 */
#ifndef _RSWAP_DECISION_FORMAT
#define _RSWAP_DECISION_FORMAT
static inline void rswap_format_decision(char *buf, size_t size, u64 pfn,
		unsigned int state, unsigned int app_policy, unsigned int preferred_tier,
		const struct rswap_location *loc)
{
	const char *policy = "interleave", *reason = "interleave";
	const char *tier = loc->tier == RSWAP_TIER_DRAM ? "dram" : "pmem";

	if (app_policy) {
		policy = app_policy == RSWAP_APP_DRAM ? "pureDRAM" : "purePMem";
		reason = loc->tier == preferred_tier ? "fixed" : "capacity_fallback";
	} else if (state == RSWAP_WARMUP) {
		policy = "adaptive(warmup)";
		reason = "interleave(warmup)";
	} else if (state == RSWAP_ADAPTIVE) {
		policy = "adaptive";
		reason = loc->tier == RSWAP_TIER_DRAM ? "hot" : "cold";
	}
	scnprintf(buf, size,
		"req_type: store, pfn: 0x%llx, placement_policy: %s, reason: %s, server: %u, target_mem: %s,",
		(unsigned long long)pfn, policy, reason, loc->server, tier);
}

static inline void rswap_format_vpn_decision(char *buf, size_t size, u64 pfn,
		const struct rswap_decision *d, const struct rswap_location *loc)
{
	u64 heat = (d->heat_q32 * 100 + RSWAP_VPN_HEAT_SCALE / 2) / RSWAP_VPN_HEAT_SCALE;
	u64 mean = d->pages ? ((d->sum_heat / d->pages) * 100 +
		RSWAP_VPN_HEAT_SCALE / 2) / RSWAP_VPN_HEAT_SCALE : 0;
	char reason[96];
	bool fallback = loc->server != d->server || loc->tier != d->tier;

	if (d->state == RSWAP_INACTIVE) {
		rswap_format_decision(buf, size, pfn, RSWAP_INACTIVE, 0, d->tier, loc);
		return;
	}
	if (d->state == RSWAP_WARMUP)
		scnprintf(reason, sizeof(reason), "warmup%s", fallback ? "; capacity_fallback" : "");
	else
		scnprintf(reason, sizeof(reason), "%s(%llu.%02llu %s %llu.%02llu)%s",
			d->reason == RSWAP_REASON_HOT ? "hot" : "cold",
			(unsigned long long)(heat / 100), (unsigned long long)(heat % 100),
			d->reason == RSWAP_REASON_HOT ? ">=" : "<",
			(unsigned long long)(mean / 100), (unsigned long long)(mean % 100),
			fallback ? "; capacity_fallback" : "");
	scnprintf(buf, size,
		"req_type: store, pfn: 0x%llx, placement_policy: %s, reason: %s, server: %u, target_mem: %s,",
		(unsigned long long)pfn, d->state == RSWAP_WARMUP ? "adaptive(warmup)" : "adaptive",
		reason, loc->server, loc->tier == RSWAP_TIER_DRAM ? "dram" : "pmem");
}
#endif

/* Display only: successful Store allocation, not post/WC success. Never Load.
 * app_policy=0 selects the legacy state-based labels. Numeric evidence is
 * available independently as decision_raw (legacy) or app_decision (fixed).
 */
TRACE_EVENT(decision,
	TP_PROTO(u64 pfn, unsigned int state, unsigned int app_policy,
		 unsigned int preferred_tier, const struct rswap_location *loc,
		 const struct rswap_decision *vpn),
	TP_ARGS(pfn, state, app_policy, preferred_tier, loc, vpn),
	TP_STRUCT__entry(__array(char, text, 256)),
	TP_fast_assign(
		if (vpn)
			rswap_format_vpn_decision(__entry->text, sizeof(__entry->text), pfn, vpn, loc);
		else
			rswap_format_decision(__entry->text, sizeof(__entry->text),
				pfn, state, app_policy, preferred_tier, loc);
	),
	TP_printk("%s", __entry->text)
);

/* Exact VPN evidence. Never interpret Q32 decisions as rounded legacy ppm. */
TRACE_EVENT(vpn_decision,
	TP_PROTO(u64 offset, u64 pfn, const struct rswap_decision *d,
		 const struct rswap_location *loc, int ret),
	TP_ARGS(offset, pfn, d, loc, ret),
	TP_STRUCT__entry(
		__field(u64, offset) __field(u64, pfn) __field(u64, alloc_seq)
		__field(u64, binding) __field(u64, vpn) __field(u64, aliases)
		__field(u64, window) __field(u64, publish_ns) __field(u64, decision_ns)
		__field(u64, heat_q32) __field(u64, pages) __field(u64, sum_heat) __field(u64, rr)
		__field(unsigned int, state) __field(unsigned int, reason)
		__field(unsigned int, preferred_server) __field(unsigned int, preferred_tier)
		__field(unsigned int, server) __field(unsigned int, tier)
		__field(u64, slot) __field(unsigned int, fallback) __field(int, ret)
	),
	TP_fast_assign(
		__entry->offset = offset; __entry->pfn = pfn;
		__entry->alloc_seq = ret ? 0 : loc->alloc_seq;
		__entry->binding = d->binding_id; __entry->vpn = d->vpn; __entry->aliases = d->aliases;
		__entry->window = d->generation; __entry->publish_ns = d->publish_ns;
		__entry->decision_ns = d->decision_ns; __entry->heat_q32 = d->heat_q32;
		__entry->pages = d->pages; __entry->sum_heat = d->sum_heat; __entry->rr = d->rr;
		__entry->state = d->state; __entry->reason = d->reason;
		__entry->preferred_server = d->server; __entry->preferred_tier = d->tier;
		__entry->server = loc->server; __entry->tier = loc->tier; __entry->slot = loc->slot;
		__entry->fallback = ret ? 0 : (loc->server ^ d->server) + 2 * (loc->tier ^ d->tier);
		__entry->ret = ret;
	),
	TP_printk("version=1 alloc_seq=%llu offset=%llu pfn=%llu binding=%llu vpn=%llu aliases=%llu window=%llu publish_ns=%llu decision_ns=%llu heat_q32=%llu pages=%llu sum_heat=%llu rr=%llu state=%u reason=%u preferred_server=%u preferred_tier=%u server=%u tier=%u slot=%llu fallback=%u alloc_ret=%d",
		__entry->alloc_seq, __entry->offset, __entry->pfn, __entry->binding, __entry->vpn, __entry->aliases,
		__entry->window, __entry->publish_ns, __entry->decision_ns, __entry->heat_q32,
		__entry->pages, __entry->sum_heat, __entry->rr, __entry->state, __entry->reason,
		__entry->preferred_server, __entry->preferred_tier, __entry->server, __entry->tier,
		__entry->slot, __entry->fallback, __entry->ret)
);

/* Emitted immediately after the allocation attempt. alloc_seq joins the
 * serialized mapping event even if CPUs emit decision events out of order.
 */
TRACE_EVENT(decision_raw,
	TP_PROTO(u64 offset, u64 pfn, const struct rswap_decision *d,
		 const struct rswap_location *loc, int ret),
	TP_ARGS(offset, pfn, d, loc, ret),
	TP_STRUCT__entry(
		__field(u64, offset) __field(u64, pfn) __field(u64, alloc_seq)
		__field(u64, run) __field(u64, generation)
		__field(u64, decision_ns) __field(u64, publish_ns) __field(u64, rr)
		__field(unsigned int, state) __field(unsigned int, reason)
		__field(int, heat) __field(int, mean) __field(int, threshold)
		__field(unsigned int, policy) __field(unsigned int, threshold_source)
		__field(unsigned int, threshold_status) __field(u64, threshold_generation)
		__field(unsigned int, preferred_server) __field(unsigned int, preferred_tier)
		__field(unsigned int, server) __field(unsigned int, tier)
		__field(u64, slot) __field(unsigned int, fallback) __field(int, ret)
		__field(unsigned int, placement_rule)
	),
	TP_fast_assign(
		__entry->offset = offset; __entry->pfn = pfn;
		__entry->alloc_seq = ret ? 0 : loc->alloc_seq;
		__entry->run = d->run_id; __entry->generation = d->generation;
		__entry->decision_ns = d->decision_ns; __entry->publish_ns = d->publish_ns;
		__entry->rr = d->rr; __entry->state = d->state; __entry->reason = d->reason;
		__entry->heat = d->heat; __entry->mean = d->mean;
		__entry->threshold = d->threshold; __entry->policy = d->policy;
		__entry->threshold_source = d->threshold_source;
		__entry->threshold_status = d->threshold_status;
		__entry->threshold_generation = d->threshold_generation;
		__entry->preferred_server = d->server; __entry->preferred_tier = d->tier;
		__entry->server = loc->server; __entry->tier = loc->tier; __entry->slot = loc->slot;
		__entry->fallback = ret ? 0 : (loc->server ^ d->server) + 2 * (loc->tier ^ d->tier);
		__entry->ret = ret;
		__entry->placement_rule = RSWAP_PLACEMENT_RULE;
	),
	TP_printk("alloc_seq=%llu offset=%llu pfn=%llu run=%llu generation=%llu decision_ns=%llu publish_ns=%llu state=%u reason=%u heat_ppm=%d mean_ppm=%d preferred_server=%u preferred_tier=%u server=%u tier=%u slot=%llu fallback=%u rr=%llu alloc_ret=%d threshold_ppm=%d policy=%u threshold_source=%u threshold_generation=%llu threshold_status=%u placement_rule=%u",
		__entry->alloc_seq, __entry->offset, __entry->pfn, __entry->run,
		__entry->generation, __entry->decision_ns, __entry->publish_ns,
		__entry->state, __entry->reason, __entry->heat, __entry->mean,
		__entry->preferred_server, __entry->preferred_tier, __entry->server,
		__entry->tier, __entry->slot, __entry->fallback, __entry->rr, __entry->ret,
		__entry->threshold, __entry->policy, __entry->threshold_source,
		__entry->threshold_generation, __entry->threshold_status, __entry->placement_rule)
);

/* Serialized allocator lifecycle; users excludes index ownership. Numeric
 * text fields avoid the 5.14 string verifier's va_list handling when seq fills.
 */
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
	TP_printk("seq=%llu action=%u offset=%llu server=%u tier=%u slot=%llu users=%u live=%u fallback=%u",
		(unsigned long long)__entry->seq,
		__entry->action,
		(unsigned long long)__entry->offset, __entry->server,
		__entry->tier,
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
	TP_printk("swap_type=%u offset=%llu server=%u tier=%u op=%d status=%d",
		__entry->swap_type, (unsigned long long)__entry->offset,
		__entry->server,
		__entry->tier, __entry->op,
		__entry->status)
);

#endif

#undef TRACE_INCLUDE_PATH
#define TRACE_INCLUDE_PATH .
#undef TRACE_INCLUDE_FILE
#define TRACE_INCLUDE_FILE rswap_trace
#include <trace/define_trace.h>
