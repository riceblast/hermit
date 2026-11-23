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

/* id is a pool-global, zero-based Slot index. A held reference prevents reuse.
 * slot remains Tier-local. Offset 0 is not subtracted from the logical index.
 */
struct rswap_location {
	unsigned int id, server, tier;
	u64 slot;
	u64 alloc_seq; /* allocation event identity; only set by alloc_preferred */
};

struct rswap_mapping_pool_stats {
	u64 capacity, live, retired, free;
};

struct rswap_mapping_stats {
	u64 pages, metadata_bytes, sequence;
	unsigned int nr_servers;
	struct rswap_mapping_pool_stats pools[RSWAP_MAX_SERVERS][RSWAP_NR_TIERS];
};

int rswap_mapping_init(const struct rswap_pool_layout *layout);
void rswap_mapping_destroy(void);
int rswap_mapping_alloc(u64 offset, struct rswap_location *location);
/* Mechanism entry: policy supplies preference, allocator owns fallback/lifetime. */
int rswap_mapping_alloc_preferred(u64 offset, unsigned int server,
		unsigned int tier, struct rswap_location *location);
int rswap_mapping_get(u64 offset, struct rswap_location *location);
void rswap_mapping_put(unsigned int id);
void rswap_mapping_invalidate(u64 offset);
int rswap_mapping_abort(u64 offset, unsigned int id);
int rswap_mapping_current(u64 offset, unsigned int id);
void rswap_mapping_reset(void); /* submissions closed and all I/O drained */
void rswap_mapping_snapshot(struct rswap_mapping_stats *stats);

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
