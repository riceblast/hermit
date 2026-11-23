"""Connect the real userspace uploader to the real kernel command implementation."""
from pathlib import Path
import subprocess
import tempfile
import unittest

import placement_harness

BRIDGE = r'''
#include <unistd.h>
static struct file bridge_file;
static int batches;
static bool short_write;
static ssize_t bridge_write(int fd, const void *buf, size_t len) {
    if (fd != 123456) return write(fd, buf, len);
    if (short_write) return len-1;
    const struct rswap_upload_command *c=buf;
    batches += c->op == RSWAP_UPLOAD_DATA;
    loff_t pos=0;
    ssize_t ret=rswap_upload_write(&bridge_file, buf, len, &pos);
    if (ret < 0) { errno=-ret; return -1; } return ret;
}
static ssize_t bridge_pread(int fd, void *buf, size_t len, off_t offset) {
    if (fd != 123456) return pread(fd, buf, len, offset);
    loff_t pos=offset;
    ssize_t ret=rswap_upload_read(&bridge_file, buf, len, &pos);
    if (ret < 0) { errno=-ret; return -1; } return ret;
}
static int bridge_close(int fd) {
    if (fd != 123456) return close(fd);
    return rswap_upload_release(NULL, &bridge_file);
}
#define write bridge_write
#define pread bridge_pread
#define close bridge_close
#define main sampler_unused_main
#include "rswap_hotness.c"
#undef main
#undef close
#undef pread
#undef write

int main(void) {
    struct rswap_pool_layout layout={.nr_servers=2, .total_pages=32,
        .server_pages={16,16}, .tier_pages={{8,8},{8,8}}};
    rswap_placement_init(&layout); rswap_placement_area(true);
    assert(!rswap_upload_open(NULL, &bridge_file));
    struct config c={.placement=true, .upload_fd=123456, .interval=1,
        .placement_log=tmpfile()};
    FILE *summary=tmpfile(); assert(summary && c.placement_log);
    uint64_t start;
    assert(!placement_start(&c, &start) && start == 1000);
    struct hot_page rows[] = {
        {.pfn=66, .state=RESIDENT, .heat={.valid=1}},
        {.pfn=65, .state=RESIDENT, .heat={.active=1,.valid=1}},
        {.pfn=65, .state=RESIDENT, .alias=1, .heat={.active=1,.valid=1}},
        {.previous_pfn=77, .state=SWAPPED, .heat={.valid=1}},
        {.pfn=70, .state=RESIDENT},
    };
    struct page_list pages={.v=rows,.n=5};
    assert(!snapshot(&c, summary, &pages, 0, start, start, start));
    struct rswap_decision d;
    assert(!rswap_placement_choose(1,65,&d) && d.reason==RSWAP_REASON_HOT && d.mean==333333);
    assert(!rswap_placement_choose(1,66,&d) && d.reason==RSWAP_REASON_ZERO);
    assert(!rswap_placement_choose(1,77,&d) && d.reason==RSWAP_REASON_UNKNOWN);
    struct rswap_policy_run *run=rcu_dereference(policy_run);
    struct rswap_heat_snapshot *s=rcu_dereference(run->snapshot);
    assert(s->count==2 && s->known==3); /* swapped contributes only to mean */
    for (size_t i=0;i<5;i++) rows[i].heat=(struct heat_window){0};
    assert(!snapshot(&c,summary,&pages,1,start,start,start));
    assert(!rswap_placement_choose(1,65,&d) && d.reason==RSWAP_REASON_NO_MEAN && d.generation==2);

    /* More than one upload chunk, sorted by the real uploader. */
    pages.n=4097; pages.v=calloc(pages.n,sizeof(*pages.v)); assert(pages.v);
    for (size_t i=0;i<pages.n;i++) pages.v[i]=(struct hot_page){
        .state=RESIDENT,.pfn=pages.n-i,.heat={.active=1,.valid=1}};
    batches=0;
    assert(!snapshot(&c,summary,&pages,2,start,start,start) && batches==2);
    assert(!rswap_placement_choose(1,65,&d) && d.reason==RSWAP_REASON_HOT && d.mean==1000000);
    short_write=true;
    assert(placement_snapshot(&c,&pages,3,4097,1000000)<0 && errno==EIO);
    short_write=false;
    assert(!rswap_placement_choose(1,65,&d) && d.generation==3);
    free(pages.v); pages.v=rows; pages.n=5;
    loff_t pos=0;
    assert(rswap_control_write(NULL,"stop",4,&pos)>0);
    /* Explicit stop keeps observation alive and closes only the stale owner. */
    assert(!snapshot(&c,summary,&pages,3,start,start,start) && c.upload_fd==-1);
    assert(!live_allocations);
    assert(!rswap_placement_choose(1,65,&d) && d.state==RSWAP_INACTIVE);
    fclose(summary); fclose(c.placement_log); rswap_placement_exit();
    puts("PASS: real userspace/kernel snapshot bridge, batching, empty snapshot and explicit Stop");
    return 0;
}
'''


class UploadTests(unittest.TestCase):
    def test_actual_sampler_to_kernel_upload(self):
        with tempfile.TemporaryDirectory(prefix="hermit-p10-upload-") as tmp:
            source, binary = Path(tmp) / "upload.c", Path(tmp) / "upload"
            source.write_text(placement_harness.STUBS + placement_harness.production() + BRIDGE)
            subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-pthread", "-Wall", "-Wextra",
                            "-Werror", "-Wno-unused-parameter", "-I", str(placement_harness.ROOT / "client"),
                            "-I", str(placement_harness.ROOT / "tools"),
                            str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
