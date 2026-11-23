"""The actual policy feeds the actual Store allocator; Load never reruns policy."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_patch3_completion import ROOT, SUPPORT, client_code, definition
from placement_harness import function

CASES = r'''
int main(void) {
    struct page p, old_read={0}, fresh={0}, read={0};
    void *cookie;
    struct rswap_location loc;
    init(); reset(&p); trace_enabled=false; /* legacy observer assumes interleave */
    struct rswap_heat_snapshot *snap=calloc(1,sizeof(*snap)+sizeof(snap->records[0]));
    assert(snap);
    snap->count=snap->known=1; snap->generation=1; snap->publish_ns=1; snap->mean=500000;
    snap->records[0]=(struct rswap_heat_record){.pfn=page_to_pfn(&p),.heat_ppm=1000000};
    struct rswap_policy_run run={.id=1,.start_ns=1,.snapshot=snap}; policy_run=&run;
    assert(!prepare(0,1,&p,0,&cookie));
    struct fs_rdma_req *req=cookie;
    assert(req->rdma_queue==&queues[0][QP_STORE] && req->tier==RSWAP_TIER_DRAM);
    unsigned old=req->mapping_id;
    atomic_store(&p.valid,1); rswap_store_commit(cookie); complete_one(&cqs[0][QP_STORE]);
    atomic_store(&old_read.refs,1); atomic_store(&old_read.locked,1);
    assert(!rswap_rdma_submit(0,1,&old_read,QP_LOAD_SYNC,NULL));
    assert(cqs[0][QP_LOAD_SYNC].queued->mapping_id==old);
    rswap_invalidate_page(0,1);
    atomic_store(&fresh.refs,1); atomic_store(&fresh.locked,1);
    snap->records[0]=(struct rswap_heat_record){.pfn=page_to_pfn(&fresh),.heat_ppm=0};
    snap->generation=2;
    assert(!prepare(0,1,&fresh,0,&cookie)); req=cookie;
    assert(req->tier==RSWAP_TIER_PMEM && req->rdma_queue==&queues[0][QP_STORE]);
    unsigned current=req->mapping_id; assert(current!=old);
    atomic_store(&fresh.valid,1); rswap_store_commit(cookie); complete_one(&cqs[0][QP_STORE]);
    complete_one(&cqs[0][QP_LOAD_SYNC]);
    assert(rswap_mapping_current(1,current) && !mapping.slots[old].refs);
    policy_run=NULL; /* Stop affects future Stores only. */
    atomic_store(&read.refs,1); atomic_store(&read.locked,1);
    assert(!rswap_rdma_submit(0,1,&read,QP_LOAD_SYNC,NULL));
    assert(cqs[0][QP_LOAD_SYNC].queued->tier==RSWAP_TIER_PMEM);
    assert(cqs[0][QP_LOAD_SYNC].queued->mapping_id==current);
    complete_one(&cqs[0][QP_LOAD_SYNC]);
    assert(!rswap_mapping_get(1,&loc) && loc.id==current); rswap_mapping_put(loc.id);
    assert(atomic_load(&old_read.uptodate) && atomic_load(&read.uptodate));
    rswap_invalidate_area(0); free(snap); rswap_mapping_destroy();
    puts("PASS: real policy/Store/Load, replacement, late old Read, and Stop with live mapping");
    return 0;
}
'''


class PolicyIOTests(unittest.TestCase):
    def test_actual_policy_and_io(self):
        source = (ROOT / "client/rswap_placement.c").read_text()
        core = (definition(source, "rswap_heat_snapshot", struct=True)
                + definition(source, "rswap_policy_run", struct=True)
                + "\nstatic struct rswap_policy_run *policy_run;\n"
                + "static u64 ktime_get_ns(void);\n"
                + "#define policy_layout pool_layout\n"
                + "#define rcu_read_lock() ((void)0)\n#define rcu_read_unlock() ((void)0)\n"
                + "#define rcu_dereference(x) (x)\n"
                + "#define atomic64_inc_return(p) (atomic_fetch_add(p, 1)+1)\n"
                + function(source, "rswap_policy_phase") + function(source, "rswap_placement_choose"))
        code = client_code(SUPPORT.replace("int main(void)", "int legacy_main(void)"))
        code = code.replace("static int policy_override = -1;", "")
        # pool_layout is declared immediately before the legacy policy stub.
        core = core.replace(" __rcu", "")
        code = code.replace(definition(code, "rswap_placement_choose"), core)
        with tempfile.TemporaryDirectory(prefix="hermit-p10-io-") as tmp:
            path, binary = Path(tmp)/"io.c", Path(tmp)/"io"
            path.write_text(code + CASES)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-pthread", "-Wall", "-Wextra",
                            "-Werror", "-Wno-unused-parameter", "-I", str(ROOT / "client"),
                            str(path), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
