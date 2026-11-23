"""Production RDMA paths pin old Slots across invalidation and replacement."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_patch3_completion import ROOT, SUPPORT, client_code

CASES = r"""
static void init_page(struct page *p) {
    *p = (struct page){0};
    atomic_store(&p->refs, 1); atomic_store(&p->locked, 1);
}
int main(void) {
    struct page original, old_read, fresh;
    struct fs_rdma_req *req;
    void *cookie;
    struct rswap_location got;
    unsigned int old_id, new_id;
    init();
    /* A completed Store survives Load; invalidate during that Load must not
     * recycle its Slot, even when a fresh Store uses the same offset. */
    reset(&original);
    assert(!prepare(0, 1, &original, 0, &cookie));
    atomic_store(&original.valid, 1); rswap_store_commit(cookie);
    complete_one(&cqs[1][QP_STORE]);
    init_page(&old_read);
    assert(!rswap_rdma_submit(0, 1, &old_read, QP_LOAD_SYNC, NULL));
    old_id = cqs[1][QP_LOAD_SYNC].queued->mapping_id;
    rswap_invalidate_page(0, 1);
    assert(rswap_mapping_get(1, &got) == -ENOENT);
    init_page(&fresh);
    assert(!prepare(0, 1, &fresh, 0, &cookie));
    req = cookie; new_id = req->mapping_id;
    assert(new_id != old_id);
    atomic_store(&fresh.valid, 1); rswap_store_commit(cookie);
    complete_one(&cqs[1][QP_STORE]);
    assert(rswap_mapping_current(1, new_id));
    assert(mapping.slots[old_id].refs == 1); /* only the old RDMA Read */
    complete_one(&cqs[1][QP_LOAD_SYNC]);
    assert(!mapping.slots[old_id].refs && rswap_mapping_current(1, new_id));
    assert(atomic_load(&old_read.uptodate) && atomic_load(&old_read.locked));
    assert(atomic_load(&old_read.refs) == 1 && atomic_load(&fresh.refs) == 1);
    rswap_invalidate_page(0, 1);
    assert(!mapping.slots[new_id].refs);

    /* Adversarial late failed Store: its reference prevents id reuse and its
     * error cleanup must never clear a newer mapping or its Frontswap bit.
     * Normal replacement Stores are additionally serialized by page locking.
     */
    reset(&original);
    assert(!prepare(0, 1, &original, 0, &cookie));
    old_id = ((struct fs_rdma_req *)cookie)->mapping_id;
    atomic_store(&original.valid, 1); rswap_store_commit(cookie);
    rswap_invalidate_page(0, 1);
    assert(!rswap_mapping_alloc(1, &got)); new_id = got.id;
    assert(old_id != new_id);
    wc_error = 1;
    complete_one(&cqs[1][QP_STORE]);
    assert(rswap_mapping_current(1, new_id));
    assert(atomic_load(&original.valid) && atomic_load(&original.dirty));
    assert(!atomic_load(&original.locked) && atomic_load(&original.refs) == 1);
    assert(!mapping.slots[old_id].refs);
    rswap_mapping_put(new_id); rswap_mapping_invalidate(1);

    /* A remote-backed Load missing its map fails; never infer a static address. */
    init_page(&old_read); rdma_sessions[1].error = 0;
    assert(rswap_rdma_submit(0, 1, &old_read, QP_LOAD_SYNC, NULL) == -ENOENT);
    assert(atomic_load(&old_read.locked) && atomic_load(&old_read.refs) == 1);
    /* Pin an explicitly different Server/Tier through the mechanism API.
     * A real Load must follow it, not recompute offset's static preference.
     */
    assert(!rswap_mapping_alloc_preferred(1, 0, RSWAP_TIER_PMEM, &got));
    rswap_mapping_put(got.id);
    init_page(&fresh); wc_error = 0; trace_enabled = false;
    assert(!rswap_rdma_submit(0, 1, &fresh, QP_LOAD_SYNC, NULL));
    assert(cqs[0][QP_LOAD_SYNC].queued->mapping_id == got.id);
    assert(cqs[0][QP_LOAD_SYNC].queued->tier == RSWAP_TIER_PMEM);
    complete_one(&cqs[0][QP_LOAD_SYNC]);
    assert(atomic_load(&fresh.uptodate) && rswap_mapping_current(1, got.id));
    rswap_invalidate_area(0);
    struct rswap_mapping_stats stats;
    rswap_mapping_snapshot(&stats);
    for (unsigned int s = 0; s < 2; s++) for (unsigned int t = 0; t < 2; t++)
        assert(stats.pools[s][t].capacity == stats.pools[s][t].free);
    rswap_frontswap_init(0);
    assert(!rswap_mapping_alloc(1, &got));
    rswap_mapping_put(got.id);
    rswap_invalidate_area(0);
    rswap_mapping_destroy();
    puts("PASS: actual RDMA late Read/Store, generation cleanup, missing-map error and swapoff reuse");
    return 0;
}
"""


class DynamicIOTests(unittest.TestCase):
    def test_actual_io_lifecycle(self):
        code = client_code(SUPPORT.replace("int main(void)", "int legacy_main(void)")) + CASES
        with tempfile.TemporaryDirectory(prefix="hermit-p9-io-") as tmp:
            source, binary = Path(tmp) / "io.c", Path(tmp) / "io"
            source.write_text(code)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-pthread", "-Wall", "-Wextra",
                            "-Werror", "-Wno-unused-parameter", "-I", str(ROOT / "client"),
                            str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
