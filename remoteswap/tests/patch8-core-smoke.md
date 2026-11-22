# Patch 8 可复制核心测试：冷热识别、Store 关联、BFS 摘要

本文件保留可复制的复测步骤；**2026-11-22 实机执行结果见末节**。
从仓库根目录逐段复制，按标明的终端执行；未列入本轮结果的场景不视为已验证。
只做两项核心验收，加一次 BFS 集成确认；不重复单节点、异容量、故障注入和性能测试。

| 测试 | 配置 | 通过标准 |
| --- | --- | --- |
| 冷热识别 | 128 MiB、2 线程、冷热各 12 秒；周期 1 秒、窗口 8 轮、Warmup 4 秒 | 热/冷有区分，交换访问模式后热度关系交换，均值与逐页记录一致 |
| Store 关联 | 双 Server、4 GiB 工作集、2 GiB 物理限制、2 轮内容校验；周期 1 秒、窗口 4 轮、Warmup 4 秒 | 已接受 Store 匹配此前已知热度，Trace 无丢失，内容与 swapoff 校验通过，无 RDMA 错误新增 |
| BFS 集成 | 64G 图、40 线程、24 GiB 物理限制、1 个计时轮次；周期 1 秒、窗口 20 轮、Warmup 20 秒 | BFS 与监控正常结束，有已知热度和均值，正常 swapoff/卸载 |

BFS **只保存摘要，不加 `--pages`，不做 BFS PFN 关联率验收**；PFN 关联在第二项完成。
不要求精确热度、匹配率百分比或 DRAM 带宽高于 PMem；Client 1.8 仍是静态 Interleave。
窗口/未知页细则已有本地回归，本轮不另做 VMA 重用、附加模式等硬件用例。

Client 使用三个终端：**A 准备和运行程序，B 带宽监控，C Trace 与活进程 swapoff**。
两台 Server 各自另开终端，保持在线直至 Client swapoff 和卸载成功。
任何步骤失败、校验 FAIL、Killed/SIGBUS、RDMA 错误、新 BUG/WARN 或持续卡住，
停止后续验收并保存日志；不强制卸载，不提前停止 Server。

## 0. Client A：一次性准备

当前机器：`optane / 10.0.0.11`，仓库 `/mnt/sdb3/data/yxr/projects/hermit`。
沿用已有内核、THP 和 Hermit 开关；不重新安装内核、不格式化 swapfile。
从**无活动 swap、旧 Client 已正常卸载**开始；不符合时先按既有正常流程收尾。
`/tmp/hermit-p8-core` 必须不存在；重测请先另存旧日志，不覆盖。

```bash
bash -euo pipefail <<'PREP'
cd /mnt/sdb3/data/yxr/projects/hermit
test "$(wc -l < /proc/swaps)" -eq 1
test ! -d /sys/module/rswap_client
test "$(cat /sys/devices/system/cpu/online)" = 0-47
test "$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' /sys/kernel/mm/transparent_hugepage/enabled)" = never
test "$(sudo cat /sys/kernel/debug/hermit/batch_account)" = N
sudo test -r /proc/kpageflags
sudo test -r /sys/kernel/mm/page_idle/bitmap
sudo test -w /sys/kernel/mm/page_idle/bitmap
test ! -e /tmp/hermit-p8-core
mkdir /tmp/hermit-p8-core
mkdir /tmp/hermit-p8-core/cold /tmp/hermit-p8-core/pattern /tmp/hermit-p8-core/bfs
cat > /tmp/hermit-p8-core/env <<'ENV'
P8_REPO=/mnt/sdb3/data/yxr/projects/hermit
P8_RUN=/tmp/hermit-p8-core
P8_TOOL=/mnt/sdb3/data/yxr/projects/hermit/remoteswap/tools/rswap_hotness
P8_CG_PATTERN=/sys/fs/cgroup/memory/hermit-p8-pattern
P8_CG_BFS=/sys/fs/cgroup/memory/hermit-p8-bfs
P8_TRACE=/sys/kernel/debug/tracing/instances/rswap_p8_core
P8_SWAP48=/mnt/sdb4/yxr/projects/swapfile/swap_48g.img
P8_SWAP80=/mnt/sdb4/yxr/projects/swapfile/swap_80g.img
P8_BFS=/mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS
P8_GRAPH=/mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_64G.adj
ENV
source /tmp/hermit-p8-core/env
test ! -e "$P8_CG_PATTERN"
test ! -e "$P8_CG_BFS"
test ! -e "$P8_TRACE"
test -x "$P8_BFS"
test -r "$P8_GRAPH"
sudo swaplabel "$P8_SWAP48"
sudo swaplabel "$P8_SWAP80"
# 逐页日志可能达到数 GiB；此处预留至少 20 GiB，暂停时不要长时间放置无人管理。
test "$(df -Pk "$P8_RUN" | awk 'NR==2 {print $4}')" -ge 20971520
df -h "$P8_RUN"
make -C remoteswap/tools
make -C remoteswap/client KDIR="$P8_REPO/linux-5.14-rc5" -j12
test "$(modinfo -F version remoteswap/client/rswap-client.ko)" = 1.8
gcc -std=gnu11 -O2 -Wall -Wextra -Werror -pthread remoteswap/tests/hotness_pattern.c -o "$P8_RUN/hotness_pattern"
gcc -std=gnu11 -O2 -Wall -Wextra -Werror remoteswap/tests/swap_pattern.c -o "$P8_RUN/swap_pattern"
cp remoteswap/client/rswap_client.conf "$P8_RUN/client.conf.before"
sudo dmesg > "$P8_RUN/dmesg.before"
echo 'PREP PASS'
PREP
```

预期最后出现 `PREP PASS`。权限不足或 PFN 不可见时后续采样会明确报错，不把它当成未知页通过。

## 1. Client A：独立冷热识别，不启用 swap/RDMA

程序自己禁用测试范围 THP，并用两个线程访问同一地址空间。
这一段约 24 秒，自动交换冷热角色并退出，**不需要 ENTER**。

```bash
bash -euo pipefail <<'COLD'
source /tmp/hermit-p8-core/env
set +e
sudo "$P8_TOOL" --out "$P8_RUN/cold/heat" --interval 1 --window 8 --warmup 4 --pages \
  -- "$P8_RUN/hotness_pattern" 128 12 2>&1 | tee "$P8_RUN/cold/console.log"
P8_RC=$?
set -e
printf 'cold_exit=%s\n' "$P8_RC" | tee "$P8_RUN/cold/exit"
exit "$P8_RC"
COLD
```

退出后仍在 **A** 复制检查段。它按程序打印的 VA 范围识别三段，
分别选两个阶段中已覆盖完整窗口的晚期快照，避开初次触页和最后内容校验。

```bash
bash -euo pipefail <<'CHECK_COLD'
source /tmp/hermit-p8-core/env
test "$(cat "$P8_RUN/cold/exit")" = cold_exit=0
sudo python3 -B - "$P8_RUN/cold" "$P8_REPO" <<'PY' | tee "$P8_RUN/cold/check.log"
import csv, re, statistics, sys
from pathlib import Path
root, repo = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo / 'remoteswap/tools'))
from rswap_hotness_check import read_summary, read_pages
text = (root / 'console.log').read_text()
assert 'PASS pages=' in text, 'workload did not finish validation'
m = re.search(r'READY pid=\d+ begin=(0x[0-9a-f]+) end=(0x[0-9a-f]+) pages=(\d+) threads=2', text)
assert m, 'missing region/threads marker'
begin, end, pages = int(m[1], 16), int(m[2], 16), int(m[3])
phases = {int(p): int(t) for p, t in re.findall(r'PHASE=(\d+) mono_ns=(\d+)', text)}
assert set(phases) == {0, 1}, 'missing phase markers'
snapshots = read_summary(root / 'heat/summary.tsv')
assert {s.phase for s in snapshots} == {'warmup', 'observe'}, 'Warmup transition missing'
for phase, lower, upper in ((0, phases[0], phases[1]), (1, phases[1], phases[1] + 12_000_000_000)):
    candidates = [s for s in snapshots if lower <= s.begin_ns and s.ready_ns < upper]
    assert len(candidates) >= 9, 'not enough completed rounds for an 8-round window; save logs and adjust interval/phase duration'
    snap = candidates[-1]
    read_pages(snap)  # Production checker validates whole-process mean and alias accounting.
    groups, seen = [[], [], []], set()
    with snap.path.open() as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            va = int(row['va'], 16)
            if not begin <= va < end:
                continue
            assert va not in seen, 'target VA counted twice'
            seen.add(va)
            valid, active = int(row['valid']), int(row['active'])
            assert int(row['heat_ppm']) == (active * 1000000 // valid if valid else -1)
            if valid and not int(row['alias']):
                group = min(((va - begin) // 4096) // (pages // 3), 2)
                groups[group].append(active / valid)
    assert len(seen) == pages, 'target region incomplete or counted per thread'
    assert all(groups), 'one region has no known heat'
    means = [statistics.mean(g) for g in groups]
    hot, cold = (0, 2) if phase == 0 else (2, 0)
    print(f'phase={phase} generation={snap.generation} region_mean={means} process_mean={snap.mean}')
    assert means[hot] > means[cold], 'hot/cold discrimination or reversal missing'
print('PASS: target region counted once, heat reverses, mean/unknown encoding is consistent.')
PY
CHECK_COLD
```

预期两行区域均值，最后 `PASS`；中间段的间歇热度只展示，不设精确比例门槛。
不使用全进程均值代替三段区域比较。如果采样超期导致不足 9 轮，先保存日志反馈，
不把不足窗口的结果判通过，也不要直接进入后续测试。

## 2. 两台 Server：各启动一个新进程

两台均使用本机 `/home/yxr/projects/hermit/remoteswap/server/run_server.sh` 中已验证的
DRAM_NODE/PMEM_NODE。下面只读取这两个值；IP、端口、核数和容量显式传入，
避免 node04 误用仓库脚本默认的 10.0.0.20。**不修改 Server 文件或猜测新的 NUMA 配置。**
若脚本已不是此前验证的 NUMA 配置，先核对后再启动。

**node04 / 10.0.0.13：**

```bash
bash -euo pipefail <<'NODE04'
cd /home/yxr/projects/hermit/remoteswap/server
sed -n '1,16p' run_server.sh
P8_DRAM_NODE=$(sed -nE 's/^DRAM_NODE="?([0-9]+)"?.*/\1/p' run_server.sh)
P8_PMEM_NODE=$(sed -nE 's/^PMEM_NODE="?(auto|[0-9]+)"?.*/\1/p' run_server.sh)
test -n "$P8_DRAM_NODE"
test -n "$P8_PMEM_NODE"
exec ./rswap-server --ip 10.0.0.13 --port 9400 --client-cores 48 \
  --dram-node "$P8_DRAM_NODE" --dram-gib 16 --pmem-node "$P8_PMEM_NODE" --pmem-gib 32
NODE04
```

**server02 / 10.0.0.20：**

```bash
bash -euo pipefail <<'SERVER02'
cd /home/yxr/projects/hermit/remoteswap/server
sed -n '1,16p' run_server.sh
P8_DRAM_NODE=$(sed -nE 's/^DRAM_NODE="?([0-9]+)"?.*/\1/p' run_server.sh)
P8_PMEM_NODE=$(sed -nE 's/^PMEM_NODE="?(auto|[0-9]+)"?.*/\1/p' run_server.sh)
test -n "$P8_DRAM_NODE"
test -n "$P8_PMEM_NODE"
exec ./rswap-server --ip 10.0.0.20 --port 9400 --client-cores 48 \
  --dram-node "$P8_DRAM_NODE" --dram-gib 16 --pmem-node "$P8_PMEM_NODE" --pmem-gib 32
SERVER02
```

两台等待 `listening on ...`，预分配/NUMA 检查成功；保持这两个终端运行。
MR 注册会在 Client 连接后完成。CPU-less Node 的唯一性检查失败时不更换为猜测的 Node。

## 3. Client A：加载 1.8，准备双节点换页

```bash
bash -euo pipefail <<'CONNECT'
source /tmp/hermit-p8-core/env
cd "$P8_REPO"
test "$(wc -l < /proc/swaps)" -eq 1
test ! -d /sys/module/rswap_client
source remoteswap/client/rswap_client.conf
test "$NR_SERVERS" = 2
test "$POOL_GIB" = 96
test "$SERVER0_IP" = 10.0.0.13
test "$SERVER1_IP" = 10.0.0.20
test "$SERVER0_PORT" = 9400
test "$SERVER1_PORT" = 9400
test "$CONNECT_TIMEOUT_MS" = 60000
sudo mkdir "$P8_CG_PATTERN" "$P8_CG_BFS"
echo -1 | sudo tee "$P8_CG_PATTERN/memory.memsw.limit_in_bytes"
echo 2G | sudo tee "$P8_CG_PATTERN/memory.limit_in_bytes"
echo 100 | sudo tee "$P8_CG_PATTERN/memory.swappiness"
echo -1 | sudo tee "$P8_CG_BFS/memory.memsw.limit_in_bytes"
echo 24G | sudo tee "$P8_CG_BFS/memory.limit_in_bytes"
echo 100 | sudo tee "$P8_CG_BFS/memory.swappiness"
bash remoteswap/client/manage_rswap_client.sh install
test "$(cat /sys/module/rswap_client/version)" = 1.8
sudo dmesg | tail -35
CONNECT
```

确认每台 protocol=2、capacity=48 GiB、DRAM 2＋PMem 4 Chunks，总池 96 GiB、`client ready`。
**没有 ready 不继续 swapon。** 然后 A 执行：

```bash
bash -euo pipefail <<'ACTIVATE'
source /tmp/hermit-p8-core/env
cd "$P8_REPO"
sudo swapon "$P8_SWAP48"
sudo mkdir "$P8_TRACE"
echo 0 | sudo tee "$P8_TRACE/tracing_on"
echo 4096 | sudo tee "$P8_TRACE/buffer_size_kb"
test "$(sudo cat "$P8_TRACE/events/rswap/store_sample/enable")" = 0
test "$(sudo cat "$P8_TRACE/events/rswap/placement/enable")" = 0
bash remoteswap/client/manage_rswap_client.sh stats > "$P8_RUN/pattern/stats.before"
ACTIVATE
```

**Client B：**启动现有带宽 monitor，保持到 BFS 结束；不要求各 Tier 带宽相等或 DRAM 更高。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
set -o pipefail
bash remoteswap/client/manage_rswap_client.sh monitor 1 2>&1 | tee /tmp/hermit-p8-core/bandwidth.log
```

## 4. Client A：运行页面校验，第一次 READY 暂停

只有子进程加入 2 GiB cgroup；监控器保持在外。**看到 READY generation=0 后先不要 ENTER。**

```bash
bash -euo pipefail <<'PATTERN'
source /tmp/hermit-p8-core/env
set +e
sudo "$P8_TOOL" --out "$P8_RUN/pattern/heat" --interval 1 --window 4 --warmup 4 --pages \
  -- bash -ec 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 2' \
  bash "$P8_CG_PATTERN" "$P8_RUN/swap_pattern" < /dev/tty 2>&1 | tee "$P8_RUN/pattern/console.log"
P8_RC=$?
set -e
printf 'pattern_exit=%s\n' "$P8_RC" | tee "$P8_RUN/pattern/exit"
exit "$P8_RC"
PATTERN
```

监控摘要会滚动输出，READY 不一定一直留在屏幕底部。命令显式从 `/dev/tty` 读取 ENTER，
不会误把外层 heredoc 的脚本内容当作程序输入；本段须在交互终端执行。
到 **Client C** 复制下面的等待段，
它确认第一次 READY，并等待从现在起至少三轮完整的已知热度快照；A 继续保持暂停。

```bash
bash -euo pipefail <<'WAIT_BASELINE'
source /tmp/hermit-p8-core/env
rg 'READY generation=0' "$P8_RUN/pattern/console.log"
if rg -q 'READY generation=1' "$P8_RUN/pattern/console.log"; then
    echo 'Already at generation=1; do not capture as the first readback.' >&2
    exit 1
fi
swapon --show
sudo python3 -B - "$P8_RUN/pattern/heat/summary.tsv" <<'PY'
import csv, io, sys, time
from pathlib import Path
path = Path(sys.argv[1])
start, deadline = time.monotonic_ns(), time.monotonic() + 120
while time.monotonic() < deadline:
    text = path.read_text()
    complete = text[:text.rfind('\n') + 1]  # Ignore a concurrently written partial row.
    rows = list(csv.DictReader(io.StringIO(complete), delimiter='\t'))
    valid = [r for r in rows if r.get('ready_ns') and int(r['ready_ns']) >= start and int(r['known']) > 0]
    print(f'new completed known snapshots={len(valid)}/3', flush=True)
    if len(valid) >= 3:
        print('BASELINE READY: keep A paused until Trace ON.')
        break
    time.sleep(1)
else:
    raise SystemExit('No usable baseline within 120s; save logs and stop acceptance.')
PY
WAIT_BASELINE
```

预期 swap Used 为 GiB 量级，最后 `BASELINE READY`。不满足时不继续采集。

## 5. Client C：采集 20 秒，A 同时开始读回

C 复制整段；出现 **Trace ON** 后马上到 A **只按一次 ENTER**。
A 到达 `VERIFIED generation=0`、`READY generation=1` 后再次暂停，不要第二次 ENTER。

```bash
bash -euo pipefail <<'TRACE'
source /tmp/hermit-p8-core/env
sudo bash -euo pipefail -s -- "$P8_TRACE" "$P8_RUN/pattern" <<'CAPTURE'
trace=$1
out=$2
reader=
cleanup() {
    echo 0 > "$trace/tracing_on"
    echo 0 > "$trace/events/rswap/store_sample/enable"
    if [[ -n "$reader" ]]; then
        kill "$reader" 2>/dev/null || true
        wait "$reader" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
test ! -e "$out/store.log"
cat "$trace/trace_pipe" > "$out/store.log" &
reader=$!
echo 1 > "$trace/events/rswap/store_sample/enable"
echo 1 > "$trace/tracing_on"
echo 'Trace ON: now press ENTER ONCE in terminal A; collecting 20 seconds.'
sleep 20
kill -0 "$reader"
echo 0 > "$trace/tracing_on"
echo 0 > "$trace/events/rswap/store_sample/enable"
sleep 1
if kill -0 "$reader" 2>/dev/null; then kill "$reader" 2>/dev/null || true; fi
set +e
wait "$reader"
reader_status=$?
set -e
reader=
[[ "$reader_status" -eq 0 || "$reader_status" -eq 143 ]]
for p in "$trace"/per_cpu/cpu*/stats; do
    printf '\n%s\n' "$p"
    cat "$p"
done > "$out/trace-buffer.stats"
echo 'Trace OFF; keep A at READY generation=1 for live swapoff.'
CAPTURE
TRACE
```

Trace 的提交返回值不是 WC 结果；最终错误仍由 stats 验证。此时不终止监控器，
启动模式下 Ctrl-C 会同时终止它启动的页面程序。逐页文件继续写，尽快完成下一步。

## 6. Client C：活进程 swapoff；A 完成最终校验

先确认 A 停在第二个 READY，宿主机有余量容纳 4 GiB 工作集。C 复制：

```bash
bash -euo pipefail <<'LIVE_SWAPOFF'
source /tmp/hermit-p8-core/env
rg 'VERIFIED generation=0|READY generation=1' "$P8_RUN/pattern/console.log"
free -h
echo -1 | sudo tee "$P8_CG_PATTERN/memory.limit_in_bytes"
sudo swapoff "$P8_SWAP48"
test "$(wc -l < /proc/swaps)" -eq 1
swapon --show
LIVE_SWAPOFF
```

swapoff 成功后，**A 再按一次 ENTER**。等待最终 VERIFIED、PASS 和 `pattern_exit=0`，
监控器随程序结束并写出 run.tsv。不要在它仍运行时做离线关联。

然后 **Client A** 执行：

```bash
bash -euo pipefail <<'CHECK_PATTERN'
source /tmp/hermit-p8-core/env
cd "$P8_REPO"
test "$(cat "$P8_RUN/pattern/exit")" = pattern_exit=0
rg 'VERIFIED generation=0|VERIFIED generation=1|PASS: all 2' "$P8_RUN/pattern/console.log" \
  | tee "$P8_RUN/pattern/check.log"
bash remoteswap/client/manage_rswap_client.sh stats > "$P8_RUN/pattern/stats.after"
python3 -B remoteswap/tests/check_patch7_stats.py "$P8_RUN/pattern/stats.before" "$P8_RUN/pattern/stats.after" \
  | tee -a "$P8_RUN/pattern/check.log"
sudo python3 -B remoteswap/tools/rswap_hotness_check.py \
  "$P8_RUN/pattern/heat/summary.tsv" "$P8_RUN/pattern/store.log" \
  --buffer-stats "$P8_RUN/pattern/trace-buffer.stats" --require-match \
  --detail "$P8_RUN/pattern/matches.tsv" | tee "$P8_RUN/pattern/association.log"
echo 'PATTERN + ASSOCIATION PASS' | tee -a "$P8_RUN/pattern/check.log"
CHECK_PATTERN
```

预期四个 Server/Tier 均有成功读写、错误增量全为 0、关联 `matched > 0` 且 Trace 无丢失。
匹配的已知热度允许为 0；不要求同时出现热/冷两类 Store，也不设匹配率百分比门槛。
比例分母是窗口内全部 Client 接受的 Store，不是精确目标 PID Store；匹配少也必须原样报告，
不能仅凭一条匹配宣称总体关联效果良好。零匹配先排查采样/时序，不直接进入 BFS。
同一组连接继续 BFS，**此时不卸载 Client，不重启 Server**。

## 7. Client A：BFS 一次运行，只保存摘要

沿用现有 64G 图、40 线程、24 GiB 物理限制；Ligra 自带的预热保留，只设 `-rounds 1`。
本节不启用 Store Trace，不添加 `--pages`，避免全程逐页日志。
B 带宽监控继续运行，C 可偶尔查看 `swapon --show`。

```bash
bash -euo pipefail <<'BFS_PREP'
source /tmp/hermit-p8-core/env
cd "$P8_REPO"
test "$(wc -l < /proc/swaps)" -eq 1
test -z "$(cat "$P8_CG_BFS/cgroup.procs")"
test "$(sudo cat "$P8_TRACE/events/rswap/store_sample/enable")" = 0
test "$(cat /sys/module/rswap_client/version)" = 1.8
sudo swapon "$P8_SWAP80"
bash remoteswap/client/manage_rswap_client.sh stats > "$P8_RUN/bfs/stats.before"
BFS_PREP
```

然后仍在 A：

```bash
bash -euo pipefail <<'BFS_RUN'
source /tmp/hermit-p8-core/env
set +e
sudo "$P8_TOOL" --out "$P8_RUN/bfs/heat" --interval 1 --window 20 --warmup 20 \
  -- bash -ec 'echo $$ > "$1/cgroup.procs"; export OMP_NUM_THREADS=40; exec "$2" -s -rounds 1 "$3"' \
  bash "$P8_CG_BFS" "$P8_BFS" "$P8_GRAPH" 2>&1 | tee "$P8_RUN/bfs/console.log"
P8_RC=$?
set -e
printf 'bfs_exit=%s\n' "$P8_RC" | tee "$P8_RUN/bfs/exit"
exit "$P8_RC"
BFS_RUN
```

退出后 A 复制检查段：

```bash
bash -euo pipefail <<'CHECK_BFS'
source /tmp/hermit-p8-core/env
cd "$P8_REPO"
test "$(cat "$P8_RUN/bfs/exit")" = bfs_exit=0
test "$(rg -c 'Running time' "$P8_RUN/bfs/console.log")" = 1
sudo python3 -B - "$P8_RUN/bfs/heat" <<'PY' | tee "$P8_RUN/bfs/check.log"
import csv, math, sys
from pathlib import Path
root = Path(sys.argv[1])
with (root / 'run.tsv').open() as f:
    run = list(csv.DictReader(f, delimiter='\t'))
assert len(run) == 1 and int(run[0]['exit_status']) == 0 and int(run[0]['monitor_error']) == 0
with (root / 'summary.tsv').open() as f:
    rows = list(csv.DictReader(f, delimiter='\t'))
assert rows and {r['phase'] for r in rows} == {'warmup', 'observe'}, 'missing Warmup/observe output'
assert any(r['phase'] == 'observe' and int(r['known']) > 0 for r in rows), 'no known heat after Warmup'
for r in rows:
    mean, known = float(r['mean']), int(r['known'])
    assert math.isfinite(mean) and (0 <= mean <= 1 if known else mean == -1)
    assert r['pages_file'] == '-', 'BFS should use summary only'
    assert int(r['ready_ns']) <= int(run[0]['end_ns'])
print(f"snapshots={len(rows)} known_observe_rounds={sum(r['phase']=='observe' and int(r['known'])>0 for r in rows)} overruns={sum(int(r['overrun']) for r in rows)}")
print('PASS: BFS monitor finished normally, Warmup completed, known heat/mean available; no PFN association claim.')
PY
bash remoteswap/client/manage_rswap_client.sh stats > "$P8_RUN/bfs/stats.after"
python3 -B remoteswap/tests/check_patch7_stats.py "$P8_RUN/bfs/stats.before" "$P8_RUN/bfs/stats.after" \
  | tee -a "$P8_RUN/bfs/check.log"
sudo dmesg > "$P8_RUN/dmesg.after-workloads"
CHECK_BFS
```

预期 `bfs_exit=0`、一条 Running time、监控检查 PASS、四个池错误增量为 0。
overrun 仅报告，不设性能门槛；如果没有已知热度或没有跨过 Warmup，不能用 BFS 正常退出替代监控验收。

## 8. 正常收尾与回传

确认程序已结束，在 **B Ctrl-C** 停止带宽 monitor。C 的 Trace 脚本应早已自动结束。
**Client A** 最后执行：

```bash
bash -euo pipefail <<'CLEANUP' | tee /tmp/hermit-p8-core/cleanup.log
source /tmp/hermit-p8-core/env
cd "$P8_REPO"
echo 0 | sudo tee "$P8_TRACE/tracing_on"
echo 0 | sudo tee "$P8_TRACE/events/rswap/store_sample/enable"
sudo swapoff "$P8_SWAP80"
test "$(wc -l < /proc/swaps)" -eq 1
bash remoteswap/client/manage_rswap_client.sh uninstall
test ! -d /sys/module/rswap_client
sudo rmdir "$P8_TRACE"
test -z "$(cat "$P8_CG_PATTERN/cgroup.procs")"
test -z "$(cat "$P8_CG_BFS/cgroup.procs")"
sudo rmdir "$P8_CG_PATTERN" "$P8_CG_BFS"
cmp remoteswap/client/rswap_client.conf "$P8_RUN/client.conf.before"
sudo dmesg > "$P8_RUN/dmesg.final"
bash remoteswap/client/manage_rswap_client.sh status
echo 'CLEANUP PASS: no active swap, Client unloaded; Servers may now be stopped.'
CLEANUP
```

最后才到两台 Server 终端 Ctrl-C 停止。保留日志，不自动删除测试输出。
结合 `dmesg.before`/`dmesg.final` 检查本轮新增的 BUG/WARN/RDMA 错误，不重复计算历史告警。

先回传这几个小文件即可，无需先发送全部逐页 TSV：

- `cold/exit`、`cold/check.log`；
- `pattern/exit`、`pattern/check.log`、`pattern/association.log`；
- `bfs/exit`、`bfs/check.log`、`cleanup.log`。

失败时另保留对应 console.log、summary.tsv/run.tsv、Store Trace/buffer stats、
stats.before/after 和相关逐页文件，以便定位。

## 已完成的本地检查（实现阶段记录）

以下是 Patch 8 实现阶段的本地结果，不是上述硬件流程的执行结果：

- 用户态工具与 Client 1.8 编译通过，无新增告警。
- 整组 42 项无特权回归通过（原有 31 项＋新增 11 项）。
- 生产采样 fixture 的 ASan/UBSan 检查通过，LeakSanitizer 因环境限制关闭。
- `git diff --check` 通过。

需要重新运行本地检查时，可在仓库根目录复制：

```bash
make -C remoteswap/tools
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
git diff --check
```

本地回归覆盖窗口/未知、PFN/VMA 变化、别名、权限错误、启动/exec/重试、
真实 C 输出与 Python 分析器兼容性，以及既有 Store 完成/失败/页面锁契约。
本地结果不能替代真实 Idle Tracking、HCA 和 BFS 验收。

## 2026-11-22 实机核心验收结果

本轮三项核心检查通过，Client 侧已正常收尾。Client 1.8、协议/stats v2，
双 Server 各 DRAM 16 GiB＋PMem 32 GiB，总池 96 GiB；沿用已有内核及配置。

| 项目 | 实际结果 |
| --- | --- |
| 独立冷热识别 | 第一阶段第一/第三段平均热度为 1.0/0.0；交换后为 0.0/约 0.999817。两线程不重复计数，逐页热度、均值及未知编码检查通过 |
| 页面内容与生命周期 | 4 GiB 工作集、2 GiB 物理限制，两轮各 1048576 页逐页/逐字校验通过；第二次 READY 期间成功 swapoff，随后最终内容校验通过，退出码 0 |
| Store 关联 | 采集 522162 条已接受提交，全部在先前已完成快照中匹配到已知热度；本轮均为 matched_cold。平均快照年龄约 1002.242 ms，最大约 1982.052 ms |
| Trace | 48 个 CPU 的 overrun/commit overrun/dropped events 检查通过，无丢失标记 |
| 原有 I/O | 页面测试及 BFS 的四个 Server/Tier 均有成功读写；WC、提交及 unrouted 错误增量为 0 |
| BFS 摘要集成 | 64G 图、40 线程、24 GiB 限制、80 GiB swap、1 个计时轮次正常完成，退出码 0；Warmup/observe 和已知热度/均值检查通过 |
| 正常收尾 | 无活动 swap，Client 已卸载，测试 cgroup 和 Trace instance 已移除，配置文件未改；本轮新增内核错误检查为空 |

匹配比例 100% **只描述该采集窗口内的近似 PFN 关联**，不是页面 owner 的准确率，
也不是 BFS 匹配率。本次 Store 样本全为冷页，不据此声称覆盖了热页 Store；
冷热识别及切换由第一项独立程序验证。BFS 本轮按计划只保存摘要，不做逐页关联。

BFS 共 24 轮采样，其中 20 轮 `overrun=1`，最大 `scan_ms` 约 **17559.299**。
因此配置周期 1 秒不代表实际按秒完成，20 轮窗口也不等于固定 20 秒。
大工作集阶段已知历史页数有明显波动，曾低至 49 页，后续达到约 985.5 万页；
均值只代表当时已知子集，不声称完整覆盖全部 BFS 页面，不作性能或监控开销达标结论。
本轮没有为这些观测另加性能门槛，也没有为改善数字修改采样代码。

实际日志位置（位于本机 `/tmp`，未纳入 Git）：

- 冷热识别：`/tmp/hermit-p8-core/cold/check.log`。
- 有效页面验收：`/tmp/hermit-p8-core/auto-20261122-213557/pattern/`，
  包括 `console.log`、`exit`、`check.log`、`association.log` 和 stats 快照。
- Trace、逐条关联与续测日志：`/tmp/hermit-p8-core/auto-20261122-213557/`，
  包括 `store.log`、`trace-buffer.stats`、`matches.tsv`、`continue.log`、`SUCCESS`。
- BFS：`/tmp/hermit-p8-core/bfs/`；全局收尾：`cleanup.log`、`continuation-result.json`、`dmesg.final`。

首次手动启动的页面程序长期停在第一个 READY。自动接管终端输入未奏效，
故保留其日志并正常中止（原 `pattern/exit` 为主动停止的非零状态，不作为验收结果）。
随后用同一页面程序和监控参数重新运行，通过控制器持有的 stdin 管道准确发送两次 ENTER；
第一次在 Trace 开启后，第二次在活进程 swapoff 成功后。
有效验收以以上 `auto-20261122-213557/pattern/exit` 的 `pattern_exit=0` 为准。
