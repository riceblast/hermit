# Patch 7 可复制测试：双节点核心验收＋64G BFS

仅两项 workload：**4 GiB 页面校验（含 Trace）＋64G 图 BFS 正常完成**。
不测性能、故障、异容量或全池边界。本文件是待执行方案，不代表已验收。

两台 Server 各 DRAM 16＋PMem 32 GiB，Client 池 96 GiB，模块版本 1.7。
沿用已验证 Hermit 开关，不修改其配置、全局 THP 或原 BFS 脚本。
仅设置测试 cgroup 的物理内存／memsw 限制、启停选定 swap、操作独立 ftrace instance。
下面所有 sudo 命令均由操作者在宿主机执行。

使用三个 Client 终端：**A 配置和运行 workload，B 持续看带宽，C 查看 swap、采 Trace、做活进程 swapoff**。
每个步骤标明终端；不要把所有段落不分终端地连续执行。
任何步骤失败、Killed、校验 FAIL、RDMA 错误、新的内核 BUG/WARN 或持续卡住，
停止后续步骤并保存日志；不强制卸载或提前停止 Server。

## 1. 两台 Server 启动，Client A 准备

两台各自在 `remoteswap/server` 目录核对已验证的 `run_server.sh` 配置，再运行：

```bash
bash run_server.sh
```

分别使用本机 IP 10.0.0.13／10.0.0.20、各自已验证 NUMA Node、16／32 GiB 容量。
Client 核数目前为 48（编号 0–47），不是 BFS 线程数；另一台不能沿用脚本默认的 10.0.0.20。
两台均等待 `listening on ...`。本轮重新连接前使用新 Server 进程。

**Client A：**从无活动 swap、旧模块已卸载开始；若还在使用，先按原流程正常收尾。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
swapon --show
bash remoteswap/client/manage_rswap_client.sh status
modinfo -F version remoteswap/client/rswap-client.ko
cat remoteswap/client/rswap_client.conf
cat /sys/devices/system/cpu/online
cat /sys/kernel/mm/transparent_hugepage/enabled
sudo cat /sys/kernel/debug/hermit/batch_account
```

预期无活动 swap、模块未加载、待加载版本 **1.7**、配置为两节点原 endpoint 顺序／96 GiB／60000 ms。
当前 THP 已为 `[never]`、batch_account 为 N，保持不变；不符合时先反馈，不执行本方案继续修改它们。

下面只做一次，`/tmp/hermit-p7` 必须不存在，以免覆盖旧日志／备份。

```bash
bash -e <<'PREP'
cd /mnt/sdb3/data/yxr/projects/hermit
test ! -e /tmp/hermit-p7
mkdir -p /tmp/hermit-p7/pattern /tmp/hermit-p7/bfs
cat > /tmp/hermit-p7/env <<'ENV'
P7_REPO=/mnt/sdb3/data/yxr/projects/hermit
P7_RUN=/tmp/hermit-p7
P7_CG=/sys/fs/cgroup/memory/hermit-p7-pattern
P7_TRACE=/sys/kernel/debug/tracing/instances/rswap_p7_core
P7_SWAP48=/mnt/sdb4/yxr/projects/swapfile/swap_48g.img
P7_SWAP80=/mnt/sdb4/yxr/projects/swapfile/swap_80g.img
ENV
source /tmp/hermit-p7/env
gcc -std=gnu11 -O2 -Wall -Wextra -Werror remoteswap/tests/swap_pattern.c -o "$P7_RUN/swap_pattern"
sudo mkdir "$P7_CG"
echo -1 | sudo tee "$P7_CG/memory.memsw.limit_in_bytes"
echo 2G | sudo tee "$P7_CG/memory.limit_in_bytes"
echo 100 | sudo tee "$P7_CG/memory.swappiness"
sudo swaplabel "$P7_SWAP48"
sudo swaplabel "$P7_SWAP80"
sudo dmesg > "$P7_RUN/dmesg.before"
bash remoteswap/client/manage_rswap_client.sh install
test "$(cat /sys/module/rswap_client/version)" = 1.7
sudo dmesg | tail -30
PREP
```

先确认每台 `protocol=2 capacity=48 GiB regions=6`、DRAM 2＋PMem 4 Chunks，
总池 96 GiB，`client ready`。**未成功不启用 swap**。然后 Client A 执行：

```bash
bash -e <<'ACTIVATE'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
sudo swapon "$P7_SWAP48"
sudo mkdir "$P7_TRACE"
echo 0 | sudo tee "$P7_TRACE/tracing_on"
echo 4096 | sudo tee "$P7_TRACE/buffer_size_kb"
sudo cat "$P7_TRACE/events/rswap/placement/enable"
bash remoteswap/client/manage_rswap_client.sh stats > "$P7_RUN/pattern/stats.before"
cat "$P7_RUN/pattern/stats.before"
ACTIVATE
```

预期 stats 为 version=2、12 行 Server/Tier/op 计数，Placement 事件为 0（关闭）。
swapfile 均沿用已有文件，不创建、不格式化；一个时刻只启用一个。

## 2. Client B：全程看带宽

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
set -o pipefail
bash remoteswap/client/manage_rswap_client.sh monitor 1 2>&1 | tee /tmp/hermit-p7/bandwidth.log
```

保持 B 运行至 BFS 结束。预期显示 0/1 的 dram、pmem、all 行；实际换页期间出现非零带宽，
all 为该 Server 两个 Tier 之和；所有错误增量为 0。空闲期间为 0 正常，不要求四个池严格等量。

## 3. Client A：页面校验（先不开 Trace）

```bash
source /tmp/hermit-p7/env
set -o pipefail
sudo bash -ec '
echo $$ > "$1/cgroup.procs"
exec "$2/swap_pattern" 4096 2
' bash "$P7_CG" "$P7_RUN" | tee "$P7_RUN/pattern/pattern.log"
P7_RC=$?
printf 'pattern_exit=%s\n' "$P7_RC" | tee "$P7_RUN/pattern/exit"
```

看到 **READY generation=0，先不按 ENTER**。
**Client C：**执行下面命令，预期 Used 增至 GiB 量级，四个池 Write 计数增长且错误为 0：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
swapon --show
bash remoteswap/client/manage_rswap_client.sh stats
```

## 4. Client C：采集 10 秒 Trace，Client A 同时开始读回

C 复制下面整段；出现 **Trace ON** 后，立即到 A 按一次 ENTER。
脚本只控制新建的 instance，10 秒后自动停止并关闭事件，不影响整个 workload。

```bash
source /tmp/hermit-p7/env
sudo bash -e -s -- "$P7_TRACE" "$P7_RUN/pattern" <<'CAPTURE'
trace=$1
out=$2
reader=
cleanup() {
    echo 0 > "$trace/tracing_on"
    echo 0 > "$trace/events/rswap/placement/enable"
    if [[ -n "$reader" ]]; then
        kill "$reader" 2>/dev/null || true
        wait "$reader" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cat "$trace/trace_pipe" > "$out/placement.log" &
reader=$!
echo 1 > "$trace/events/rswap/placement/enable"
echo 1 > "$trace/tracing_on"
echo 'Trace ON: now press ENTER in terminal A; capturing for 10 seconds.'
sleep 10
kill -0 "$reader"
echo 0 > "$trace/tracing_on"
echo 0 > "$trace/events/rswap/placement/enable"
sleep 1
# A drained trace_pipe may return EOF after tracing_on becomes 0.
if kill -0 "$reader" 2>/dev/null; then
    kill "$reader" 2>/dev/null || true
fi
set +e
wait "$reader"
reader_status=$?
set -e
reader=
[[ "$reader_status" -eq 0 || "$reader_status" -eq 143 ]]
for f in "$trace"/per_cpu/cpu*/stats; do
    printf '\n%s\n' "$f"
    cat "$f"
done > "$out/trace-buffer.stats"
echo 'Trace OFF; buffer statistics saved.'
CAPTURE
```

**Client A：**等到 `VERIFIED generation=0` 和 `READY generation=1`，再次暂停，不按 ENTER。
**Client C：**采集结束后核对：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
bash remoteswap/client/manage_rswap_client.sh trace-check "$P7_RUN/pattern/placement.log" 16:32,16:32
python3 - "$P7_RUN/pattern/trace-buffer.stats" <<'PY'
from pathlib import Path
import re
import sys
values = re.findall(r'^(?:overrun|commit overrun|dropped events):\s*(\d+)\s*$',
                    Path(sys.argv[1]).read_text(), re.M)
assert values and all(int(n) == 0 for n in values), 'Trace buffer loss; save logs and report'
print('PASS: trace buffer overrun/drop counters are zero')
PY
```

预期两个 PASS。无事件、记录错误位置或缓冲区丢失均不计通过；先反馈，不扩展故障测试。
短 Trace 窗口不与整场计数强行对账，也不宣称覆盖全部 Chunk／尾段。

## 5. Client C：活进程 swapoff；A 完成最终校验

先看 `free -h`，宿主机须有余量容纳 4 GiB 工作集。A 必须停在 READY generation=1。

```bash
bash -e <<'PATTERN_SWAPOFF'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
echo -1 | sudo tee "$P7_CG/memory.limit_in_bytes"
sudo swapoff "$P7_SWAP48"
swapon --show
PATTERN_SWAPOFF
```

预期无活动 swap。**A 再按一次 ENTER**，必须得到 `VERIFIED generation=1`、最终 PASS、
`pattern_exit=0`。然后 **A** 核对本阶段计数：

```bash
bash -e <<'CHECK_PATTERN'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
bash remoteswap/client/manage_rswap_client.sh stats > "$P7_RUN/pattern/stats.after"
python3 remoteswap/tests/check_patch7_stats.py "$P7_RUN/pattern/stats.before" "$P7_RUN/pattern/stats.after"
CHECK_PATTERN
```

预期四个池成功 Write／Read 均有增长、所有错误增量为 0；异步 Read 为 0 允许。
此时**不卸载 Client、不重启 Server**，同一组连接继续 BFS。

## 6. Client A：64G BFS，一次进程、1 个计时轮次

使用实际存在的 `rMat_sym_64G.adj`、40 线程、24 GiB 物理限制。
这里 64G 是已有图的规模配置，文本文件约 17 GiB，不要求文件本身为 64 GiB。
直接调用现有 BFS 二进制，只将 rounds 设为 1（Ligra 自带的预热仍保留）；
不修改原本跑 8 轮的脚本或图。
本节开启现有 80 GiB swap，仍小于 96 GiB 远端池。保持 Placement Trace 关闭、B 继续监控。

```bash
bash -e <<'BFS_PREP'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
test "$(wc -l < /proc/swaps)" -eq 1
test -x /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS
test -r /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_64G.adj
test "$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' /sys/kernel/mm/transparent_hugepage/enabled)" = never
sudo mkdir -p /sys/fs/cgroup/memory/hermit-test
test -z "$(cat /sys/fs/cgroup/memory/hermit-test/cgroup.procs)"
echo -1 | sudo tee /sys/fs/cgroup/memory/hermit-test/memory.memsw.limit_in_bytes
echo 24G | sudo tee /sys/fs/cgroup/memory/hermit-test/memory.limit_in_bytes
echo 100 | sudo tee /sys/fs/cgroup/memory/hermit-test/memory.swappiness
sudo swapon "$P7_SWAP80"
bash remoteswap/client/manage_rswap_client.sh stats > "$P7_RUN/bfs/stats.before"
BFS_PREP
```

下面整段独立复制到 **A**，运行期间 **C** 偶尔执行 `swapon --show`，确认 Used 增长。

```bash
source /tmp/hermit-p7/env
set -o pipefail
sudo bash -ec '
echo $$ > /sys/fs/cgroup/memory/hermit-test/cgroup.procs
OMP_NUM_THREADS=40 exec /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS -s -rounds 1 /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_64G.adj
' 2>&1 | tee "$P7_RUN/bfs/bfs.log"
P7_RC=$?
printf 'bfs_exit=%s\n' "$P7_RC" | tee "$P7_RUN/bfs/exit"
```

预期 `bfs_exit=0`、一条 `Running time`，没有 Killed／SIGBUS／报错。
只验证 BFS 正常完成，不做算法结果或性能对比；计数覆盖整个进程，包括读图／解析阶段。
若退出非零，停止，不继续收尾之外的后续验收。

成功后 **A** 执行：

```bash
bash -e <<'CHECK_BFS'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
test "$(cat "$P7_RUN/bfs/exit")" = bfs_exit=0
test "$(rg -c 'Running time' "$P7_RUN/bfs/bfs.log")" -eq 1
bash remoteswap/client/manage_rswap_client.sh stats > "$P7_RUN/bfs/stats.after"
python3 remoteswap/tests/check_patch7_stats.py "$P7_RUN/bfs/stats.before" "$P7_RUN/bfs/stats.after"
sudo dmesg > "$P7_RUN/dmesg.after-workloads"
CHECK_BFS
```

预期四个池有成功读写，错误增量为 0；B 日志中有非零带宽。
不要求计算阶段每秒换页，不要求 dram／pmem 数量相等。

## 7. 正常收尾

确认 workload 已结束，在 **B 按 Ctrl-C** 停止 monitor；Trace collector 应已自动结束。
**A** 执行：

```bash
bash -e <<'CLEANUP'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p7/env
echo 0 | sudo tee "$P7_TRACE/tracing_on"
echo 0 | sudo tee "$P7_TRACE/events/rswap/placement/enable"
sudo swapoff "$P7_SWAP80"
bash remoteswap/client/manage_rswap_client.sh uninstall
bash remoteswap/client/manage_rswap_client.sh status
sudo rmdir "$P7_TRACE"
sudo rmdir "$P7_CG"
sudo dmesg > "$P7_RUN/dmesg.final"
CLEANUP
```

预期无活动 swap、模块已卸载；Server 保持在线直至卸载完成，之后可停止。
原有 hermit-test cgroup 保留原来的 24 GiB／无限 memsw 设置；Hermit 开关、全局 THP
和配置文件没有被本方案修改。检查本轮新增 dmesg，不重复计算旧告警。

回传最小结果：pattern.log／exit、Trace 校验结果、两组 stats.before／after、
bfs.log／exit、bandwidth.log，以及 swapoff／卸载是否成功。日志均在 `/tmp/hermit-p7`。
