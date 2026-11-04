#include "rswap_rdma.h"
#include <linux/jiffies.h>
#include <linux/overflow.h>

struct rdma_session_context rdma_sessions[RSWAP_NUM_SERVERS];
int online_cores;
int num_queues; /* Per session. */
u64 rmda_ops_count;
u64 cq_notify_count;
u64 cq_get_count;

enum rdma_queue_type get_qp_type(int idx)
{
	return idx / online_cores;
}

struct rswap_rdma_queue *get_rdma_queue(struct rdma_session_context *session,
		unsigned int cpu, enum rdma_queue_type type)
{
	if (WARN_ON(cpu >= online_cores || type >= NUM_QP_TYPE))
		return NULL;
	return &session->rdma_queues[cpu + online_cores * type];
}

void get_rdma_queue_cpu_type(struct rdma_session_context *session,
		struct rswap_rdma_queue *queue, unsigned int *cpu,
		enum rdma_queue_type *type)
{
	*cpu = queue->q_index % online_cores;
	*type = queue->type;
}

static void queue_error(struct rswap_rdma_queue *queue, int error)
{
	cmpxchg(&queue->error, 0, error < 0 ? error : -EIO);
	cmpxchg(&queue->rdma_session->error, 0, error < 0 ? error : -EIO);
	wake_up_all(&queue->sem);
}

/* Initialization runs in process context; never use the I/O busy-drain here. */
static int wait_queue_state(struct rswap_rdma_queue *queue,
		enum rdma_queue_state state)
{
	struct rdma_session_context *session = queue->rdma_session;
	long ret;

	for (;;) {
		if (READ_ONCE(session->error))
			return READ_ONCE(session->error);
		if (READ_ONCE(queue->error))
			return READ_ONCE(queue->error);
		if (READ_ONCE(session->stopping))
			return -ESHUTDOWN;
		if (READ_ONCE(queue->state) == state &&
		    !atomic_read(&queue->rdma_post_counter))
			return 0;
		if (time_after_eq(jiffies, session->deadline)) {
			pr_err("rswap: server=%u queue=%d timeout waiting for state=%d\n",
			       session->server_id, queue->q_index, state);
			return -ETIMEDOUT;
		}
		if (queue->cq && queue->cq->poll_ctx == IB_POLL_DIRECT) {
			ib_process_cq_direct(queue->cq, 16);
			if (READ_ONCE(queue->error) ||
			    (READ_ONCE(queue->state) == state &&
			     !atomic_read(&queue->rdma_post_counter)))
				continue;
		}
		ret = wait_event_interruptible_timeout(queue->sem,
			READ_ONCE(queue->error) || READ_ONCE(session->stopping) ||
			(READ_ONCE(queue->state) == state &&
			 !atomic_read(&queue->rdma_post_counter)), 1);
		if (ret < 0)
			return ret;
		cond_resched();
	}
}

int handle_recv_wr(struct rswap_rdma_queue *queue, struct ib_wc *wc)
{
	struct rdma_session_context *session = queue->rdma_session;
	struct message *msg = session->rdma_recv_req.recv_buf;
	unsigned int i;
	u64 end;

	ib_dma_sync_single_for_cpu(session->rdma_dev->dev,
		session->rdma_recv_req.recv_dma_addr, sizeof(*msg), DMA_BIDIRECTIONAL);
	if (wc->byte_len != sizeof(*msg) || msg->type != queue->expected_message)
		return -EPROTO;

	switch (msg->type) {
	case AVAILABLE_TO_QUERY:
		WRITE_ONCE(queue->state, MEMORY_SERVER_AVAILABLE);
		break;
	case FREE_SIZE:
		if (msg->mapped_chunk != session->expected_chunks)
			return -ENOSPC;
		WRITE_ONCE(queue->state, FREE_MEM_RECV);
		break;
	case GOT_CHUNKS:
		if (msg->mapped_chunk != session->expected_chunks)
			return -EPROTO;
		for (i = 0; i < session->expected_chunks; i++) {
			if (!msg->buf[i] || (msg->buf[i] & (PAGE_SIZE - 1)) ||
			    msg->mapped_size[i] != (u64)REGION_SIZE_GB * ONE_GB ||
			    check_add_overflow(msg->buf[i], msg->mapped_size[i], &end))
				return -EPROTO;
		}
		bind_remote_memory_chunks(session);
		WRITE_ONCE(queue->state, RECEIVED_CHUNKS);
		break;
	default:
		return -EPROTO;
	}
	return 0;
}

void two_sided_message_done(struct ib_cq *cq, struct ib_wc *wc)
{
	struct rswap_rdma_queue *queue = cq->cq_context;
	int ret = 0;

	if (!READ_ONCE(queue->rdma_session->stopping)) {
		if (wc->status != IB_WC_SUCCESS)
			ret = -EIO;
		else if (wc->opcode == IB_WC_RECV)
			ret = handle_recv_wr(queue, wc);
		else if (wc->opcode != IB_WC_SEND)
			ret = -EPROTO;
		if (ret) {
			pr_err("rswap: server=%u queue=%d control completion status=%d error=%d\n",
			       queue->rdma_session->server_id, queue->q_index,
			       wc->status, ret);
			queue_error(queue, ret);
		}
	}
	/* Errors and WR_FLUSH_ERR consume a posted WR just like success. */
	atomic_dec(&queue->rdma_post_counter);
	wake_up_all(&queue->sem);
}

static int post_control_recv(struct rdma_session_context *session,
		struct rswap_rdma_queue *queue, int expected)
{
	const struct ib_recv_wr *bad;
	int ret;

	queue->expected_message = expected;
	ib_dma_sync_single_for_device(session->rdma_dev->dev,
		session->rdma_recv_req.recv_dma_addr, sizeof(struct message), DMA_BIDIRECTIONAL);
	atomic_inc(&queue->rdma_post_counter);
	ret = ib_post_recv(queue->qp, &session->rdma_recv_req.rq_wr, &bad);
	if (ret)
		atomic_dec(&queue->rdma_post_counter);
	return ret;
}

int send_message_to_remote(struct rdma_session_context *session,
		int index, int type, int chunks)
{
	struct rswap_rdma_queue *queue = &session->rdma_queues[index];
	const struct ib_send_wr *bad;
	int ret;

	ret = post_control_recv(session, queue,
		type == QUERY ? FREE_SIZE : GOT_CHUNKS);
	if (ret)
		return ret;
	ib_dma_sync_single_for_cpu(session->rdma_dev->dev,
		session->rdma_send_req.send_dma_addr, sizeof(struct message), DMA_BIDIRECTIONAL);
	session->rdma_send_req.send_buf->type = type;
	session->rdma_send_req.send_buf->mapped_chunk = chunks;
	ib_dma_sync_single_for_device(session->rdma_dev->dev,
		session->rdma_send_req.send_dma_addr, sizeof(struct message), DMA_BIDIRECTIONAL);
	atomic_inc(&queue->rdma_post_counter);
	ret = ib_post_send(queue->qp, &session->rdma_send_req.sq_wr, &bad);
	if (ret)
		atomic_dec(&queue->rdma_post_counter);
	return ret;
}

int rswap_query_available_memory(struct rdma_session_context *session)
{
	int ret = send_message_to_remote(session, 0, QUERY, 0);

	if (ret)
		return ret;
	ret = wait_queue_state(&session->rdma_queues[0], FREE_MEM_RECV);
	if (ret)
		return ret;
	/* Allocate in process context, not from a CQ/softirq callback. */
	return init_remote_chunk_list(session);
}

int rswap_request_for_chunk(struct rdma_session_context *session)
{
	int ret = send_message_to_remote(session, 0, REQUEST_CHUNKS,
					 session->expected_chunks);

	return ret ? ret : wait_queue_state(&session->rdma_queues[0], RECEIVED_CHUNKS);
}

int rswap_rdma_cm_event_handler(struct rdma_cm_id *id,
		struct rdma_cm_event *event)
{
	struct rswap_rdma_queue *queue = id->context;
	int ret;

	if (READ_ONCE(queue->rdma_session->stopping))
		return 0;
	switch (event->event) {
	case RDMA_CM_EVENT_ADDR_RESOLVED:
		ret = rdma_resolve_route(id, 2000);
		if (ret)
			queue_error(queue, ret);
		break;
	case RDMA_CM_EVENT_ROUTE_RESOLVED:
		WRITE_ONCE(queue->state, ROUTE_RESOLVED);
		wake_up_all(&queue->sem);
		break;
	case RDMA_CM_EVENT_ESTABLISHED:
		/* The ready message can complete before this CM event. */
		break;
	case RDMA_CM_EVENT_ADDR_ERROR:
	case RDMA_CM_EVENT_ROUTE_ERROR:
	case RDMA_CM_EVENT_CONNECT_ERROR:
	case RDMA_CM_EVENT_UNREACHABLE:
	case RDMA_CM_EVENT_REJECTED:
	case RDMA_CM_EVENT_DISCONNECTED:
	case RDMA_CM_EVENT_DEVICE_REMOVAL:
	case RDMA_CM_EVENT_ADDR_CHANGE:
		queue_error(queue, event->status < 0 ? event->status : -ECONNRESET);
		break;
	case RDMA_CM_EVENT_TIMEWAIT_EXIT:
		break;
	default:
		queue_error(queue, -EPROTO);
	}
	/* Destruction belongs to process-context cleanup, never CM auto-destroy. */
	return 0;
}

static int rdma_resolve_ip_to_ib_device(struct rdma_session_context *session,
		struct rswap_rdma_queue *queue)
{
	struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = session->port };
	int ret;

	memcpy(&addr.sin_addr.s_addr, session->addr, 4);
	ret = rdma_resolve_addr(queue->cm_id, NULL, (struct sockaddr *)&addr, 2000);
	return ret ? ret : wait_queue_state(queue, ROUTE_RESOLVED);
}

int rswap_create_qp(struct rdma_session_context *session,
		struct rswap_rdma_queue *queue)
{
	struct ib_qp_init_attr attr = {};
	int ret;

	attr.cap.max_send_wr = session->send_queue_depth;
	attr.cap.max_recv_wr = session->recv_queue_depth;
	attr.cap.max_recv_sge = MAX_REQUEST_SGL;
	attr.cap.max_send_sge = MAX_REQUEST_SGL;
	attr.sq_sig_type = IB_SIGNAL_REQ_WR;
	attr.qp_type = IB_QPT_RC;
	attr.send_cq = queue->cq;
	attr.recv_cq = queue->cq;
	ret = rdma_create_qp(queue->cm_id, session->rdma_dev->pd, &attr);
	if (!ret)
		queue->qp = queue->cm_id->qp;
	return ret;
}

int rswap_create_rdma_queue(struct rdma_session_context *session, int index)
{
	struct rswap_rdma_queue *queue = &session->rdma_queues[index];
	struct ib_device *dev = queue->cm_id->device;
	struct ib_pd *pd;
	int ret;

	if (!session->rdma_dev) {
		session->rdma_dev = kzalloc(sizeof(*session->rdma_dev), GFP_KERNEL);
		if (!session->rdma_dev)
			return -ENOMEM;
		/* The Client needs a local DMA key, not a remotely accessible PD. */
		pd = ib_alloc_pd(dev, 0);
		if (IS_ERR(pd))
			return PTR_ERR(pd);
		session->rdma_dev->pd = pd;
		session->rdma_dev->dev = dev;
		ret = setup_rdma_session_comm_buffer(session);
		if (ret)
			return ret;
	} else if (session->rdma_dev->dev != dev) {
		return -EXDEV;
	}
	queue->cq = ib_alloc_cq(dev, queue,
		session->send_queue_depth + session->recv_queue_depth, 0,
		queue->type == QP_LOAD_ASYNC ? IB_POLL_SOFTIRQ : IB_POLL_DIRECT);
	if (IS_ERR(queue->cq)) {
		ret = PTR_ERR(queue->cq);
		queue->cq = NULL;
		return ret;
	}
	return rswap_create_qp(session, queue);
}

void rswap_setup_message_wr(struct rdma_session_context *session)
{
	struct two_sided_rdma_recv *recv = &session->rdma_recv_req;
	struct two_sided_rdma_send *send = &session->rdma_send_req;

	recv->recv_sgl.addr = recv->recv_dma_addr;
	recv->recv_sgl.length = sizeof(struct message);
	recv->recv_sgl.lkey = session->rdma_dev->pd->local_dma_lkey;
	recv->rq_wr.sg_list = &recv->recv_sgl;
	recv->rq_wr.num_sge = 1;
	recv->cqe.done = two_sided_message_done;
	recv->rq_wr.wr_cqe = &recv->cqe;
	send->send_sgl.addr = send->send_dma_addr;
	send->send_sgl.length = sizeof(struct message);
	send->send_sgl.lkey = session->rdma_dev->pd->local_dma_lkey;
	send->sq_wr.sg_list = &send->send_sgl;
	send->sq_wr.num_sge = 1;
	send->sq_wr.opcode = IB_WR_SEND;
	send->sq_wr.send_flags = IB_SEND_SIGNALED;
	send->cqe.done = two_sided_message_done;
	send->sq_wr.wr_cqe = &send->cqe;
}

int rswap_setup_buffers(struct rdma_session_context *session)
{
	struct ib_device *dev = session->rdma_dev->dev;

	session->rdma_recv_req.recv_buf = kzalloc(sizeof(struct message), GFP_KERNEL);
	session->rdma_send_req.send_buf = kzalloc(sizeof(struct message), GFP_KERNEL);
	if (!session->rdma_recv_req.recv_buf || !session->rdma_send_req.send_buf)
		return -ENOMEM;
	session->rdma_recv_req.recv_dma_addr = ib_dma_map_single(dev,
		session->rdma_recv_req.recv_buf, sizeof(struct message), DMA_BIDIRECTIONAL);
	if (ib_dma_mapping_error(dev, session->rdma_recv_req.recv_dma_addr))
		return -ENOMEM;
	session->recv_mapped = true;
	session->rdma_send_req.send_dma_addr = ib_dma_map_single(dev,
		session->rdma_send_req.send_buf, sizeof(struct message), DMA_BIDIRECTIONAL);
	if (ib_dma_mapping_error(dev, session->rdma_send_req.send_dma_addr))
		return -ENOMEM;
	session->send_mapped = true;
	rswap_setup_message_wr(session);
	return 0;
}

int setup_rdma_session_comm_buffer(struct rdma_session_context *session)
{
	return rswap_setup_buffers(session);
}

int rswap_connect_remote_memory_server(struct rdma_session_context *session, int index)
{
	struct rswap_rdma_queue *queue = &session->rdma_queues[index];
	struct rdma_conn_param param = {
		.responder_resources = 1, .initiator_depth = 1, .retry_count = 7,
	};
	int ret = post_control_recv(session, queue, AVAILABLE_TO_QUERY);

	if (ret)
		return ret;
	queue->connect_started = true;
	ret = rdma_connect(queue->cm_id, &param);
	return ret ? ret : wait_queue_state(queue, MEMORY_SERVER_AVAILABLE);
}

int init_remote_chunk_list(struct rdma_session_context *session)
{
	session->remote_mem_pool.chunks = kcalloc(session->expected_chunks,
					 sizeof(struct remote_chunk), GFP_KERNEL);
	return session->remote_mem_pool.chunks ? 0 : -ENOMEM;
}

void bind_remote_memory_chunks(struct rdma_session_context *session)
{
	struct message *msg = session->rdma_recv_req.recv_buf;
	unsigned int i;

	for (i = 0; i < session->expected_chunks; i++) {
		struct remote_chunk *chunk = &session->remote_mem_pool.chunks[i];

		chunk->remote_rkey = msg->rkey[i];
		chunk->remote_addr = msg->buf[i];
		chunk->mapped_size = msg->mapped_size[i];
		chunk->chunk_state = MAPPED;
	}
}

int init_rdma_sessions(struct rdma_session_context *session)
{
	int i;

	session->rdma_queues = kcalloc(num_queues, sizeof(struct rswap_rdma_queue), GFP_KERNEL);
	if (!session->rdma_queues)
		return -ENOMEM;
	session->send_queue_depth = RDMA_SEND_QUEUE_DEPTH + 1;
	session->recv_queue_depth = RDMA_RECV_QUEUE_DEPTH + 1;
	for (i = 0; i < num_queues; i++) {
		struct rswap_rdma_queue *queue = &session->rdma_queues[i];

		queue->rdma_session = session;
		queue->q_index = i;
		queue->type = get_qp_type(i);
		queue->state = IDLE;
		init_waitqueue_head(&queue->sem);
		spin_lock_init(&queue->cq_lock);
		atomic_set(&queue->rdma_post_counter, 0);
	}
	return 0;
}

int rswap_init_rdma_queue(struct rdma_session_context *session, int index)
{
	struct rswap_rdma_queue *queue = &session->rdma_queues[index];
	struct rdma_cm_id *id;
	char name[48];

	id = rdma_create_id(&init_net, rswap_rdma_cm_event_handler, queue,
			    RDMA_PS_TCP, IB_QPT_RC);
	if (IS_ERR(id))
		return PTR_ERR(id);
	queue->cm_id = id;
	snprintf(name, sizeof(name), "rswap_s%u_q%d", session->server_id, index);
	queue->fs_rdma_req_cache = kmem_cache_create(name, sizeof(struct fs_rdma_req),
				0, SLAB_TEMPORARY | SLAB_HWCACHE_ALIGN, NULL);
	if (!queue->fs_rdma_req_cache)
		return -ENOMEM;
	return rdma_resolve_ip_to_ib_device(session, queue);
}

int rdma_session_connect(struct rdma_session_context *session)
{
	int i, ret;

	session->deadline = jiffies + msecs_to_jiffies(session->timeout_ms);
	for (i = 0; i < num_queues; i++) {
		ret = rswap_init_rdma_queue(session, i);
		if (!ret)
			ret = rswap_create_rdma_queue(session, i);
		if (!ret)
			ret = rswap_connect_remote_memory_server(session, i);
		if (ret) {
			pr_err("rswap: server=%u queue=%d initialization failed: %d\n",
			       session->server_id, i, ret);
			return ret;
		}
	}
	ret = rswap_query_available_memory(session);
	if (!ret)
		ret = rswap_request_for_chunk(session);
	if (!ret)
		ret = READ_ONCE(session->error);
	if (!ret)
		pr_info("rswap: server=%u ready queues=%d capacity=%u GiB regions=%u\n",
			session->server_id, num_queues,
			session->remote_mem_pool.remote_mem_size, session->expected_chunks);
	return ret;
}

void rswap_free_buffers(struct rdma_session_context *session)
{
	struct ib_device *dev = session->rdma_dev ? session->rdma_dev->dev : NULL;

	if (session->send_mapped) {
		ib_dma_unmap_single(dev, session->rdma_send_req.send_dma_addr,
				    sizeof(struct message), DMA_BIDIRECTIONAL);
		session->send_mapped = false;
	}
	if (session->recv_mapped) {
		ib_dma_unmap_single(dev, session->rdma_recv_req.recv_dma_addr,
				    sizeof(struct message), DMA_BIDIRECTIONAL);
		session->recv_mapped = false;
	}
	kfree(session->rdma_recv_req.recv_buf);
	session->rdma_recv_req.recv_buf = NULL;
	kfree(session->rdma_send_req.send_buf);
	session->rdma_send_req.send_buf = NULL;
	kfree(session->remote_mem_pool.chunks);
	session->remote_mem_pool.chunks = NULL;
}

int rswap_disconnect_and_collect_resource(struct rdma_session_context *session)
{
	int i;

	WRITE_ONCE(session->stopping, true);
	if (!session->rdma_queues)
		return 0;
	for (i = 0; i < num_queues; i++) {
		struct rswap_rdma_queue *queue = &session->rdma_queues[i];

		if (queue->qp) {
			if (queue->connect_started)
				rdma_disconnect(queue->cm_id);
			/* Flush both SQ and RQ, including an unanswered control receive. */
			ib_drain_qp(queue->qp);
			rdma_destroy_qp(queue->cm_id);
			queue->qp = NULL;
		}
		if (queue->cq) {
			ib_free_cq(queue->cq);
			queue->cq = NULL;
		}
		WARN_ON(atomic_read(&queue->rdma_post_counter));
	}
	/* CM IDs still hold their device references while DMA and PD are released. */
	rswap_free_buffers(session);
	if (session->rdma_dev) {
		if (session->rdma_dev->pd)
			ib_dealloc_pd(session->rdma_dev->pd);
		kfree(session->rdma_dev);
		session->rdma_dev = NULL;
	}
	for (i = 0; i < num_queues; i++) {
		struct rswap_rdma_queue *queue = &session->rdma_queues[i];

		if (queue->cm_id) {
			/* Waits for CM handlers and cancels unresolved address/route work. */
			rdma_destroy_id(queue->cm_id);
			queue->cm_id = NULL;
		}
		if (queue->fs_rdma_req_cache)
			kmem_cache_destroy(queue->fs_rdma_req_cache);
	}
	kfree(session->rdma_queues);
	session->rdma_queues = NULL;
	pr_info("rswap: server=%u resources released\n", session->server_id);
	return 0;
}

/**
 * Debug utils
 */
char *rdma_cm_message_print(int cm_message_id)
{
	char *type2str[17] = { "RDMA_CM_EVENT_ADDR_RESOLVED",
			       "RDMA_CM_EVENT_ADDR_ERROR",
			       "RDMA_CM_EVENT_ROUTE_RESOLVED",
			       "RDMA_CM_EVENT_ROUTE_ERROR",
			       "RDMA_CM_EVENT_CONNECT_REQUEST",
			       "RDMA_CM_EVENT_CONNECT_RESPONSE",
			       "RDMA_CM_EVENT_CONNECT_ERROR",
			       "RDMA_CM_EVENT_UNREACHABLE",
			       "RDMA_CM_EVENT_REJECTED",
			       "RDMA_CM_EVENT_ESTABLISHED",
			       "RDMA_CM_EVENT_DISCONNECTED",
			       "RDMA_CM_EVENT_DEVICE_REMOVAL",
			       "RDMA_CM_EVENT_MULTICAST_JOIN",
			       "RDMA_CM_EVENT_MULTICAST_ERROR",
			       "RDMA_CM_EVENT_ADDR_CHANGE",
			       "RDMA_CM_EVENT_TIMEWAIT_EXIT",
			       "ERROR Message Type" };

	char *message_type_name = (char *)kzalloc(32, GFP_KERNEL);
	strcpy(message_type_name,
	       type2str[cm_message_id < 16 ? cm_message_id : 16]);
	return message_type_name;
}

/**
 * wc.status name
 */
char *rdma_wc_status_name(int wc_status_id)
{
	char *type2str[23] = {
		"IB_WC_SUCCESS",	   "IB_WC_LOC_LEN_ERR",
		"IB_WC_LOC_QP_OP_ERR",	   "IB_WC_LOC_EEC_OP_ERR",
		"IB_WC_LOC_PROT_ERR",	   "IB_WC_WR_FLUSH_ERR",
		"IB_WC_MW_BIND_ERR",	   "IB_WC_BAD_RESP_ERR",
		"IB_WC_LOC_ACCESS_ERR",	   "IB_WC_REM_INV_REQ_ERR",
		"IB_WC_REM_ACCESS_ERR",	   "IB_WC_REM_OP_ERR",
		"IB_WC_RETRY_EXC_ERR",	   "IB_WC_RNR_RETRY_EXC_ERR",
		"IB_WC_LOC_RDD_VIOL_ERR",  "IB_WC_REM_INV_RD_REQ_ERR",
		"IB_WC_REM_ABORT_ERR",	   "IB_WC_INV_EECN_ERR",
		"IB_WC_INV_EEC_STATE_ERR", "IB_WC_FATAL_ERR",
		"IB_WC_RESP_TIMEOUT_ERR",  "IB_WC_GENERAL_ERR",
		"ERROR Message Type",
	};

	char *message_type_name = (char *)kzalloc(32, GFP_KERNEL);
	strcpy(message_type_name,
	       type2str[wc_status_id < 22 ? wc_status_id : 22]);
	return message_type_name;
}

/**
 * The message type name, used for 2-sided RDMA communication.
 */
char *rdma_message_print(int message_id)
{
	char *message_type_name;
	char *type2str[12] = {
		"DONE",
		"GOT_CHUNKS",
		"GOT_SINGLE_CHUNK",
		"FREE_SIZE",
		"EVICT",
		"ACTIVITY",
		"STOP",
		"REQUEST_CHUNKS",
		"REQUEST_SINGLE_CHUNK",
		"QUERY",
		"AVAILABLE_TO_QUERY",
		"ERROR Message Type",
	};

	message_id -= 1;

	message_type_name = (char *)kzalloc(32, GFP_KERNEL);
	strcpy(message_type_name, type2str[message_id < 11 ? message_id : 11]);
	return message_type_name;
}

char *rdma_session_context_state_print(int id)
{
	char *rdma_seesion_state_name;

	char *state2str[20] = { "IDLE",
				"CONNECT_REQUEST",
				"ADDR_RESOLVED",
				"ROUTE_RESOLVED",
				"CONNECTED",
				"FREE_MEM_RECV",
				"RECEIVED_CHUNKS",
				"RDMA_BUF_ADV",
				"WAIT_OPS",
				"RECV_STOP",
				"RECV_EVICT",
				"RDMA_WRITE_RUNNING",
				"RDMA_READ_RUNNING",
				"SEND_DONE",
				"RDMA_DONE",
				"RDMA_READ_ADV",
				"RDMA_WRITE_ADV",
				"CM_DISCONNECT",
				"ERROR",
				"Un-defined state." };

	id -= 1;
	rdma_seesion_state_name = (char *)kzalloc(32, GFP_KERNEL); // 32 bytes.
	strcpy(rdma_seesion_state_name, state2str[id < 19 ? id : 19]);
	return rdma_seesion_state_name;
}

/**
 * print the information of this scatterlist
 */
void print_scatterlist_info(struct scatterlist *sl_ptr, int nents)
{
	int i;

	pr_info("\n %s, %d entries , Start\n", __func__, nents);
	for (i = 0; i < nents; i++) {
		pr_info("%s, \n page_link(struct page*) : 0x%lx \n  offset : 0x%x bytes\n  length : 0x%x bytes \n dma_addr : 0x%llx \n ",
			__func__, sl_ptr[i].page_link, sl_ptr[i].offset,
			sl_ptr[i].length, sl_ptr[i].dma_address);
		//  }
	}
	pr_info(" %s End\n\n", __func__);
}
