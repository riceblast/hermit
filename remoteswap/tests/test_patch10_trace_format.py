"""Reproduce 5.14's full-seq va_list warning using its actual string verifier."""
import ast
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from placement_harness import ROOT, function
from test_patch7_trace import trace
from test_patch9_trace import event

STUBS = r'''
#include <assert.h>
#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct trace_seq { int full; char buffer[512]; };
struct trace_iterator { struct trace_seq seq; char *fmt; int fmt_size; };
static char static_fmt_buf[128];
static int warnings;
#define static_branch_unlikely(p) 0
static bool warn_if(bool condition) { warnings+=condition; return condition; }
#define WARN_ON_ONCE(c) warn_if(c)
#define WARN_ONCE(c, ...) warn_if(c)
static char *trace_iter_expand_format(struct trace_iterator *iter) { return NULL; }
static const char *safe = "alloc";
static bool trace_safe_str(struct trace_iterator *iter, const char *p) { return p==safe; }
static int copy_from_kernel_nofault(void *a, const void *b, size_t n) { return -EFAULT; }
static int strncpy_from_kernel_nofault(void *a, const void *b, size_t n) { return -EFAULT; }
static void trace_seq_vprintf(struct trace_seq *seq, const char *fmt, va_list ap) {
    if (seq->full) return; /* Exact early-return behavior of trace_seq.c. */
    vsnprintf(seq->buffer,sizeof(seq->buffer),fmt,ap);
}
static void trace_seq_printf(struct trace_seq *seq, const char *fmt, ...) {
    va_list ap; va_start(ap,fmt); trace_seq_vprintf(seq,fmt,ap); va_end(ap);
}
'''


class TraceFormatTests(unittest.TestCase):
    def test_full_sequence_does_not_enter_string_verifier(self):
        header = (ROOT / 'client/rswap_trace.h').read_text()
        formats = {}
        for name in ('mapping', 'placement', 'decision', 'store_result'):
            block = header[header.index('TRACE_EVENT(' + name + ','):]
            value = re.search(r'TP_printk\(("(?:[^"\\]|\\.)*")', block).group(1)
            formats[name] = ast.literal_eval(value)
            self.assertNotIn('%s', formats[name])
        kernel = (ROOT.parent / 'linux-5.14-rc5/kernel/trace/trace.c').read_text()
        cases = r'''
static int check(const char *fmt, ...) {
    char buffer[4096];
    struct trace_iterator iter={.seq={.full=1},.fmt=buffer,.fmt_size=sizeof(buffer)};
    int before=warnings;
    va_list ap; va_start(ap,fmt); trace_check_vprintf(&iter,fmt,ap); va_end(ap);
    return warnings-before;
}
int main(void) {
    assert(check("seq=%llu action=%s",7ULL,safe)>0);
    assert(check(MAPPING,7ULL,0U,1ULL,0U,0U,0ULL,1U,1U,0U)==0);
    assert(check(PLACEMENT,0U,1ULL,0U,0U,0,0)==0);
    puts("PASS: actual 5.14 verifier reproduces legacy warning; numeric trace formats avoid it");
    return 0;
}
'''
        import json
        defines = '\n'.join('#define ' + n.upper() + ' ' + json.dumps(v) for n,v in formats.items()) + '\n'
        with tempfile.TemporaryDirectory(prefix='hermit-p10-trace-format-') as tmp:
            path, binary = Path(tmp)/'format.c', Path(tmp)/'format'
            path.write_text(STUBS + function(kernel,'trace_check_vprintf') + defines + cases)
            subprocess.run(['gcc','-std=gnu11','-O1','-Wall','-Wextra','-Werror',
                            '-Wno-unused-parameter',str(path),'-o',str(binary)],check=True)
            subprocess.run([str(binary)],check=True,timeout=5)

    def test_numeric_mapping_and_wc_keep_legacy_compatibility(self):
        lines = [event(1,'alloc'), event(2,'put',users=0),
                 event(3,'invalidate',users=0,live=0), event(4,'free',users=0,live=0)]
        numeric = []
        actions = {'alloc':0,'get':1,'put':2,'invalidate':3,'free':4}
        for line in lines:
            for name,n in actions.items():
                line=line.replace('action='+name+' ','action='+str(n)+' ')
            numeric.append(line.replace('tier=dram','tier=0'))
        self.assertEqual(trace.check_mapping_records(lines,((2,2),),True),
                         trace.check_mapping_records(numeric,((2,2),),True))
        counts, errors = trace.check_trace(['placement: swap_type=0 offset=0 server=0 tier=0 op=0 status=0\n'],((16,32),))
        self.assertEqual((counts[(0,'dram','write')],errors),(1,0))


if __name__ == '__main__':
    unittest.main()
