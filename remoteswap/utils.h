#ifndef __RSWAP_UTILS_H
#define __RSWAP_UTILS_H

#include "constants.h"

/* Version 1 was implicit. Version 2 requires matching Client and Server. */
#define RSWAP_PROTOCOL_VERSION 2U

enum rdma_queue_type { QP_STORE, QP_LOAD_SYNC, QP_LOAD_ASYNC, NUM_QP_TYPE };

/* 2-sided RDMA message type */
enum message_type {
  DONE = 1,
  GOT_CHUNKS,
  GOT_SINGLE_CHUNK,
  FREE_SIZE,
  EVICT,

  ACTIVITY,
  STOP,

  REQUEST_CHUNKS,
  REQUEST_SINGLE_CHUNK,
  QUERY,

  AVAILABLE_TO_QUERY
};

/**
 * RDMA command line attached to each RDMA message.
 *
 */
struct message {
  uint64_t buf[MAX_REGION_NUM];
  uint64_t mapped_size[MAX_REGION_NUM];
  uint32_t rkey[MAX_REGION_NUM];
  int mapped_chunk;

  enum message_type type;

  uint32_t protocol_version;
  /* Descriptors [0, dram_chunks) are DRAM; the remainder are PMem. */
  uint32_t dram_chunks;
  uint32_t pmem_chunks;
};

#endif // __RSWAP_UTILS_H
