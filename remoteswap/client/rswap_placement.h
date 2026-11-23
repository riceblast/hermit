/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_PLACEMENT_H
#define RSWAP_PLACEMENT_H

#include "rswap_mapping.h"
#include "../rswap_placement_uapi.h"

enum rswap_policy_state { RSWAP_INACTIVE, RSWAP_WARMUP, RSWAP_ADAPTIVE };
enum rswap_policy_reason { RSWAP_REASON_INACTIVE, RSWAP_REASON_WARMUP,
	RSWAP_REASON_NO_SNAPSHOT, RSWAP_REASON_NO_MEAN, RSWAP_REASON_UNKNOWN,
	RSWAP_REASON_HOT, RSWAP_REASON_COLD, RSWAP_REASON_COUNT };

struct rswap_decision {
	u64 run_id, generation, publish_ns, decision_ns, rr;
	int heat, mean;
	unsigned int state, reason, server, tier;
};

struct dentry;
void rswap_placement_init(const struct rswap_pool_layout *layout);
int rswap_placement_debugfs(struct dentry *root);
void rswap_placement_area(bool active);
void rswap_placement_exit(void);
int rswap_placement_choose(u64 offset, u64 pfn, struct rswap_decision *decision);
void rswap_policy_stats_init(u64 instance_ns);
void rswap_policy_record_alloc(const struct rswap_decision *d,
		const struct rswap_location *loc, int status);
void rswap_policy_record_submit(unsigned int reason, int status);
void rswap_policy_record_wc(unsigned int reason, int status);

#endif
