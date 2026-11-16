# Patch 6 核心硬件测试：单节点、双节点各一轮

只验证四件事：**v2 连接发现成功、确实发生远端换页、页面内容正确、swapoff／卸载成功。**
不做故障注入、性能对比、BFS、异容量、全 Chunk／尾段扫描或 TB 级压力测试。
本文件是待执行流程，不表示硬件测试已通过；小工作集不证明每个 Tier 的实际访问分布，
逐 Tier 观察留到 Patch 7。

| 轮次 | Server | 每台 DRAM／PMem | Client 总池 | 使用已有 swapfile |
| --- | --- | --- | --- | --- |
| single | 10.0.0.20 | 16／32 GiB | 48 GiB | swap_48g.img |
| dual | 10.0.0.13、10.0.0.20 | 16／32 GiB | 96 GiB | swap_80g.img |

swapfile 沿用此前路径 `/mnt/sdb4/yxr/projects/swapfile/`，必须已经初始化。
96 GiB 池可以配现有 80 GiB swap，不需要新建 96 GiB 文件。
流程不创建、格式化或截断 swapfile。只有一个活动 swap area。

Client 使用两个终端：**A 跑校验程序，B 做准备、观察和收尾**。
Server 保持在线直至 Client swapoff／卸载结束。任何步骤失败、数据 FAIL、Killed、
出现 RDMA／内核错误或持续卡住时，停止并保存日志，不继续下一轮或强制卸载。

## 1. Client B：一次性准备

先确认无活动 swap、旧 Client 未加载；若仍在使用，先按原来的正常流程停用。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
swapon --show
bash remoteswap/client/manage_rswap_client.sh status
modinfo -F version remoteswap/client/rswap-client.ko
getconf _NPROCESSORS_ONLN
cat /sys/devices/system/cpu/online
sudo cat /proc/kallsyms | rg -w frontswap_register_managed_ops_v2
```

预期无活动 swap、模块未加载、待加载版本 **1.6**、存在 v2 内核符号。
Server 的 Client 核数参数必须与这里一致，目前为 48／0–47。
不需要重新安装内核。

下面准备只执行一次；重测整套流程时先另存旧日志目录，避免覆盖原设置备份。
使用此前验证的 cgroup-v1 环境；目录若不可写，停止反馈，不重挂载。

```bash
bash -e <<'PREP'
cd /mnt/sdb3/data/yxr/projects/hermit
test ! -e /tmp/hermit-p6-core
mkdir -p /tmp/hermit-p6-core/settings
cp remoteswap/client/rswap_client.conf /tmp/hermit-p6-core/settings/client.conf.before
gcc -std=gnu11 -O2 -Wall -Wextra -Werror remoteswap/tests/swap_pattern.c -o /tmp/hermit-p6-core/swap_pattern

sudo mkdir /sys/fs/cgroup/memory/hermit-p6-core
echo -1 | sudo tee /sys/fs/cgroup/memory/hermit-p6-core/memory.memsw.limit_in_bytes
echo 100 | sudo tee /sys/fs/cgroup/memory/hermit-p6-core/memory.swappiness

for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    sudo cat "/sys/kernel/debug/hermit/$flag" > "/tmp/hermit-p6-core/settings/$flag.before"
    echo N | sudo tee "/sys/kernel/debug/hermit/$flag"
done
cat /sys/kernel/mm/swap/readahead_win > /tmp/hermit-p6-core/settings/readahead_win.before
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
PREP
```

只使用物理内存限制制造压力，不测试已知存在问题的 cgroup swap 记账。
校验程序自行禁用映射的 THP；无需改全局 THP 或采集 ftrace。

## 2. 第一轮：单 Server

**在 10.0.0.20 上：**检查 `remoteswap/server/run_server.sh`，IP 为本机地址、
Client 核数为上一步结果、DRAM／PMem Node 使用已验证值、容量为 16／32 GiB。
在 Server 的 `remoteswap/server` 目录启动新进程：

```bash
bash run_server.sh
```

等到 `listening on ...`，保持进程运行。它尚在等待 Client 连接。

**Client B：**配置单节点，并记录本轮路径。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
cat > /tmp/hermit-p6-core/current.env <<'ENV'
P6_RUN=/tmp/hermit-p6-core/single
P6_SWAP=/mnt/sdb4/yxr/projects/swapfile/swap_48g.img
P6_CG=/sys/fs/cgroup/memory/hermit-p6-core
ENV
cat > remoteswap/client/rswap_client.conf <<'CONF'
NR_SERVERS=1
POOL_GIB=48
CONNECT_TIMEOUT_MS=60000
SERVER0_IP="10.0.0.20"
SERVER0_PORT=9400
CONF
```

**然后完整执行第 3 节公共测试。完成后才进入双节点。**

## 3. 公共测试：两轮各执行一次

### 3.1 Client B：加载、检查发现结果、启用 swap

```bash
bash -e <<'CONNECT'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p6-core/current.env
mkdir -p "$P6_RUN"
sudo dmesg > "$P6_RUN/dmesg.before"
sudo swaplabel "$P6_SWAP"
echo 2147483648 | sudo tee "$P6_CG/memory.limit_in_bytes"
bash remoteswap/client/manage_rswap_client.sh install
cat /sys/module/rswap_client/version
sudo dmesg | tail -30
CONNECT
```

先检查输出，再执行下面的 swapon：

- 模块版本为 1.6；每台为 `protocol=2`、`capacity=48 GiB regions=6`，
  `DRAM=16 GiB/2 chunks PMem=32 GiB/4 chunks`。
- single 只有 server=0，总量 48 GiB；dual 有 server=0/1，总量 96 GiB。
- 有 `two-level page interleave` 和 `client ready` 日志。
  **此时已经完成 QUERY／REQUEST_CHUNKS，不是仅 MR 注册成功。**

```bash
bash -e <<'ACTIVATE'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p6-core/current.env
sudo swapon "$P6_SWAP"
swapon --show
bash remoteswap/client/manage_rswap_client.sh stats > "$P6_RUN/stats.before"
ACTIVATE
```

### 3.2 Client A：4 GiB 工作集、两轮内容校验

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p6-core/current.env
set -o pipefail
sudo bash -c '
echo $$ > "$1/cgroup.procs"
exec /tmp/hermit-p6-core/swap_pattern 4096 2
' bash "$P6_CG" | tee "$P6_RUN/pattern.log"
echo "workload_exit=$?"
```

看到 `READY generation=0` 后，先不要按 ENTER。

**Client B：**检查 `swapon --show` 的 Used 明显增长，通常为 GiB 量级，
并查看 `bash remoteswap/client/manage_rswap_client.sh stats`，成功 Write 应增长。
只有 READY、没有实际 swap／RDMA 写入，不能算通过。

**Client A：**按一次 ENTER，等到 `VERIFIED generation=0` 和 `READY generation=1`。
再次暂停，保留这个进程，先做下面的活进程 swapoff。

### 3.3 Client B：活进程 swapoff、检查统计

先运行 `free -h`，确认宿主机有余量容纳整个 4 GiB 工作集，再执行：

```bash
bash -e <<'SWAPOFF'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p6-core/current.env
echo -1 | sudo tee "$P6_CG/memory.limit_in_bytes"
sudo swapoff "$P6_SWAP"
swapon --show
bash remoteswap/client/manage_rswap_client.sh stats > "$P6_RUN/stats.after"
cat "$P6_RUN/stats.after"
SWAPOFF
```

预期 swapoff 成功、无活动 swap；与 stats.before 相比，**每个参与 Server 的成功 Write
及 Read（同步＋异步）均有增长，所有 wc_error、submit_error、unrouted_error 增量为 0**。
不要求异步 Read 必须出现，也不采集带宽曲线。

**Client A：**再按一次 ENTER，必须得到 `VERIFIED generation=1`、最终 `PASS`、
`workload_exit=0`。这同时验证 swapoff 读回后内容仍正确。

### 3.4 Client B：卸载

确认 A 已 PASS，再执行：

```bash
bash -e <<'UNLOAD'
cd /mnt/sdb3/data/yxr/projects/hermit
source /tmp/hermit-p6-core/current.env
bash remoteswap/client/manage_rswap_client.sh uninstall
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg > "$P6_RUN/dmesg.after"
UNLOAD
```

预期模块已卸载，本轮没有新增相关 WARNING／BUG／RDMA 错误。
单节点完成后再进入下一节；Server 通常会随连接释放退出，下一轮必须启动新进程。

## 4. 第二轮：双 Server

在 **10.0.0.13 和 10.0.0.20** 上各自检查 `run_server.sh`，使用各自 IP、已验证 Node，
容量均为 16／32 GiB、Client 核数一致。注意脚本默认 IP 是 10.0.0.20，
不能在 10.0.0.13 上直接沿用这个地址。分别启动新 Server，等到 listening。

**Client B：**切换本轮配置，随后重复第 3 节。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
cat > /tmp/hermit-p6-core/current.env <<'ENV'
P6_RUN=/tmp/hermit-p6-core/dual
P6_SWAP=/mnt/sdb4/yxr/projects/swapfile/swap_80g.img
P6_CG=/sys/fs/cgroup/memory/hermit-p6-core
ENV
cat > remoteswap/client/rswap_client.conf <<'CONF'
NR_SERVERS=2
POOL_GIB=96
CONNECT_TIMEOUT_MS=60000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"
SERVER1_PORT=9400
CONF
```

## 5. 两轮结束后恢复设置

确认无活动 swap、模块已卸载、校验进程已退出，恢复本次修改的设置：

```bash
bash -e <<'RESTORE'
cd /mnt/sdb3/data/yxr/projects/hermit
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    sudo tee "/sys/kernel/debug/hermit/$flag" < "/tmp/hermit-p6-core/settings/$flag.before"
done
sudo tee /sys/kernel/mm/swap/readahead_win < /tmp/hermit-p6-core/settings/readahead_win.before
cp /tmp/hermit-p6-core/settings/client.conf.before remoteswap/client/rswap_client.conf
sudo rmdir /sys/fs/cgroup/memory/hermit-p6-core
RESTORE
```

每轮只需回传：**Client 发现日志、pattern.log、stats.before／stats.after，以及是否
swapoff／卸载成功**；出现异常时补充对应 dmesg 和 Server 输出。
