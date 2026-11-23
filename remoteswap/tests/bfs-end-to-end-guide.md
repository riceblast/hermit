# BFS：双节点冷热识别、页面放置与带宽观察

适用当前 Client **1.11**、协议 v2，以及 optane 上已验证的 Hermit **#8 内核**。
下面按终端与操作顺序拆分，每个命令块可以单独复制；使用 Bash，不需要把整篇拼成一个脚本。
本文件是手动实验指南，写入文档本身不会启动实验。

## 先明确这次能观察什么

完整链路是：Server 预分配 DRAM/PMem → Client 连接并启用 swap → 启动热度采样与 BFS →
Warmup 使用 Interleave → 自动进入 Adaptive → 新换出页面根据热度选择位置 → 按原位置换入 → 收尾。

**当前实现没有主动页面迁移。** 热度变化只影响之后的新 Store；已放置页面不会因变热或变冷而
自动搬到另一台 Server/另一个 Tier。本文观察的是冷热识别、换出放置、池占用和 RDMA 带宽，
不会出现一个主动迁移计数或迁移任务。

| 资源 | 本文配置 |
| --- | --- |
| Client | optane，10.0.0.11，`/mnt/sdb3/data/yxr/projects/hermit` |
| Server 0 | node04，10.0.0.13；DRAM NUMA 0 / PMem NUMA 1 |
| Server 1 | server02，10.0.0.20；DRAM NUMA 0 / PMem NUMA 2 |
| 每台容量 | DRAM 16 GiB＋PMem 32 GiB，总池 96 GiB |
| swap | 既有 `swap_80g.img`，不重新创建或格式化 |
| BFS | 64G 图、40 线程、24 GiB 物理内存限制、1 次计时轮次 |
| 热度 | 1 秒目标采样周期、最近 20 轮窗口、20 秒 Warmup、自动 Adaptive |

建议保留两个远端 Server 终端，以及三个 Client 终端：

- **A：Client 控制/BFS**，负责初始化、运行 BFS 和最终收尾。
- **B：累计采集**，先于 BFS 启动，一直保留到 swapoff 后。
- **C：观察**，随时查看热度、放置、带宽，或可选地抓取短 Trace。

## 1. Client A：检查初始状态和准备目录

进入项目目录：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
```

确认使用已验证的内核，在线 CPU 为 `0-47`，页面大小为 `4096`：

```bash
uname -a
```

```bash
cat /sys/devices/system/cpu/online
```

```bash
getconf PAGE_SIZE
```

检查当前 Client 和 swap。本文从 **Client 未加载、无活动 swap** 开始；若已有实验运行，先按其收尾流程结束。
不要在有活动 swap 时覆盖配置或启动第二轮实验。

```bash
bash remoteswap/client/manage_rswap_client.sh status
```

每轮修改下面的 `bfs-manual-01` 后缀；A/B/C 使用完全相同的路径。
`mkdir` 应成功；已有目录不要直接复用，采样和 collector 子目录也必须是新目录。

```bash
export BFS_RUN=/mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/artifacts/bfs-manual-01
```

```bash
mkdir -m 755 "$BFS_RUN"
```

```bash
mkdir "$BFS_RUN/settings"
```

保存配置和内核日志基线：

```bash
cp remoteswap/client/rswap_client.conf "$BFS_RUN/client.conf.before"
```

```bash
sudo dmesg > "$BFS_RUN/dmesg.before"
```

可选：重新编译当前 Client 和采样工具；不需要编译/安装新内核。

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
```

```bash
make -C remoteswap/tools
```

```bash
modinfo -F version remoteswap/client/rswap-client.ko
```

期望版本为 `1.11`。

## 2. 两个远端终端：分别启动 Server

两台机器均已将 PMem 配置为独立 system-ram NUMA Node；本文不重复转换 PMem。
Server 使用既有协议 v2 可执行文件。每次重新加载 Client 前，参与的 Server 都应是新启动的进程。

### Server 0：node04

在独立终端登录：

```bash
ssh yxr@10.0.0.13
```

```bash
cd /home/yxr/projects/hermit
```

检查是否已有服务；期望无输出。若有旧服务，先核实旧 Client 已关闭再正常结束旧服务。

```bash
pgrep -a -x rswap-server
```

确认 NUMA 0/1 可见，`ulimit -l` 为 `unlimited`：

```bash
numactl --hardware
```

```bash
ulimit -l
```

启动并保持前台运行：

```bash
./remoteswap/server/rswap-server \
  --ip 10.0.0.13 --port 9400 --client-cores 48 \
  --dram-node 0 --dram-gib 16 --pmem-node 1 --pmem-gib 32 \
  2>&1 | tee /tmp/hermit-bfs-manual-node04-01.log
```

### Server 1：server02

在另一个独立终端登录：

```bash
ssh yxr@10.0.0.20
```

```bash
cd /home/yxr/projects/hermit
```

```bash
pgrep -a -x rswap-server
```

确认 NUMA 0/2 可见，memlock 为 unlimited：

```bash
numactl --hardware
```

```bash
ulimit -l
```

```bash
./remoteswap/server/rswap-server \
  --ip 10.0.0.20 --port 9400 --client-cores 48 \
  --dram-node 0 --dram-gib 16 --pmem-node 2 --pmem-gib 32 \
  2>&1 | tee /tmp/hermit-bfs-manual-server02-01.log
```

两台均等待出现 `listening on ... waiting for matching Client` 后再连接 Client。
DRAM 和 PMem 的 NUMA 验证均应为 `misplaced=0`。server02 预分配 PMem 可能需要一两分钟。
**MR 注册发生在 Client 连接时**；连接后各应显示 `all 6 data MRs registered; pool available`。

## 3. Client A：保存并设置 4 KiB 实验参数

以下循环只批量处理同一组 HMT 开关。先保存，再关闭，方便结束后恢复原值。

```bash
sudo bash -c 'for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do cat "/sys/kernel/debug/hermit/$flag" > "$1/$flag"; done' save "$BFS_RUN/settings"
```

```bash
sudo bash -c 'for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do echo N > "/sys/kernel/debug/hermit/$flag"; done'
```

保存 THP 当前选项，以及换页参数：

```bash
sed -n 's/.*\[\([^]]*\)\].*/\1/p' /sys/kernel/mm/transparent_hugepage/enabled > "$BFS_RUN/settings/thp"
```

```bash
cat /proc/sys/vm/page-cluster > "$BFS_RUN/settings/page-cluster"
```

```bash
cat /sys/kernel/mm/swap/readahead_win > "$BFS_RUN/settings/readahead_win"
```

设置为本轮已验证的 4 KiB 路径：

```bash
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

```bash
echo 0 | sudo tee /proc/sys/vm/page-cluster
```

```bash
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
```

## 4. Client A：连接两台 Server 并启用 swap

配置文件是唯一配置来源；当前目录仍是 Client 仓库根目录。覆盖前已在第 1 步保存原文件。

```bash
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

```bash
bash remoteswap/client/manage_rswap_client.sh install
```

```bash
bash remoteswap/client/manage_rswap_client.sh status
```

期望模块已加载，`nr_servers=2`、`rmsize=96`，地址顺序与上表一致。
同时检查两个 Server 终端的 6 个 MR 注册结果。

启用既有 swap 文件：

```bash
sudo swapon /mnt/sdb4/yxr/projects/swapfile/swap_80g.img
```

```bash
cat /proc/swaps
```

期望只有这个 swap 文件。查看初始池，四池应 `live=retired=0`、`free=capacity`：

```bash
bash remoteswap/client/manage_rswap_client.sh mapping
```

```bash
bash remoteswap/client/manage_rswap_client.sh placement
```

这时 `state=inactive` 是正常的：启用 swap 不会自动开启冷热识别或 Adaptive。

## 5. Client A：创建仅限制 BFS 的 cgroup

使用本机 cgroup v1 的 memory 控制器。采样器和 collector 不进入该 cgroup。

```bash
export BFS_CGROUP=/sys/fs/cgroup/memory/hermit-bfs-manual
```

```bash
sudo mkdir "$BFS_CGROUP"
```

目录应是新目录；若已存在，先检查是否有残留任务，不要直接覆盖其限制。
先解除 memsw 总配额，再设物理内存限制，保证仍可使用远端 swap。

```bash
echo -1 | sudo tee "$BFS_CGROUP/memory.memsw.limit_in_bytes"
```

```bash
echo 25769803776 | sudo tee "$BFS_CGROUP/memory.limit_in_bytes"
```

```bash
echo 100 | sudo tee "$BFS_CGROUP/memory.swappiness"
```

```bash
cat "$BFS_CGROUP/memory.limit_in_bytes" "$BFS_CGROUP/memory.memsw.limit_in_bytes"
```

第一个值应为 24 GiB 字节数 `25769803776`，第二个是系统的 unlimited 哨兵值，不一定原样显示 `-1`。

## 6. Client B：先启动完整窗口采集

新开 Client 本机终端 B，设置相同路径：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
```

```bash
export BFS_RUN=/mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/artifacts/bfs-manual-01
```

启动只读 collector；命令会留在前台，输出重定向到文件，没有控制台滚动是正常的。
**保持此终端运行，BFS 和 swapoff 完成后再 Ctrl-C。**

```bash
sudo python3 remoteswap/tools/rswap_pool.py monitor \
  --interval 1 --out "$BFS_RUN/pool" > "$BFS_RUN/pool-monitor.log"
```

它保存原始 `pool/samples.jsonl` 和每个窗口的可重算报告。

## 7. Client A：开启冷热识别并运行 BFS

先确认 collector 已生成基线：

```bash
ls -lh "$BFS_RUN/pool/samples.jsonl"
```

然后运行。该块只有一条启动流水线和一条退出码保存命令，请一起复制。
`bash -ec` 包装器把 **BFS 自身** 加入受限 cgroup，再 exec BFS；采样器保留在外部。

```bash
sudo env OMP_NUM_THREADS=40 remoteswap/tools/rswap_hotness \
  --out "$BFS_RUN/heat" --placement --interval 1 --window 20 --warmup 20 \
  -- bash -ec 'echo "$$" > "$1/cgroup.procs"; exec "$2" -s -rounds 1 "$3"' \
  bfs "$BFS_CGROUP" \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_64G.adj \
  2>&1 | tee "$BFS_RUN/bfs.log"
printf '%s\n' "${PIPESTATUS[0]}" > "$BFS_RUN/bfs.exit"
```

这是启动冷热识别的显式 Start：`--placement` 先建立策略 run，再放行 BFS，
从启动时开始计时，包含读图、初始化和 Ligra 预热；不等 BFS 的算法计时段才开始。
前 20 秒 Warmup，继续 Interleave；到期自动 Adaptive，每次完整上传后更新热度和均值。
目标时间到了即可切换，但首次完整快照/有效均值未准备好时仍会回退 Interleave。

默认不加 `--pages`，避免 64G 工作集生成海量逐页 TSV。摘要、逐代上传、实际决策累计计数仍然保留。
BFS 的 `-rounds 1` 表示一次计时轮次，程序仍有自带预热。
主终端运行期间，转到 C 观察；A 出现 `Running time` 不代表采样器已经完成，等命令返回。

## 8. Client C：观察热度、页面放置和带宽

新开 Client 本机终端 C：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
```

```bash
export BFS_RUN=/mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/artifacts/bfs-manual-01
```

以下都是观察命令，可按需要重复执行。

### 8.1 冷热识别是否在持续更新

```bash
sudo tail -n 5 "$BFS_RUN/heat/summary.tsv"
```

```bash
sudo tail -n 5 "$BFS_RUN/heat/placement.tsv"
```

`heat/` 由 root 采样器建立，权限为 0700，所以读取该目录需要 sudo。

| 字段 | 如何理解 |
| --- | --- |
| summary：`phase` | `warmup` / `observe` 是用户态观察标签；内核策略状态看下一节 |
| `known` / `mean` | 有有效历史的去重页数 / 平均热度；`mean=-1` 表示无已知热度 |
| `active` / `idle` / `invalid` | 本轮可观测页的访问/未访问情况，以及无效或不可观测数量 |
| `scan_ms` / `overrun` | 扫描耗时 / 是否超过目标采样周期 |
| publication：`generation` / `records` | 已上传完整代次 / 本次上传的已知 resident PFN 数 |
| `mean_ppm` / `upload_ms` | 内核使用的整数均值 / 上传耗时 |

热度近似为最近 20 轮有效观测中被访问的比例，不是精确访问次数。
`known` 包括仍有历史的换出页；可上传的 `records` 仅包含当前 resident PFN，两者可以相差很大。
配置 1 秒并不保证每秒完成一次；此前相同 BFS 测试单次扫描达到约 17 秒。

### 8.2 策略状态和实际放置

```bash
bash remoteswap/client/manage_rswap_client.sh placement
```

正常顺序为 `inactive → warmup → adaptive → inactive`。最后一步通常由 BFS 结束、
上传句柄关闭触发。`generation` 应随上传增加，`age_ns` 是当前快照年龄，1e9 ns = 1 秒。

查看当前占用：

```bash
bash remoteswap/client/manage_rswap_client.sh mapping
```

查看策略累计计数的原始格式：

```bash
sudo cat /sys/kernel/debug/rswap/policy_stats
```

| 行 | 含义 |
| --- | --- |
| `reason ID decisions post_ok post_error wc_ok wc_error` | 各来源决策及后续提交/完成计数 |
| `pool SERVER TIER preferred allocated` | 各池首选次数 / 实际分配次数；Tier 0=DRAM、1=PMem |
| `fallback ID count` | 容量 Fallback；0=首选，1=另一 Server 同 Tier，2/3=跨 Tier |
| `allocation_error count` | 没有可分配 Slot 的次数 |

Reason：0 Inactive、1 Warmup、2 无快照、3 无有效均值、4 PFN miss、5 热、6 冷。
Adaptive 中 `heat_ppm > mean_ppm` 首选 DRAM，否则首选 PMem；目标 Tier 内两 Server 轮转。
未知热度继续 Interleave，首选池不足才尝试 Fallback。

想看工具转换后的带拓扑、容量和覆盖率的 JSON：

```bash
bash remoteswap/client/manage_rswap_client.sh pool-status
```

重点看 `policy.reasons`、`policy.pools`、`adaptive_heat_coverage`、
`available_snapshot_pfn_hit_ratio`，以及各池 `live/retired/free`。
`used=live+retired`，不是进程精确存活页数。BFS 释放内存时占用会下降，不能把下降直接当成迁移。
这里的计数属于整个 Client；本轮应只运行一个测试，不把它当成严格的目标 PID 归属统计。
运行中各计数不是原子事务，不要立即要求 decisions/post/WC 相等；收尾 drain 后再对账。

### 8.3 带宽观察

下面进入每 2 秒刷新的表格模式，**Ctrl-C 只停止这份只读带宽监控**，不停止 B 的 collector 或 BFS。

```bash
bash remoteswap/client/manage_rswap_client.sh monitor 2
```

看每个 Server/Tier 的 `write_MiB/s`、`read_total_MiB/s`、`total_MiB/s`，以及
`wc_error_delta`、`submit_error_delta`、`unrouted_error_delta`。
带宽按成功 RDMA WC 的 4 KiB 逻辑载荷计，不是网卡线速/介质峰值。
热度和实际换出概率不同；**不要求 DRAM 带宽一定高于 PMem**。

### 8.4 可选：查看几条具体页面换出决策

累计计数不能显示单条 Slot。若需要 PFN、热度、均值、目标和 Fallback，
在 BFS 确实有远端 Write 时，用 C 暂停上面的带宽表格后抓取约 1 秒 decision Trace。
该步骤只做短窗口观察，不开启全程逐页 Trace，也不主动迁移页面。

```bash
export BFS_TRACE=/sys/kernel/debug/tracing/instances/hermit_bfs_manual
```

```bash
sudo mkdir "$BFS_TRACE"
```

```bash
echo 0 | sudo tee "$BFS_TRACE/tracing_on"
```

```bash
echo global | sudo tee "$BFS_TRACE/trace_clock"
```

```bash
echo 1 | sudo tee "$BFS_TRACE/events/rswap/decision/enable"
```

将下面两行一起复制，以便抓取后立即关掉 tracing。`timeout` 到期的退出码 124 是预期行为。

```bash
sudo timeout 1s bash -c 'echo 1 > "$1/tracing_on"; exec cat "$1/trace_pipe"' trace "$BFS_TRACE" > "$BFS_RUN/decisions-short.log"
echo 0 | sudo tee "$BFS_TRACE/tracing_on"
```

```bash
echo 0 | sudo tee "$BFS_TRACE/events/rswap/decision/enable"
```

```bash
head -n 10 "$BFS_RUN/decisions-short.log"
```

关注 `reason=5/6`、`heat_ppm`、`mean_ppm`、`preferred_server/preferred_tier`、
实际 `server/tier/slot`、`fallback`、`alloc_ret`。`alloc_ret=0` 表示已分配位置，
不单凭该事件认定后续 RDMA 成功；成功写入仍看 WC 统计。
`heat_ppm=-1` 是未查询到已知热度的标记，不是“极冷”；`alloc_seq` 区分分配生命周期。
这段短 Trace 不含完整 mapping/post/WC 生命周期，也没有逐页快照，
不能拿它运行完整的 `--require-empty --store-results --heat-dir` 正式验证。
若文件为空，可能该时段没有新 Store；在有 Write 的时段重试，另换日志文件名。

清理专用 Trace instance：

```bash
sudo rmdir "$BFS_TRACE"
```

## 9. BFS 结束后：Client A 保存结果并关闭 swap

先确认启动命令已经返回：

```bash
cat "$BFS_RUN/bfs.exit"
```

```bash
sudo cat "$BFS_RUN/heat/run.tsv"
```

正常应退出 0，`monitor_error=0`、`stop_signal=0`。保存一次收尾前状态：

```bash
bash remoteswap/client/manage_rswap_client.sh pool-status > "$BFS_RUN/status.after-bfs.json"
```

**Server 保持在线**，关闭本轮 swap：

```bash
sudo swapoff /mnt/sdb4/yxr/projects/swapfile/swap_80g.img
```

```bash
bash remoteswap/client/manage_rswap_client.sh mapping
```

```bash
bash remoteswap/client/manage_rswap_client.sh placement
```

期望四池 `live=retired=0`、`free=capacity`，策略 `inactive`。
确认 B 的 collector 又完成至少一轮采样，再在 **B 按 Ctrl-C**，停止采集。
不要先卸载模块或停止 Server；如果 swapoff 失败，保留 Client/Server 连接先诊断。

## 10. Client A：生成正式摘要报告

collector 停止后执行。`--heat-dir` 读取 root 的 heat 目录，所以整条报告命令使用 sudo。
`--settled` 检查最终累计计数等式，只应在前面的 swapoff/drain 完成后使用。

```bash
sudo python3 remoteswap/tools/rswap_pool.py report \
  --samples "$BFS_RUN/pool/samples.jsonl" --settled \
  --heat-dir "$BFS_RUN/heat" > "$BFS_RUN/report.json"
```

按需查看重点，避免手动翻阅所有每秒 JSON：

```bash
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps(r["sampling"],indent=2))' "$BFS_RUN/report.json"
```

```bash
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps(r["segments"][0]["window"]["policy"],indent=2))' "$BFS_RUN/report.json"
```

```bash
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps(r["segments"][0]["window"]["bandwidth_MiB_s"],indent=2))' "$BFS_RUN/report.json"
```

```bash
python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(json.dumps(r["segments"][0]["final"]["pools"],indent=2))' "$BFS_RUN/report.json"
```

本流程不重载模块，所以报告应只有一个 segment，且 `settled_checked=true`。
`window.policy` 是 collector 基线到最终快照的增量，不会把此模块更早的累计值误当本轮结果。
热度覆盖率分母是所有 Adaptive Store 决策；有快照 PFN 命中率分母排除无快照和无均值决策。
分母为 0 会显示 null。带宽窗口包括启动/收尾，不能直接当作 BFS 算法计时段的带宽。

参考刚通过的同配置验收：实际热度覆盖约 **29.97%**，33 轮采样、21 轮超期；
这些只是示例，不是下一轮必须达到的数值。是否开启短 Trace 也会影响实验开销。

## 11. Client A：卸载、恢复参数、保存 Server 日志

C 若仍运行带宽观察，先 Ctrl-C；B 的 collector 应已停止，可选 Trace instance 应已删除。
再次确认无活动 swap，再卸载 Client：

```bash
cat /proc/swaps
```

```bash
bash remoteswap/client/manage_rswap_client.sh uninstall
```

卸载可能需要几十秒。等命令成功返回后，两个 Server 通常会自动结束并显示
`Server resources released`；若仍在前台，确认 Client 已卸载后再在相应 Server 终端 Ctrl-C。
下一轮实验必须重新启动参与的 Server。

恢复 HMT 开关：

```bash
sudo bash -c 'for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do cat "$1/$flag" > "/sys/kernel/debug/hermit/$flag"; done' restore "$BFS_RUN/settings"
```

恢复其余参数和配置：

```bash
sudo tee /sys/kernel/mm/transparent_hugepage/enabled < "$BFS_RUN/settings/thp"
```

```bash
sudo tee /proc/sys/vm/page-cluster < "$BFS_RUN/settings/page-cluster"
```

```bash
sudo tee /sys/kernel/mm/swap/readahead_win < "$BFS_RUN/settings/readahead_win"
```

```bash
cp "$BFS_RUN/client.conf.before" remoteswap/client/rswap_client.conf
```

确认 cgroup 无进程后删除：

```bash
cat "$BFS_CGROUP/cgroup.procs"
```

```bash
sudo rmdir "$BFS_CGROUP"
```

保存最终内核日志和 Server 日志：

```bash
sudo dmesg > "$BFS_RUN/dmesg.after"
```

```bash
scp yxr@10.0.0.13:/tmp/hermit-bfs-manual-node04-01.log "$BFS_RUN/server-node04.log"
```

```bash
scp yxr@10.0.0.20:/tmp/hermit-bfs-manual-server02-01.log "$BFS_RUN/server-server02.log"
```

```bash
bash remoteswap/client/manage_rswap_client.sh status
```

最终应为 Client 未加载、无活动 swap。以 `dmesg.before` 为基线检查新增内核日志，
不要把更早实验的旧 warning 当成本轮新增错误。
结果保留在 `$BFS_RUN`；`artifacts/` 沿用项目 Git 忽略规则，数据不会随普通源码提交自动上传。

## 12. 可选：下一轮改为手动切换 Adaptive

完成本轮收尾后，换新 run 目录、重新启动 Server/Client 并按上面步骤准备。
**仅在第 7 步的启动命令增加 `--manual`**，其余保持一致。
Warmup 20 秒到期后，内核仍保持 Warmup，直到在 C 显式执行：

```bash
bash remoteswap/client/manage_rswap_client.sh adaptive
```

不足 20 秒会返回 EAGAIN；BFS 已结束、策略已 Inactive 时不能再切换。
手动切换不要等待过久，本轮 BFS 实际有换页的时间主要在输入/初始化/预热阶段。
如果想在运行中停止 Adaptive 放置并继续 BFS，可在 C 执行：

```bash
bash remoteswap/client/manage_rswap_client.sh placement-stop
```

Stop 后采样器会检测策略停止并继续纯观察，BFS 继续运行，新的 Store 恢复 Interleave；
既有映射按原位置读回，累计计数不会清零。Stop 不会在同一次采样进程中自动重新 Start，
需要下一轮重新启动带 `--placement` 的采样器。
