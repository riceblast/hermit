"""Compile the production allocator with userspace locks/allocation/trace stubs."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUBS = r"""
typedef uint32_t u32;
#define U32_MAX UINT32_MAX
#define BUILD_BUG_ON(x) _Static_assert(!(x), #x)
#define GFP_KERNEL 0
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
#define DEFINE_SPINLOCK(name) pthread_mutex_t name = PTHREAD_MUTEX_INITIALIZER
static int mapping_alloc_fail_after = -1;
static unsigned int mapping_alloc_calls;
static void *mapping_calloc(size_t n, size_t size, int flags) {
    mapping_alloc_calls++;
    if (mapping_alloc_fail_after == 0) return NULL;
    if (mapping_alloc_fail_after > 0) mapping_alloc_fail_after--;
    return calloc(n, size);
}
#define kvcalloc mapping_calloc
#define kvfree free
#define pr_info(...) ((void)0)
static void mock_mapping_trace(u64, unsigned int, u64, unsigned int,
                               unsigned int, u64, unsigned int, unsigned int, unsigned int);
#define trace_mapping mock_mapping_trace
"""


def production():
    source = (ROOT / "client/rswap_mapping.c").read_text()
    return "\n".join(line for line in source.splitlines()
                     if not line.startswith("#include")) + "\n"
