#ifndef __RSWAP_MAPPING_H
#define __RSWAP_MAPPING_H

#include <linux/errno.h>
#include <linux/types.h>

#include "rswap_ops.h"

/* Built from validated remote descriptors; immutable while the pool is live. */
struct rswap_pool_layout {
	u64 server_pages[RSWAP_MAX_SERVERS];
	u64 total_pages;
	unsigned int nr_servers;
	u64 tier_pages[RSWAP_MAX_SERVERS][RSWAP_NR_TIERS];
};

/* Both levels stripe their common capacity and append the larger side's tail.
 * Capacities and their sums are validated before publishing the pool.
 */
static inline void rswap_interleave_pair(u64 offset, u64 a, u64 b,
		unsigned int *target, u64 *slot)
{
	u64 common = a < b ? a : b;

	if ((offset >> 1) < common) {
		*target = offset & 1;
		*slot = offset >> 1;
	} else {
		*target = a > b ? 0 : 1;
		*slot = offset - common;
	}
}

/* offset is the original swap page offset, including the header position.
 * The returned slot is Tier-local, never Server-local.
 */
static inline int rswap_map_page(const struct rswap_pool_layout *pool,
		u64 offset, unsigned int *server, unsigned int *tier, u64 *slot)
{
	u64 server_slot;

	if (!pool->nr_servers || pool->nr_servers > RSWAP_MAX_SERVERS ||
	    !pool->total_pages)
		return -ENODEV;
	if (offset >= pool->total_pages)
		return -ERANGE;

	*server = 0;
	server_slot = offset;
	if (pool->nr_servers == 2)
		rswap_interleave_pair(offset, pool->server_pages[0],
			pool->server_pages[1], server, &server_slot);
	if (server_slot >= pool->server_pages[*server])
		return -ERANGE;
	if (!pool->tier_pages[*server][RSWAP_TIER_DRAM] ||
	    !pool->tier_pages[*server][RSWAP_TIER_PMEM])
		return -ENODEV;
	rswap_interleave_pair(server_slot,
		pool->tier_pages[*server][RSWAP_TIER_DRAM],
		pool->tier_pages[*server][RSWAP_TIER_PMEM], tier, slot);
	if (*slot >= pool->tier_pages[*server][*tier])
		return -ERANGE;
	return 0;
}

#endif /* __RSWAP_MAPPING_H */
