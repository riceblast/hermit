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
};

/* offset is the original swap page offset, including the header position. */
static inline int rswap_map_page(const struct rswap_pool_layout *pool,
		u64 offset, unsigned int *server, u64 *slot)
{
	u64 common_pages;

	if (!pool->nr_servers || pool->nr_servers > RSWAP_MAX_SERVERS ||
	    !pool->total_pages)
		return -ENODEV;
	if (offset >= pool->total_pages)
		return -ERANGE;

	*server = 0;
	*slot = offset;
	if (pool->nr_servers == 2) {
		common_pages = pool->server_pages[0] < pool->server_pages[1] ?
			pool->server_pages[0] : pool->server_pages[1];
		/* Equivalent to offset < 2 * common_pages, without overflow. */
		if ((offset >> 1) < common_pages) {
			*server = offset & 1;
			*slot = offset >> 1;
		} else {
			*server = pool->server_pages[0] > pool->server_pages[1] ? 0 : 1;
			*slot = offset - common_pages;
		}
	}
	if (*slot >= pool->server_pages[*server])
		return -ERANGE;
	return 0;
}

#endif /* __RSWAP_MAPPING_H */
