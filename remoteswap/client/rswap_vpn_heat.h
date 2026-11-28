/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_VPN_HEAT_H
#define RSWAP_VPN_HEAT_H

#include "../rswap_vpn_heat_uapi.h"

struct dentry;
struct seq_file;
struct rswap_pool_layout;
struct rswap_decision;
int rswap_vpn_heat_debugfs(struct dentry *root);
void rswap_vpn_heat_area(bool active);
/* Values only. */
int rswap_vpn_heat_lookup(__u64 binding_id, __u64 vpn,
			struct rswap_vpn_heat_ack *result);
/* 1: VPN mode chose a preference (including ordinary non-target Interleave),
 * 0: no VPN policy; negative: invalid request. No pointers escape RCU. */
int rswap_vpn_placement_choose(unsigned int type, __u64 offset,
		const struct rswap_pool_layout *layout, struct rswap_decision *decision);
bool rswap_vpn_placement_show(struct seq_file *m, bool raw);
void rswap_vpn_placement_stop(void);

#endif
