"""Real upload transactions, phase selection, RCU publication and owner teardown."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import placement_harness

CASES = r'''
static struct rswap_pool_layout layout = {.nr_servers=2, .total_pages=32,
    .server_pages={16,16}, .tier_pages={{8,8},{8,8}}};
static struct file owner;
static loff_t position;
static ssize_t command(struct file *f, unsigned op, u64 gen, u64 count, u64 value, int mean) {
    struct rswap_upload_command c = {.version=RSWAP_PLACEMENT_VERSION, .op=op, .generation=gen,
        .count=count, .value=value, .mean_ppm=mean};
    if (op == RSWAP_UPLOAD_BEGIN) {
        struct { struct rswap_upload_command command; struct rswap_threshold_metadata metadata; } packet = {
            .command=c, .metadata={.threshold_ppm=mean,.generation=mean>=0 ? gen : 0}};
        return rswap_upload_write(f,(void *)&packet,sizeof(packet),&position);
    }
    return rswap_upload_write(f, (void *)&c, sizeof(c), &position);
}
static ssize_t data(struct file *f, u64 gen, struct rswap_heat_record *records, size_t n) {
    char buf[sizeof(struct rswap_upload_command) + 4 * sizeof(*records)];
    assert(n <= 4);
    struct rswap_upload_command c = {.version=RSWAP_PLACEMENT_VERSION, .op=RSWAP_UPLOAD_DATA,
        .generation=gen, .count=n};
    memcpy(buf, &c, sizeof(c)); memcpy(buf+sizeof(c), records, n*sizeof(*records));
    return rswap_upload_write(f, buf, sizeof(c)+n*sizeof(*records), &position);
}
static void publish(u64 gen, int heat, int mean) {
    struct rswap_heat_record r = {.pfn=65, .heat_ppm=heat};
    assert(command(&owner, 2, gen, 1, 1, mean) > 0);
    assert(data(&owner, gen, &r, 1) > 0);
    assert(command(&owner, 4, gen, 0, 0, 0) > 0);
}
static struct rswap_decision choose(u64 pfn) {
    struct rswap_decision d;
    assert(!rswap_placement_choose(1, pfn, &d)); return d;
}
static void open_owner(void) {
    rswap_placement_init(&layout); rswap_placement_area(true);
    assert(!rswap_upload_open(NULL, &owner));
}
static void finish(void) {
    rswap_upload_release(NULL, &owner); rswap_placement_exit();
    assert(!live_allocations);
}
static void phases(void) {
    struct rswap_decision d;
    open_owner();
    assert(choose(65).reason == RSWAP_REASON_INACTIVE);
    assert(command(&owner, 1, 0, 100, 0, 0) > 0);
    assert(command(&owner, 1, 0, 100, 0, 0) == -EBUSY);
    assert(choose(65).state == RSWAP_WARMUP);
    assert(rswap_control_write(NULL, "adaptive\n", 9, &position) == -EAGAIN);
    atomic_store(&mock_now, 1100);
    d = choose(65); assert(d.reason == RSWAP_REASON_NO_SNAPSHOT && d.heat == -1);
    assert(d.tier == 0 && d.server == 0 && d.rr == 0);
    publish(1, 900000, 500000);
    d = choose(65); assert(d.reason == RSWAP_REASON_HOT && d.tier == 0 && d.server == 1 && d.rr == 1);
    d = choose(65); assert(d.server == 0 && d.rr == 2);
    d = choose(66); assert(d.reason == RSWAP_REASON_UNKNOWN && d.heat == -1);
    assert(d.tier == 0 && d.server == 1 && d.rr == 3);
    publish(2, 500000, 500000);
    d = choose(65); assert(d.reason == RSWAP_REASON_HOT && d.tier == 0 && d.rr == 4);
    assert(command(&owner, 2, 3, 0, 0, -1) > 0);
    assert(command(&owner, 4, 3, 0, 0, 0) > 0);
    d = choose(65); assert(d.reason == RSWAP_REASON_NO_MEAN && d.heat == -1);
    assert(d.tier == 0 && d.server == 1 && d.rr == 5);
    assert(command(&owner, 2, 4, 0, 1, 0) > 0); /* known swapped history, no resident PFN */
    assert(command(&owner, 4, 4, 0, 0, 0) > 0);
    d = choose(65); assert(d.reason == RSWAP_REASON_UNKNOWN && d.heat == -1);
    assert(d.tier == 0 && d.server == 0 && d.rr == 6);
    assert(rswap_control_write(NULL, "stop\n", 5, &position) > 0);
    assert(choose(65).state == RSWAP_INACTIVE);
    assert(command(&owner, 4, 4, 0, 0, 0) == -ESTALE);
    assert(command(&owner, 1, 0, 0, 1, 0) == -ESTALE);
    rswap_upload_release(NULL, &owner); assert(!rswap_upload_open(NULL, &owner));
    assert(command(&owner, 1, 0, 0, 1, 0) > 0);
    publish(1, 1000000, 0);
    assert(choose(65).state == RSWAP_WARMUP); /* manual doesn't auto switch */
    assert(rswap_control_write(NULL, "adaptive", 8, &position) > 0);
    assert(choose(65).reason == RSWAP_REASON_HOT);
    rswap_placement_area(false);
    assert(choose(65).state == RSWAP_INACTIVE);
    assert(command(&owner, 1, 0, 0, 0, 0) == -ENODEV);
    rswap_placement_area(true);
    assert(choose(65).state == RSWAP_INACTIVE);
    finish();
    /* One Server never consumes a nonexistent endpoint. */
    layout.nr_servers=1; layout.total_pages=16; layout.server_pages[1]=0;
    open_owner(); assert(command(&owner, 1, 0, 0, 0, 0) > 0); publish(1, 0, 0);
    for (int i=0; i<10; i++) assert(choose(65).server == 0);
    finish();
}
static void transactions(void) {
    open_owner(); assert(command(&owner, 1, 0, 0, 0, 0) > 0); publish(1, 900000, 500000);
    struct rswap_heat_record bad[] = {{.pfn=66, .heat_ppm=0}, {.pfn=65, .heat_ppm=0}};
    assert(command(&owner, 2, 2, 2, 2, 500000) > 0);
    assert(choose(65).generation == 1);
    assert(data(&owner, 2, bad, 2) == -EINVAL); /* unsorted; staging discarded */
    assert(command(&owner, 4, 2, 0, 0, 0) == -EINVAL);
    assert(choose(65).generation == 1);
    assert(command(&owner, 2, 2, 1, 1, 0) > 0);
    assert(command(&owner, 4, 2, 0, 0, 0) == -EINVAL); /* incomplete */
    assert(command(&owner, 2, 1, 0, 0, -1) == -ESTALE);
    assert(command(&owner, 2, 2, UINT64_MAX, UINT64_MAX, 0) == -EINVAL);
    assert(command(&owner, 2, 2, 1, 0, -1) == -EINVAL);
    allocation_fail=0;
    assert(command(&owner, 2, 2, 1, 1, 0) == -ENOMEM); allocation_fail=-1;
    assert(choose(65).generation == 1);
    assert(command(&owner, 2, 2, 1, 1, 0) > 0);
    bad[0].heat_ppm=1000001;
    assert(data(&owner, 2, bad, 1) == -EINVAL);
    assert(command(&owner, 2, 2, 1, 1, 0) > 0);
    copy_fault=true;
    assert(command(&owner, 4, 2, 0, 0, 0) == -EFAULT); copy_fault=false;
    assert(!((struct rswap_uploader *)owner.private_data)->staging);
    publish(2, 0, 500000);
    assert(choose(65).generation == 2 && choose(65).reason == RSWAP_REASON_ZERO);
    struct rswap_upload_ack ack; loff_t pos=0;
    assert(rswap_upload_read(&owner, (void *)&ack, sizeof(ack), &pos) == sizeof(ack));
    assert(ack.generation == 2 && ack.run_id && ack.publish_ns);
    finish();
}
static void unknown_defaults(void) {
    for (unsigned int servers=1; servers<=2; servers++) {
        layout.nr_servers=servers; layout.total_pages=16*servers;
        open_owner();
        assert(command(&owner,1,0,0,0,0)>0);
        for (u64 offset=0; offset<layout.total_pages; offset++) {
            struct rswap_decision d;
            assert(!rswap_placement_choose(offset,99,&d));
            assert(d.reason==RSWAP_REASON_NO_SNAPSHOT && d.tier==0 && d.heat==-1);
            assert(d.rr==offset && d.server==offset%servers);
        }
        u64 next=layout.total_pages;
        publish(1,0,0);
        for (u64 offset=0; offset<layout.total_pages; offset++) {
            struct rswap_decision d;
            unsigned int s,t; u64 slot;
            assert(!rswap_map_page(&layout,offset,&s,&t,&slot));
            assert(!rswap_placement_choose(offset,65,&d));
            assert(d.reason==RSWAP_REASON_ZERO && d.server==s && d.tier==t && d.rr==0);
            assert(!rswap_placement_choose(offset,99,&d));
            assert(d.reason==RSWAP_REASON_UNKNOWN && d.tier==0 && d.rr==next);
            assert(d.server==next%servers); next++;
        }
        publish(2,1000000,500000);
        struct rswap_decision d=choose(65);
        assert(d.reason==RSWAP_REASON_HOT && d.rr==next && d.server==next%servers);
        assert(d.tier==0); next++;
        publish(3,100000,500000);
        d=choose(65); assert(d.reason==RSWAP_REASON_COLD && d.tier==1 && d.rr==0);
        assert(command(&owner,2,4,0,0,-1)>0 && command(&owner,4,4,0,0,0)>0);
        d=choose(65); assert(d.reason==RSWAP_REASON_NO_MEAN && d.tier==0 && d.rr==next);
        assert(d.server==next%servers);
        assert(rswap_control_write(NULL,"stop",4,&position)>0);
        assert(command(&owner,1,0,0,0,0)==-ESTALE);
        finish();
        /* A new run starts the shared DRAM RR at zero. */
        open_owner(); assert(command(&owner,1,0,0,0,0)>0);
        d=choose(99); assert(d.rr==0 && d.server==0 && d.tier==0);
        finish();
    }
}
static void ownership(void) {
    struct file other = {0};
    open_owner(); assert(!rswap_upload_open(NULL, &other));
    assert(command(&owner, 1, 0, 0, 0, 0) > 0); publish(1, 1, 0);
    assert(command(&other, 1, 0, 0, 0, 0) == -EBUSY);
    assert(command(&other, 2, 2, 0, 0, -1) == -ESTALE);
    rswap_control_write(NULL, "stop", 4, &position);
    assert(command(&other, 1, 0, 0, 0, 0) > 0);
    rswap_upload_release(NULL, &owner); /* stale owner cannot stop new run */
    assert(choose(65).state == RSWAP_ADAPTIVE);
    rswap_upload_release(NULL, &other);
    assert(choose(65).state == RSWAP_INACTIVE && !live_allocations);
    privileged=false;
    assert(rswap_upload_open(NULL, &owner) == -EPERM);
    privileged=true; allocation_fail=0;
    assert(rswap_upload_open(NULL, &owner) == -ENOMEM); allocation_fail=-1;
    rswap_placement_exit();
}
static atomic_int done;
static void *reader(void *unused) {
    while (!atomic_load(&done)) {
        struct rswap_decision d=choose(65);
        if (d.generation) {
            assert(d.publish_ns <= d.decision_ns);
            assert(d.heat == (d.generation & 1 ? 700000 : 800000));
            assert(d.mean == (d.generation & 1 ? 300000 : 900000));
            assert(d.tier == (d.generation & 1 ? 0 : 1));
            assert(d.server == d.rr % 2);
        }
    }
    return NULL;
}
static void concurrent(void) {
    pthread_t threads[4];
    open_owner(); assert(command(&owner, 1, 0, 0, 0, 0) > 0);
    for (int i=0; i<4; i++) assert(!pthread_create(&threads[i], NULL, reader, NULL));
    for (unsigned i=1; i<=200; i++) publish(i, i&1 ? 700000 : 800000, i&1 ? 300000 : 900000);
    assert(rswap_control_write(NULL,"stop",4,&position)>0);
    rswap_upload_release(NULL,&owner); assert(!rswap_upload_open(NULL,&owner));
    assert(command(&owner,1,0,0,0,0)>0); publish(1,700000,300000);
    atomic_store(&done, 1);
    for (int i=0; i<4; i++) pthread_join(threads[i], NULL);
    finish();
}
int main(int argc, char **argv) {
    _Static_assert(sizeof(struct rswap_upload_command)==40, "ABI header");
    _Static_assert(sizeof(struct rswap_heat_record)==16, "ABI record");
    _Static_assert(sizeof(struct rswap_upload_ack)==32, "ABI ack");
    assert(argc == 2);
    switch (atoi(argv[1])) {
    case 0: phases(); break; case 1: transactions(); break;
    case 2: ownership(); break; case 3: concurrent(); break;
    case 4: unknown_defaults(); break;
    default: abort();
    }
    puts("PASS: production placement/upload checks"); return 0;
}
'''


class PlacementTests(unittest.TestCase):
    def test_actual_placement_control_and_publication(self):
        with tempfile.TemporaryDirectory(prefix="hermit-p10-placement-") as tmp:
            source, binary = Path(tmp) / "placement.c", Path(tmp) / "placement"
            source.write_text(placement_harness.STUBS + placement_harness.production() + CASES)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-pthread", "-Wall", "-Wextra",
                            "-Werror", "-Wno-unused-parameter", "-I", str(placement_harness.ROOT / "client"),
                            str(source), "-o", str(binary)], check=True)
            for case in range(5):
                with self.subTest(case=case):
                    subprocess.run([str(binary), str(case)], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
