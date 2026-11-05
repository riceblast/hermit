// SPDX-License-Identifier: GPL-2.0-only
/*
 * Frontswap frontend
 *
 * This code provides the generic "frontend" layer to call a matching
 * "backend" driver implementation of frontswap.  See
 * Documentation/vm/frontswap.rst for more information.
 *
 * Copyright (C) 2009-2012 Oracle Corp.  All rights reserved.
 * Author: Dan Magenheimer
 */

#include <linux/mman.h>
#include <linux/swap.h>
#include <linux/swapops.h>
#include <linux/security.h>
#include <linux/module.h>
#include <linux/debugfs.h>
#include <linux/frontswap.h>
#include <linux/swapfile.h>
#include <linux/mutex.h>
#include <linux/srcu.h>

DEFINE_STATIC_KEY_FALSE(frontswap_enabled_key);
EXPORT_SYMBOL(frontswap_enabled_key);

/* Legacy backends remain permanent. A managed backend is exclusive. */
struct frontswap_ops *frontswap_ops __read_mostly;
EXPORT_SYMBOL(frontswap_ops);

static DEFINE_MUTEX(frontswap_mutex);
DEFINE_STATIC_SRCU(frontswap_srcu);
static struct module *managed_owner;
static unsigned long managed_max_pages;
static int managed_type = -1;
static bool managed_unregistering;

/* Callers hold either the control mutex or a SRCU read-side section. */
#define for_each_frontswap_ops(ops)                                      \
	for ((ops) = srcu_dereference_check(frontswap_ops, &frontswap_srcu, \
			lockdep_is_held(&frontswap_mutex));                   \
	     (ops); (ops) = READ_ONCE((ops)->next))

/*
 * If enabled, frontswap_store will return failure even on success.  As
 * a result, the swap subsystem will always write the page to swap, in
 * effect converting frontswap into a writethrough cache.  In this mode,
 * there is no direct reduction in swap writes, but a frontswap backend
 * can unilaterally "reclaim" any pages in use with no data loss, thus
 * providing increases control over maximum memory usage due to frontswap.
 */
static bool frontswap_writethrough_enabled __read_mostly;

/*
 * If enabled, the underlying tmem implementation is capable of doing
 * exclusive gets, so frontswap_load, on a successful tmem_get must
 * mark the page as no longer in frontswap AND mark it dirty.
 */
static bool frontswap_tmem_exclusive_gets_enabled __read_mostly;

#ifdef CONFIG_DEBUG_FS
/*
 * Counters available via /sys/kernel/debug/frontswap (if debugfs is
 * properly configured).  These are for information only so are not protected
 * against increment races.
 */
static u64 frontswap_loads;
static u64 frontswap_succ_stores;
static u64 frontswap_failed_stores;
static u64 frontswap_invalidates;

static inline void inc_frontswap_loads(void)
{
	data_race(frontswap_loads++);
}
static inline void inc_frontswap_succ_stores(void)
{
	data_race(frontswap_succ_stores++);
}
static inline void inc_frontswap_failed_stores(void)
{
	data_race(frontswap_failed_stores++);
}
static inline void inc_frontswap_invalidates(void)
{
	data_race(frontswap_invalidates++);
}
#else
static inline void inc_frontswap_loads(void) { }
static inline void inc_frontswap_succ_stores(void) { }
static inline void inc_frontswap_failed_stores(void) { }
static inline void inc_frontswap_invalidates(void) { }
#endif

/*
 * A swap-area binding pins a managed module until invalidate_area has
 * completed. SRCU independently protects calls which already fetched ops,
 * including CPU-only poll/peek callbacks. RDMA completion lifetime remains
 * the backend's responsibility: it must quiesce those before module_exit.
 */
/*
 * Register operations for frontswap
 */
void frontswap_register_ops(struct frontswap_ops *ops)
{
	DECLARE_BITMAP(a, MAX_SWAPFILES);
	DECLARE_BITMAP(b, MAX_SWAPFILES);
	struct swap_info_struct *si;
	unsigned int i;

	mutex_lock(&frontswap_mutex);
	if (managed_owner || managed_unregistering) {
		pr_err("frontswap: an exclusive backend is registered\n");
		mutex_unlock(&frontswap_mutex);
		return;
	}

	bitmap_zero(a, MAX_SWAPFILES);
	bitmap_zero(b, MAX_SWAPFILES);

	spin_lock(&swap_lock);
	plist_for_each_entry(si, &swap_active_head, list) {
		if (!WARN_ON(!si->frontswap_map))
			set_bit(si->type, a);
	}
	spin_unlock(&swap_lock);

	/* the new ops needs to know the currently active swap devices */
	for_each_set_bit(i, a, MAX_SWAPFILES)
		ops->init(i);

	/*
	 * Setting frontswap_ops must happen after the ops->init() calls
	 * above; cmpxchg implies smp_mb() which will ensure the init is
	 * complete at this point.
	 */
	do {
		ops->next = frontswap_ops;
	} while (cmpxchg(&frontswap_ops, ops->next, ops) != ops->next);

	static_branch_inc(&frontswap_enabled_key);

	spin_lock(&swap_lock);
	plist_for_each_entry(si, &swap_active_head, list) {
		if (si->frontswap_map)
			set_bit(si->type, b);
	}
	spin_unlock(&swap_lock);

	/*
	 * On the very unlikely chance that a swap device was added or
	 * removed between setting the "a" list bits and the ops init
	 * calls, we re-check and do init or invalidate for any changed
	 * bits.
	 */
	if (unlikely(!bitmap_equal(a, b, MAX_SWAPFILES))) {
		for (i = 0; i < MAX_SWAPFILES; i++) {
			if (!test_bit(i, a) && test_bit(i, b))
				ops->init(i);
			else if (test_bit(i, a) && !test_bit(i, b))
				ops->invalidate_area(i);
		}
	}
	mutex_unlock(&frontswap_mutex);
}
EXPORT_SYMBOL(frontswap_register_ops);

/* Register only before swap is enabled, never over an existing backend. */
int frontswap_register_managed_ops_v2(struct frontswap_ops *ops,
		struct module *owner, unsigned long max_pages)
{
	unsigned int type;
	int ret = 0;

	if (!ops || !owner || !ops->init ||
	    (!ops->store && !ops->store_prepare) || !ops->load ||
	    !ops->invalidate_page || !ops->invalidate_area)
		return -EINVAL;
	if (!!ops->store_prepare != !!ops->store_commit)
		return -EINVAL;
#ifdef RSWAP_KERNEL_SUPPORT
	if (!ops->load_async || !ops->poll_load)
		return -EINVAL;
#if RSWAP_KERNEL_SUPPORT >= 2
	if ((!ops->store_on_core && !ops->store_prepare) || !ops->poll_store)
		return -EINVAL;
#endif
#if RSWAP_KERNEL_SUPPORT >= 3
	if (!ops->peek_load || !ops->peek_store)
		return -EINVAL;
#endif
#endif
	mutex_lock(&frontswap_mutex);
	if (frontswap_ops || managed_unregistering) {
		ret = -EBUSY;
		goto out;
	}
	if (frontswap_writethrough_enabled || frontswap_tmem_exclusive_gets_enabled) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	/* SWP_USED also covers swap areas being prepared or torn down. */
	spin_lock(&swap_lock);
	for (type = 0; type < MAX_SWAPFILES; type++) {
		if (swap_info[type] && (swap_info[type]->flags & SWP_USED)) {
			ret = -EBUSY;
			break;
		}
	}
	spin_unlock(&swap_lock);
	if (ret)
		goto out;

	managed_owner = owner;
	managed_max_pages = max_pages;
	managed_type = -1;
	ops->next = NULL;
	rcu_assign_pointer(frontswap_ops, ops);
	static_branch_inc(&frontswap_enabled_key);
out:
	mutex_unlock(&frontswap_mutex);
	return ret;
}
EXPORT_SYMBOL_GPL(frontswap_register_managed_ops_v2);

/* Caller owns ops; an active binding prevents its module_exit from running. */
void frontswap_unregister_managed_ops(struct frontswap_ops *ops)
{
	mutex_lock(&frontswap_mutex);
	if (WARN_ON(frontswap_ops != ops || !managed_owner || managed_type != -1)) {
		mutex_unlock(&frontswap_mutex);
		return;
	}
	managed_unregistering = true;
	rcu_assign_pointer(frontswap_ops, NULL);
	static_branch_dec(&frontswap_enabled_key);
	mutex_unlock(&frontswap_mutex);

	/* Never wait while holding a lock which a reader might acquire. */
	synchronize_srcu(&frontswap_srcu);
	mutex_lock(&frontswap_mutex);
	managed_owner = NULL;
	managed_max_pages = 0;
	managed_unregistering = false;
	mutex_unlock(&frontswap_mutex);
}
EXPORT_SYMBOL_GPL(frontswap_unregister_managed_ops);

/*
 * Enable/disable frontswap writethrough (see above).
 */
void frontswap_writethrough(bool enable)
{
	mutex_lock(&frontswap_mutex);
	if (enable && managed_owner)
		pr_warn("frontswap: managed backend requires writeback mode\n");
	else
		frontswap_writethrough_enabled = enable;
	mutex_unlock(&frontswap_mutex);
}
EXPORT_SYMBOL(frontswap_writethrough);

/*
 * Enable/disable frontswap exclusive gets (see above).
 */
void frontswap_tmem_exclusive_gets(bool enable)
{
	mutex_lock(&frontswap_mutex);
	if (enable && managed_owner)
		pr_warn("frontswap: managed backend retains pages until invalidation\n");
	else
		frontswap_tmem_exclusive_gets_enabled = enable;
	mutex_unlock(&frontswap_mutex);
}
EXPORT_SYMBOL(frontswap_tmem_exclusive_gets);

/*
 * Called when a swap device is swapon'd.
 */
int __frontswap_init(unsigned type, unsigned long *map)
{
	struct swap_info_struct *sis = swap_info[type];
	struct frontswap_ops *ops;
	int ret = 0;

	VM_BUG_ON(sis == NULL);
	mutex_lock(&frontswap_mutex);
	if (managed_owner) {
		if (managed_unregistering || managed_type != -1) {
			ret = -EBUSY;
			goto out;
		}
		if (!map || !managed_max_pages) {
			ret = -EOPNOTSUPP;
			goto out;
		}
		if (sis->max > managed_max_pages) {
			ret = -ENOSPC;
			goto out;
		}
		if (!try_module_get(managed_owner)) {
			ret = -EBUSY;
			goto out;
		}
		managed_type = type;
	}
	frontswap_map_set(sis, map);
	/* No bitmap means this ordinary swap area cannot use a backend. */
	if (map)
		for_each_frontswap_ops(ops)
			ops->init(type);
out:
	mutex_unlock(&frontswap_mutex);
	return ret;
}
EXPORT_SYMBOL(__frontswap_init);

bool __frontswap_test(struct swap_info_struct *sis,
				pgoff_t offset)
{
	if (sis->frontswap_map)
		return test_bit(offset, sis->frontswap_map);
	return false;
}
EXPORT_SYMBOL(__frontswap_test);

static inline void __frontswap_set(struct swap_info_struct *sis,
				   pgoff_t offset)
{
	if (!test_and_set_bit(offset, sis->frontswap_map))
		atomic_inc(&sis->frontswap_pages);
}

static inline void __frontswap_clear(struct swap_info_struct *sis,
				     pgoff_t offset)
{
	if (test_and_clear_bit(offset, sis->frontswap_map))
		atomic_dec(&sis->frontswap_pages);
}

/*
 * "Store" data from a page to frontswap and associate it with the page's
 * swaptype and offset.  Page must be locked and in the swap cache.
 * If frontswap already contains a page with matching swaptype and
 * offset, the frontswap implementation may either overwrite the data and
 * return success or invalidate the page from frontswap and return failure.
 */
int __frontswap_store(struct page *page, int core)
{
	int ret = -1;
	int idx;
	swp_entry_t entry = { .val = page_private(page), };
	int type = swp_type(entry);
	struct swap_info_struct *sis = swap_info[type];
	pgoff_t offset = swp_offset(entry);
	struct frontswap_ops *ops;
	void *cookie = NULL;

	VM_BUG_ON(!PageLocked(page));
	VM_BUG_ON(sis == NULL);

	idx = srcu_read_lock(&frontswap_srcu);
	if (managed_owner && PageCompound(page)) {
		unsigned long i;

		/* A disk fallback writes the whole THP. Retire every old base-page copy. */
		for (i = 0; i < thp_nr_pages(page); i++)
			__frontswap_invalidate_page(type, offset + i);
		srcu_read_unlock(&frontswap_srcu, idx);
		inc_frontswap_failed_stores();
		return -EOPNOTSUPP;
	}
	/* Retire an old copy even if the new store will fall back to disk. */
	if (__frontswap_test(sis, offset)) {
		__frontswap_clear(sis, offset);
		for_each_frontswap_ops(ops)
			ops->invalidate_page(type, offset);
	}
	/* Try to store in each implementation, until one succeeds. */
	for_each_frontswap_ops(ops) {
		if (managed_owner && ops->store_prepare)
			ret = ops->store_prepare(type, offset, page, core, &cookie);
		else if (core == -1)
			ret = ops->store(type, offset, page);
		else
			ret = ops->store_on_core(type, offset, page, core);
		if (!ret) /* successful store */
			break;
	}
	if (ret == 0) {
		__frontswap_set(sis, offset);
		inc_frontswap_succ_stores();
		/* Keep the backend and commit callback under SRCU protection. */
		if (managed_owner && ops->store_prepare)
			ops->store_commit(cookie);
	} else {
		inc_frontswap_failed_stores();
	}
	srcu_read_unlock(&frontswap_srcu, idx);
	if (frontswap_writethrough_enabled)
		/* report failure so swap also writes to swap device */
		ret = -1;
	return ret;
}
EXPORT_SYMBOL(__frontswap_store);

#if RSWAP_KERNEL_SUPPORT >= 2
int __frontswap_poll_store(int core)
{
	struct frontswap_ops *ops;
	int idx = srcu_read_lock(&frontswap_srcu);
	int ret = 0;

	for_each_frontswap_ops(ops) {
		ret = ops->poll_store(core);
		break;
	}
	srcu_read_unlock(&frontswap_srcu, idx);
	return ret;
}
#else // RSWAP_KERNEL_SUPPORT < 2
int __frontswap_poll_store(int core)
{
	BUG();
	return -1;
}
#endif // RSWAP_KERNEL_SUPPORT >= 2
EXPORT_SYMBOL(__frontswap_poll_store);

/*
 * "Get" data from frontswap associated with swaptype and offset that were
 * specified when the data was put to frontswap and use it to fill the
 * specified page with data. Page must be locked and in the swap cache.
 */
int __frontswap_load(struct page *page, bool sync)
{
	int ret = -1;
	int idx;
	swp_entry_t entry = { .val = page_private(page), };
	int type = swp_type(entry);
	struct swap_info_struct *sis = swap_info[type];
	pgoff_t offset = swp_offset(entry);
	struct frontswap_ops *ops;

	VM_BUG_ON(!PageLocked(page));
	VM_BUG_ON(sis == NULL);

	if (managed_owner && PageCompound(page)) {
		unsigned long i;

		/* Never read a whole THP from disk if any constituent page is remote. */
		for (i = 0; i < thp_nr_pages(page); i++)
			if (__frontswap_test(sis, offset + i))
				return FRONTSWAP_LOAD_ERROR;
		return -1;
	}
	if (!__frontswap_test(sis, offset))
		return -1;

	idx = srcu_read_lock(&frontswap_srcu);
	/* Try loading from each implementation, until one succeeds. */
	for_each_frontswap_ops(ops) {
		if (sync)
			ret = ops->load(type, offset, page);
		else
			ret = ops->load_async(type, offset, page);
		if (!ret) /* successful load */
			break;
	}
	if (ret && managed_owner)
		ret = FRONTSWAP_LOAD_ERROR;
	srcu_read_unlock(&frontswap_srcu, idx);
	if (ret == 0) {
		inc_frontswap_loads();
		if (frontswap_tmem_exclusive_gets_enabled) {
			SetPageDirty(page);
			__frontswap_clear(sis, offset);
		}
	}
	return ret;
}
EXPORT_SYMBOL(__frontswap_load);

#ifdef RSWAP_KERNEL_SUPPORT
int __frontswap_poll_load(int core)
{
	struct frontswap_ops *ops;
	int idx = srcu_read_lock(&frontswap_srcu);
	int ret = 0;

	for_each_frontswap_ops(ops) {
		ret = ops->poll_load(core);
		break;
	}
	srcu_read_unlock(&frontswap_srcu, idx);
	return ret;
}
#else // !defined(RSWAP_KERNEL_SUPPORT)
int __frontswap_poll_load(int core)
{
	BUG();
	return -1;
}
#endif // RSWAP_KERNEL_SUPPORT
EXPORT_SYMBOL(__frontswap_poll_load);

#if RSWAP_KERNEL_SUPPORT >= 3
int __frontswap_peek_load(int core)
{
	struct frontswap_ops *ops;
	int idx = srcu_read_lock(&frontswap_srcu);
	int ret = 0;

	for_each_frontswap_ops(ops) {
		ret = ops->peek_load(core);
		break;
	}
	srcu_read_unlock(&frontswap_srcu, idx);
	return ret;
}

int __frontswap_peek_store(int core)
{
	struct frontswap_ops *ops;
	int idx = srcu_read_lock(&frontswap_srcu);
	int ret = 0;

	for_each_frontswap_ops(ops) {
		ret = ops->peek_store(core);
		break;
	}
	srcu_read_unlock(&frontswap_srcu, idx);
	return ret;
}
#else // RSWAP_KERNEL_SUPPORT < 3
int __frontswap_poll_load(int core)
{
	BUG();
	return -1;
}

int __frontswap_poll_store(int core)
{
	BUG();
	return -1;
}
#endif // RSWAP_KERNEL_SUPPORT
EXPORT_SYMBOL(__frontswap_peek_load);
EXPORT_SYMBOL(__frontswap_peek_store);

/*
 * Invalidate any data from frontswap associated with the specified swaptype
 * and offset so that a subsequent "get" will fail.
 */
void __frontswap_invalidate_page(unsigned type, pgoff_t offset)
{
	struct swap_info_struct *sis = swap_info[type];
	struct frontswap_ops *ops;
	int idx;

	VM_BUG_ON(sis == NULL);

	if (!__frontswap_test(sis, offset))
		return;

	idx = srcu_read_lock(&frontswap_srcu);
	for_each_frontswap_ops(ops)
		ops->invalidate_page(type, offset);
	srcu_read_unlock(&frontswap_srcu, idx);
	__frontswap_clear(sis, offset);
	inc_frontswap_invalidates();
}
EXPORT_SYMBOL(__frontswap_invalidate_page);

/*
 * Invalidate all data from frontswap associated with all offsets for the
 * specified swaptype.
 */
void __frontswap_invalidate_area(unsigned type)
{
	struct swap_info_struct *sis = swap_info[type];
	struct frontswap_ops *ops;
	struct module *owner = NULL;

	VM_BUG_ON(sis == NULL);
	mutex_lock(&frontswap_mutex);
	if (sis->frontswap_map) {
		for_each_frontswap_ops(ops)
			ops->invalidate_area(type);
		atomic_set(&sis->frontswap_pages, 0);
		bitmap_zero(sis->frontswap_map, sis->max);
	}
	if (managed_owner && managed_type == type) {
		managed_type = -1;
		owner = managed_owner;
	}
	mutex_unlock(&frontswap_mutex);
	/* No backend pointer or module code may be accessed after this put. */
	if (owner)
		module_put(owner);
}
EXPORT_SYMBOL(__frontswap_invalidate_area);

static unsigned long __frontswap_curr_pages(void)
{
	unsigned long totalpages = 0;
	struct swap_info_struct *si = NULL;

	assert_spin_locked(&swap_lock);
	plist_for_each_entry(si, &swap_active_head, list)
		totalpages += atomic_read(&si->frontswap_pages);
	return totalpages;
}

static int __frontswap_unuse_pages(unsigned long total, unsigned long *unused,
					int *swapid)
{
	int ret = -EINVAL;
	struct swap_info_struct *si = NULL;
	int si_frontswap_pages;
	unsigned long total_pages_to_unuse = total;
	unsigned long pages = 0, pages_to_unuse = 0;

	assert_spin_locked(&swap_lock);
	plist_for_each_entry(si, &swap_active_head, list) {
		si_frontswap_pages = atomic_read(&si->frontswap_pages);
		if (total_pages_to_unuse < si_frontswap_pages) {
			pages = pages_to_unuse = total_pages_to_unuse;
		} else {
			pages = si_frontswap_pages;
			pages_to_unuse = 0; /* unuse all */
		}
		/* ensure there is enough RAM to fetch pages from frontswap */
		if (security_vm_enough_memory_mm(current->mm, pages)) {
			ret = -ENOMEM;
			continue;
		}
		vm_unacct_memory(pages);
		*unused = pages_to_unuse;
		*swapid = si->type;
		ret = 0;
		break;
	}

	return ret;
}

/*
 * Used to check if it's necessary and feasible to unuse pages.
 * Return 1 when nothing to do, 0 when need to shrink pages,
 * error code when there is an error.
 */
static int __frontswap_shrink(unsigned long target_pages,
				unsigned long *pages_to_unuse,
				int *type)
{
	unsigned long total_pages = 0, total_pages_to_unuse;

	assert_spin_locked(&swap_lock);

	total_pages = __frontswap_curr_pages();
	if (total_pages <= target_pages) {
		/* Nothing to do */
		*pages_to_unuse = 0;
		return 1;
	}
	total_pages_to_unuse = total_pages - target_pages;
	return __frontswap_unuse_pages(total_pages_to_unuse, pages_to_unuse, type);
}

/*
 * Frontswap, like a true swap device, may unnecessarily retain pages
 * under certain circumstances; "shrink" frontswap is essentially a
 * "partial swapoff" and works by calling try_to_unuse to attempt to
 * unuse enough frontswap pages to attempt to -- subject to memory
 * constraints -- reduce the number of pages in frontswap to the
 * number given in the parameter target_pages.
 */
void frontswap_shrink(unsigned long target_pages)
{
	unsigned long pages_to_unuse = 0;
	int type, ret;

	/*
	 * we don't want to hold swap_lock while doing a very
	 * lengthy try_to_unuse, but swap_list may change
	 * so restart scan from swap_active_head each time
	 */
	spin_lock(&swap_lock);
	ret = __frontswap_shrink(target_pages, &pages_to_unuse, &type);
	spin_unlock(&swap_lock);
	if (ret == 0)
		try_to_unuse(type, true, pages_to_unuse);
	return;
}
EXPORT_SYMBOL(frontswap_shrink);

/*
 * Count and return the number of frontswap pages across all
 * swap devices.  This is exported so that backend drivers can
 * determine current usage without reading debugfs.
 */
unsigned long frontswap_curr_pages(void)
{
	unsigned long totalpages = 0;

	spin_lock(&swap_lock);
	totalpages = __frontswap_curr_pages();
	spin_unlock(&swap_lock);

	return totalpages;
}
EXPORT_SYMBOL(frontswap_curr_pages);

static int __init init_frontswap(void)
{
#ifdef CONFIG_DEBUG_FS
	struct dentry *root = debugfs_create_dir("frontswap", NULL);
	if (root == NULL)
		return -ENXIO;
	debugfs_create_u64("loads", 0444, root, &frontswap_loads);
	debugfs_create_u64("succ_stores", 0444, root, &frontswap_succ_stores);
	debugfs_create_u64("failed_stores", 0444, root,
			   &frontswap_failed_stores);
	debugfs_create_u64("invalidates", 0444, root, &frontswap_invalidates);
#endif
	return 0;
}

module_init(init_frontswap);
