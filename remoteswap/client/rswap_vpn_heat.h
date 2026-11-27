/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_VPN_HEAT_H
#define RSWAP_VPN_HEAT_H

#include "../rswap_vpn_heat_uapi.h"

struct dentry;
int rswap_vpn_heat_debugfs(struct dentry *root);
void rswap_vpn_heat_area(bool active);
/* Values only. This observer API is not consumed by Store placement yet. */
int rswap_vpn_heat_lookup(__u64 binding_id, __u64 vpn,
			struct rswap_vpn_heat_ack *result);

#endif
