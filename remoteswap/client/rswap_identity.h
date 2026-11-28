/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_IDENTITY_H
#define RSWAP_IDENTITY_H

#include <linux/types.h>

struct dentry;

/* Copied values only; no table/mm pointer escapes the lookup. vpn is the
 * lowest associated VPN; aliases counts all distinct monitored VPNs.
 * Stage 1 exposes identity only and does not change Store placement.
 */
struct rswap_identity_result {
	u64 binding_id, vpn, aliases;
	unsigned int pid;
};

int rswap_identity_init(u64 max_pages);
void rswap_identity_exit(void);
void rswap_identity_area(bool active, unsigned int type);
int rswap_identity_debugfs(struct dentry *root);
int rswap_identity_lookup(unsigned int type, u64 offset,
			  struct rswap_identity_result *result);
bool rswap_identity_binding_valid(u64 binding_id);
/* Callback runs under RCU and the IRQ-safe entry lock: no sleep/allocation.
 * expected_binding prevents an old heat snapshot from joining a new mm. */
int rswap_identity_visit(unsigned int type, u64 offset, u64 expected_binding,
		void (*visit)(void *arg, u64 vpn), void *arg,
		struct rswap_identity_result *result);

#endif
