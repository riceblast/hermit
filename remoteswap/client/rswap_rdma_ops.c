#include <linux/ktime.h>
#include <linux/mutex.h>
#include <linux/overflow.h>
#include <linux/types.h>
#include <linux/tracepoint.h>

#include <linux/swap_stats.h>
#include <linux/hermit.h>

#include "rswap_rdma.h"
#include "rswap_mapping.h"

static unsigned int active_servers;
static struct rswap_pool_layout pool_layout;

struct rswap_remote_page {
	struct rdma_session_context *session;
	struct remote_chunk *chunk;
	u64 chunk_offset;
};

struct end2end_rdma_lat** qp_store_lat;
struct end2end_rdma_lat** qp_sync_load_lat;
struct end2end_rdma_lat** qp_async_load_lat;

struct mutex* qp_store_mutex;
struct mutex* qp_sync_load_mutex;
struct mutex* qp_async_load_mutex;

u64 list_size;

/**
 * Wait for the finish of ALL the outstanding rdma_request
 */
void drain_rdma_queue(struct rswap_rdma_queue *rdma_queue)
{
	int nr_pending = atomic_read(&rdma_queue->rdma_post_counter);
	int nr_done = 0;

	preempt_disable();
	while (atomic_read(&rdma_queue->rdma_post_counter) > 0) {
		int nr_completed;
		// IB_POLL_BATCH is 16 by default
		nr_completed = ib_process_cq_direct(rdma_queue->cq, 4);
		nr_done += nr_completed;
		if (nr_done >= nr_pending)
			break;
		cpu_relax();
	}
	preempt_enable();
}

void write_drain_rdma_queue(struct rswap_rdma_queue *rdma_queue)
{
	int nr_pending = atomic_read(&rdma_queue->rdma_post_counter);
	int nr_done = 0;

	while (atomic_read(&rdma_queue->rdma_post_counter) > 0) {
		int nr_completed;
		// IB_POLL_BATCH is 16 by default
		nr_completed = ib_process_cq_direct(rdma_queue->cq, 64);
		nr_done += nr_completed;
		if (nr_done >= nr_pending)
			break;
		cpu_relax();
	}
}

static inline int peek_rdma_queue(struct rswap_rdma_queue *rdma_queue)
{
	if (atomic_read(&rdma_queue->rdma_post_counter) > 0)
		ib_process_cq_direct(rdma_queue->cq, 4);
	return atomic_read(&rdma_queue->rdma_post_counter);
}

/**
 * Drain all the outstanding messages for a specific memory server.
 */
void drain_all_rdma_queues(int target_mem_server)
{
	int i;
	struct rdma_session_context *rdma_session = &rdma_sessions[0];

	for (i = 0; i < num_queues; i++) {
		drain_rdma_queue(&(rdma_session->rdma_queues[i]));
	}
}

int _get_free_idx(struct end2end_rdma_lat* rdma_lat_list)
{
	int idx = 0;

	for (idx = 0; idx < (RDMA_SEND_QUEUE_DEPTH + RDMA_RECV_QUEUE_DEPTH); idx += 1) {
		if (rdma_lat_list[idx].wr_id_ == 0) {
			return idx;
		}
	}
	return -1;
}

// 0: success
// -1: failed
int _init_lat_entry(struct end2end_rdma_lat* rdma_lat_list, struct mutex* mtx, u64 wr_id)
{
	int free_idx = -1;
	int success = -1;
	u64 cur_time_us;
	ktime_t kt;

	//while(1) {
		// if (nr_try_times == 10) {
		// 	printk("WARN, nr_try_times == 10\n");
		// }

		free_idx = _get_free_idx(rdma_lat_list);
		if (free_idx == -1 || free_idx >= (RDMA_SEND_QUEUE_DEPTH + RDMA_RECV_QUEUE_DEPTH)) {
			printk("(FREE_IDX BUG!! free_idx == -1\n");
			return -1;
		}

		
		//mutex_lock(mtx);
		if (rdma_lat_list[free_idx].wr_id_ == 0) {
			rdma_lat_list[free_idx].wr_id_ = wr_id;
			
			kt = ktime_get();
			cur_time_us = ktime_to_us(kt);
			rdma_lat_list[free_idx].start_time_us_ = cur_time_us;
			success = 0;
		} else {
			printk("init_lat_entry conflict\n");
		}
		//mutex_unlock(mtx);

		// if (success == 0) {
		// 	kt = ktime_get();
		// 	cur_time_us = ktime_to_us(kt);
		// 	rdma_lat_list[free_idx].start_time_us_ = cur_time_us;

		// 	//break;
		// } else {
		// 	printk("init_lat_entry conflict\n");
		// }
		//nr_try_times += 1;
	//}
	return success;
}

void record_start_time(u64 wr_id, int cpu, enum rdma_queue_type type) 
{
	if (cpu >= list_size) {
		printk("BUG!! cpu > list_size, %d - %lld\n", cpu, list_size);
		return;
	}

	switch (type) {
		case QP_STORE:
			_init_lat_entry(qp_store_lat[cpu], &qp_store_mutex[cpu], wr_id);
			break;

		case QP_LOAD_SYNC:
			_init_lat_entry(qp_sync_load_lat[cpu], &qp_sync_load_mutex[cpu], wr_id);
			break;

		case QP_LOAD_ASYNC:
			_init_lat_entry(qp_async_load_lat[cpu], &qp_async_load_mutex[cpu], wr_id);
			break;

		case NUM_QP_TYPE:
			printk("(NUM_QP_TYPE)BUG!!\n");
			break;
	}
}

int _get_target_idx(struct end2end_rdma_lat* rdma_lat_list, u64 wr_id)
{
	int idx = 0;

	for (idx = 0; idx < (RDMA_SEND_QUEUE_DEPTH + RDMA_RECV_QUEUE_DEPTH); idx += 1) {
		if (rdma_lat_list[idx].wr_id_ == wr_id) {
			return idx;
		}
	}
	return -1;
}

int _caculate_lat(struct end2end_rdma_lat* rdma_lat_list, struct mutex* mtx, u64 wr_id, enum rdma_queue_type type)
{
	int success = -1;
	int target_idx = -1;
	u64 cur_time_us;
	ktime_t kt;

	target_idx = _get_target_idx(rdma_lat_list, wr_id);
	if (target_idx == -1) {
		printk("TARGET_IDX BUG!! target_idx == -1\n");
		return -1;
	}
	if (rdma_lat_list[target_idx].wr_id_ != wr_id) {
		printk("TARGET WR_ID BUG!! wr_id not match\n");
		return -1;
	}
	if (rdma_lat_list[target_idx].start_time_us_ == 0) {
		printk("START_TIME_US BUG!! start_time_us == 0\n");
		return -1;
	}

	kt = ktime_get();
	cur_time_us = ktime_to_us(kt);
	trace_printk("type: %d lat: %llu\n", type, cur_time_us - rdma_lat_list[target_idx].start_time_us_);

	//mutex_lock(mtx);
	rdma_lat_list[target_idx].wr_id_ = 0;
	// rdma_lat_list[target_idx].start_time_us_ = 0;
	// rdma_lat_list[target_idx].end_time_us_ = 0;
	//mutex_unlock(mtx);

	return success;
}

void dump_lat(u64 wr_id, int cpu, enum rdma_queue_type type)
{
	if (cpu >= list_size) {
		printk("BUG!! cpu > list_size, %d - %lld\n", cpu, list_size);
		return;
	}

	switch (type) {
		case QP_STORE:
			_caculate_lat(qp_store_lat[cpu], &qp_store_mutex[cpu], wr_id, type);
			break;

		case QP_LOAD_SYNC:
			_caculate_lat(qp_sync_load_lat[cpu], &qp_sync_load_mutex[cpu], wr_id, type);
			break;

		case QP_LOAD_ASYNC:
			_caculate_lat(qp_async_load_lat[cpu], &qp_async_load_mutex[cpu], wr_id, type);
			break;

		case NUM_QP_TYPE:
			printk("(NUM_QP_TYPE)BUG!!\n");
			break;
	}
}

/**
 * The callback function for rdma requests.
 */
void fs_rdma_callback(struct ib_cq *cq, struct ib_wc *wc)
{
	struct fs_rdma_req *rdma_req =
		container_of(wc->wr_cqe, struct fs_rdma_req, cqe);
	struct rswap_rdma_queue *rdma_queue = cq->cq_context;
	struct ib_device *ibdev = rdma_queue->rdma_session->rdma_dev->dev;
	bool unlock = true;
	int cpu;
	enum rdma_queue_type type;

#if defined(RSWAP_KERNEL_SUPPORT) && RSWAP_KERNEL_SUPPORT >= 3
	unlock = !hmt_ctl_flag(HMT_LAZY_POLL);
#else
	unlock = true;
#endif

	if (unlikely(wc->status != IB_WC_SUCCESS)) {
		pr_err("%s status is not success, it is=%d\n", __func__,
		       wc->status);
	}
	get_rdma_queue_cpu_type(rdma_queue->rdma_session, rdma_queue, &cpu, &type);
	if (type == QP_STORE) { // STORE requests
		dump_lat(wc->wr_id, cpu, type);

		// set_page_writeback(rdma_req->page);
		unlock_page(rdma_req->page);
		// end_page_writeback(rdma_req->page);
	} else if (type == QP_LOAD_SYNC) { // LOAD SYNC requests
		dump_lat(wc->wr_id, cpu, type);

		// originally called in swap_readpage(). Moved here for asynchrony.
		SetPageUptodate(rdma_req->page);
		if (unlock)
			unlock_page(rdma_req->page);
	} else if (type == QP_LOAD_ASYNC) { // LOAD ASYNC requests
		dump_lat(wc->wr_id, cpu, type);

		// originally called in swap_readpage(). Moved here for asynchrony.
		SetPageUptodate(rdma_req->page);
		unlock_page(rdma_req->page);
	} else {
		printk("Err cq handle cpu: %d type: %d\n", cpu, type);
	}

	atomic_dec(&rdma_queue->rdma_post_counter);
	ib_dma_unmap_page(ibdev, rdma_req->dma_addr, PAGE_SIZE,
			  type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE);
	kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
}

int fs_enqueue_send_wr(struct rdma_session_context *rdma_session,
		       struct rswap_rdma_queue *rdma_queue,
		       struct fs_rdma_req *rdma_req)
{
	int ret = 0;
	const struct ib_send_wr *bad_wr;
	int test;

	int cpu;
	enum rdma_queue_type type;

	rdma_req->rdma_queue = rdma_queue;

	get_rdma_queue_cpu_type(rdma_session, rdma_queue, &cpu, &type);
	if (type != rdma_queue->type) {
		printk("RDMA_QUEUE_TYPE BUG!! %d - %d\n", type, rdma_queue->type);
	}

	while (1) {
		test = atomic_inc_return(&rdma_queue->rdma_post_counter);
		if (test < RDMA_SEND_QUEUE_DEPTH - 16) {

			record_start_time((u64)rdma_req, cpu, type);

			ret = ib_post_send(
				rdma_queue->qp,
				(struct ib_send_wr *)&rdma_req->rdma_wr,
				&bad_wr);
			if (unlikely(ret)) {
				pr_err("%s, post 1-sided RDMA send wr failed, "
				       "return value :%d. counter %d \n",
				       __func__, ret, test);
				ret = -1;
				goto err;
			}

			return ret;
		} else { // RDMA send queue is full, wait for next turn.
			test = atomic_dec_return(
				&rdma_queue->rdma_post_counter);
			cpu_relax();

			drain_rdma_queue(rdma_queue);
			pr_err("%s, back pressure...\n", __func__);
		}
	}
err:
	pr_err(" Error in %s \n", __func__);
	return -1;
}

/**
 * Build a rdma_wr for frontswap data path.
 */
int fs_build_rdma_wr(struct rdma_session_context *rdma_session,
		     struct rswap_rdma_queue *rdma_queue,
		     struct fs_rdma_req *rdma_req,
		     struct remote_chunk *remote_chunk_ptr,
		     size_t offset_within_chunk, struct page *page,
		     enum rdma_queue_type type)
{
	int ret = 0;
	enum dma_data_direction dir;
	struct ib_device *dev = rdma_session->rdma_dev->dev;

	rdma_req->page = page;

	dir = type == QP_STORE ? DMA_TO_DEVICE : DMA_FROM_DEVICE;
	rdma_req->dma_addr = ib_dma_map_page(dev, page, 0, PAGE_SIZE, dir);
	if (unlikely(ib_dma_mapping_error(dev, rdma_req->dma_addr))) {
		pr_err("%s, ib_dma_mapping_error\n", __func__);
		ret = -ENOMEM;
		kmem_cache_free(rdma_queue->fs_rdma_req_cache, rdma_req);
		goto out;
	}

	ib_dma_sync_single_for_device(dev, rdma_req->dma_addr, PAGE_SIZE, dir);

	rdma_req->cqe.done = fs_rdma_callback;

	rdma_req->sge.addr = rdma_req->dma_addr;
	rdma_req->sge.length = PAGE_SIZE;
	rdma_req->sge.lkey = rdma_session->rdma_dev->pd->local_dma_lkey;

	rdma_req->rdma_wr.wr.next = NULL;
	rdma_req->rdma_wr.wr.wr_cqe = &rdma_req->cqe;
	rdma_req->rdma_wr.wr.sg_list = &(rdma_req->sge);
	rdma_req->rdma_wr.wr.num_sge = 1;
	rdma_req->rdma_wr.wr.opcode =
		(dir == DMA_TO_DEVICE ? IB_WR_RDMA_WRITE : IB_WR_RDMA_READ);
	rdma_req->rdma_wr.wr.send_flags = IB_SEND_SIGNALED;
	rdma_req->rdma_wr.remote_addr =
		remote_chunk_ptr->remote_addr + offset_within_chunk;
	rdma_req->rdma_wr.rkey = remote_chunk_ptr->remote_rkey;

// debug
#ifdef DEBUG_MODE_BRIEF
	if (dir == DMA_FROM_DEVICE) {
		pr_info("%s, read data from remote 0x%lx, size 0x%lx \n",
			__func__, (size_t)rdma_req->rdma_wr.remote_addr,
			(size_t)PAGE_SIZE);
	}
#endif

out:
	return ret;
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

/* Optionally return the actual queue so a synchronous store can wait on it. */
static int rswap_rdma_send_on_queue(int cpu, pgoff_t offset, struct page *page,
		enum rdma_queue_type type, struct rswap_rdma_queue **target_queue)
{
	struct rswap_remote_page remote;
	struct rswap_rdma_queue *rdma_queue;
	struct fs_rdma_req *rdma_req;
	int ret;

	if (cpu < 0 || cpu >= online_cores || (unsigned int)type >= NUM_QP_TYPE)
		return -EINVAL;
	ret = rswap_resolve_remote_page(offset, &remote);
	if (ret)
		return ret;
	if (READ_ONCE(remote.session->stopping))
		return -ESHUTDOWN;
	ret = READ_ONCE(remote.session->error);
	if (ret)
		return ret;
	if (!remote.session->rdma_queues || !remote.session->rdma_dev ||
	    !remote.session->rdma_dev->pd)
		return -ENODEV;

	rdma_queue = get_rdma_queue(remote.session, cpu, type);
	rdma_req = (struct fs_rdma_req *)kmem_cache_alloc(
		rdma_queue->fs_rdma_req_cache, GFP_ATOMIC);
	if (!rdma_req) {
		pr_err("%s, get reserved fs_rdma_req failed. \n", __func__);
		ret = -ENOMEM;
		goto out;
	}

	ret = fs_build_rdma_wr(remote.session, rdma_queue, rdma_req,
			       remote.chunk, remote.chunk_offset, page,
			       type);
	if (unlikely(ret)) {
		pr_err("%s, Build rdma_wr failed.\n", __func__);
		goto out;
	}

	ret = fs_enqueue_send_wr(remote.session, rdma_queue, rdma_req);
	if (unlikely(ret)) {
		pr_err("%s, enqueue rdma_wr failed.\n", __func__);
		goto out;
	}
	if (target_queue)
		*target_queue = rdma_queue;

out:
	return ret;
}

int rswap_rdma_send(int cpu, pgoff_t offset, struct page *page,
		    enum rdma_queue_type type)
{
	return rswap_rdma_send_on_queue(cpu, offset, page, type, NULL);
}

/**
 * Synchronously write data to memory server.
 */
int rswap_frontswap_store(unsigned type, pgoff_t swap_entry_offset,
			  struct page *page)
{
	int ret = 0;
	int cpu;
	struct rswap_rdma_queue *rdma_queue;

	cpu = get_cpu();
	ret = rswap_rdma_send_on_queue(cpu, swap_entry_offset, page, QP_STORE,
				      &rdma_queue);
	put_cpu();
	if (unlikely(ret)) {
		pr_err("%s, enqueuing rdma frontswap write failed.\n",
		       __func__);
		goto out;
	}
	write_drain_rdma_queue(rdma_queue);

	ret = 0;
out:
	return ret;
}

int rswap_frontswap_store_on_core(unsigned type, pgoff_t swap_entry_offset,
				  struct page *page, int core)
{
	int ret = 0;

	ret = rswap_rdma_send(core, swap_entry_offset, page, QP_STORE);
	if (unlikely(ret)) {
		pr_err("%s, enqueuing rdma frontswap write failed.\n",
		       __func__);
		goto out;
	}

	ret = 0;
out:
	return ret;
}

int rswap_frontswap_poll_store(int core)
{
	struct rswap_rdma_queue *rdma_queue;

	core %= NR_WRITE_QUEUE;

	rdma_queue = get_rdma_queue(&rdma_sessions[0], core, QP_STORE);
	write_drain_rdma_queue(rdma_queue);
	return 0;
}

/**
 * Synchronously read data from memory server.
 *
 * return:
 *  0 : success
 *  non-zero : failed.
 */
int rswap_frontswap_load(unsigned type, pgoff_t swap_entry_offset,
			 struct page *page)
{
	int ret = 0;
	int cpu;
	cpu = smp_processor_id();
	ret = rswap_rdma_send(cpu, swap_entry_offset, page, QP_LOAD_SYNC);
	if (unlikely(ret)) {
		pr_err("%s, enqueuing rdma frontswap write failed.\n",
		       __func__);
		goto out;
	}

	ret = 0;
out:
	return ret;
}

int rswap_frontswap_load_async(unsigned type, pgoff_t swap_entry_offset,
			       struct page *page)
{
	int ret = 0;
	int cpu;

	cpu = smp_processor_id();
	ret = rswap_rdma_send(cpu, swap_entry_offset, page, QP_LOAD_ASYNC);
	if (unlikely(ret)) {
		pr_err("%s, enqueuing rdma frontswap write failed.\n",
		       __func__);
		goto out;
	}

	ret = 0;

out:
	return ret;
}

int rswap_frontswap_poll_load(int cpu)
{
	struct rswap_rdma_queue *rdma_queue;

	rdma_queue = get_rdma_queue(&rdma_sessions[0], cpu, QP_LOAD_SYNC);
	drain_rdma_queue(rdma_queue);
	return 0;
}

int rswap_frontswap_peek_load(int cpu)
{
	struct rswap_rdma_queue *rdma_queue =
		get_rdma_queue(&rdma_sessions[0], cpu, QP_LOAD_SYNC);
	return peek_rdma_queue(rdma_queue);
}

int rswap_frontswap_peek_store(int cpu)
{
	struct rswap_rdma_queue *rdma_queue;
	cpu %= NR_WRITE_QUEUE;
	rdma_queue = get_rdma_queue(&rdma_sessions[0], cpu, QP_STORE);
	return peek_rdma_queue(rdma_queue);
}

static void rswap_invalidate_page(unsigned type, pgoff_t offset)
{
	return;
}

static void rswap_invalidate_area(unsigned type)
{
	return;
}

static void rswap_frontswap_init(unsigned type)
{
}

static struct frontswap_ops rswap_frontswap_ops = {
	.init = rswap_frontswap_init,
	.store = rswap_frontswap_store,
	.load = rswap_frontswap_load,
	.invalidate_page = rswap_invalidate_page,
	.invalidate_area = rswap_invalidate_area,
#ifdef RSWAP_KERNEL_SUPPORT
	.load_async = rswap_frontswap_load_async,
	.poll_load = rswap_frontswap_poll_load,
#if RSWAP_KERNEL_SUPPORT >= 2
	.store_on_core = rswap_frontswap_store_on_core,
	.poll_store = rswap_frontswap_poll_store,
#endif // RSWAP_KERNEL_SUPPORT >= 2
#if RSWAP_KERNEL_SUPPORT >= 3
	.peek_load = rswap_frontswap_peek_load,
	.peek_store = rswap_frontswap_peek_store,
#endif // RSWAP_KERNEL_SUPPORT >= 3
#endif
};

/* Patch 2 keeps swap disabled until Patch 3 fixes multi-session completion. */
int rswap_register_frontswap(void)
{
	return frontswap_register_managed_ops(&rswap_frontswap_ops, THIS_MODULE, 0);
}

void rswap_deregister_frontswap(void)
{
	frontswap_unregister_managed_ops(&rswap_frontswap_ops);
}

static void free_latency_buffers(void)
{
	unsigned int cpu;

	for (cpu = 0; cpu < list_size; cpu++) {
		if (qp_store_lat)
			kfree(qp_store_lat[cpu]);
		if (qp_sync_load_lat)
			kfree(qp_sync_load_lat[cpu]);
		if (qp_async_load_lat)
			kfree(qp_async_load_lat[cpu]);
	}
	kfree(qp_store_lat);
	kfree(qp_sync_load_lat);
	kfree(qp_async_load_lat);
	kfree(qp_store_mutex);
	kfree(qp_sync_load_mutex);
	kfree(qp_async_load_mutex);
	qp_store_lat = NULL;
	qp_sync_load_lat = NULL;
	qp_async_load_lat = NULL;
	qp_store_mutex = NULL;
	qp_sync_load_mutex = NULL;
	qp_async_load_mutex = NULL;
	list_size = 0;
}

static int init_latency_buffers(void)
{
	unsigned int cpu;
	unsigned int entries = RDMA_SEND_QUEUE_DEPTH + RDMA_RECV_QUEUE_DEPTH;

	list_size = online_cores;
	qp_store_lat = kcalloc(online_cores, sizeof(*qp_store_lat), GFP_KERNEL);
	qp_sync_load_lat = kcalloc(online_cores, sizeof(*qp_sync_load_lat), GFP_KERNEL);
	qp_async_load_lat = kcalloc(online_cores, sizeof(*qp_async_load_lat), GFP_KERNEL);
	qp_store_mutex = kcalloc(online_cores, sizeof(*qp_store_mutex), GFP_KERNEL);
	qp_sync_load_mutex = kcalloc(online_cores, sizeof(*qp_sync_load_mutex), GFP_KERNEL);
	qp_async_load_mutex = kcalloc(online_cores, sizeof(*qp_async_load_mutex), GFP_KERNEL);
	if (!qp_store_lat || !qp_sync_load_lat || !qp_async_load_lat ||
	    !qp_store_mutex || !qp_sync_load_mutex || !qp_async_load_mutex)
		return -ENOMEM;
	for (cpu = 0; cpu < online_cores; cpu++) {
		qp_store_lat[cpu] = kcalloc(entries, sizeof(**qp_store_lat), GFP_KERNEL);
		qp_sync_load_lat[cpu] = kcalloc(entries, sizeof(**qp_sync_load_lat), GFP_KERNEL);
		qp_async_load_lat[cpu] = kcalloc(entries, sizeof(**qp_async_load_lat), GFP_KERNEL);
		if (!qp_store_lat[cpu] || !qp_sync_load_lat[cpu] || !qp_async_load_lat[cpu])
			return -ENOMEM;
		mutex_init(&qp_store_mutex[cpu]);
		mutex_init(&qp_sync_load_mutex[cpu]);
		mutex_init(&qp_async_load_mutex[cpu]);
	}
	return 0;
}

int rswap_client_init(const struct rswap_client_config *config)
{
	struct rswap_pool_layout layout = {};
	struct rdma_session_context *session;
	unsigned int id, total_gib = 0;
	int ret;

	online_cores = num_online_cpus();
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
	ret = init_latency_buffers();
	if (ret)
		goto fail;
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
	pr_info("rswap: pool ready servers=%u total=%u GiB (static mapping ready; swapon disabled)\n",
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

	/* Stop every selected session before reverse-order destruction. */
	for (id = 0; id < active_servers; id++)
		WRITE_ONCE(rdma_sessions[id].stopping, true);
	for (id = active_servers; id > 0; id--)
		rswap_disconnect_and_collect_resource(&rdma_sessions[id - 1]);
	active_servers = 0;
	memset(&pool_layout, 0, sizeof(pool_layout));
	free_latency_buffers();
}
