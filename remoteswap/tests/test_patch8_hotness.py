#!/usr/bin/env python3
"""Production sampler/window checks against unprivileged pagemap/idle fixtures."""
from pathlib import Path
import csv
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <fcntl.h>
static FILE *fixture_fopen(const char *, const char *);
static int fixture_open(const char *, int, ...);
#define fopen fixture_fopen
#define open fixture_open
#define main hotness_tool_main
#include "rswap_hotness.c"
#undef main
#undef fopen
#undef open
#include <assert.h>

static int mock_proc, mock_opens, mock_empty, mock_pm, mock_deny, mock_always_empty;
static FILE *fixture_fopen(const char *path, const char *mode) {
    const char *map = "00001000-00003000 rw-p 00000000 00:00 0 [heap]\n";
    if (mock_proc && !strcmp(path, "/proc/7777/maps"))
        return fmemopen((void *)map, strlen(map), "r");
    return fopen(path, mode);
}
static int fixture_open(const char *path, int flags, ...) {
    if (mock_proc && !strcmp(path, "/proc/7777/pagemap")) {
        mock_opens++;
        if (mock_deny) { errno = EACCES; return -1; }
        return dup(mock_always_empty || mock_opens == 1 ? mock_empty : mock_pm);
    }
    return open(path, flags);
}

static void write_words(int fd, const uint64_t *words, size_t count) {
    assert(!exact_io(fd, (void *)words, count * 8, 0, true));
}
static void round_from(const char *map, int pm, int flags, int idle,
                       struct page_list *old, struct page_list *next, unsigned window) {
    FILE *maps = fmemopen((void *)map, strlen(map), "r");
    assert(maps && !collect_round(maps, pm, flags, idle, old, next, window));
    fclose(maps);
}
static void replace(struct page_list *old, struct page_list *next) {
    free(old->v); *old = *next; *next = (struct page_list){0};
}

int main(int argc, char **argv) {
    assert(argc == 2);
    /* Exact window semantics, including 64-bit windows and invalid rounds. */
    for (unsigned window = 1; window <= 64; window++) {
        struct heat_window h = {0};
        int history[256];
        for (unsigned i = 0; i < 256; i++) {
            history[i] = i % 3 == 0 ? -1 : i % 3 == 1 ? 1 : 0;
            heat_push(&h, history[i], window);
            unsigned active = 0, valid = 0;
            unsigned first = i + 1 > window ? i + 1 - window : 0;
            for (unsigned j = first; j <= i; j++) {
                valid += history[j] >= 0; active += history[j] == 1;
            }
            assert(heat_valid(&h) == valid && heat_active(&h) == active);
            assert(heat_ppm(&h) == (valid ? (int)(active * 1000000 / valid) : -1));
        }
        for (unsigned i = 0; i < window; i++) heat_push(&h, -1, window);
        assert(heat_ppm(&h) == -1);
    }
    assert(target_flags(FLAG(14) | FLAG(5))); /* anon or shmem */
    assert(!target_flags(FLAG(5))); /* ordinary file cache */
    for (unsigned bit = 15; bit <= 24; bit++) {
        if (bit == 21) continue; /* KSM is observed approximately. */
        assert(!target_flags(FLAG(14) | FLAG(5) | FLAG(bit)));
    }
    FILE *pm_file = tmpfile(), *flags_file = tmpfile(), *idle_file = tmpfile();
    assert(pm_file && flags_file && idle_file);
    int pm = fileno(pm_file), flags = fileno(flags_file), idle = fileno(idle_file);
    uint64_t pmap[512] = {0}, kflags[512] = {0}, bitmap[8] = {0};
    pmap[1] = PM_PRESENT | 65; pmap[2] = PM_PRESENT | 66;
    kflags[65] = kflags[66] = kflags[67] = FLAG(14) | FLAG(5);
    write_words(pm, pmap, 512); write_words(flags, kflags, 512); write_words(idle, bitmap, 8);
    const char *map = "00001000-00003000 rw-p 00000000 00:00 0 [heap]\n";
    struct page_list old = {0}, next = {0};
    round_from(map, pm, flags, idle, &old, &next, 3);
    assert(next.n == 2 && next.v[0].armed && next.v[1].armed);
    assert(heat_ppm(&next.v[0].heat) == -1); /* first round only arms */
    assert(!exact_io(idle, bitmap, sizeof(bitmap), 0, false));
    assert(bitmap[1] == (FLAG(1) | FLAG(2))); /* Only target bits written. */
    replace(&old, &next);
    bitmap[1] = FLAG(2); /* PFN 65 accessed, PFN 66 idle. */
    write_words(idle, bitmap, 8);
    round_from(map, pm, flags, idle, &old, &next, 3);
    assert(heat_ppm(&next.v[0].heat) == 1000000 && heat_ppm(&next.v[1].heat) == 0);
    replace(&old, &next);
    pmap[1] = PM_SWAPPED | (99ULL << 5) | 2;
    pmap[2] = PM_PRESENT | 67;
    write_words(pm, pmap, 512);
    round_from(map, pm, flags, idle, &old, &next, 3);
    assert(next.v[0].state == SWAPPED && !next.v[0].armed && next.v[0].previous_pfn == 65);
    assert(heat_ppm(&next.v[0].heat) == 1000000); /* unavailable is not cold */
    assert(next.v[1].sample == -1 && next.v[1].armed && heat_ppm(&next.v[1].heat) == 0);
    replace(&old, &next);
    for (int i = 0; i < 2; i++) {
        round_from(map, pm, flags, idle, &old, &next, 3);
        replace(&old, &next);
    }
    assert(heat_ppm(&old.v[0].heat) == -1); /* invalid rounds also advance window */
    /* VMA identity changes clear history, even at the same VA. */
    round_from("00001000-00003000 rw-p 00000000 00:00 0 [replacement]\n",
               pm, flags, idle, &old, &next, 3);
    assert(!heat_valid(&next.v[1].heat));
    replace(&old, &next);
    /* A frame changing after idle reads invalidates only this round. */
    old.v[1].sample = 1; old.v[1].heat = (struct heat_window){1, 1};
    pmap[2] = PM_PRESENT | 66; write_words(pm, pmap, 512);
    assert(!verify_pages(&old, pm));
    assert(old.v[1].state == CHANGED && old.v[1].sample == -1 && !old.v[1].eligible);
    assert(heat_ppm(&old.v[1].heat) == 1000000);
    /* Stable but non-LRU target pages are unknown this round and not armed. */
    kflags[66] = FLAG(14); write_words(flags, kflags, 512);
    round_from(map, pm, flags, idle, &old, &next, 3);
    assert(!next.v[1].eligible && !next.v[1].armed && next.v[1].sample == -1);
    replace(&old, &next);
    /* Same PFN aliases produce only one mean contributor. */
    kflags[66] = FLAG(14) | FLAG(5); write_words(flags, kflags, 512);
    pmap[1] = pmap[2] = PM_PRESENT | 66; write_words(pm, pmap, 512);
    round_from(map, pm, flags, idle, &old, &next, 3); replace(&old, &next);
    round_from(map, pm, flags, idle, &old, &next, 3); replace(&old, &next);
    assert(old.n == 2 && old.v[0].alias + old.v[1].alias == 1);
    struct config config = {.pid = getpid(), .out = argv[1], .pages = true, .interval = 1, .window = 3};
    char *summary_path; assert(asprintf(&summary_path, "%s/summary.tsv", argv[1]) >= 0);
    FILE *summary = fopen(summary_path, "wx"); free(summary_path); assert(summary);
    fputs(summary_header, summary); uint64_t now = monotonic_ns();
    assert(!snapshot(&config, summary, &old, 0, now, now, now)); fclose(summary);
    assert(!run_metadata(&config, 1, now, monotonic_ns(), 0, 0, 0));
    /* exec wrapper regression: an old-mm pagemap returns EOF. Reopen and
     * retry once, invalidating the old baseline rather than declaring cold.
     */
    FILE *empty = tmpfile(); assert(empty);
    mock_proc = 1; mock_empty = fileno(empty); mock_pm = pm; mock_opens = 0;
    old.v[0].heat = (struct heat_window){1, 1};
    pmap[1] = PM_PRESENT | 67; write_words(pm, pmap, 512);
    assert(!collect_process(7777, flags, idle, &old, &next, 3));
    assert(mock_opens == 2 && next.v[0].pfn == 67 && next.v[0].sample == -1);
    assert(heat_ppm(&next.v[0].heat) == 1000000);
    replace(&old, &next);
    mock_opens = 0;
    assert(!collect_process(7777, flags, idle, &old, &next, 3));
    assert(mock_opens == 2); replace(&old, &next);
    mock_deny = 1; mock_opens = 0;
    assert(collect_process(7777, flags, idle, &old, &next, 3) == -1 && errno == EACCES);
    assert(mock_opens == 1); mock_deny = 0;
    mock_always_empty = 1; mock_opens = 0;
    assert(collect_process(7777, flags, idle, &old, &next, 3) == -1 && errno == EIO);
    assert(mock_opens == 2);
    free(next.v); next = (struct page_list){0};
    mock_proc = 0; fclose(empty);
    /* Unmapped entries disappear; unrelated file-backed pages are filtered. */
    pmap[1] = 0; kflags[66] = FLAG(5);
    write_words(pm, pmap, 512); write_words(flags, kflags, 512);
    round_from(map, pm, flags, idle, &old, &next, 3); assert(!next.n);
    replace(&old, &next);
    pmap[1] = PM_PRESENT; pmap[2] = 0; write_words(pm, pmap, 512);
    FILE *maps = fmemopen((void *)map, strlen(map), "r");
    assert(scan_maps(maps, pm, &old, &next) == -1 && errno == EACCES);
    fclose(maps); free(next.v); free(old.v);
    fclose(pm_file); fclose(flags_file); fclose(idle_file);
    char stat[] = "123 (name ) with spaces) R 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 987 0";
    uint64_t token; char state;
    assert(!parse_identity(stat, &token, &state) && token == 987 && state == 'R');
    assert(identity(getpid(), &token) == 1 && token > 0);
    assert(seconds_value("nan", false) < 0 && seconds_value("0", false) < 0);
    assert(integer_value("65", 64) < 0 && integer_value("20", 64) == 20);
    /* Launcher handshake and command status do not need privileged interfaces. */
    uint64_t start; int status;
    char *command[] = {"/bin/sh", "-c", "exit 7", NULL};
    pid_t child = launch(command, &start);
    assert(child > 0 && start <= monotonic_ns());
    assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 7);
    char *missing[] = {"/nonexistent-rswap-command", NULL};
    assert(launch(missing, &start) == -1 && errno == ENOENT);
    puts("PASS: production sampler fixtures, windows, aliases, mapping changes and launcher");
}
'''


class SamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hermit-patch8-")
        cls.path = Path(cls.tmp.name)
        cls.tool = cls.path / "rswap_hotness"
        cls.pattern = cls.path / "hotness_pattern"
        for source, output, extra in ((ROOT / "tools/rswap_hotness.c", cls.tool, []),
                                      (ROOT / "tests/hotness_pattern.c", cls.pattern, ["-pthread"])):
            subprocess.run(["gcc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                            str(source), *extra, "-o", str(output)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_production_sampler_with_fixture_interfaces(self):
        source = self.path / "sampler.c"
        binary = self.path / "sampler"
        source.write_text(HARNESS)
        subprocess.run(["gcc", "-std=gnu11", "-O1", "-g", "-Wall", "-Wextra", "-Werror",
                        "-fsanitize=undefined", "-I", str(ROOT / "tools"), str(source),
                        "-o", str(binary)], check=True)
        result = subprocess.run([str(binary), str(self.path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("PASS: production sampler", result.stdout)
        with (self.path / "summary.tsv").open() as stream:
            row = next(csv.DictReader(stream, delimiter="\t"))
        trace = self.path / "stores.log"
        trace.write_text(f"store_sample: start_ns={row['ready_ns']} pfn=66 swap_type=0 offset=17 submit_ret=0\n")
        result = subprocess.run([sys.executable, str(ROOT / "tools/rswap_hotness_check.py"),
                                 str(self.path / "summary.tsv"), str(trace), "--require-match"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"matched": 1', result.stdout)

    def test_cli_rejects_bad_config_before_opening_interfaces(self):
        result = subprocess.run([str(self.tool), "--help"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0)
        for args in (("--window", "65"), ("--interval", "nan"), ("--interval", "0"),
                     ("--warmup", "-1"), ("--pid", "0"),
                     ("--pid", "1", "--out", "/tmp/not-created", "--", "/bin/true")):
            with self.subTest(args=args):
                result = subprocess.run([str(self.tool), *args], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_controlled_shared_address_space_workload(self):
        result = subprocess.run([str(self.pattern), "3", "1"], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("threads=2", result.stdout)
        self.assertIn("PHASE=0", result.stdout)
        self.assertIn("PHASE=1", result.stdout)
        self.assertIn("PASS pages=768", result.stdout)


if __name__ == "__main__":
    unittest.main()
