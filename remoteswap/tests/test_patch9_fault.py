"""Compile actual swap-fault error/PTE validation and cleanup blocks."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PREFIX = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#define unlikely(x) (x)
#define VM_FAULT_SIGBUS 2
#define VM_FAULT_MAJOR 4
#define VM_FAULT_OOM 8
#define PageUptodate(p) ((p)->uptodate)
#define adc_pf_breakdown_end(...) ((void)0)
#define adc_profile_counter_inc(...) ((void)0)
#define pr_err(...) assert(0)
struct page { bool uptodate, locked, pending; unsigned int refs; };
struct vm_area_struct { int vm_mm; };
struct vm_fault { int *pte; int pmd, address, ptl, orig_pte; };
static int current_pte, checks, unlocks, copies, polls, devices, normal;
static bool pte_locked, change_during_poll;
static int *pte_offset_map_lock(int mm, int pmd, int address, int *ptl) {
    assert(!pte_locked); pte_locked = true; checks++; return &current_pte;
}
static void pte_unmap_unlock(int *pte, int ptl) {
    assert(pte_locked && pte == &current_pte); pte_locked = false; unlocks++;
}
static bool pte_same(int a, int b) { assert(pte_locked); return a == b; }
static void hermit_poll_read(int cpu, struct page *p, bool unlock, void *stats) {
    assert(cpu >= 0 && p->pending && p->locked && !pte_locked && !unlock);
    polls++; p->pending = false;
    if (change_during_poll) current_pte = 2;
}
static struct page *hermit_ksm_might_need_to_copy(struct page *p,
        struct vm_area_struct *vma, int address, int cpu) {
    assert(p->uptodate && !p->pending && !pte_locked); copies++; return p;
}
static void unlock_page(struct page *p) {
    assert(p->locked && !p->pending && !pte_locked); p->locked = false;
}
static void put_page(struct page *p) { assert(!p->locked && p->refs == 1); p->refs--; }
static void put_swap_device(void *si) { assert(si); devices++; }
static int actual(int cpu, bool uptodate, bool changed, bool during_poll, bool cached) {
    struct page data = {.uptodate=uptodate, .locked=true, .pending=cpu>=0, .refs=1};
    struct page *page = &data, *swapcache = cached ? page : NULL;
    struct vm_area_struct area = {0}, *vma = &area;
    struct vm_fault fault = {.orig_pte=1}, *vmf = &fault;
    void *pf_breakdown = NULL, *si = &area;
    int ret = VM_FAULT_MAJOR;
    current_pte = changed ? 2 : 1;
    checks = unlocks = copies = polls = devices = normal = 0;
    pte_locked = false; change_during_poll = during_poll;
"""

MIDDLE = r"""
    normal++; /* Only successfully read, still-current data may reach accounting. */
    goto out_nomap;
"""

SUFFIX = r"""
}
int main(void) {
    /* The previously broken case is first: a stale failed read is not SIGBUS. */
    assert(actual(-1, false, true, false, false) == VM_FAULT_MAJOR);
    for (int rdma = 0; rdma < 2; rdma++)
        for (int valid = 0; valid < 2; valid++)
            for (int changed = 0; changed < 2; changed++)
                for (int cached = 0; cached < 2; cached++) {
                    int ret = actual(rdma ? 0 : -1, valid, changed, false, cached);
                    assert(ret == (valid || changed ? VM_FAULT_MAJOR : VM_FAULT_SIGBUS));
                    assert(checks == 1 && unlocks == 1 && !pte_locked && devices == 1);
                    assert(copies == valid && normal == (valid && !changed) && polls == rdma);
                }
    assert(actual(0, false, false, true, false) == VM_FAULT_MAJOR);
    assert(polls == 1 && checks == 1 && unlocks == 1 && !normal && !copies);
    puts("PASS: actual fault blocks: changed PTE retries, current PTE SIGBUS, I/O-before-check, balanced cleanup (18 cases)");
    return 0;
}
"""


def harness(source):
    start = source.index("\tif (cpu != -1) {\n\t\thermit_poll_read(cpu, page, false, pf_breakdown);",
                         source.index("vm_fault_t do_swap_page_profiling"))
    end = source.index("\n\t/*\n\t * The page isn't present yet", start)
    cleanup = source.index("\nout_nomap:", end)
    cleanup_end = source.index("// [RMGrid] profiling\nout_profiling:", cleanup)
    return PREFIX + source[start:end] + MIDDLE + source[cleanup:cleanup_end] + SUFFIX


class FaultTests(unittest.TestCase):
    def test_actual_error_revalidation_and_cleanup(self):
        source = (ROOT.parent / "linux-5.14-rc5/mm/memory.c").read_text()
        with tempfile.TemporaryDirectory(prefix="hermit-p9-fault-") as tmp:
            c, binary = Path(tmp) / "fault.c", Path(tmp) / "fault"
            c.write_text(harness(source))
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                            "-Wno-unused-parameter", "-Wno-unused-label", str(c), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
