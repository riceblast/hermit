/* SPDX-License-Identifier: GPL-2.0 */
#undef TRACE_SYSTEM
#define TRACE_SYSTEM rswap

#if !defined(_RSWAP_TRACE_H) || defined(TRACE_HEADER_MULTI_READ)
#define _RSWAP_TRACE_H

#include <linux/tracepoint.h>

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
