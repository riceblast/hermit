/* SPDX-License-Identifier: GPL-2.0 */
#ifndef RSWAP_APP_H
#define RSWAP_APP_H

#include "rswap_mapping.h"
#include "../rswap_app_uapi.h"

struct page;
struct dentry;
struct rswap_app_decision {
	u64 binding_id, rr;
	unsigned int policy, server, tier;
};

void rswap_app_init(unsigned int nr_servers);
void rswap_app_area(bool active);
int rswap_app_debugfs(struct dentry *root);
bool rswap_app_choose(struct page *page, struct rswap_app_decision *d);

#endif
