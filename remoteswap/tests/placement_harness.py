"""Compile the production placement control/RCU code with userspace fixtures."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

STUBS = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <pthread.h>
#include <stdatomic.h>
typedef uint64_t u64;
typedef uint16_t u16;
typedef atomic_llong atomic64_t;
#define atomic64_inc_return(p) (atomic_fetch_add(p, 1) + 1)
#define atomic64_set(p, v) atomic_store(p, v)
#define atomic64_inc(p) ((void)atomic_fetch_add(p, 1))
#define atomic64_read(p) atomic_load(p)
#define PAGE_SIZE 4096UL
#define READ_ONCE(x) __atomic_load_n(&(x), __ATOMIC_ACQUIRE)
#define WRITE_ONCE(x, v) __atomic_store_n(&(x), v, __ATOMIC_RELEASE)
#define __rcu
#define __user
#define GFP_KERNEL 0
#define NSEC_PER_SEC 1000000000ULL
#define CAP_SYS_ADMIN 0
static bool privileged = true;
static int capable(int cap) { return privileged; }
#define DEFINE_MUTEX(n) pthread_mutex_t n = PTHREAD_MUTEX_INITIALIZER
#define mutex_lock(p) pthread_mutex_lock(p)
#define mutex_unlock(p) pthread_mutex_unlock(p)
static pthread_rwlock_t rcu_lock = PTHREAD_RWLOCK_INITIALIZER;
#define rcu_read_lock() pthread_rwlock_rdlock(&rcu_lock)
#define rcu_read_unlock() pthread_rwlock_unlock(&rcu_lock)
#define rcu_dereference(p) __atomic_load_n(&(p), __ATOMIC_ACQUIRE)
#define rcu_dereference_protected(p, c) rcu_dereference(p)
#define rcu_access_pointer(p) rcu_dereference(p)
#define rcu_assign_pointer(p, v) __atomic_store_n(&(p), v, __ATOMIC_RELEASE)
static void synchronize_rcu(void) {
    pthread_rwlock_wrlock(&rcu_lock); pthread_rwlock_unlock(&rcu_lock);
}
static atomic_ullong mock_now = 1000;
static u64 ktime_get_ns(void) { return atomic_load(&mock_now); }
static int allocation_fail = -1, live_allocations;
static void *zalloc(size_t size, int flags) {
    if (allocation_fail == 0) return NULL;
    if (allocation_fail > 0) allocation_fail--;
    void *p = calloc(1, size);
    if (p) live_allocations++;
    return p;
}
static void dealloc(void *p) { if (p) { live_allocations--; free(p); } }
#define kzalloc zalloc
#define kvzalloc zalloc
#define kfree dealloc
#define kvfree dealloc
static unsigned long totalram_pages(void) { return 16384; }
static bool copy_fault;
static int copy_from_user(void *dst, const void *src, size_t len) {
    if (copy_fault) return 1;
    memcpy(dst, src, len); return 0;
}
struct module { int state; };
static struct module module;
#define THIS_MODULE (&module)
#define MODULE_STATE_LIVE 0
struct inode { int unused; };
struct file { void *private_data; };
struct dentry { int unused; };
struct seq_file { int unused; };
struct file_operations {
    struct module *owner;
    int (*open)(struct inode *, struct file *);
    int (*release)(struct inode *, struct file *);
    ssize_t (*read)(struct file *, char *, size_t, loff_t *);
    ssize_t (*write)(struct file *, const char *, size_t, loff_t *);
    loff_t (*llseek)(struct file *, loff_t, int);
};
static ssize_t simple_read_from_buffer(char *buf, size_t len, loff_t *pos,
                                      const void *data, size_t size) {
    if (*pos < 0) return -EINVAL;
    if ((size_t)*pos >= size) return 0;
    if (len > size - *pos) len = size - *pos;
    memcpy(buf, (const char *)data + *pos, len); *pos += len; return len;
}
static loff_t default_llseek(struct file *f, loff_t off, int whence) { return off; }
#define no_llseek default_llseek
static int single_open(struct file *f, int (*show)(struct seq_file *, void *), void *v) { return 0; }
static int single_release(struct inode *i, struct file *f) { return 0; }
static ssize_t seq_read(struct file *f, char *p, size_t n, loff_t *pos) { return 0; }
#define seq_lseek default_llseek
static void seq_printf(struct seq_file *m, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt); vprintf(fmt, ap); va_end(ap);
}
static void seq_puts(struct seq_file *m, const char *text) { fputs(text, stdout); }
static bool sysfs_streq(const char *a, const char *b) {
    size_t n = strlen(b); return !strncmp(a, b, n) && (!a[n] || (a[n] == '\n' && !a[n+1]));
}
static struct dentry *debugfs_create_file(const char *name, int mode, struct dentry *root,
                                         void *data, const struct file_operations *ops) {
    return (struct dentry *)1;
}
#define IS_ERR_OR_NULL(p) (!(p))
#define PTR_ERR(p) (-ENOMEM)
'''


def production():
    source = (ROOT / "client/rswap_placement.c").read_text()
    return ('#include "rswap_placement.h"\n#include "rswap_vpn_heat.h"\n'
            '/* Legacy PFN fixture: VPN policy is disabled; stage-3 tests use both real modules. */\n'
            'void rswap_vpn_placement_stop(void) {}\n'
            'bool rswap_vpn_placement_show(struct seq_file *m, bool raw) { return false; }\n') + "\n".join(
        line for line in source.splitlines() if not line.startswith("#include"))


def function(source, name):
    start = re.search(r"^(?:static )?[\w *]+\b" + name + r"\(", source, re.MULTILINE).start()
    brace = source.index("{", start)
    end, depth = brace + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end] + "\n"
