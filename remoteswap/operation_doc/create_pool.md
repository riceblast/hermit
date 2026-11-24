# 建池展示：四个操作问题

## 1. 如何验证管理工具能够连接多个混合内存池节点？

**client A**：以下命令均在 `/mnt/sdb3/data/yxr/projects/hermit` 下执行。前提是已完成第 2 节的 Client 构建，两台 Server 已初始化并显示 `listening`；Client 尚未加载且没有活动 swap。

以每台 **DRAM 16 GiB＋PMem 32 GiB** 为例，配置双节点总池 96 GiB：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
cat > remoteswap/client/rswap_client.conf <<'CONF'
NR_SERVERS=2
POOL_GIB=96
CONNECT_TIMEOUT_MS=60000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"
SERVER1_PORT=9400
CONF
bash remoteswap/client/manage_rswap_client.sh install
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg | tail -n 100
```

工具为 `manage_rswap_client.sh`：`install` 加载模块并连接配置中的全部节点，`status` 查看模块实际运行参数。配置文件是唯一配置来源，`POOL_GIB` 必须等于两台实际池容量之和。

验证时看三处：

- **client A 的 status**：`rswap_client: loaded`、`nr_servers=2`、`rmsize=96`，`sip=10.0.0.13`、`sip2=10.0.0.20`。
- **Client 本轮新增内核日志**：server 0、1 均出现 `ready protocol=2`，各 48 GiB；最后出现 `pool ready servers=2 total=96 GiB`。
- **node04 A、server02 A 的 Server 日志**：各出现 `all 6 data MRs registered; pool available`。

以上共同证明两个节点成功连接；任一节点连接失败或容量不一致时，不继续启用 swap。endpoint 顺序决定编号：**0=node04，1=server02**。

## 2. 如何构建 Client、启用 swap，结束后关闭 swap 并卸载 Client？

**client A：构建。** 使用已验证的 Hermit #8 内核及对应构建目录：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
modinfo -F version remoteswap/client/rswap-client.ko
```

产物是 `remoteswap/client/rswap-client.ko`，当前版本应为 `1.13`，默认是 RDMA 后端。若此前编译过 `BACKEND=DRAM`，先用相同 `KDIR` 执行 `make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" clean`，再构建。构建完成后执行第 1 节连接。

**client A：连接成功后启用 swap。** 双节点沿用已初始化的 80 GiB swap 文件：

```bash
sudo swapon /mnt/sdb4/yxr/projects/swapfile/swap_80g.img
swapon --show
```

`install` 不自动启用 swap，必须单独 `swapon`。单节点 48 GiB 池则使用既有 `swap_48g.img`，路径目录相同。只启用一个 swap area，容量不得超过总池；无需重新执行 `mkswap`。

双节点仍只显示一个 Linux swap area，由 Client 将两个远端节点组织为统一后端。

**client A：负载结束后关闭并卸载。** 两台 Server 始终保持在线：

```bash
sudo swapoff /mnt/sdb4/yxr/projects/swapfile/swap_80g.img
cat /proc/swaps
bash remoteswap/client/manage_rswap_client.sh mapping
```

单节点替换为对应的 `swap_48g.img`。确认 `swapoff` 成功、`/proc/swaps` 只剩标题、各池 `live=retired=0` 且 `free=capacity`。然后在 client B/C/D 停止正在运行的监控，再在 A 执行：

```bash
bash remoteswap/client/manage_rswap_client.sh uninstall
bash remoteswap/client/manage_rswap_client.sh status
```

最终应为 Client 未加载、无活动 swap，此时才可停止 Server。`swapoff` 失败时保留连接排查；重新连接 Client 前需重启参与的 Server 进程。

## 3. 如何查询多节点配置及各节点的异构细节？

**client A 或 D**，在 Client 仓库根目录执行：

```bash
bash remoteswap/client/manage_rswap_client.sh status
bash remoteswap/client/manage_rswap_client.sh pool-status
bash remoteswap/client/manage_rswap_client.sh mapping
```

| 命令 | 功能／重点字段 |
| --- | --- |
| `status` | 实际节点数量、总池容量、各节点 IP／端口 |
| `pool-status` | JSON 格式；`server_count`、总 `capacity_bytes`；`topology` 中每节点容量及 `dram_fraction`／`pmem_fraction`；`pools` 中每节点／Tier 的容量与占用 |
| `mapping` | 简明表格，分别列出 `0/1 × dram/pmem` 的 Slot 总数、占用和空闲 |

本文双节点配置应显示每台 48 GiB，其中 DRAM 16 GiB、PMem 32 GiB，总池 96 GiB。单节点则只有 server 0 的两个 Tier。

**server02 B、node04 B**：查询各自物理 NUMA 拓扑：

```bash
numactl --hardware
```

结合 **server02 A、node04 A** 的初始化日志 `NUMA verified tier=... node=... misplaced=0`，确认 DRAM／PMem 实际位置。既有配置为 server02 的 DRAM node 0／PMem node 2，node04 的 DRAM node 0／PMem node 1。Client 查询接口不报告远端 NUMA ID。

## 4. 如何运行 BFS，查看每节点容量、剩余容量和读写情况？

### client A：限制 BFS 的本地内存并运行

沿用已配置的 4 KiB Hermit 测试环境，关闭 THP 及以下实验开关：

```bash
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
sudo bash -ec 'for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do echo N > "/sys/kernel/debug/hermit/$flag"; done'
echo 0 | sudo tee /proc/sys/vm/page-cluster
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
```

使用本机 **cgroup v1 memory** 控制器建立 BFS 专用组。下面目录必须是新目录，重复运行时更换后缀：

```bash
export BFS_CGROUP=/sys/fs/cgroup/memory/hermit-pool-bfs-01
sudo mkdir "$BFS_CGROUP"
echo -1 | sudo tee "$BFS_CGROUP/memory.memsw.limit_in_bytes"
echo 25769803776 | sudo tee "$BFS_CGROUP/memory.limit_in_bytes"
echo 100 | sudo tee "$BFS_CGROUP/memory.swappiness"
```

这将 BFS 本地物理内存限制为 **24 GiB**，memsw 不设有限配额，允许继续使用 swap。项目既有 cgroup swap 记账问题未修复，因此 `batch_account` 保持关闭，不用 cgroup swap 统计验收。

先在 B/C 启动下述监控，再在 A 运行现有 Ligra BFS：

```bash
sudo env OMP_NUM_THREADS=40 bash -ec \
  'echo "$$" > "$1/cgroup.procs"; exec "$2" -s -rounds 1 "$3"' \
  bfs "$BFS_CGROUP" \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_64G.adj
echo "BFS exit=$?"
```

包装器只将 BFS 及其线程放入受限组，监控工具留在组外。使用 40 线程、对称图、1 次计时轮次，保留程序自带预热；命令返回后检查退出码为 0 且有 `Running time` 输出。这里不启动冷热采样器，普通 BFS 即可验证内存池使用。

### client B：持续查看每节点／Tier 的容量和剩余容量

```bash
sudo watch -n 2 cat /sys/kernel/debug/rswap/mapping
```

每两秒显示：

```text
server tier capacity live retired free
0 dram ... ... ... ...
0 pmem ... ... ... ...
1 dram ... ... ... ...
1 pmem ... ... ... ...
```

- `capacity`：该节点／Tier 的 Slot 总容量；`free`：剩余可分配容量。
- 已占容量为 `live+retired`，其中 `retired` 是等待在途引用释放的位置。
- 数值单位均为 **4 KiB 页**；**GiB＝页数÷262144**。同一 server 的 DRAM、PMem 相加即该节点总量。例如节点剩余 GiB＝`(dram.free+pmem.free)/262144`。

运行中应能看到各节点占用增加、剩余减少。BFS 释放页后占用可回落，最终 swapoff 后应全部归还。Server 已预分配整个池，不能用 Server 主机的 `free -h` 代替池内剩余 Slot。

### client C：持续查看各节点／Tier 的 RDMA 带宽

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
bash remoteswap/client/manage_rswap_client.sh monitor 2
```

看每个 Server/Tier 的 `write_MiB/s`、`read_total_MiB/s`、`total_MiB/s`；双节点应在运行期间都出现成功读写，`wc_error_delta`、`submit_error_delta`、`unrouted_error_delta` 应无新增。

### client D：查看 Adaptive 阈值、分类统计和逐页选择

观察 Adaptive 前，需已通过 `rswap_hotness --placement` 启动策略并切入 `adaptive`；上面的普通 BFS 命令不会自动开启 Adaptive。以下持续查看命令按需切换，Ctrl-C 退出当前查看。

**placement：看当前阈值。**

```bash
sudo watch -n 1 cat /sys/kernel/debug/rswap/placement
```

重点看 `state=adaptive`、实际阈值 `threshold_ppm`、快照代次 `generation` 和年龄 `age_ns`。`mean_ppm` 是原始均值，不能代替 capacity-mean 的实际阈值；ppm 除以 1000000 得到热度比例。

当前规则：已知正热度 `heat_ppm >= threshold_ppm` 首选 DRAM，较低的正热度首选 PMem；已知零热度走 Interleave，未知页首选 DRAM。

**policy_stats：看实际换出分类统计。**

```bash
sudo watch -n 1 cat /sys/kernel/debug/rswap/policy_stats
```

`reason` 行格式为 `reason 编号 decisions post_ok post_error wc_ok wc_error`，依次为决策次数、提交成功／失败、写完成成功／失败。

| reason 编号 | 分类 |
| --- | --- |
| 5 | 已知正热度热页，首选 DRAM |
| 6 | 已知正热度冷页，首选 PMem |
| 7 | 已知零热度，Interleave |
| 2／3／4 | 无快照／无可用阈值／PFN 未命中，未知页 |

这些是模块生命周期内累计的 Store 次数，不是去重后的冷热页数量；观察某阶段时比较前后差值。

**ftrace decision：看逐页放置选择。**

Client 模块加载后，首次创建独立 instance 并开启放置决策事件：

```bash
TRACE=/sys/kernel/debug/tracing/instances/rswap_demo
sudo mkdir "$TRACE"
echo 1 | sudo tee "$TRACE/events/rswap/decision/enable"
echo 1 | sudo tee "$TRACE/tracing_on"
```

在 BFS 运行期间，每秒刷新显示最新 10 行：

```bash
sudo watch -n 1 'tail -n 10 /sys/kernel/debug/tracing/instances/rswap_demo/trace'
```

查看 `pfn/offset`（页面／swap offset）、`reason`（分类）、`heat_ppm/threshold_ppm`（该次热度／实际阈值）、`preferred_server/preferred_tier`（首选位置）、`server/tier/slot`（实际位置）、`fallback`（容量回退）和 `alloc_ret`（0 表示分配成功）。Tier 0=DRAM、1=PMem；首选位置可能因容量不足而改变，分配成功不等于 RDMA 写完成。普通 Interleave 也会产生这些事件。

Ctrl-C 只退出查看，采集仍继续。观察结束后关闭并删除本 instance：

```bash
echo 0 | sudo tee "$TRACE/tracing_on"
echo 0 | sudo tee "$TRACE/events/rswap/decision/enable"
sudo rmdir "$TRACE"
```
