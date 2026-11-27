#!/usr/bin/env python3
"""Operator-started root smoke test: two Servers, identity, content, live swapoff.

No Server management, formatting, cgroup, heat upload, or automatic failure retry.
On failure retain the control fd and workloads for diagnosis until interrupted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import struct
import subprocess
import sys
import time
import traceback

REPO = Path(__file__).resolve().parents[2]
RS = Path('/sys/kernel/debug/rswap')
HMT = Path('/sys/kernel/debug/hermit')
FLAGS = 'bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim'.split()
SWAP = '/swap.img'
RUN = None
CHILDREN = []
CONTROL = None


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def save(name, value):
    (RUN / name).write_text(value)


def state(message):
    save('state', message + '\n')
    print(message, flush=True)


def command(args, name):
    with (RUN / name).open('w') as output:
        subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=180)


def fields(text):
    return dict(word.split('=', 1) for word in text.split() if '=' in word)


def swaps():
    return [row.split() for row in Path('/proc/swaps').read_text().splitlines()[1:]]


def snapshot(label):
    for name in ('identity_status', 'stats', 'mapping_raw', 'placement_raw', 'policy_stats', 'address_map'):
        save(label + '.' + name, (RS / name).read_text())
    s = fields((RS / 'identity_status').read_text())
    require(int(s['alloc_errors']) == 0, 'identity allocation errors: ' + str(s))
    text = (RS / 'stats').read_text().splitlines()
    require(text[-1] == 'unrouted_error=0', 'Unrouted I/O errors')
    for line in text[2:-1]:
        require(line.split()[-2:] == ['0', '0'], 'I/O errors: ' + line)
    return s


class Workload:
    def __init__(self, name, mib):
        self.name = name
        self.log = (RUN / (name + '.console.jsonl')).open('w', buffering=1)
        self.err = (RUN / (name + '.stderr')).open('w')
        self.process = subprocess.Popen([str(RUN / 'pattern'), str(mib)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.err, bufsize=0)
        CHILDREN.append(self)
        meta = self.receive()
        require(meta.get('event') == 'ready', 'Missing READY: ' + str(meta))
        self.pid, self.base, self.length = meta['pid'], meta['base'], meta['bytes']
        require(self.pid == self.process.pid and self.base % 4096 == 0, 'Invalid target metadata')
        self.pm = os.open('/proc/' + str(self.pid) + '/pagemap', os.O_RDONLY | os.O_CLOEXEC)
        save(name + '.metadata.json', json.dumps(meta, indent=2) + '\n')

    def receive(self):
        require(select.select([self.process.stdout], [], [], 60)[0], self.name + ' workload timeout')
        line = self.process.stdout.readline()
        require(bool(line), self.name + ' exited before response')
        self.log.write(line.decode())
        return json.loads(line)

    def send(self, text):
        self.process.stdin.write((text + '\n').encode())
        self.process.stdin.flush()
        result = self.receive()
        require(result.get('command') == text and result.get('event') == 'done', 'Bad response: ' + str(result))
        return result

    def pages(self):
        raw = os.pread(self.pm, self.length // 4096 * 8, self.base // 4096 * 8)
        require(len(raw) == self.length // 4096 * 8, 'Short pagemap read')
        return struct.unpack('=' + 'Q' * (len(raw) // 8), raw)

    def finish(self):
        self.send('finish')
        require(self.process.wait(timeout=30) == 0, self.name + ' content verification failed')
        os.close(self.pm)
        self.process.stdin.close()
        self.process.stdout.close()
        self.log.close()
        self.err.close()


def swapped(entry):
    return bool(entry & (1 << 62)) and not entry & (1 << 63)


def pfn_changes(before, after):
    pairs = [(a & ((1 << 55) - 1), b & ((1 << 55) - 1)) for a, b in zip(before, after)
             if a & (1 << 63) and b & (1 << 63)]
    require(pairs and all(a and b for a, b in pairs), 'Resident PFNs unavailable')
    return dict(compared=len(pairs), changed=sum(a != b for a, b in pairs))


def await_swap(work, begin, count):
    deadline = time.monotonic() + 15
    while True:
        pages = work.pages()
        actual = sum(swapped(p) for p in pages[begin:begin + count])
        if actual >= count // 2:
            return pages
        require(time.monotonic() < deadline, 'Insufficient swapped pages: ' + str(actual))
        time.sleep(0.1)


def audit(work, label, expected):
    checked = retries = 0
    with (RUN / (label + '.identity.tsv')).open('w') as output:
        output.write('pid\tva\tvpn\ttype\toffset\tfound\treturned_vpn\taliases\n')
        for page, entry in enumerate(work.pages()):
            if not swapped(entry):
                continue
            for attempt in range(4):
                va = work.base + page * 4096
                before = struct.unpack('=Q', os.pread(work.pm, 8, va // 4096 * 8))[0]
                if not swapped(before):
                    break
                typ, offset = before & 31, (before >> 5) & ((1 << 50) - 1)
                os.write(CONTROL, ('lookup ' + str(offset) + '\n').encode())
                result = fields(os.pread(CONTROL, 512, 0).decode())
                after = struct.unpack('=Q', os.pread(work.pm, 8, va // 4096 * 8))[0]
                if before != after:
                    retries += 1
                    require(attempt < 3, 'Pagemap never settled')
                    continue
                require(result['attached'] == '1' and int(result['type']) == typ, 'Wrong binding/type')
                require(int(result['found']) == int(expected), label + ': unexpected identity: ' + str(result))
                if expected:
                    require(int(result['vpn']) == va // 4096 and int(result['va'], 16) == va
                            and result['aliases'] == '1' and int(result['pid']) == work.pid,
                            label + ': incorrect VPN/VA/aliases: ' + str(result))
                output.write(f"{work.pid}\t{va:#x}\t{va // 4096}\t{typ}\t{offset}\t{result['found']}\t{result['vpn']}\t{result['aliases']}\n")
                checked += 1
                break
    require(checked > 0, label + ': no stable swapped pages checked')
    snapshot(label)
    result = dict(checked=checked, retries=retries, expected_found=expected)
    state(label + ' PASS: ' + json.dumps(result))
    return result


def empty_mapping():
    rows = (RS / 'mapping_raw').read_text().splitlines()[2:]
    require(len(rows) == 4, 'Expected four remote pools')
    for row in rows:
        server, tier, cap, live, retired, free = row.split()
        require((int(live), int(retired), int(free)) == (0, 0, int(cap)), 'Slots remain: ' + row)


def execute():
    global CONTROL
    require(os.geteuid() == 0, 'Operator must launch this controller with sudo')
    require(not (RUN / 'started').exists(), 'Run already started; never retry in the same directory')
    require(os.uname().version == '#9 SMP Sat Nov 28 00:02:37 CST 2026', 'Expected #9 kernel')
    require(Path('/sys/devices/system/cpu/online').read_text().strip() == '0-47', 'Unexpected Client CPU set')
    require(not Path('/sys/module/rswap_client').exists(), 'Client already loaded')
    original_swaps = swaps()
    require(len(original_swaps) == 1 and original_swaps[0][0] == SWAP and int(original_swaps[0][3]) == 0,
            'Expected only unused /swap.img; inspect before proceeding')
    config = (REPO / 'remoteswap/client/rswap_client.conf').read_text()
    for line in ('NR_SERVERS=2', 'POOL_GIB=96', 'SERVER0_IP="10.0.0.13"', 'SERVER1_IP="10.0.0.20"'):
        require(line in config.splitlines(), 'Unexpected dual-node configuration: ' + line)
    for path, digest in json.loads((RUN / 'artifacts.json').read_text()).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest, 'Artifact changed: ' + path)
    save('started', time.strftime('%Y-%m-%d %H:%M:%S %z') + '\n')
    save('client.conf.before', config)
    save('swaps.before', Path('/proc/swaps').read_text())
    command(['dmesg'], 'dmesg.before')
    settings = {str(HMT / name): (HMT / name).read_text().strip() for name in FLAGS}
    for path in ('/proc/sys/vm/page-cluster', '/sys/kernel/mm/swap/readahead_win'):
        settings[path] = Path(path).read_text().strip()
    save('settings.json', json.dumps(settings, indent=2) + '\n')
    for name in FLAGS:
        (HMT / name).write_text('N\n')
    Path('/proc/sys/vm/page-cluster').write_text('0\n')
    Path('/sys/kernel/mm/swap/readahead_win').write_text('1\n')
    command(['swapoff', SWAP], 'original-swapoff.log')
    command(['bash', str(REPO / 'remoteswap/client/manage_rswap_client.sh'), 'install'], 'client-install.log')
    require(Path('/sys/module/rswap_client/version').read_text().strip() == '1.17', 'Expected Client 1.17')
    empty_mapping()
    command(['swapon', SWAP], 'test-swapon.log')
    snapshot('baseline')
    state('CONNECTED: dual-node Client 1.17; preparing anonymous-page workload')
    results = dict(version=1, kernel=os.uname().version, client='1.17', servers=['10.0.0.13', '10.0.0.20'])
    target = Workload('target', 32)
    initial = target.pages()
    target.send('out a')
    await_swap(target, 0, target.length // 8192)
    CONTROL = os.open(RS / 'identity', os.O_RDWR | os.O_CLOEXEC)
    os.write(CONTROL, ('bind ' + str(target.pid) + '\n').encode())
    save('bind.response', os.pread(CONTROL, 512, 0).decode())
    results['prescan'] = audit(target, 'prescan', True)
    other = Workload('non-target', 4)
    other.send('out all')
    await_swap(other, 0, other.length // 4096)
    results['isolation'] = audit(other, 'isolation', False)
    other.finish()
    target.send('out b')
    await_swap(target, target.length // 8192, target.length // 8192)
    results['postbind'] = audit(target, 'postbind', True)
    target.send('verify')
    changes = [pfn_changes(initial, target.pages())]
    for generation in (1, 2):
        require(target.send('rewrite')['generation'] == generation, 'Wrong generation')
        resident = target.pages()
        target.send('out all')
        await_swap(target, 0, target.length // 4096)
        results['generation' + str(generation)] = audit(target, 'generation' + str(generation), True)
        if generation == 1:
            target.send('verify')
            changes.append(pfn_changes(resident, target.pages()))
    snapshot('before-swapoff')
    state('IDENTITY AND CONTENT PASS: performing live-process swapoff')
    command(['swapoff', SWAP], 'live-swapoff.log')
    require(not swaps(), 'Swap remains active')
    s = snapshot('after-swapoff')
    require(s['bound'] == '0' and s['entries'] == '0' and s['aliases'] == '0' and s['area_active'] == '0', 'Identity did not reset')
    require(fields(os.pread(CONTROL, 512, 0).decode())['attached'] == '0', 'Control fd remains attached')
    empty_mapping()
    changes.append(pfn_changes(resident, target.pages()))
    target.finish()
    os.close(CONTROL)
    CONTROL = None
    results['pfn_changes'] = changes
    results['target_generations_verified'] = 3
    results['target_pages_per_generation'] = target.length // 4096
    io = (RS / 'stats').read_text().splitlines()[2:-1]
    results['io'] = [dict(zip(('server', 'tier', 'op', 'ok', 'wc_error', 'submit_error'), row.split())) for row in io]
    for server in ('0', '1'):
        for op in ('write', 'read_sync'):
            require(sum(int(r['ok']) for r in results['io'] if r['server'] == server and r['op'] == op) > 0,
                    'No actual I/O on Server ' + server + ' op ' + op)
    command(['bash', str(REPO / 'remoteswap/client/manage_rswap_client.sh'), 'uninstall'], 'client-uninstall.log')
    require(not Path('/sys/module/rswap_client').exists(), 'Client remains loaded')
    for path, value in settings.items():
        Path(path).write_text(value + '\n')
        require(Path(path).read_text().strip() == value, 'Setting not restored: ' + path)
    require((REPO / 'remoteswap/client/rswap_client.conf').read_text() == config, 'Client configuration changed')
    command(['swapon', SWAP], 'original-swap-restore.log')
    require([row[0] for row in swaps()] == [SWAP], 'Original swap not restored')
    command(['dmesg'], 'dmesg.final')
    before, after = (RUN / 'dmesg.before').read_text(), (RUN / 'dmesg.final').read_text()
    require(after.startswith(before), 'dmesg baseline rotated; inspect logs manually')
    added = after[len(before):]
    save('dmesg.added', added)
    suspicious = [line for line in added.splitlines() if re.search(r'BUG:|WARNING:|Oops:|general protection fault|I/O error|\berror\b|\bfailed\b', line, re.I)]
    require(not suspicious, 'New suspicious kernel logs: ' + '\n'.join(suspicious))
    results['cleanup'] = dict(slot_pools_empty=True, identity_reset=True, module_unloaded=True,
                              settings_restored=True, original_swap_restored=True)
    results['elapsed_seconds'] = time.monotonic() - START
    results['not_covered'] = ['offset reuse after final release', 'concurrency stress', 'exec and old-fd ownership', 'Hermit reclaim path', 'EWMA and new placement']
    save('results.json', json.dumps(results, indent=2) + '\n')
    save('SUCCESS', 'Core dual-node smoke test passed; see results.json for coverage\n')
    state('COMPLETE: three generations verified; identity correct; both Servers used; live swapoff and restoration passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    args = parser.parse_args()
    RUN = args.run_dir.resolve()
    require(RUN.is_dir(), 'Run directory must be prepared first')
    START = time.monotonic()
    try:
        execute()
    except Exception:
        save('failure.txt', traceback.format_exc())
        state('FAILED: stop; preserving workload/control fd and settings. Ask Codex to inspect failure.txt.')
        traceback.print_exc()
        while True:
            time.sleep(1)
