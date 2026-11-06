# Patch 4：BFS 单节点／双节点最小功能测试

只跑两轮，不比较性能、不注入断连、不测试 cgroup swap 记账。
每轮验证：**BFS 正常跑完、参与节点有成功 RDMA 读写、请求计数正确显示、有带宽和延迟样本。**
本文件仅是待操作者执行的方案，不代表这些硬件测试已经执行。

| 项目 | 单节点 | 双节点 |
| --- | --- | --- |
| Server 0：10.0.0.13:9400 | 48 GiB | 48 GiB |
| Server 1：10.0.0.20:9400 | 不使用 | 32 GiB |
| Client `POOL_GIB` | 48 | 80 |
| 启用的 swapfile | `swap_48g.img` | `swap_80g.img` |
| BFS | 现有脚本：64G 图、40 线程、8 轮 | 完全相同 |
| cgroup 物理限制 | 24 GiB | 24 GiB |
| memsw 上限 | 无限 | 无限 |

swapfile 位于 `/mnt/sdb4/yxr/projects/swapfile/`，假定均已初始化为合法 swap。
本文不创建、截断或格式化文件；32／64 GiB 文件本轮不使用。
Server 命令最后的 **48 是 Client 在线 CPU 数，不是 BFS 的 40 个线程**。

使用 Client 三个终端：**A** 配置、运行 BFS、收尾；**B** 查看带宽；**C** 采集延迟。
先完整完成单节点并卸载，再做双节点。Server 必须保持在线直至 Client swapoff／卸载完成。
出现 Killed、SIGBUS、非零 BFS 退出、WC／提交错误、新的内核 WARNING／BUG 或持续卡住时，
停止后续步骤并保存输出，不强制卸载、不重启 Server 来尝试恢复。

## 1. 一次性准备（Client 终端 A）

**目标／内容：**确认正确版本和 workload；只用物理内存限制制造换页，关闭其他优化。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
uname -a
grep -w frontswap_register_managed_ops_v2 /proc/kallsyms
modinfo -F version remoteswap/client/rswap-client.ko
getconf _NPROCESSORS_ONLN
cat /sys/devices/system/cpu/online
swapon --show
bash remoteswap/client/manage_rswap_client.sh status
cat /mnt/sdb2/yxr/K_R_benchmarks/ligra/scripts/cgroup_run_bfs.sh
```

**预期：**存在 v2 符号，待加载模块版本为 **1.4**，CPU 为 48 个、编号 0–47；
没有活动 swap、Client 未加载。BFS 脚本实际为 `mem_limit_gb=24`、`-rounds 8`、
`rMat_sym_64G.adj`。注释可能过时，以实际命令为准，不修改你的 BFS 脚本。
若已有活动 swap／模块，先按原来的正常流程停用、卸载，再开始本方案。

```bash
bash -e <<'PREP'
mkdir -p /tmp/hermit-p4-bfs/settings
cp remoteswap/client/rswap_client.conf /tmp/hermit-p4-bfs/settings/rswap_client.conf.before
sudo dmesg > /tmp/hermit-p4-bfs/settings/dmesg.before

# 不修复或验证 swap 记账，仅解除有限 memsw 配额。
sudo mkdir -p /sys/fs/cgroup/memory/hermit-test
echo -1 | sudo tee /sys/fs/cgroup/memory/hermit-test/memory.memsw.limit_in_bytes
echo 24G | sudo tee /sys/fs/cgroup/memory/hermit-test/memory.limit_in_bytes

# 保存原值并关闭 THP，保证本轮使用 4 KiB 页面。
sed -n 's/.*\[\([^]]*\)\].*/\1/p' /sys/kernel/mm/transparent_hugepage/enabled > /tmp/hermit-p4-bfs/settings/thp.before
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    sudo cat "/sys/kernel/debug/hermit/$flag" > "/tmp/hermit-p4-bfs/settings/$flag.before"
    echo N | sudo tee "/sys/kernel/debug/hermit/$flag"
done
cat /sys/kernel/mm/swap/readahead_win > /tmp/hermit-p4-bfs/settings/readahead_win.before
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win

# 保存采集设置，临时关闭之前触发格式告警的 mlx5_fw 事件。
sudo cat /sys/kernel/debug/tracing/tracing_on > /tmp/hermit-p4-bfs/settings/tracing_on.before
sudo cat /sys/kernel/debug/tracing/options/trace_printk > /tmp/hermit-p4-bfs/settings/trace_printk.before
if sudo test -f /sys/kernel/debug/tracing/events/mlx5/mlx5_fw/enable; then
    sudo cat /sys/kernel/debug/tracing/events/mlx5/mlx5_fw/enable > /tmp/hermit-p4-bfs/settings/mlx5_fw.before
    echo 0 | sudo tee /sys/kernel/debug/tracing/events/mlx5/mlx5_fw/enable
fi
PREP
```

**预期：**所有设置成功，THP 为 `never`，基线优化均关闭。不要读取 cgroup swap 统计作为验收依据。
此处调整了全局 THP／tracing，使用独占测试窗口；第 6 节恢复。
本节只执行一次；重测整个方案前另存旧目录，避免覆盖原值备份。

## 2. 单节点：连接 48 GiB 池并启用 48 GiB swap

**目标／内容：**只连接 Server 0，确认实际容量及活动 swap 匹配。

在 **Server 0 自己的 `remoteswap/server` 目录**启动新进程：

```bash
./rswap-server 10.0.0.13 9400 48 48
```

等待预分配完成，保留 Server 输出。Client **A** 执行：

```bash
bash -e <<'SINGLE'
cd /mnt/sdb3/data/yxr/projects/hermit
cat > /tmp/hermit-p4-bfs/current.env <<'ENV'
P4_RUN=/tmp/hermit-p4-bfs/single
P4_SWAP=/mnt/sdb4/yxr/projects/swapfile/swap_48g.img
P4_SERVERS=1
ENV
source /tmp/hermit-p4-bfs/current.env
mkdir -p "$P4_RUN"
cat > remoteswap/client/rswap_client.conf <<'CONF'
NR_SERVERS=1
POOL_GIB=48
CONNECT_TIMEOUT_MS=60000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"
SERVER1_PORT=9400
CONF
sudo swaplabel "$P4_SWAP"
sudo chmod 600 "$P4_SWAP"
bash remoteswap/client/manage_rswap_client.sh install
cat /sys/module/rswap_client/version
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg | grep 'rswap:' | tail -10
sudo swapon "$P4_SWAP"
swapon --show --bytes --output NAME,SIZE,USED
bash remoteswap/client/manage_rswap_client.sh stats
SINGLE
```

**预期：**swaplabel 能读到合法 swap 头；模块 1.4；仅 Server 0，容量 48 GiB／6 Regions；
总池 48 GiB。swapon 成功，列表只有 `swap_48g.img`，可用容量因头页略小于 48 GiB。
统计文件只有 Server 0 的三类操作。任一命令失败就停止，不继续跑 BFS。

**接下来执行第 3 节公共测试，再执行第 4 节收尾。**

## 3. 公共测试：看带宽／延迟，运行 BFS，检查计数

本节在单节点、双节点各执行一次。每个终端都显式读取 `current.env`，
避免变量为空写到 `/bandwidth.log` 等错误位置。切换拓扑前必须先停止旧监控。

### 3.1 终端 B：启动每秒带宽监控

**目标／内容：**显示各 Server 成功 RDMA 页数据的逻辑带宽及区间错误次数。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p4-bfs/current.env
echo "本轮日志：${P4_RUN:?}"
set -o pipefail
bash remoteswap/client/manage_rswap_client.sh monitor 1 2>&1 | tee "$P4_RUN/bandwidth.log"
```

**预期：**先显示基线建立，再每秒输出一组。空闲时带宽 0 正常；有真实换页时应出现非零值。
单节点一行，双节点两行；`wc_error_delta`、`submit_error_delta` 和 `unrouted_error_delta`
均应保持 0。B 会持续运行，先不按 Ctrl-C。

### 3.2 终端 C：采集少量本轮 RDMA 延迟

**目标／内容：**看到目标 Server、请求类型和单请求延迟；不做全量日志或延迟性能评测。
本命令清除旧全局 trace，所以必须使用没有其他 tracing 使用者的测试窗口。

```bash
source /tmp/hermit-p4-bfs/current.env
echo "本轮日志：${P4_RUN:?}，节点数：${P4_SERVERS:?}"
sudo bash -ceu '
    echo 0 > /sys/kernel/debug/tracing/tracing_on
    : > /sys/kernel/debug/tracing/trace
    echo 1 > /sys/kernel/debug/tracing/options/trace_printk
    echo 1 > /sys/kernel/debug/tracing/tracing_on
'
set -o pipefail
sudo awk -v servers="$P4_SERVERS" '
/fs_rdma_callback:/ && match($0, /server: [01] type: [012] lat: [0-9]+/) {
    split(substr($0, RSTART, RLENGTH), field, " ")
    server = field[2] + 0; type = field[4] + 0
    if (server >= servers) next
    key = server ":" type
    if (samples[key]++ < 16) { print; fflush() }
    done = 1
    for (s = 0; s < servers; s++)
        if (samples[s ":0"] < 16 || samples[s ":1"] < 16) done = 0
    if (done) exit
}' /sys/kernel/debug/tracing/trace_pipe | tee "$P4_RUN/latency.log"
```

**预期：**C 在尚无 I/O 时等待。看到的记录例如：

```text
... fs_rdma_callback: server: 0 type: 0 lat: 6
... fs_rdma_callback: server: 0 type: 1 lat: 8
```

`type=0` 为 Write、`type=1` 为同步 Read、`type=2` 为异步 Read，`lat` 单位为微秒。
该延迟从提交前到完成回调中测得，包含 polling 等等待，不是纯链路 RTT。
每节点／类型最多保存 16 条；各参与节点的 Write／同步 Read 都收满后自动退出。
基线不要求 type=2 非零。文件应创建在本轮目录，不能出现 Permission denied。

### 3.3 终端 A：原样运行你现有的 BFS 脚本

**目标／内容：**在 24 GiB 物理限制下，运行 64G 图、40 线程、8 轮，覆盖整个进程的换页。
包括读图／解析阶段，不要求 BFS 计算阶段每秒持续产生 RDMA。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p4-bfs/current.env
bash remoteswap/client/manage_rswap_client.sh stats > "$P4_RUN/stats.before"
swapon --show --bytes --output NAME,SIZE,USED
set -o pipefail
bash /mnt/sdb2/yxr/K_R_benchmarks/ligra/scripts/cgroup_run_bfs.sh 2>&1 | tee "$P4_RUN/bfs.log"
P4_RC=$?
printf 'bfs_exit=%s\n' "$P4_RC" | tee "$P4_RUN/bfs.exit"
bash remoteswap/client/manage_rswap_client.sh stats > "$P4_RUN/stats.after"
sudo dmesg > "$P4_RUN/dmesg.after"
grep -c 'Running time' "$P4_RUN/bfs.log"
```

**预期：**脚本显示 `Memory Limit: 24GB`，最终 `bfs_exit=0`，有 **8 条** `Running time`。
不出现 Killed、SIGBUS 或其他错误。此处只确认 BFS 正常完成，不扩展 BFS 算法结果校验。
原脚本和图文件均不修改；不要用另一个小图替代这轮测试。

### 3.4 停止监控并核对结果（终端 A）

**目标／内容：**证明本轮参与的节点确有成功读写，且没有用失败／SSD 回退掩盖结果。

BFS 结束后，在 **B** 按 Ctrl-C 停止带宽监控。C 若已自动退出，不再操作；
若仍等待，则只在 **C** 按 Ctrl-C 停止，并检查是否缺少读写样本。
停止监控的 Ctrl-C 不等于 BFS 失败，BFS 以 `bfs.exit` 为准。

```bash
source /tmp/hermit-p4-bfs/current.env
cat "$P4_RUN/bfs.exit"
cat "$P4_RUN/stats.before"
cat "$P4_RUN/stats.after"
for ((server=0; server<P4_SERVERS; server++)); do
    for type in 0 1; do
        printf 'server=%s type=%s latency_samples=' "$server" "$type"
        grep -c "server: $server type: $type " "$P4_RUN/latency.log"
    done
done
```

**预期：**每个参与节点的 `write/ok`、`read_sync/ok` 相比 before 增长；
`wc_error`、`submit_error`、`unrouted_error` 均没有增长。每节点 type 0/1 的延迟样本
大于 0，通常均为 16。`read_async` 为 0 可以通过，不额外开启预读。

可用下面的同一条命令自动检查本轮计数差值：

```bash
source /tmp/hermit-p4-bfs/current.env
python3 - "$P4_RUN" "$P4_SERVERS" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, '/mnt/sdb3/data/yxr/projects/hermit/remoteswap/tools')
from rswap_stats import read_snapshot
run, servers = Path(sys.argv[1]), int(sys.argv[2])
before = read_snapshot(run / 'stats.before')
after = read_snapshot(run / 'stats.after')
assert before.instance_ns == after.instance_ns, '本轮模块被重载，统计不可比较'
assert set(after.rows) == {(s, op) for s in range(servers) for op in ('write', 'read_sync', 'read_async')}, '节点数不匹配'
assert before.rows.keys() == after.rows.keys(), '统计布局改变'
for key in sorted(after.rows):
    delta = tuple(a - b for a, b in zip(after.rows[key], before.rows[key]))
    print('server={} op={} ok_delta={} wc_error_delta={} submit_error_delta={}'.format(*key, *delta))
    assert min(delta) >= 0, '计数倒退'
    assert delta[1:] == (0, 0), '本轮出现 RDMA 错误'
    if key[1] in ('write', 'read_sync'):
        assert delta[0] > 0, '本轮未确认该节点的正常读写'
assert after.unrouted_error == before.unrouted_error, '本轮出现未归属提交失败'
print('PASS: 参与节点均有成功 Write/同步 Read，所有错误计数差值为 0')
PY
```

带宽日志在 `$P4_RUN/bandwidth.log`，延迟日志在 `$P4_RUN/latency.log`。
计数／带宽覆盖该 Client 在测量窗口内的所有 RDMA 数据请求，不按 BFS PID 单独归属；
回调可能由 kworker 执行，这不表示请求不是 BFS 引发的。
延迟是少量样本，不用于计算全局平均值／P99。样本缺失或成功计数不增长时，记录为未验证，
不能仅凭 BFS 退出 0 判定远端路径通过。检查 dmesg 时只看本轮新增异常，不把旧告警重复算入。

## 4. 每轮收尾：先 swapoff，再卸载

**目标／内容：**正常停止内存池访问，避免在还有远端页面时停止 Server。
前提是 BFS 已结束，B／C 均已停止，统计已保存。

```bash
bash -e <<'CLEANUP'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p4-bfs/current.env
sudo swapoff "$P4_SWAP"
swapon --show
bash remoteswap/client/manage_rswap_client.sh uninstall
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg > "$P4_RUN/dmesg.final"
CLEANUP
```

**预期：**swapoff、卸载成功，swap 列表为空、模块未加载。swapoff 失败或卡住时停止，
不要继续卸载，也不使用 `rmmod -f`。Server 通常随最后连接释放而退出。
下一轮必须启动新的 Server 进程。

## 5. 双节点：48＋32 GiB 池，80 GiB swap

**目标／内容：**同一个 BFS 在两个 Server 上正常换页，两节点的读写、带宽和延迟均可见。
先确认单节点已完成第 4 节；本轮不要求双节点比单节点更快。

两端各自在自己的 `remoteswap/server` 目录启动新进程：

```bash
# 只在 Server 0 执行
./rswap-server 10.0.0.13 9400 48 48
```

```bash
# 只在 Server 1 执行
./rswap-server 10.0.0.20 9400 32 48
```

Client **A** 执行：

```bash
bash -e <<'DUAL'
cd /mnt/sdb3/data/yxr/projects/hermit
cat > /tmp/hermit-p4-bfs/current.env <<'ENV'
P4_RUN=/tmp/hermit-p4-bfs/dual
P4_SWAP=/mnt/sdb4/yxr/projects/swapfile/swap_80g.img
P4_SERVERS=2
ENV
source /tmp/hermit-p4-bfs/current.env
mkdir -p "$P4_RUN"
sed -i -e 's/^NR_SERVERS=.*/NR_SERVERS=2/' -e 's/^POOL_GIB=.*/POOL_GIB=80/' remoteswap/client/rswap_client.conf
cat remoteswap/client/rswap_client.conf
sudo swaplabel "$P4_SWAP"
sudo chmod 600 "$P4_SWAP"
bash remoteswap/client/manage_rswap_client.sh install
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg | grep 'rswap:' | tail -12
sudo swapon "$P4_SWAP"
swapon --show --bytes --output NAME,SIZE,USED
bash remoteswap/client/manage_rswap_client.sh stats
DUAL
```

**预期：**Server 0 为 48 GiB／6 Regions，Server 1 为 32 GiB／4 Regions，总池 80 GiB；
只启用 `swap_80g.img`，统计中出现 Server 0 和 Server 1 的三类操作。

**然后原样重复第 3 节全部步骤，最后执行第 4 节收尾。**
B／C 必须重新执行各自命令中的 `source current.env`，确认日志路径显示 `/tmp/hermit-p4-bfs/dual`。
本轮日志独立于 single。验收标准不变，但两个节点都必须有成功 Write／同步 Read 和延迟样本。
两节点带宽／次数不要求相等，80 GiB swap 被启用也不表示整个尾段已经实际访问。

## 6. 两轮结束后恢复与反馈

**目标／内容：**恢复配置和全局测试设置，保留结果。先确认模块已卸载、没有活动测试 swap。

```bash
bash -e <<'RESTORE'
cd /mnt/sdb3/data/yxr/projects/hermit
cp /tmp/hermit-p4-bfs/settings/rswap_client.conf.before remoteswap/client/rswap_client.conf
sudo tee /sys/kernel/mm/transparent_hugepage/enabled < /tmp/hermit-p4-bfs/settings/thp.before
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    sudo tee "/sys/kernel/debug/hermit/$flag" < "/tmp/hermit-p4-bfs/settings/$flag.before"
done
sudo tee /sys/kernel/mm/swap/readahead_win < /tmp/hermit-p4-bfs/settings/readahead_win.before
sudo tee /sys/kernel/debug/tracing/options/trace_printk < /tmp/hermit-p4-bfs/settings/trace_printk.before
if [ -f /tmp/hermit-p4-bfs/settings/mlx5_fw.before ]; then
    sudo tee /sys/kernel/debug/tracing/events/mlx5/mlx5_fw/enable < /tmp/hermit-p4-bfs/settings/mlx5_fw.before
fi
sudo tee /sys/kernel/debug/tracing/tracing_on < /tmp/hermit-p4-bfs/settings/tracing_on.before
RESTORE
```

cgroup 的 swap 计数可能仍残留；不清零、不修复、不据此判定本轮失败，也不恢复有限 memsw 上限。
若测试前有原 SSD swap，仅在 Client 已卸载后恢复那个原 area，不擅自启用其他文件。

反馈只需：单／双节点各自的 `bfs.exit`、8 轮是否完成、计数差值检查结果、
几行带宽和延迟样本、swapoff／卸载结果。有异常附本轮新增 dmesg。
所有日志保存在 `/tmp/hermit-p4-bfs/single/` 和 `/tmp/hermit-p4-bfs/dual/`。
