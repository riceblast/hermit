/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_VPN_HEAT_UAPI_H
#define RSWAP_VPN_HEAT_UAPI_H

#include <linux/types.h>

#define RSWAP_VPN_HEAT_VERSION 1
#define RSWAP_VPN_HEAT_SCALE (1ULL << 32)
#define RSWAP_VPN_HEAT_BATCH 4096

enum rswap_vpn_heat_op {
	RSWAP_VPN_HEAT_BIND = 1, RSWAP_VPN_HEAT_BEGIN,
	RSWAP_VPN_HEAT_DATA, RSWAP_VPN_HEAT_COMMIT, RSWAP_VPN_HEAT_QUERY,
};

/* Native-endian local ABI; one complete command per write.
 * BIND: binding_id from the held identity fd; other values zero.
 * BEGIN: window is the completed EWMA group (zero is the initial baseline),
 * count is retained VPN histories, pages/sum_heat cover only in-scope records.
 * DATA: count records immediately follow; only binding_id/window/count set.
 * COMMIT: only binding_id/window set. QUERY: only binding_id/vpn set.
 */
struct rswap_vpn_heat_command {
	__u32 version, op;
	__u64 binding_id, window, count, pages, sum_heat, vpn;
};

struct rswap_vpn_heat_record {
	__u64 vpn, heat; /* [0, 2^32], including exactly 1.0 */
	__u32 in_scope, reserved;
};

/* pread(offset=0) after a command. Missing history in a current binding has
 * heat=0 and found=0. A stale/outside binding returns ESTALE, never cold.
 */
struct rswap_vpn_heat_ack {
	__u64 binding_id, window, publish_ns, records, pages, sum_heat, heat;
	__u32 has_snapshot, found;
};

#endif
