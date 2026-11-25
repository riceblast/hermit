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

产物是 `remoteswap/client/rswap-client.ko`，当前版本应为 `1.14`，默认是 RDMA 后端。若此前编译过 `BACKEND=DRAM`，先用相同 `KDIR` 执行 `make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" clean`，再构建。构建完成后执行第 1 节连接。

**client A：连接成功后启用 swap。** 双节点沿用已初始化的 64 GiB swap 文件：

```bash
sudo swapon /mnt/sdb4/yxr/swapfile/swap_64g.img
swapon --show
```

`install` 不自动启用 swap，必须单独 `swapon`。单节点 48 GiB 池则使用既有 `swap_48g.img`，路径目录相同。只启用一个 swap area，容量不得超过总池；无需重新执行 `mkswap`。

双节点仍只显示一个 Linux swap area，由 Client 将两个远端节点组织为统一后端。

**client A：负载结束后关闭并卸载。** 两台 Server 始终保持在线：

```bash
sudo swapoff /mnt/sdb4/yxr/swapfile/swap_64g.img
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
bash remoteswap/client/manage_rswap_client.sh address-map
```

| 命令 | 功能／重点字段 |
| --- | --- |
| `status` | 实际节点数量、总池容量、各节点 IP／端口 |
| `pool-status` | JSON 格式；`server_count`、总 `capacity_bytes`；`topology` 中每节点容量及 `dram_fraction`／`pmem_fraction`；`pools` 中每节点／Tier 的容量与占用 |
| `mapping` | 简明表格，分别列出 `0/1 × dram/pmem` 的容量、占用和空闲，使用一位小数的 KB／MB／GB |
| `mapping --raw` | 精确整数：各池的 Slot 数及 Client 主体元数据字节数，供程序读取／核对 |
| `address-map` | 按 DRAM／PMem、memory pool（Server 编号）列出远端 MR 地址范围、Chunk 编号及容量 |

本文双节点配置应显示每台 48 GiB，其中 DRAM 16 GiB、PMem 32 GiB，总池 96 GiB。单节点则只有 server 0 的两个 Tier。

### 查看各介质的远端地址空间

Client 连接成功后即可读取，不要求先启用 swap。脚本与直接读取 debugfs 等价：

```bash
bash remoteswap/client/manage_rswap_client.sh address-map
sudo cat /sys/kernel/debug/rswap/address_map
```

输出先列 DRAM，再列 PMem；每种介质下依 endpoint 顺序列 `memory pool 0/1`，
包含实际 Server IP／端口、该 Tier 的 Chunk 数和 GiB 容量，每个 MR 单独一行。
`chunk` 沿用 Server 原始描述符编号：DRAM 从 0 开始，PMem 接在 DRAM 后面。
`start`／`end` 为十六进制的 **Server 进程虚拟地址**，范围是左闭右开 `[start,end)`；
`bytes` 是精确长度，当前每个 MR 为 8 GiB（8589934592 字节）。
这些地址不代表 Server 物理地址或 Client 本地地址；不同 Server 可以出现相同范围。
地址列表保留独立 MR 边界，不假定不同 MR 或不同介质的地址连续。

接口只读、权限为 root 读取，展示当前 Session 发现的整个已注册池，不显示逐页
swap offset 映射或空闲区间。Slot 占用使用原 `mapping` 命令查看。
换页和策略切换期间 MR 范围保持不变，重启 Server／重新连接后地址可能变化。

核心验收只需一次双节点连接（不启用 swap）：检查两个介质下均有 pool 0/1，
对照各 Server 的 `MR registered` 日志核对 Chunk 编号、起点和长度。
在本文每台 16/32 GiB 配置下，每个 Server 应分别输出 2 个 DRAM MR 和 4 个 PMem MR，
每行 `end-start=bytes=8589934592`，两台共 12 行 MR。
重复读取结果应一致，脚本输出与直接 cat 一致；正常卸载 Client 后接口应消失。

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

### client A：32GB 图、4GiB 本地限制，180 秒后自动切换 Adaptive

本段替代上面的 24GiB cgroup 创建和普通 BFS 启动命令。先完成双节点连接、启用第 2 节的 64GiB swap，并沿用上面的 4KiB 测试环境设置。配置为 **128 轮、40 线程、capacity-mean、20 秒采样间隔、4 轮窗口**。

创建本轮专用 cgroup；cgroup 和输出目录均须使用新名称，重复运行时更换后缀：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
make -C remoteswap/tools
export BFS_CGROUP=/sys/fs/cgroup/memory/hermit-bfs32-4g-auto180-01
export BFS_OUT=/tmp/hermit-bfs32-4g-auto180-01
sudo mkdir "$BFS_CGROUP"
echo -1 | sudo tee "$BFS_CGROUP/memory.memsw.limit_in_bytes"
echo 4294967296 | sudo tee "$BFS_CGROUP/memory.limit_in_bytes"
echo 100 | sudo tee "$BFS_CGROUP/memory.swappiness"
```

先启动下述 B/C/D 监控，再运行原始 Ligra BFS：

```bash
sudo env OMP_NUM_THREADS=40 remoteswap/tools/rswap_hotness \
  --out "$BFS_OUT" \
  --placement --interval 20 --window 4 --warmup 180 \
  --threshold-policy capacity-mean --memory-cgroup "$BFS_CGROUP" \
  --reserve-gib 1 -- \
  bash -ec 'echo "$$" > "$1/cgroup.procs"; exec "$2" -s -rounds 128 "$3"' \
  bfs "$BFS_CGROUP" \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_32G.adj \
  2>&1 | tee "$BFS_OUT.console.log"
echo "sampler exit=${PIPESTATUS[0]}"
sudo cat "$BFS_OUT/run.tsv"
```

`--placement` 启用策略，省略 `--manual` 后按 `--warmup 180` 自动切换：从**策略启动时刻**开始计时，前 180 秒用 Interleave 并持续采样，满 180 秒后的新换出使用 Adaptive。计时包含读图、初始化和首次预热，不固定在某一轮切换。使用上面的原始 BFS，无需第 64 轮暂停或手动写入 `adaptive`。

本地物理内存上限为 4GiB；`--reserve-gib 1` 仅让 capacity-mean 按 3GiB 有效本地容量估计阈值，不改变 cgroup 上限。BFS 自带一次不计入轮数的预热，之后输出 128 条 `Running time`。

运行期间在 D 查看 `placement`：切换后应为 `state=adaptive`、`mode=auto`、`warmup_ns=180000000000`；B/C 查看容量和各节点／Tier 带宽。结束后 `run.tsv` 的 `exit_status`、`monitor_error`、`stop_signal` 均应为 0；`placement.tsv` 保留阈值记录，`$BFS_OUT.console.log` 保留逐轮输出。跨越切换点的遍历单独归类，带宽的跨状态采样窗口也单列。

结束后按第 2 节关闭 swap、卸载 Client，再删除空 cgroup：

```bash
sudo rmdir "$BFS_CGROUP"
```

### client B：持续查看每节点／Tier 的容量和剩余容量

```bash
sudo watch -n 2 cat /sys/kernel/debug/rswap/mapping
```

每两秒显示：

```text
server tier capacity used free
0 dram 16.0GB 0.0KB 16.0GB
0 pmem 32.0GB 0.0KB 32.0GB
1 dram 16.0GB 0.0KB 16.0GB
1 pmem 32.0GB 0.0KB 32.0GB
```

- `capacity`：该节点／Tier 的 Slot 总容量；`free`：剩余可分配容量。
- `used`：当前已占容量，按内部 `live+retired` 计算，包含等待在途引用释放的位置；默认输出不再单列 live／retired。
- 容量字段按 **1024 进制**显示一位小数，后缀为 KB／MB／GB；从 KB 开始，超过 1024 才换下一个单位，最大到 GB。例如 `16.0GB`、`512.0MB`、`0.0KB`，3.5 TiB 显示 `3584.0GB`。
- 第一行 `version=2 capacity=... metadata=... sequence=...` 中，`capacity` 是全池容量，`metadata` 是 Client 映射主体元数据占用；序号保持整数。
- 如需原始精确数值，执行 `bash remoteswap/client/manage_rswap_client.sh mapping --raw` 或读取 `/sys/kernel/debug/rswap/mapping_raw`，仍为 v1 格式、4 KiB Slot 计数及元数据字节数。监控工具已改为读取原始接口，并兼容旧 Client。
- 显示值存在舍入误差，精确核对 `capacity=used+free` 时使用原始接口（其中 used=live+retired）。同一 server 的 DRAM、PMem 相加即该节点容量。

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

重点看 `state=adaptive`、实际阈值 `threshold`、快照代次 `generation` 和年龄 `age_ns`。`mean` 是原始均值的显示值，不能代替 capacity-mean 的实际阈值；热度显示为 0～1、两位小数，未知显示 `-`。计算及上传保留原精度，精确值可用 `placement --raw` 或 debugfs `placement_raw` 查看。

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

新 `decision` 只显示 Store 成功分配后的六项：`req_type`、十六进制 `pfn`、`placement_policy`、`reason`、`server`、`target_mem`，每项后带逗号。Interleave／Warmup 分别标记 `interleave`／`interleave(warmup)`；Adaptive 的 hot/cold 按实际 DRAM／PMem 目标标记，包含未知页和容量回退，不是原始热度分类。分配成功不等于 RDMA 写完成，Load 不输出 decision。

如需原来的热度、阈值、offset、alloc_seq、RR 和 fallback 等精确证据，独立开启 `events/rswap/decision_raw/enable`。详细格式及采样器 `--raw-logs` 选项见 [输出说明](../tests/compact-output.md)。

Ctrl-C 只退出查看，采集仍继续。观察结束后关闭并删除本 instance：

```bash
echo 0 | sudo tee "$TRACE/tracing_on"
echo 0 | sudo tee "$TRACE/events/rswap/decision/enable"
sudo rmdir "$TRACE"
```
