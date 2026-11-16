#!/usr/bin/env python3
"""Compile actual Client v2 control functions; no module, RDMA or swap access."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_patch3_completion import definition

ROOT = Path(__file__).resolve().parents[1]

STUBS = r"""
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
typedef uint32_t u32;
#include "utils.h"
#define PAGE_SIZE 4096ULL
#define WRITE_ONCE(x, value) ((x) = (value))
#define atomic_read(p) (*(p))
#define atomic_inc(p) (++*(p))
#define atomic_dec(p) (--*(p))
#define check_add_overflow(a, b, out) __builtin_add_overflow(a, b, out)
#define DMA_BIDIRECTIONAL 0
enum chunk_mapping_state { EMPTY, MAPPED };
enum { MEMORY_SERVER_AVAILABLE = 1, FREE_MEM_RECV, RECEIVED_CHUNKS };
struct ib_recv_wr { int unused; };
struct ib_send_wr { int unused; };
struct ib_wc { unsigned int byte_len; };
/* ACTUAL_STRUCTS */
struct device { void *dev; };
struct rswap_rdma_queue;
struct rdma_session_context {
    struct chunk_list remote_mem_pool;
    struct device *rdma_dev;
    struct { struct message *recv_buf; u64 recv_dma_addr; struct ib_recv_wr rq_wr; } rdma_recv_req;
    struct { struct message *send_buf; u64 send_dma_addr; struct ib_send_wr sq_wr; } rdma_send_req;
    struct rswap_rdma_queue *rdma_queues;
};
struct rswap_rdma_queue {
    struct rdma_session_context *rdma_session;
    int expected_message, state, rdma_post_counter;
    void *qp;
};
static struct message posted;
static struct rdma_session_context *sending;
static int recv_error, send_error, receives, sends;
static void ib_dma_sync_single_for_cpu(void *dev, u64 addr, size_t bytes, int dir) {
    assert(bytes == sizeof(struct message));
}
static void ib_dma_sync_single_for_device(void *dev, u64 addr, size_t bytes, int dir) {
    assert(bytes == sizeof(struct message));
}
static int ib_post_recv(void *qp, struct ib_recv_wr *wr, const struct ib_recv_wr **bad) {
    receives++; return recv_error;
}
static int ib_post_send(void *qp, struct ib_send_wr *wr, const struct ib_send_wr **bad) {
    sends++; posted = *sending->rdma_send_req.send_buf; return send_error;
}
void bind_remote_memory_chunks(struct rdma_session_context *session);
"""

CASES = r"""
static struct message message(int type, unsigned int d, unsigned int p)
{
    struct message m = {0};
    m.type = type; m.protocol_version = RSWAP_PROTOCOL_VERSION;
    m.dram_chunks = d; m.pmem_chunks = p; m.mapped_chunk = d + p;
    if (type == GOT_CHUNKS) {
        for (int i = 0; i < m.mapped_chunk; i++) {
            /* Two separate ranges; PMem below DRAM, no 8-GiB alignment. */
            m.buf[i] = i < (int)d ? 0x400000010000ULL + (u64)i * REGION_SIZE_GB * ONE_GB :
                0x100000010000ULL + (u64)(i - d) * REGION_SIZE_GB * ONE_GB;
            m.mapped_size[i] = (u64)REGION_SIZE_GB * ONE_GB;
            m.rkey[i] = i; /* RKey zero is legal. */
        }
    }
    return m;
}

static int deliver(struct rswap_rdma_queue *q, struct message *m, int type)
{
    struct ib_wc wc = {.byte_len = sizeof(*m)};
    m->type = type; q->expected_message = type;
    return handle_recv_wr(q, &wc);
}

int main(void)
{
    struct message in, out, expected;
    struct remote_chunk chunks[MAX_REGION_NUM] = {{0}};
    struct device dev = {0};
    struct rdma_session_context session = {.rdma_dev = &dev};
    struct rswap_rdma_queue queues[2] = {{.rdma_session = &session}};
    struct ib_wc wc = {.byte_len = sizeof(in)};
    session.rdma_recv_req.recv_buf = &in;
    session.rdma_send_req.send_buf = &out;
    session.rdma_queues = queues;
    sending = &session;
    queues[0].expected_message = AVAILABLE_TO_QUERY;
    /* Reject invalid topology before saving anything or advancing state. */
    in = message(AVAILABLE_TO_QUERY, 2, 4);
    wc.byte_len--;
    assert(handle_recv_wr(queues, &wc) == -EPROTO);
    wc.byte_len++;
    in.protocol_version = 1;
    assert(handle_recv_wr(queues, &wc) == -EPROTO);
    in = message(AVAILABLE_TO_QUERY, 2, 4); in.mapped_chunk = -1;
    assert(handle_recv_wr(queues, &wc) == -EPROTO);
    in.mapped_chunk = MAX_REGION_NUM + 1;
    assert(handle_recv_wr(queues, &wc) == -EPROTO);
    in = message(AVAILABLE_TO_QUERY, 2, 4); in.dram_chunks = 0;
    assert(handle_recv_wr(queues, &wc) == -EPROTO);
    in = message(AVAILABLE_TO_QUERY, 2, 4); in.pmem_chunks = 0;
    assert(handle_recv_wr(queues, &wc) == -EPROTO);
    in = message(AVAILABLE_TO_QUERY, 2, 4); in.dram_chunks = UINT32_MAX; in.pmem_chunks = 7;
    assert(handle_recv_wr(queues, &wc) == -EPROTO); /* 32-bit sum would wrap to 6. */
    assert(!session.remote_mem_pool.chunk_num && !queues[0].state);

    in = message(AVAILABLE_TO_QUERY, 2, 4);
    assert(!deliver(queues, &in, AVAILABLE_TO_QUERY));
    assert(session.remote_mem_pool.chunk_num == 6 && session.remote_mem_pool.dram_chunks == 2);
    queues[1].rdma_session = &session;
    assert(!deliver(&queues[1], &in, AVAILABLE_TO_QUERY));
    in.dram_chunks = 3; in.pmem_chunks = 3;
    assert(deliver(&queues[1], &in, AVAILABLE_TO_QUERY) == -EPROTO);
    assert(deliver(queues, &in, FREE_SIZE) == -EPROTO);
    assert(queues[0].state == MEMORY_SERVER_AVAILABLE);

    /* Production sender zeroes stale arrays and fills protocol/count fields. */
    memset(&out, 0xab, sizeof(out));
    assert(!send_message_to_remote(&session, 0, QUERY, 0));
    memset(&expected, 0, sizeof(expected));
    expected.type = QUERY; expected.protocol_version = RSWAP_PROTOCOL_VERSION;
    assert(!memcmp(&posted, &expected, sizeof(posted)));
    assert(queues[0].rdma_post_counter == 2);
    assert(send_message_to_remote(&session, 0, QUERY, 0) == -EBUSY);
    queues[0].rdma_post_counter = 0; /* Model termination of control RECV + SEND. */
    in = message(FREE_SIZE, 2, 4);
    assert(!deliver(queues, &in, FREE_SIZE));
    assert(session.remote_mem_pool.remote_mem_size == 48);
    assert(!send_message_to_remote(&session, 0, REQUEST_CHUNKS, 6));
    expected = message(REQUEST_CHUNKS, 2, 4);
    assert(!memcmp(&posted, &expected, sizeof(posted)));
    queues[0].rdma_post_counter = 0;
    assert(send_message_to_remote(&session, 1, QUERY, 0) == -EINVAL);
    assert(send_message_to_remote(&session, 0, DONE, 0) == -EINVAL);
    assert(send_message_to_remote(&session, 0, REQUEST_CHUNKS, 5) == -EINVAL);

    session.remote_mem_pool.chunks = chunks;
    in = message(GOT_CHUNKS, 2, 4); in.buf[5] = in.buf[0];
    assert(deliver(queues, &in, GOT_CHUNKS) == -EPROTO);
    for (int i = 0; i < 6; i++) assert(chunks[i].chunk_state == EMPTY);
    in = message(GOT_CHUNKS, 2, 4); in.buf[5]++;
    assert(deliver(queues, &in, GOT_CHUNKS) == -EPROTO);
    in = message(GOT_CHUNKS, 2, 4); in.mapped_size[5]--;
    assert(deliver(queues, &in, GOT_CHUNKS) == -EPROTO);
    in = message(GOT_CHUNKS, 2, 4); in.buf[5] = UINT64_MAX & ~(PAGE_SIZE - 1);
    assert(deliver(queues, &in, GOT_CHUNKS) == -EPROTO);
    in = message(GOT_CHUNKS, 3, 3);
    assert(deliver(queues, &in, GOT_CHUNKS) == -EPROTO);
    in = message(GOT_CHUNKS, 2, 4);
    assert(!deliver(queues, &in, GOT_CHUNKS));
    for (int i = 0; i < 6; i++) {
        assert(chunks[i].chunk_state == MAPPED);
        assert(chunks[i].remote_addr == in.buf[i] && chunks[i].remote_rkey == in.rkey[i]);
    }
    assert(queues[0].state == RECEIVED_CHUNKS);

    /* Separate Session may advertise identical addresses/keys. Upper capacity boundary. */
    struct rdma_session_context other = {.rdma_dev = &dev};
    struct rswap_rdma_queue q = {.rdma_session = &other};
    other.rdma_recv_req.recv_buf = &in;
    in = message(AVAILABLE_TO_QUERY, 2, 4);
    assert(!deliver(&q, &in, AVAILABLE_TO_QUERY));
    in = message(FREE_SIZE, 2, 4);
    assert(!deliver(&q, &in, FREE_SIZE));
    other.remote_mem_pool.chunks = chunks;
    in = message(GOT_CHUNKS, 2, 4);
    assert(!deliver(&q, &in, GOT_CHUNKS));
    memset(&other.remote_mem_pool, 0, sizeof(other.remote_mem_pool));
    in = message(AVAILABLE_TO_QUERY, 1, 399);
    assert(!deliver(&q, &in, AVAILABLE_TO_QUERY));
    in = message(FREE_SIZE, 1, 399);
    assert(!deliver(&q, &in, FREE_SIZE));
    assert(other.remote_mem_pool.remote_mem_size == 3200);
    other.remote_mem_pool.chunks = chunks;
    in = message(GOT_CHUNKS, 1, 399);
    assert(!deliver(&q, &in, GOT_CHUNKS));
    assert(chunks[399].chunk_state == MAPPED && chunks[399].remote_addr == in.buf[399]);

    recv_error = -ENOMEM;
    assert(send_message_to_remote(&session, 0, QUERY, 0) == -ENOMEM);
    assert(!queues[0].rdma_post_counter);
    recv_error = 0; send_error = -EIO;
    assert(send_message_to_remote(&session, 0, QUERY, 0) == -EIO);
    assert(queues[0].rdma_post_counter == 1); /* Posted RECV remains for existing teardown. */
    puts("PASS: actual v2 sender/receiver, discovery, topology consistency, MR validation and limits");
    return 0;
}
"""


class ProtocolTests(unittest.TestCase):
    def test_actual_control_functions(self):
        source = (ROOT / "client/rswap_rdma.c").read_text()
        header = (ROOT / "client/rswap_rdma.h").read_text()
        structs = definition(header, "remote_chunk", struct=True) + definition(header, "chunk_list", struct=True)
        names = ("handle_recv_wr", "post_control_recv", "send_message_to_remote", "bind_remote_memory_chunks")
        code = STUBS.replace("/* ACTUAL_STRUCTS */", structs)
        code += "\n".join(definition(source, name) for name in names) + CASES
        with tempfile.TemporaryDirectory(prefix="hermit-patch6-protocol-") as tmp:
            c, binary = Path(tmp) / "protocol.c", Path(tmp) / "protocol"
            c.write_text(code)
            subprocess.run(["gcc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", "-I", str(ROOT), str(c), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
