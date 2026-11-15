#include <sys/mman.h>
#include <poll.h>
#include <signal.h>
#include <limits.h>
#include <numa.h>
#include <numaif.h>
#include <atomic>
#include <deque>
#include <fstream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "rswap_server.hpp"

struct context *global_rdma_ctx = NULL;
int rdma_queue_count = 0;
int online_cores = 0;
int rdma_num_queues = 0;

static const size_t page_size = 4096;
static const size_t region_size = ONE_GB * REGION_SIZE_GB;
static const char *tier_names[] = {"DRAM", "PMem"};
static volatile sig_atomic_t interrupted;
static std::atomic<bool> stopping(false);
static std::atomic<bool> cq_failed(false);
static int queue_slots_used;

struct server_options {
  std::string ip;
  unsigned int port = 0;
  unsigned int cores = 0;
  int nodes[2] = {-1, -1};
  size_t gib[2] = {0, 0};
};
static server_options options;

/* Only one shared control SEND buffer may be owned by the HCA at a time.
 * Queued replies are copied into it only after the previous SEND completion.
 */
struct control_reply {
  struct rswap_rdma_queue *queue;
  struct message msg;
};
static std::mutex send_lock;
static std::deque<control_reply> replies;
static bool send_busy;

void die(const char *reason) {
  throw std::runtime_error(reason);
}

static void fail_errno(const char *reason) {
  int saved_errno = errno;
  throw std::runtime_error(std::string(reason) + ": " + strerror(saved_errno));
}

static void signal_stop(int) {
  interrupted = 1;
}

static bool should_stop() {
  return interrupted || stopping.load();
}

static unsigned long long parse_number(const char *text, const char *name) {
  if (!*text)
    die((std::string("empty ") + name).c_str());
  for (const char *p = text; *p; ++p)
    if (*p < '0' || *p > '9')
      die((std::string("invalid ") + name + ": " + text).c_str());
  char *end;
  errno = 0;
  unsigned long long value = strtoull(text, &end, 10);
  if (errno || *end)
    die((std::string("out-of-range ") + name).c_str());
  return value;
}

static void usage(const char *program) {
  fprintf(stderr,
      "Usage: %s --ip IPv4 --port PORT --client-cores N "
      "--dram-node N --dram-gib GiB --pmem-node N|auto --pmem-gib GiB\n"
      "Both capacities must be positive multiples of 8 GiB. "
      "The CPU count belongs to the Client.\n", program);
}

static void parse_options(int argc, char **argv) {
  unsigned int seen = 0;
  const char *names[] = {"--ip", "--port", "--client-cores", "--dram-node",
                         "--dram-gib", "--pmem-node", "--pmem-gib"};
  for (int i = 1; i < argc; i += 2) {
    if (!strcmp(argv[i], "--help")) {
      usage(argv[0]);
      exit(0);
    }
    int key = 0;
    while (key < 7 && strcmp(argv[i], names[key]))
      ++key;
    if (key == 7 || i + 1 == argc || (seen & (1U << key)))
      die((std::string("unknown, repeated or incomplete option: ") + argv[i]).c_str());
    seen |= 1U << key;
    const char *value = argv[i + 1];
    if (key == 0) {
      struct in_addr address;
      if (inet_pton(AF_INET, value, &address) != 1)
        die("--ip must be an IPv4 address");
      options.ip = value;
    } else if (key == 5 && !strcmp(value, "auto")) {
      options.nodes[1] = -1;
    } else {
      unsigned long long n = parse_number(value, names[key]);
      switch (key) {
      case 1:
        if (!n || n > 65535)
          die("port must be in 1..65535");
        options.port = n;
        break;
      case 2:
        if (!n || n > INT_MAX / NUM_QP_TYPE)
          die("invalid Client CPU count");
        options.cores = n;
        break;
      case 3:
      case 5:
        if (n > INT_MAX)
          die("NUMA node ID is out of range");
        options.nodes[key == 3 ? 0 : 1] = n;
        break;
      case 4:
      case 6:
        if (!n || n % REGION_SIZE_GB ||
            n > MAX_REGION_NUM * REGION_SIZE_GB || n > SIZE_MAX / ONE_GB)
          die("Tier capacity must be a positive multiple of 8 GiB within the Chunk limit");
        options.gib[key == 4 ? 0 : 1] = n;
        break;
      }
    }
  }
  if (seen != (1U << 7) - 1)
    die("all seven options are required");
  if ((options.gib[0] + options.gib[1]) / REGION_SIZE_GB > MAX_REGION_NUM)
    die("total DRAM + PMem Chunk count exceeds MAX_REGION_NUM");
}

static bool node_has_cpus(int node) {
  std::ifstream input("/sys/devices/system/node/node" +
                      std::to_string(node) + "/cpulist");
  if (!input)
    die("cannot read NUMA node cpulist");
  std::string cpus;
  std::getline(input, cpus);
  return cpus.find_first_not_of(" \t\r\n") != std::string::npos;
}

static bool memory_node(int node) {
  if (node < 0 || node > numa_max_node() ||
      !numa_bitmask_isbitset(numa_all_nodes_ptr, node))
    return false;
  return numa_node_size64(node, NULL) > 0;
}

using node_mask = std::unique_ptr<struct bitmask, decltype(&numa_bitmask_free)>;

static void validate_nodes() {
  if (numa_available() < 0 || sysconf(_SC_PAGESIZE) != (long)page_size)
    die("NUMA support and 4-KiB system pages are required");
  if (options.nodes[1] == -1) {
    std::vector<int> candidates;
    for (int node = 0; node <= numa_max_node(); ++node)
      if (memory_node(node) && !node_has_cpus(node))
        candidates.push_back(node);
    fprintf(stderr, "PMem CPU-less memory node candidates:");
    for (int node : candidates)
      fprintf(stderr, " %d", node);
    fprintf(stderr, "\n");
    if (candidates.size() != 1)
      die("--pmem-node auto requires exactly one CPU-less memory node; specify a node explicitly");
    options.nodes[1] = candidates[0];
  }
  if (options.nodes[0] == options.nodes[1])
    die("DRAM and PMem must use different NUMA nodes");
  node_mask allowed(numa_allocate_nodemask(), numa_bitmask_free);
  if (!allowed)
    die("cannot allocate NUMA nodemask");
  if (get_mempolicy(NULL, allowed->maskp, allowed->size, NULL,
                    MPOL_F_MEMS_ALLOWED))
    fail_errno("get_mempolicy(MPOL_F_MEMS_ALLOWED)");
  for (unsigned int tier = 0; tier < 2; ++tier) {
    int node = options.nodes[tier];
    if (!memory_node(node) || !numa_bitmask_isbitset(allowed.get(), node))
      die("selected NUMA node must be online, contain memory and be allowed by the cpuset");
    if (node_has_cpus(node) != (tier == 0))
      die("DRAM node must have CPUs; PMem node must be CPU-less");
    long long free_bytes = 0;
    if (numa_node_size64(node, &free_bytes) < 0)
      fail_errno("numa_node_size64");
    fprintf(stderr, "tier=%s node=%d requested_gib=%zu free_bytes=%lld\n",
            tier_names[tier], node, options.gib[tier], free_bytes);
    if (free_bytes < 0 || (unsigned long long)free_bytes < options.gib[tier] * ONE_GB)
      die("selected NUMA node has insufficient free memory");
  }
}

static void touch_range(char *start, size_t pages) {
  const size_t workers = 16;
  std::vector<std::thread> threads;
  try {
    for (size_t worker = 0; worker < workers; ++worker) {
      size_t first = pages * worker / workers;
      size_t last = pages * (worker + 1) / workers;
      threads.emplace_back([=] {
        for (size_t page = first; page < last; ++page) {
          if (!(page & 4095) && should_stop())
            break;
          *reinterpret_cast<volatile char *>(start + page * page_size) = 0;
        }
      });
    }
  } catch (...) {
    stopping.store(true);
    for (auto &thread : threads)
      thread.join();
    throw;
  }
  for (auto &thread : threads)
    thread.join();
  if (should_stop())
    die("interrupted during memory allocation");
}

static void verify_range(const struct rdma_mem_pool::tier_range &range,
                         unsigned int tier) {
  const size_t batch = 4096;
  void *addresses[batch];
  int nodes[batch];
  size_t pages = range.size / page_size;
  for (size_t first = 0; first < pages; first += batch) {
    if (should_stop())
      die("interrupted during NUMA verification");
    size_t count = pages - first < batch ? pages - first : batch;
    for (size_t i = 0; i < count; ++i)
      addresses[i] = range.start + (first + i) * page_size;
    // nodes=NULL queries placement only; it never requests migration.
    if (move_pages(0, count, addresses, NULL, nodes, 0) < 0)
      fail_errno("move_pages NUMA query");
    for (size_t i = 0; i < count; ++i) {
      if (nodes[i] != range.node) {
        fprintf(stderr, "NUMA mismatch tier=%s address=%p expected_node=%d status=%d\n",
                tier_names[tier], addresses[i], range.node, nodes[i]);
        die("physical NUMA placement verification failed");
      }
    }
  }
  fprintf(stderr, "NUMA verified tier=%s node=%d pages=%zu bytes=%zu misplaced=0\n",
          tier_names[tier], range.node, pages, range.size);
}

void init_memory_pool(struct context *ctx) {
  ctx->mem_pool = (struct rdma_mem_pool *)calloc(1, sizeof(struct rdma_mem_pool));
  if (!ctx->mem_pool)
    fail_errno("allocate pool metadata");
  node_mask mask(numa_allocate_nodemask(), numa_bitmask_free);
  node_mask actual(numa_allocate_nodemask(), numa_bitmask_free);
  if (!mask || !actual)
    die("cannot allocate NUMA nodemask");
  for (unsigned int tier = 0; tier < 2; ++tier) {
    auto &range = ctx->mem_pool->tiers[tier];
    range.node = options.nodes[tier];
    range.size = options.gib[tier] * ONE_GB;
    range.chunks = options.gib[tier] / REGION_SIZE_GB;
    void *address = mmap(NULL, range.size, PROT_READ | PROT_WRITE,
                         MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (address == MAP_FAILED)
      fail_errno("mmap Tier range");
    range.start = (char *)address;
    numa_bitmask_clearall(mask.get());
    numa_bitmask_setbit(mask.get(), range.node);
    if (mbind(address, range.size, MPOL_BIND, mask->maskp, mask->size, MPOL_MF_STRICT))
      fail_errno("mbind(MPOL_BIND)");
    int mode = -1;
    if (get_mempolicy(&mode, actual->maskp, actual->size, address, MPOL_F_ADDR))
      fail_errno("get_mempolicy Tier range");
    if (mode != MPOL_BIND || !numa_bitmask_equal(mask.get(), actual.get()))
      die("NUMA policy readback does not match the requested single-node binding");
    if (madvise(address, range.size, MADV_NOHUGEPAGE))
      fail_errno("madvise(MADV_NOHUGEPAGE)");
    fprintf(stderr, "allocating tier=%s node=%d range=[%p,%p) gib=%zu chunks=%u policy=bind\n",
            tier_names[tier], range.node, address, range.start + range.size,
            options.gib[tier], range.chunks);
    touch_range(range.start, range.size / page_size);
    verify_range(range, tier);
    for (unsigned int local = 0; local < range.chunks; ++local) {
      int index = ctx->mem_pool->region_num++;
      ctx->mem_pool->region_list[index] = range.start + local * region_size;
      ctx->mem_pool->region_mapped_size[index] = region_size;
      ctx->mem_pool->cache_status[index] = -1;
      fprintf(stderr, "chunk=%d tier=%s node=%d address=%p bytes=%zu\n", index,
              tier_names[tier], range.node, ctx->mem_pool->region_list[index], region_size);
    }
  }
  fprintf(stderr, "memory allocation done: protocol=%u dram_chunks=%u pmem_chunks=%u total_chunks=%d total_gib=%zu; MR registration pending Client connection\n",
          RSWAP_PROTOCOL_VERSION, ctx->mem_pool->tiers[0].chunks,
          ctx->mem_pool->tiers[1].chunks, ctx->mem_pool->region_num,
          options.gib[0] + options.gib[1]);
}

static struct message make_message(enum message_type type) {
  struct message msg = {};
  msg.type = type;
  msg.protocol_version = RSWAP_PROTOCOL_VERSION;
  msg.mapped_chunk = global_rdma_ctx->mem_pool->region_num;
  msg.dram_chunks = global_rdma_ctx->mem_pool->tiers[0].chunks;
  msg.pmem_chunks = global_rdma_ctx->mem_pool->tiers[1].chunks;
  return msg;
}

static void post_next_reply() {
  if (send_busy || replies.empty())
    return;
  auto &reply = replies.front();
  struct context *ctx = reply.queue->rdma_session;
  *ctx->send_msg = reply.msg;
  struct ibv_sge sge = {};
  sge.addr = (uintptr_t)ctx->send_msg;
  sge.length = sizeof(struct message);
  sge.lkey = ctx->send_mr->lkey;
  struct ibv_send_wr wr = {}, *bad = NULL;
  wr.wr_id = (uintptr_t)reply.queue;
  wr.opcode = IBV_WR_SEND;
  wr.sg_list = &sge;
  wr.num_sge = 1;
  wr.send_flags = IBV_SEND_SIGNALED;
  if (ibv_post_send(reply.queue->qp, &wr, &bad))
    die("ibv_post_send control reply failed");
  send_busy = true;
  replies.pop_front();
}

void send_message(struct rswap_rdma_queue *queue, const struct message &msg) {
  std::lock_guard<std::mutex> guard(send_lock);
  replies.push_back({queue, msg});
  post_next_reply();
}

void register_rdma_comm_buffer(struct context *ctx) {
  ctx->send_msg = (struct message *)calloc(1, sizeof(struct message));
  ctx->recv_msg = (struct message *)calloc(1, sizeof(struct message));
  if (!ctx->send_msg || !ctx->recv_msg)
    fail_errno("allocate control buffers");
  ctx->send_mr = ibv_reg_mr(ctx->rdma_dev->pd, ctx->send_msg,
                          sizeof(struct message), IBV_ACCESS_LOCAL_WRITE);
  if (!ctx->send_mr)
    fail_errno("register control SEND MR");
  ctx->recv_mr = ibv_reg_mr(ctx->rdma_dev->pd, ctx->recv_msg,
                          sizeof(struct message), IBV_ACCESS_LOCAL_WRITE);
  if (!ctx->recv_mr)
    fail_errno("register control RECV MR");
}

void get_device_info(struct rswap_rdma_queue *queue) {
  struct context *ctx = global_rdma_ctx;
  if (!ctx->rdma_dev) {
    ctx->rdma_dev = (struct rswap_rdma_dev *)calloc(1, sizeof(struct rswap_rdma_dev));
    if (!ctx->rdma_dev)
      fail_errno("allocate RDMA metadata");
    ctx->rdma_dev->ctx = queue->cm_id->verbs;
    ctx->rdma_dev->pd = ibv_alloc_pd(queue->cm_id->verbs);
    if (!ctx->rdma_dev->pd)
      fail_errno("ibv_alloc_pd");
    ctx->comp_channel = ibv_create_comp_channel(queue->cm_id->verbs);
    if (!ctx->comp_channel)
      fail_errno("ibv_create_comp_channel");
    ctx->control_cq = ibv_create_cq(queue->cm_id->verbs, 16, NULL, ctx->comp_channel, 0);
    if (!ctx->control_cq)
      fail_errno("ibv_create_cq");
    if (ibv_req_notify_cq(ctx->control_cq, 0))
      die("ibv_req_notify_cq failed");
    register_rdma_comm_buffer(ctx);
  } else if (ctx->rdma_dev->ctx != queue->cm_id->verbs) {
    die("all Client QPs must use the same Server RDMA device");
  }
  queue->cq = ctx->control_cq;
}

void build_qp_attr(struct rswap_rdma_queue *queue, struct ibv_qp_init_attr *attr) {
  memset(attr, 0, sizeof(*attr));
  attr->send_cq = queue->cq;
  attr->recv_cq = queue->cq;
  attr->qp_type = IBV_QPT_RC;
  attr->cap.max_send_wr = 16;
  attr->cap.max_recv_wr = 16;
  attr->cap.max_send_sge = MAX_REQUEST_SGL;
  attr->cap.max_recv_sge = MAX_REQUEST_SGL;
}

void build_connection(struct rswap_rdma_queue *queue) {
  queue->rdma_session = global_rdma_ctx;
  queue->cm_id->context = queue;
  get_device_info(queue);
  struct ibv_qp_init_attr attr;
  build_qp_attr(queue, &attr);
  if (rdma_create_qp(queue->cm_id, global_rdma_ctx->rdma_dev->pd, &attr))
    fail_errno("rdma_create_qp");
  queue->qp = queue->cm_id->qp;
}

void build_params(struct rdma_conn_param *params) {
  memset(params, 0, sizeof(*params));
  params->initiator_depth = params->responder_resources = 1;
  params->rnr_retry_count = 7;
}

enum rdma_queue_type get_qp_type(int idx) {
  return (enum rdma_queue_type)(idx / online_cores);
}

struct rswap_rdma_queue *get_rdma_queue(unsigned int cpu, enum rdma_queue_type type) {
  if (cpu >= (unsigned int)online_cores || type >= NUM_QP_TYPE)
    return NULL;
  return &global_rdma_ctx->rdma_queues[cpu + type * online_cores];
}

int on_connect_request(struct rdma_cm_id *id) {
  if (queue_slots_used >= rdma_num_queues) {
    rdma_reject(id, NULL, 0);
    rdma_destroy_id(id);
    die("too many Client connections (check --client-cores)");
  }
  auto *queue = &global_rdma_ctx->rdma_queues[queue_slots_used++];
  queue->q_index = queue_slots_used - 1;
  queue->type = get_qp_type(queue->q_index);
  queue->cm_id = id;
  build_connection(queue);
  struct rdma_conn_param params;
  build_params(&params);
  if (rdma_accept(id, &params))
    fail_errno("rdma_accept");
  queue->connected = 1;
  ++rdma_queue_count;
  fprintf(stderr, "accepted queue=%d\n", queue->q_index);
  return 0;
}

void post_receives(struct rswap_rdma_queue *queue) {
  struct ibv_recv_wr wr = {}, *bad = NULL;
  struct ibv_sge sge = {};
  sge.addr = (uintptr_t)queue->rdma_session->recv_msg;
  sge.length = sizeof(struct message);
  sge.lkey = queue->rdma_session->recv_mr->lkey;
  wr.wr_id = (uintptr_t)queue;
  wr.sg_list = &sge;
  wr.num_sge = 1;
  if (ibv_post_recv(queue->qp, &wr, &bad))
    die("ibv_post_recv control request failed");
}

void inform_memory_pool_available(struct rswap_rdma_queue *queue) {
  if (!queue->rdma_session->pool_ready)
    die("attempt to announce an unregistered pool");
  send_message(queue, make_message(AVAILABLE_TO_QUERY));
}

int rdma_connected(struct rswap_rdma_queue *queue) {
  auto *ctx = queue->rdma_session;
  if (!ctx->pool_ready) {
    auto *pool = ctx->mem_pool;
    for (int i = 0; i < pool->region_num; ++i) {
      unsigned int tier = (unsigned int)i < pool->tiers[0].chunks ? 0 : 1;
      pool->mr_buffer[i] = ibv_reg_mr(ctx->rdma_dev->pd, pool->region_list[i],
          pool->region_mapped_size[i], IBV_ACCESS_LOCAL_WRITE |
          IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ);
      if (!pool->mr_buffer[i]) {
        fprintf(stderr, "MR registration failed chunk=%d tier=%s node=%d bytes=%zu: %s\n",
                i, tier_names[tier], pool->tiers[tier].node,
                pool->region_mapped_size[i], strerror(errno));
        die("pool unavailable: partial MR registration; rolling back");
      }
      fprintf(stderr, "MR registered chunk=%d tier=%s node=%d address=%p bytes=%zu rkey=0x%x\n",
              i, tier_names[tier], pool->tiers[tier].node, pool->mr_buffer[i]->addr,
              pool->region_mapped_size[i], pool->mr_buffer[i]->rkey);
    }
    ctx->pool_ready = true;
    ctx->connected = 1;
    ctx->expected_request = QUERY;
    int ret = pthread_create(&ctx->cq_poller_thread, NULL, poll_cq, ctx);
    if (ret)
      die((std::string("pthread_create: ") + strerror(ret)).c_str());
    ctx->cq_poller_started = true;
    fprintf(stderr, "all %d data MRs registered; pool available\n", pool->region_num);
  }
  // Existing Client sends QUERY / REQUEST_CHUNKS on queue zero only.
  if (queue->q_index == 0)
    post_receives(queue);
  inform_memory_pool_available(queue);
  return 0;
}

void send_free_mem_size(struct rswap_rdma_queue *queue) {
  send_message(queue, make_message(FREE_SIZE));
}

void send_regions(struct rswap_rdma_queue *queue) {
  if (!queue->rdma_session->pool_ready)
    die("attempt to publish an unregistered pool");
  auto *pool = queue->rdma_session->mem_pool;
  struct message msg = make_message(GOT_CHUNKS);
  for (int i = 0; i < pool->region_num; ++i) {
    msg.buf[i] = (uintptr_t)pool->mr_buffer[i]->addr;
    msg.mapped_size[i] = pool->region_mapped_size[i];
    msg.rkey[i] = pool->mr_buffer[i]->rkey;
  }
  send_message(queue, msg);
}

void handle_cqe(struct ibv_wc *wc) {
  if (wc->status != IBV_WC_SUCCESS)
    die((std::string("control WC failed: ") + ibv_wc_status_str(wc->status)).c_str());
  auto *queue = (struct rswap_rdma_queue *)(uintptr_t)wc->wr_id;
  auto *ctx = queue->rdma_session;
  if (wc->opcode == IBV_WC_SEND) {
    std::lock_guard<std::mutex> guard(send_lock);
    send_busy = false;
    post_next_reply();
  } else if (wc->opcode == IBV_WC_RECV) {
    auto *msg = ctx->recv_msg;
    if (queue->q_index != 0 || wc->byte_len != sizeof(*msg) ||
        msg->protocol_version != RSWAP_PROTOCOL_VERSION ||
        msg->type != ctx->expected_request)
      die("control request length, version or sequence mismatch; matching v2 Client required");
    if (msg->type == QUERY) {
      if (msg->mapped_chunk || msg->dram_chunks || msg->pmem_chunks)
        die("QUERY must have zero Chunk counts");
      ctx->expected_request = REQUEST_CHUNKS;
      send_free_mem_size(queue);
    } else if (msg->type == REQUEST_CHUNKS) {
      if (msg->mapped_chunk != ctx->mem_pool->region_num ||
          msg->dram_chunks != ctx->mem_pool->tiers[0].chunks ||
          msg->pmem_chunks != ctx->mem_pool->tiers[1].chunks)
        die("REQUEST_CHUNKS counts disagree with advertised topology");
      ctx->expected_request = DONE;
      ctx->server_state = S_BIND;
      send_regions(queue);
    } else {
      die("unsupported control request");
    }
    post_receives(queue);
  } else {
    die("unexpected control WC opcode");
  }
}

void *poll_cq(void *arg) {
  auto *ctx = (struct context *)arg;
  try {
    while (!should_stop()) {
      struct pollfd descriptor = {ctx->comp_channel->fd, POLLIN, 0};
      int ret = poll(&descriptor, 1, 200);
      if (ret < 0) {
        if (errno == EINTR)
          continue;
        fail_errno("poll control CQ");
      }
      if (!ret)
        continue;
      if (!(descriptor.revents & POLLIN))
        die("control completion channel closed");
      struct ibv_cq *cq;
      void *cq_context;
      if (ibv_get_cq_event(ctx->comp_channel, &cq, &cq_context))
        fail_errno("ibv_get_cq_event");
      ibv_ack_cq_events(cq, 1);
      if (ibv_req_notify_cq(cq, 0))
        die("ibv_req_notify_cq failed");
      struct ibv_wc wc;
      while ((ret = ibv_poll_cq(cq, 1, &wc)) > 0) {
        if (should_stop())
          break;
        handle_cqe(&wc);
      }
      if (ret < 0)
        die("ibv_poll_cq failed");
    }
  } catch (const std::exception &error) {
    fprintf(stderr, "ERROR: %s\n", error.what());
    cq_failed.store(true);
    stopping.store(true);
  }
  return NULL;
}

int on_disconnect(struct rswap_rdma_queue *queue) {
  if (queue->connected) {
    queue->connected = 0;
    --rdma_queue_count;
  }
  // Leave QPs and queue objects alive until the CQ thread has stopped.
  if (!rdma_queue_count)
    stopping.store(true);
  return 0;
}

int on_cm_event(struct rdma_cm_event *event) {
  auto *queue = (struct rswap_rdma_queue *)event->id->context;
  switch (event->event) {
  case RDMA_CM_EVENT_CONNECT_REQUEST:
    return on_connect_request(event->id);
  case RDMA_CM_EVENT_ESTABLISHED:
    return rdma_connected(queue);
  case RDMA_CM_EVENT_DISCONNECTED:
    return on_disconnect(queue);
  default:
    die((std::string("RDMA CM event: ") + rdma_event_str(event->event)).c_str());
  }
  return -1;
}

static void check_cleanup(int result, const char *operation) {
  if (result) {
    // Do not munmap a range whose MR could still be active. On this fatal
    // cleanup error let process teardown release the remaining HCA objects.
    fprintf(stderr, "ERROR: cleanup %s failed (%d); leaving remaining resources to process teardown\n",
            operation, result);
    _exit(EXIT_FAILURE);
  }
}

void destroy_connection(struct context *ctx) {
  if (!ctx)
    return;
  stopping.store(true);
  if (ctx->cq_poller_started)
    pthread_join(ctx->cq_poller_thread, NULL);
  replies.clear();
  for (int i = 0; i < queue_slots_used; ++i) {
    auto *queue = &ctx->rdma_queues[i];
    if (queue->qp)
      rdma_destroy_qp(queue->cm_id);
  }
  if (ctx->send_mr)
    check_cleanup(ibv_dereg_mr(ctx->send_mr), "control SEND MR deregistration");
  if (ctx->recv_mr)
    check_cleanup(ibv_dereg_mr(ctx->recv_mr), "control RECV MR deregistration");
  free(ctx->send_msg);
  free(ctx->recv_msg);
  if (ctx->mem_pool) {
    for (int i = 0; i < ctx->mem_pool->region_num; ++i)
      if (ctx->mem_pool->mr_buffer[i])
        check_cleanup(ibv_dereg_mr(ctx->mem_pool->mr_buffer[i]), "data MR deregistration");
    for (auto &range : ctx->mem_pool->tiers)
      if (range.start)
        check_cleanup(munmap(range.start, range.size), "munmap Tier range");
    free(ctx->mem_pool);
  }
  if (ctx->control_cq)
    check_cleanup(ibv_destroy_cq(ctx->control_cq), "ibv_destroy_cq");
  if (ctx->comp_channel)
    check_cleanup(ibv_destroy_comp_channel(ctx->comp_channel), "ibv_destroy_comp_channel");
  if (ctx->rdma_dev) {
    if (ctx->rdma_dev->pd)
      check_cleanup(ibv_dealloc_pd(ctx->rdma_dev->pd), "ibv_dealloc_pd");
    free(ctx->rdma_dev);
  }
  // CM IDs own the verbs context references; keep them until all MRs, CQs
  // and the PD have been released.
  for (int i = 0; i < queue_slots_used; ++i)
    if (ctx->rdma_queues[i].cm_id)
      check_cleanup(rdma_destroy_id(ctx->rdma_queues[i].cm_id), "rdma_destroy_id");
  free(ctx->rdma_queues);
  free(ctx);
  global_rdma_ctx = NULL;
  fprintf(stderr, "Server resources released\n");
}

int main(int argc, char **argv) {
  struct rdma_event_channel *ec = NULL;
  struct rdma_cm_id *listener = NULL;
  int result = EXIT_SUCCESS;
  try {
    parse_options(argc, argv);
    struct sigaction action = {};
    action.sa_handler = signal_stop;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL))
      fail_errno("sigaction");
    validate_nodes();
    online_cores = options.cores;
    rdma_num_queues = online_cores * NUM_QP_TYPE;
    global_rdma_ctx = (struct context *)calloc(1, sizeof(struct context));
    if (!global_rdma_ctx)
      fail_errno("allocate context");
    global_rdma_ctx->rdma_queues = (struct rswap_rdma_queue *)calloc(
        rdma_num_queues, sizeof(struct rswap_rdma_queue));
    if (!global_rdma_ctx->rdma_queues)
      fail_errno("allocate queues");
    ec = rdma_create_event_channel();
    if (!ec)
      fail_errno("rdma_create_event_channel");
    if (rdma_create_id(ec, &listener, NULL, RDMA_PS_TCP))
      fail_errno("rdma_create_id listener");
    struct sockaddr_in address = {};
    address.sin_family = AF_INET;
    address.sin_port = htons(options.port);
    inet_pton(AF_INET, options.ip.c_str(), &address.sin_addr);
    if (rdma_bind_addr(listener, (struct sockaddr *)&address))
      fail_errno("rdma_bind_addr");
    init_memory_pool(global_rdma_ctx);
    if (should_stop())
      die("interrupted before listening");
    if (rdma_listen(listener, 10))
      fail_errno("rdma_listen");
    fprintf(stderr, "listening on %s:%u client_cores=%u protocol=%u message_bytes=%zu; waiting for matching Client to register MRs\n",
            options.ip.c_str(), options.port, options.cores,
            RSWAP_PROTOCOL_VERSION, sizeof(struct message));
    while (!should_stop()) {
      struct pollfd descriptor = {ec->fd, POLLIN, 0};
      int ret = poll(&descriptor, 1, 200);
      if (ret < 0) {
        if (errno == EINTR)
          continue;
        fail_errno("poll RDMA CM");
      }
      if (!ret)
        continue;
      if (!(descriptor.revents & POLLIN))
        die("RDMA CM channel closed");
      struct rdma_cm_event *event;
      if (rdma_get_cm_event(ec, &event))
        fail_errno("rdma_get_cm_event");
      struct rdma_cm_event copy = *event;
      rdma_ack_cm_event(event);
      if (copy.status)
        die("RDMA CM event reported an error");
      on_cm_event(&copy);
    }
  } catch (const std::exception &error) {
    fprintf(stderr, "ERROR: %s\n", error.what());
    result = EXIT_FAILURE;
  }
  // Stop accepting, join the poller, destroy QPs, then release MRs and memory.
  if (listener)
    check_cleanup(rdma_destroy_id(listener), "destroy listener");
  destroy_connection(global_rdma_ctx);
  if (ec)
    rdma_destroy_event_channel(ec);
  return cq_failed.load() ? EXIT_FAILURE : result;
}
