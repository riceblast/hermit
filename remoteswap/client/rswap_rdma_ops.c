#include <linux/ktime.h>
#include <linux/overflow.h>
#include <linux/tracepoint.h>
#include <linux/cpu.h>

#include <linux/swap_stats.h>
#include <linux/hermit.h>

#include "rswap_rdma.h"
#include "rswap_mapping.h"

static unsigned int active_servers;
static struct rswap_pool_layout pool_layout;
static bool area_active;
static unsigned int area_type;
static DEFINE_SPINLOCK(area_lock);
static atomic_t area_submissions = ATOMIC_INIT(0);

struct rswap_remote_page {
	struct rdma_session_context *session;
	struct remote_chunk *chunk;
	u64 chunk_offset;
};

/* All DIRECT processing, including callbacks, is serialized per CQ. */
static void rswap_poll_queue(struct rswap_rdma_queue *queue, int budget)
{
	unsigned long flags;

	if (queue->cq->poll_ctx != IB_POLL_DIRECT)
		return;
	spin_lock_irqsave(&queue->cq_lock, flags);
	ib_process_cq_direct(queue->cq, budget);
	spin_unlock_irqrestore(&queue->cq_lock, flags);
}

static void rswap_wait_progress(struct rswap_rdma_queue *queue)
{
	if (queue->cq->poll_ctx == IB_POLL_DIRECT) {
		rswap_poll_queue(queue, 64);
	} else if (!in_atomic() && !irqs_disabled()) {
		/* SOFTIRQ owns this CQ. Do not also process it directly. */
		wait_event_timeout(queue->sem,
			!atomic_read(&queue->rdma_post_counter), 1);
	}
	if (!in_atomic() && !irqs_disabled())
		cond_resched();
	cpu_relax();
}

void drain_rdma_queue(struct rswap_rdma_queue *queue)
{
	while (atomic_read(&queue->rdma_post_counter) || atomic_read(&queue->store_pending))
		rswap_wait_progress(queue);
	/* Also synchronize with a DIRECT callback's return, not just pending. */
	if (queue->cq->poll_ctx == IB_POLL_DIRECT)
		rswap_poll_queue(queue, 4);
}

void drain_all_rdma_queues(int target_mem_server)
{
	struct rdma_session_context *session;
	int i;

	if (WARN_ON(target_mem_server < 0 || (unsigned int)target_mem_server >= active_servers))
		return;
	session = &rdma_sessions[target_mem_server];
	for (i = 0; i < num_queues; i++)
		drain_rdma_queue(&session->rdma_queues[i]);
}

static void rswap_request_done(struct rswap_rdma_queue *queue)
{
	atomic_dec(&queue->rdma_post_counter);
	wake_up_all(&queue->sem);
}

/* Last owner retires the store. A failed synchronous prepare never publishes
 * and leaves the lock with swap_writepage() for its disk fallback.
 */
static void rswap_store_put(struct fs_rdma_req *req)
{
	struct rswap_rdma_queue *queue = req->rdma_queue;
	struct page *page = req->page;

	if (!refcount_dec_and_test(&req->store_refs))
		return;
	if (req->store_published) {
		if (req->status) {
			set_page_dirty(page);
			ClearPageReclaim(page);
			/* commit already published the bit, so it cannot reappear
			 * after this invalidate. The dirty local copy stays resident.
			 */
			frontswap_invalidate_page(req->swap_type, req->offset);
		}
		unlock_page(page);
	}
	put_page(page);
	kmem_cache_free(queue->fs_rdma_req_cache, req);
	atomic_dec(&queue->store_pending);
	wake_up_all(&queue->sem);
}

void fs_rdma_callback(struct ib_cq *cq, struct ib_wc *wc)
{
	struct fs_rdma_req *req = container_of(wc->wr_cqe, struct fs_rdma_req, cqe);
	struct rswap_rdma_queue *queue = req->rdma_queue;
	struct ib_device *dev = queue->rdma_session->rdma_dev->dev;
	enum rdma_queue_type type = queue->type;
	struct page *page = req->page;
	int status = wc->status == IB_WC_SUCCESS ? 0 : -EIO;

	ib_dma_unmap_page(dev, req->dma_addr, PAGE_SIZE,
			type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE);
	rswap_stats_wc(queue->rdma_session, type, status);
	if (status) {
		rswap_rdma_error(queue, status);
		pr_err_ratelimited("rswap: server=%u queue=%d WC status=%d\n",
			queue->rdma_session->server_id, queue->q_index, wc->status);
	}
	trace_printk("server: %u type: %d lat: %llu\n",
		queue->rdma_session->server_id, type,
		(unsigned long long)((ktime_get_ns() - req->start_ns) / 1000));

	if (type == QP_STORE) {
		req->status = status;
		smp_store_release(&req->io_done, true);
		rswap_store_put(req);
		rswap_request_done(queue);
		return;
	}
	if (status) {
		ClearPageUptodate(page);
		SetPageError(page);
	} else {
		ClearPageError(page);
		SetPageUptodate(page);
	}
	/* Demand reads remain locked for Hermit, independent of lazy_poll. */
	if (type == QP_LOAD_ASYNC)
		unlock_page(page);
	put_page(page);
	kmem_cache_free(queue->fs_rdma_req_cache, req);
	rswap_request_done(queue);
}

static int fs_enqueue_send_wr(struct rswap_rdma_queue *queue,
		struct fs_rdma_req *req)
{
	const struct ib_send_wr *bad_wr;
	int pending, ret;

	for (;;) {
		if (READ_ONCE(queue->rdma_session->stopping))
			return -ESHUTDOWN;
		ret = READ_ONCE(queue->rdma_session->error);
		if (ret)
			return ret;
		pending = atomic_read(&queue->rdma_post_counter);
		if (pending >= RDMA_SEND_QUEUE_DEPTH - 16) {
			/* A local disabled BH cannot service a SOFTIRQ CQ. */
			if (queue->cq->poll_ctx != IB_POLL_DIRECT &&
			    (in_softirq() || irqs_disabled()))
				return -EAGAIN;
			rswap_wait_progress(queue);
			continue;
		}
		if (atomic_cmpxchg(&queue->rdma_post_counter, pending, pending + 1) == pending)
			break;
	}
	req->start_ns = ktime_get_ns();
	ret = ib_post_send(queue->qp, &req->rdma_wr.wr, &bad_wr);
	if (ret)
		rswap_request_done(queue);
	/* On success a load callback may already have freed req. */
	return ret;
}

static int fs_build_rdma_wr(struct rswap_rdma_queue *queue,
		struct fs_rdma_req *req, const struct rswap_remote_page *remote,
		struct page *page)
{
	struct ib_device *dev = queue->rdma_session->rdma_dev->dev;
	enum dma_data_direction dir = queue->type == QP_STORE ?
		DMA_TO_DEVICE : DMA_FROM_DEVICE;

	req->dma_addr = ib_dma_map_page(dev, page, 0, PAGE_SIZE, dir);
	if (ib_dma_mapping_error(dev, req->dma_addr))
		return -ENOMEM;
	req->cqe.done = fs_rdma_callback;
	req->sge.addr = req->dma_addr;
	req->sge.length = PAGE_SIZE;
	req->sge.lkey = queue->rdma_session->rdma_dev->pd->local_dma_lkey;
	req->rdma_wr.wr.wr_cqe = &req->cqe;
	req->rdma_wr.wr.sg_list = &req->sge;
	req->rdma_wr.wr.num_sge = 1;
	req->rdma_wr.wr.opcode = queue->type == QP_STORE ? IB_WR_RDMA_WRITE : IB_WR_RDMA_READ;
	req->rdma_wr.wr.send_flags = IB_SEND_SIGNALED;
	req->rdma_wr.remote_addr = remote->chunk->remote_addr + remote->chunk_offset;
	req->rdma_wr.rkey = remote->chunk->remote_rkey;
	return 0;
}

/* All store/load paths resolve the same immutable pool layout. */
static int rswap_resolve_remote_page(pgoff_t offset,
		struct rswap_remote_page *remote)
{
	struct rdma_session_context *session;
	struct remote_chunk *chunk;
	u64 slot, byte_offset, chunk_idx, chunk_offset, end;
	unsigned int server;
	int ret;

	ret = rswap_map_page(&pool_layout, offset, &server, &slot);
	if (ret)
		return ret;
	session = &rdma_sessions[server];
	if (!session->remote_mem_pool.chunks)
		return -ENODEV;
	if (slot > (U64_MAX >> PAGE_SHIFT))
		return -EOVERFLOW;
	byte_offset = slot << PAGE_SHIFT;
	chunk_idx = byte_offset >> CHUNK_SHIFT;
	chunk_offset = byte_offset & CHUNK_MASK;
	if (chunk_idx >= session->remote_mem_pool.chunk_num)
		return -ERANGE;
	chunk = &session->remote_mem_pool.chunks[chunk_idx];
	if (chunk->chunk_state != MAPPED)
		return -ENXIO;
	if (chunk->mapped_size < PAGE_SIZE ||
	    chunk_offset > chunk->mapped_size - PAGE_SIZE)
		return -ERANGE;
	if (check_add_overflow(chunk->remote_addr, chunk_offset, &end) ||
	    check_add_overflow(end, (u64)PAGE_SIZE, &end))
		return -EOVERFLOW;

	remote->session = session;
	remote->chunk = chunk;
	remote->chunk_offset = chunk_offset;
	return 0;
}

/* Stores retain a publication reference until Frontswap commits their bit. */
static int rswap_rdma_submit_active(int cpu, pgoff_t offset, struct page *page,
		enum rdma_queue_type type, struct fs_rdma_req **store_req)
{
	struct rswap_remote_page remote;
	struct rdma_session_context *session = NULL;
	struct rswap_rdma_queue *queue;
	struct fs_rdma_req *req;
	int ret;

	if (PageCompound(page)) {
		ret = -EOPNOTSUPP;
		goto submit_error;
	}
	if (cpu < 0 || cpu >= online_cores || (unsigned int)type >= NUM_QP_TYPE) {
		ret = -EINVAL;
		goto submit_error;
	}
	ret = rswap_resolve_remote_page(offset, &remote);
	if (ret)
		goto submit_error;
	session = remote.session;
	queue = get_rdma_queue(remote.session, cpu, type);
	req = kmem_cache_zalloc(queue->fs_rdma_req_cache, GFP_ATOMIC);
	if (!req) {
		ret = -ENOMEM;
		goto submit_error;
	}
	req->rdma_queue = queue;
	req->page = page;
	req->offset = offset;
	if (type == QP_STORE) {
		req->swap_type = area_type;
		refcount_set(&req->store_refs, 2);
	}
	get_page(page);
	ret = fs_build_rdma_wr(queue, req, &remote, page);
	if (ret)
		goto free_req;
	if (type == QP_STORE)
		atomic_inc(&queue->store_pending);
	ret = fs_enqueue_send_wr(queue, req);
	if (ret) {
		ib_dma_unmap_page(remote.session->rdma_dev->dev, req->dma_addr,
			PAGE_SIZE, type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE);
		if (type == QP_STORE)
			atomic_dec(&queue->store_pending);
		goto free_req;
	}
	if (type == QP_STORE)
		*store_req = req;
	return 0;
free_req:
	put_page(page);
	kmem_cache_free(queue->fs_rdma_req_cache, req);
submit_error:
	rswap_stats_submit_error(session, type);
	return ret;
}

static int rswap_rdma_submit(int cpu, pgoff_t offset, struct page *page,
		enum rdma_queue_type type, struct fs_rdma_req **store_req)
{
	unsigned long flags;
	int ret;

	spin_lock_irqsave(&area_lock, flags);
	if (!area_active) {
		spin_unlock_irqrestore(&area_lock, flags);
		rswap_stats_submit_error(NULL, type);
		return -ESHUTDOWN;
	}
	atomic_inc(&area_submissions);
	spin_unlock_irqrestore(&area_lock, flags);
	ret = rswap_rdma_submit_active(cpu, offset, page, type, store_req);
	atomic_dec(&area_submissions);
	return ret;
}

int rswap_rdma_send(int cpu, pgoff_t offset, struct page *page,
		enum rdma_queue_type type)
{
	/* Stores must enter through prepare/commit, never bypass publication. */
	if (type == QP_STORE) {
		rswap_stats_submit_error(NULL, type);
		return -EINVAL;
	}
	return rswap_rdma_submit(cpu, offset, page, type, NULL);
}

static int rswap_store_prepare(unsigned int type, pgoff_t offset,
		struct page *page, int core, void **cookie)
{
	struct fs_rdma_req *req;
	int cpu, ret;

	*cookie = NULL;
	if (type != READ_ONCE(area_type)) {
		rswap_stats_submit_error(NULL, QP_STORE);
		return -EINVAL;
	}
	cpu = core == -1 ? get_cpu() : core;
	ret = rswap_rdma_submit(cpu, offset, page, QP_STORE, &req);
	if (core == -1)
		put_cpu();
	if (ret)
		return ret;
	if (core == -1) {
		while (!smp_load_acquire(&req->io_done))
			rswap_wait_progress(req->rdma_queue);
		ret = req->status;
		if (ret) {
			rswap_store_put(req);
			return ret;
		}
	}
	*cookie = req;
	return 0;
}

static void rswap_store_commit(void *cookie)
{
	struct fs_rdma_req *req = cookie;

	req->store_published = true;
	rswap_store_put(req);
}

static int rswap_poll_cpu(int cpu, enum rdma_queue_type type, bool peek)
{
	unsigned int id;
	int ret = 0, pending = 0;

	if (cpu < 0 || cpu >= online_cores)
		return -EINVAL;
	for (id = 0; id < active_servers; id++) {
		struct rswap_rdma_queue *queue = get_rdma_queue(&rdma_sessions[id], cpu, type);

		if (peek) {
			int count, stores;

			rswap_poll_queue(queue, 4);
			count = atomic_read(&queue->rdma_post_counter);
			stores = atomic_read(&queue->store_pending);
			pending += count > stores ? count : stores;
		} else {
			drain_rdma_queue(queue);
			if (!ret)
				ret = READ_ONCE(rdma_sessions[id].error);
		}
	}
	/* peek reports terminal completion; page flags carry read success. */
	return peek ? pending : ret;
}

static int rswap_frontswap_poll_store(int cpu)
{
	return rswap_poll_cpu(cpu, QP_STORE, false);
}

static int rswap_frontswap_poll_load(int cpu)
{
	return rswap_poll_cpu(cpu, QP_LOAD_SYNC, false);
}

static int rswap_frontswap_peek_store(int cpu)
{
	return rswap_poll_cpu(cpu, QP_STORE, true);
}

static int rswap_frontswap_peek_load(int cpu)
{
	return rswap_poll_cpu(cpu, QP_LOAD_SYNC, true);
}

static int rswap_frontswap_load(unsigned int type, pgoff_t offset, struct page *page)
{
	if (type != READ_ONCE(area_type)) {
		rswap_stats_submit_error(NULL, QP_LOAD_SYNC);
		return -EINVAL;
	}
	return rswap_rdma_send(smp_processor_id(), offset, page, QP_LOAD_SYNC);
}

static int rswap_frontswap_load_async(unsigned int type, pgoff_t offset, struct page *page)
{
	int cpu = get_cpu();

	put_cpu();
	if (type != READ_ONCE(area_type)) {
		rswap_stats_submit_error(NULL, QP_LOAD_ASYNC);
		return -EINVAL;
	}
	/* This queue completes by SOFTIRQ, so migration after selection is safe. */
	return rswap_rdma_send(cpu, offset, page, QP_LOAD_ASYNC);
}

static void rswap_invalidate_page(unsigned int type, pgoff_t offset)
{
	/* Static slots follow Linux's entry lifetime; do not sleep here. */
}

static void rswap_invalidate_area(unsigned int type)
{
	unsigned int id;
	unsigned long flags;

	spin_lock_irqsave(&area_lock, flags);
	area_active = false;
	spin_unlock_irqrestore(&area_lock, flags);
	/* No submitter can reserve a WR after the drain has passed its queue. */
	while (atomic_read(&area_submissions))
		cond_resched();
	for (id = 0; id < active_servers; id++)
		drain_all_rdma_queues(id);
}

static void rswap_frontswap_init(unsigned int type)
{
	unsigned long flags;

	spin_lock_irqsave(&area_lock, flags);
	WRITE_ONCE(area_type, type);
	area_active = true;
	spin_unlock_irqrestore(&area_lock, flags);
}

static struct frontswap_ops rswap_frontswap_ops = {
	.init = rswap_frontswap_init,
	.store_prepare = rswap_store_prepare,
	.store_commit = rswap_store_commit,
	.load = rswap_frontswap_load,
	.load_async = rswap_frontswap_load_async,
	.poll_load = rswap_frontswap_poll_load,
	.poll_store = rswap_frontswap_poll_store,
	.peek_load = rswap_frontswap_peek_load,
	.peek_store = rswap_frontswap_peek_store,
	.invalidate_page = rswap_invalidate_page,
	.invalidate_area = rswap_invalidate_area,
};

int rswap_register_frontswap(void)
{
	return frontswap_register_managed_ops_v2(&rswap_frontswap_ops, THIS_MODULE,
					    pool_layout.total_pages);
}

void rswap_deregister_frontswap(void)
{
	frontswap_unregister_managed_ops(&rswap_frontswap_ops);
}

int rswap_client_init(const struct rswap_client_config *config)
{
	struct rswap_pool_layout layout = {};
	struct rdma_session_context *session;
	unsigned int id, total_gib = 0;
	int ret;

	/* The wire protocol numbers queues by a dense, fixed online CPU set. */
	if (PAGE_SIZE != 4096)
		return -EOPNOTSUPP;
	online_cores = num_online_cpus();
	for_each_online_cpu(id) {
		if (id >= online_cores) {
			pr_err("rswap: online CPU IDs must be contiguous from zero\n");
			return -EINVAL;
		}
	}
	num_queues = online_cores * NUM_QP_TYPE;
	/* Includes selected but unstarted sessions, so partial failure can unwind. */
	active_servers = config->nr_servers;
	for (id = 0; id < active_servers; id++) {
		session = &rdma_sessions[id];
		session->server_id = id;
		session->timeout_ms = config->timeout_ms;
		session->port = htons(config->servers[id].port);
		if (!in4_pton(config->servers[id].ip, -1, session->addr, '\0', NULL)) {
			ret = -EINVAL;
			goto fail;
		}
	}
	for (id = 0; id < active_servers; id++) {
		session = &rdma_sessions[id];
		pr_info("rswap: connecting server=%u endpoint=%s:%u\n",
			id, config->servers[id].ip, config->servers[id].port);
		ret = init_rdma_sessions(session);
		if (!ret)
			ret = rdma_session_connect(session);
		if (ret)
			goto fail;
		/* Each peer advertises at most MAX_REGION_NUM 8-GiB regions. */
		total_gib += session->remote_mem_pool.remote_mem_size;
		layout.server_pages[id] =
			((u64)session->remote_mem_pool.remote_mem_size << GB_SHIFT) >> PAGE_SHIFT;
		layout.total_pages += layout.server_pages[id];
	}
	for (id = 0; id < active_servers; id++) {
		ret = READ_ONCE(rdma_sessions[id].error);
		if (ret)
			goto fail;
	}
	if (total_gib != config->size_gb) {
		pr_err("rswap: capacity mismatch: configured=%u GiB discovered=%u GiB servers=%u\n",
			config->size_gb, total_gib, active_servers);
		ret = -ENOSPC;
		goto fail;
	}
	/* Publish only after every selected session and total capacity are valid. */
	layout.nr_servers = active_servers;
	pool_layout = layout;
	ret = rswap_stats_init(active_servers);
	if (ret) {
		pr_err("rswap: statistics interface initialization failed: %d\n", ret);
		goto fail;
	}
	pr_info("rswap: pool ready servers=%u total=%u GiB (4-KiB swap ready)\n",
		active_servers, total_gib);
	return 0;
fail:
	pr_err("rswap: initialization failed (%d); rolling back %u selected session(s)\n",
		ret, active_servers);
	rswap_client_exit();
	return ret;
}

void rswap_client_exit(void)
{
	unsigned int id;

	/* Remove the interface before any Session storage is released. */
	rswap_stats_exit();
	/* Stop every selected session before reverse-order destruction. */
	for (id = 0; id < active_servers; id++)
		WRITE_ONCE(rdma_sessions[id].stopping, true);
	for (id = active_servers; id > 0; id--)
		rswap_disconnect_and_collect_resource(&rdma_sessions[id - 1]);
	active_servers = 0;
	memset(&pool_layout, 0, sizeof(pool_layout));
}
