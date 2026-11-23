// SPDX-License-Identifier: GPL-2.0
#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/mm.h>
#include <linux/vmalloc.h>
#include <linux/spinlock.h>
#include <linux/sched.h>

#include "rswap_mapping.h"
#include "rswap_trace.h"

/* Exactly 12 bytes/page: u32 forward index plus this eight-byte Slot record.
 * LIVE is the index's ownership; low bits count request references. link is
 * the owner's offset while allocated, the next free id+1 while free.
 * Never reuse a Slot before LIVE and every request reference are gone.
 */
#define RSWAP_SLOT_LIVE (1U << 31)
#define RSWAP_SLOT_USERS (RSWAP_SLOT_LIVE - 1)
struct rswap_slot {
	u32 refs, link;
};

struct rswap_slot_pool {
	u32 base, head;
	struct rswap_mapping_pool_stats stats;
};

static struct {
	struct rswap_pool_layout layout;
	u32 *index;
	struct rswap_slot *slots;
	struct rswap_slot_pool pools[RSWAP_MAX_SERVERS][RSWAP_NR_TIERS];
	u64 sequence;
} mapping;
static DEFINE_SPINLOCK(mapping_lock);

static struct rswap_slot_pool *rswap_slot_location(u32 id,
		struct rswap_location *loc)
{
	unsigned int s, t;

	for (s = 0; s < mapping.layout.nr_servers; s++) {
		for (t = 0; t < RSWAP_NR_TIERS; t++) {
			struct rswap_slot_pool *pool = &mapping.pools[s][t];

			if (id >= pool->base && (u64)id - pool->base < pool->stats.capacity) {
				loc->id = id;
				loc->server = s;
				loc->tier = t;
				loc->slot = id - pool->base;
				return pool;
			}
		}
	}
	return NULL;
}

/* All mapping events are emitted under mapping_lock, including free. The
 * sequence gives an unambiguous order across CPUs; events default to off.
 * action: 0 alloc, 1 get, 2 put, 3 invalidate, 4 free.
 */
static void rswap_slot_trace(unsigned int action, u32 id, u32 offset,
		unsigned int fallback)
{
	struct rswap_location loc;
	struct rswap_slot *slot = &mapping.slots[id];

	rswap_slot_location(id, &loc);
	trace_mapping(++mapping.sequence, action, (u64)offset, loc.server,
		loc.tier, loc.slot, slot->refs & RSWAP_SLOT_USERS,
		!!(slot->refs & RSWAP_SLOT_LIVE), fallback);
}

/* Caller holds mapping_lock; refs == 0, previously counted as retired. */
static void rswap_slot_free(u32 id)
{
	struct rswap_location loc;
	struct rswap_slot_pool *pool = rswap_slot_location(id, &loc);
	struct rswap_slot *slot = &mapping.slots[id];

	if (WARN_ON(!pool || slot->refs))
		return;
	rswap_slot_trace(4, id, slot->link, 0);
	slot->link = pool->head;
	pool->head = id + 1;
	pool->stats.retired--;
	pool->stats.free++;
}

static void rswap_slot_invalidate(u32 offset)
{
	u32 encoded = mapping.index[offset];
	struct rswap_location loc;
	struct rswap_slot_pool *pool;
	struct rswap_slot *slot;

	if (!encoded)
		return;
	slot = &mapping.slots[encoded - 1];
	pool = rswap_slot_location(encoded - 1, &loc);
	mapping.index[offset] = 0;
	slot->refs &= ~RSWAP_SLOT_LIVE;
	pool->stats.live--;
	pool->stats.retired++;
	rswap_slot_trace(3, encoded - 1, offset, 0);
	if (!slot->refs)
		rswap_slot_free(encoded - 1);
}

int rswap_mapping_init(const struct rswap_pool_layout *layout)
{
	u64 base = 0, total = 0;
	unsigned int s, t;

	BUILD_BUG_ON(sizeof(struct rswap_slot) != 8);
	if (mapping.index || mapping.slots)
		return -EBUSY;
	if (!layout->nr_servers || layout->nr_servers > RSWAP_MAX_SERVERS ||
	    !layout->total_pages || layout->total_pages > U32_MAX)
		return -EINVAL;
	for (s = 0; s < layout->nr_servers; s++) {
		u64 a = layout->tier_pages[s][0], b = layout->tier_pages[s][1];

		if (!a || !b || a > U32_MAX || b > U32_MAX ||
		    a + b != layout->server_pages[s])
			return -EINVAL;
		total += a + b;
	}
	if (total != layout->total_pages)
		return -EINVAL;
	mapping.layout = *layout;
	mapping.index = kvcalloc(total, sizeof(*mapping.index), GFP_KERNEL);
	if (!mapping.index)
		goto no_memory;
	mapping.slots = kvcalloc(total, sizeof(*mapping.slots), GFP_KERNEL);
	if (!mapping.slots)
		goto no_memory;
	for (s = 0; s < layout->nr_servers; s++) {
		for (t = 0; t < RSWAP_NR_TIERS; t++) {
			struct rswap_slot_pool *pool = &mapping.pools[s][t];
			u64 i, capacity = layout->tier_pages[s][t];

			pool->base = base;
			pool->head = base + 1;
			pool->stats.capacity = pool->stats.free = capacity;
			for (i = 0; i < capacity; i++) {
				mapping.slots[base + i].link =
					i + 1 < capacity ? base + i + 2 : 0;
				if (!(i & 0xffff))
					cond_resched();
			}
			base += capacity;
		}
	}
	pr_info("rswap: dynamic mapping pages=%llu metadata_bytes=%llu (12 bytes/page)\n",
		(unsigned long long)total, (unsigned long long)(total * 12));
	return 0;
no_memory:
	rswap_mapping_destroy();
	return -ENOMEM;
}

void rswap_mapping_destroy(void)
{
	/* Initialization rollback or after all data callbacks have retired. */
	kvfree(mapping.index);
	kvfree(mapping.slots);
	memset(&mapping, 0, sizeof(mapping));
}

int rswap_mapping_alloc(u64 offset, struct rswap_location *loc)
{
	unsigned int s, t;
	u64 ignored;
	int ret = rswap_map_page(&mapping.layout, offset, &s, &t, &ignored);

	if (ret)
		return ret;
	return rswap_mapping_alloc_preferred(offset, s, t, loc);
}

int rswap_mapping_alloc_preferred(u64 offset, unsigned int s,
		unsigned int t, struct rswap_location *loc)
{
	unsigned int attempt;
	unsigned long flags;
	int ret;

	if (offset >= mapping.layout.total_pages)
		return -ERANGE;
	if (s >= mapping.layout.nr_servers || t >= RSWAP_NR_TIERS)
		return -EINVAL;
	spin_lock_irqsave(&mapping_lock, flags);
	if (mapping.index[offset]) {
		ret = -EEXIST;
		goto out;
	}
	/* Preferred -> other Server/same Tier -> same Server/other Tier -> last.
	 * This is capacity fallback, not recovery from a failed Session or WC.
	 */
	for (attempt = 0; attempt < 4; attempt++) {
		unsigned int server = s ^ (attempt & 1);
		unsigned int tier = t ^ (attempt >> 1);
		struct rswap_slot_pool *pool;
		struct rswap_slot *slot;
		u32 id;

		if (server >= mapping.layout.nr_servers)
			continue;
		pool = &mapping.pools[server][tier];
		if (!pool->head)
			continue;
		id = pool->head - 1;
		slot = &mapping.slots[id];
		pool->head = slot->link;
		slot->link = offset;
		slot->refs = RSWAP_SLOT_LIVE | 1; /* index + Store request */
		mapping.index[offset] = id + 1;
		pool->stats.free--;
		pool->stats.live++;
		rswap_slot_location(id, loc);
		rswap_slot_trace(0, id, offset, attempt);
		loc->alloc_seq = mapping.sequence;
		ret = 0;
		goto out;
	}
	ret = -ENOSPC;
out:
	spin_unlock_irqrestore(&mapping_lock, flags);
	return ret;
}

int rswap_mapping_get(u64 offset, struct rswap_location *loc)
{
	unsigned long flags;
	u32 encoded;
	int ret = -ENOENT;

	spin_lock_irqsave(&mapping_lock, flags);
	if (offset >= mapping.layout.total_pages) {
		ret = -ERANGE;
		goto out;
	}
	encoded = mapping.index[offset];
	if (!encoded)
		goto out;
	if ((mapping.slots[encoded - 1].refs & RSWAP_SLOT_USERS) == RSWAP_SLOT_USERS) {
		ret = -EOVERFLOW;
		goto out;
	}
	mapping.slots[encoded - 1].refs++;
	rswap_slot_location(encoded - 1, loc);
	rswap_slot_trace(1, encoded - 1, offset, 0);
	ret = 0;
out:
	spin_unlock_irqrestore(&mapping_lock, flags);
	return ret;
}

void rswap_mapping_put(unsigned int id)
{
	unsigned long flags;
	struct rswap_slot *slot;

	spin_lock_irqsave(&mapping_lock, flags);
	if (WARN_ON(id >= mapping.layout.total_pages))
		goto out;
	slot = &mapping.slots[id];
	if (WARN_ON(!(slot->refs & RSWAP_SLOT_USERS)))
		goto out;
	slot->refs--;
	rswap_slot_trace(2, id, slot->link, 0);
	if (!slot->refs)
		rswap_slot_free(id);
out:
	spin_unlock_irqrestore(&mapping_lock, flags);
}

void rswap_mapping_invalidate(u64 offset)
{
	unsigned long flags;

	spin_lock_irqsave(&mapping_lock, flags);
	if (offset < mapping.layout.total_pages)
		rswap_slot_invalidate(offset);
	spin_unlock_irqrestore(&mapping_lock, flags);
}

/* Caller holds a request reference, so id cannot have been recycled. */
int rswap_mapping_abort(u64 offset, unsigned int id)
{
	unsigned long flags;
	int matched;

	spin_lock_irqsave(&mapping_lock, flags);
	matched = offset < mapping.layout.total_pages && mapping.index[offset] == id + 1;
	if (matched)
		rswap_slot_invalidate(offset);
	spin_unlock_irqrestore(&mapping_lock, flags);
	return matched;
}

int rswap_mapping_current(u64 offset, unsigned int id)
{
	unsigned long flags;
	int matched;

	spin_lock_irqsave(&mapping_lock, flags);
	matched = offset < mapping.layout.total_pages && mapping.index[offset] == id + 1;
	spin_unlock_irqrestore(&mapping_lock, flags);
	return matched;
}

void rswap_mapping_reset(void)
{
	u64 begin, offset, end, remaining = 0;
	unsigned long flags;
	unsigned int s, t;

	/* No new submissions; batching bounds each IRQ-disabled interval. */
	spin_lock_irqsave(&mapping_lock, flags);
	for (s = 0; s < mapping.layout.nr_servers; s++)
		for (t = 0; t < RSWAP_NR_TIERS; t++)
			remaining += mapping.pools[s][t].stats.live;
	spin_unlock_irqrestore(&mapping_lock, flags);
	/* Ordinary swapoff already invalidated every page: avoid scanning a TB
	 * pool's forward table when no residual mappings need to be removed.
	 */
	for (begin = 0; remaining && begin < mapping.layout.total_pages; begin = end) {
		end = min_t(u64, begin + 1024, mapping.layout.total_pages);
		spin_lock_irqsave(&mapping_lock, flags);
		for (offset = begin; offset < end; offset++) {
			if (mapping.index[offset]) {
				rswap_slot_invalidate(offset);
				remaining--;
			}
		}
		spin_unlock_irqrestore(&mapping_lock, flags);
		cond_resched();
	}
	for (begin = 0; begin < mapping.layout.nr_servers; begin++) {
		for (t = 0; t < RSWAP_NR_TIERS; t++)
			WARN_ON(mapping.pools[begin][t].stats.free !=
				mapping.pools[begin][t].stats.capacity);
	}
}

void rswap_mapping_snapshot(struct rswap_mapping_stats *stats)
{
	unsigned long flags;
	unsigned int s, t;

	memset(stats, 0, sizeof(*stats));
	spin_lock_irqsave(&mapping_lock, flags);
	stats->nr_servers = mapping.layout.nr_servers;
	stats->pages = mapping.layout.total_pages;
	stats->metadata_bytes = stats->pages * 12;
	stats->sequence = mapping.sequence;
	for (s = 0; s < stats->nr_servers; s++)
		for (t = 0; t < RSWAP_NR_TIERS; t++)
			stats->pools[s][t] = mapping.pools[s][t].stats;
	spin_unlock_irqrestore(&mapping_lock, flags);
}
