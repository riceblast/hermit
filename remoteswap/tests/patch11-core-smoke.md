# Patch 11：核心实机验收与结果

## 范围

2026-11-23 已完成以下单节点、双节点核心实机验收，结果见文末。
补单节点 Adaptive，再做双节点冷热/生命周期和双节点 BFS。
操作者执行 sudo，助手准备具体控制器、同步 READY、分析日志。
Client optane / 10.0.0.11，现有 #8 内核，1.11，固定 48 CPU，4 KiB、THP=never、
batch_account 关闭、memsw 无有限配额。Server/协议不变。

不新增 Store 写失败后的磁盘回退验证，不做全池耗尽或硬件故障注入。
容量 Fallback 由实际分配器本地回归覆盖，不新增填满 96 GiB 的硬件轮次。
不做采样优化、性能提升门槛或要求 DRAM 带宽高于 PMem。

## 1. 单节点：决策、结果链、计数和内容

1. 保存原双节点配置。在无活动 swap/旧 Client 卸载后，改用 server02 / 10.0.0.20，
   DRAM 16 / PMem 32 GiB，NR_SERVERS=1、POOL_GIB=48；启用既有 48 GiB swap。
2. 确认两 Tier misplaced=0、6 个 MR 可用、Client 1.11 ready，初始池空。
   用 pool-monitor 建立基线，保存原始累计快照。
3. 从空池采集 mapping/decision/store_result，global 时钟、持续读取并保存 buffer stats。
4. `adaptive_pattern 32` 配合 `--placement --interval 1 --window 4 --warmup 4 --pages`。
   每个 READY 等待完整热度发布和冷热分离，释放换出、逐 word 读回，再交换冷热区域重复。
5. 预期热页 DRAM、冷页 PMem，Server 始终为 0；每个成功分配有对应 post/WC 序号链。
   不刻意制造容量不足，不要求出现 Fallback。
6. READY_FINAL 活进程 swapoff，确认各池 capacity=free、live=retired=0 及策略 Inactive，释放最终内容校验。
   等进程/采样器退出后保存最终计数，执行 settled/Trace/publication/数据检查。

预期累计决策、Trace 与成功 WC 增量对账，无错误/丢失、Slot 全部归还。
运行中不强制事务等式；分母 0 显示 null。
单节点另做一个短运行，配置足够长的 Warmup，在其到期前释放 READY 触发实际换出，
核对 reason=warmup、Interleave 首选和对应累计计数；使用独立采集/热度目录，避免混合两个 run。

## 2. 双节点：Tier 内 RR 与生命周期

单节点 swapoff/卸载后停止 Server，恢复保存配置并启动新 Server 进程：
endpoint 0=node04 / 10.0.0.13、Node 0/1；endpoint 1=server02 / 10.0.0.20、Node 0/2，
各 16+32 GiB，总池 96 GiB。重连前重启所有参与的 Server。

重复小工作集：预期目标 Tier 内两个 Server 均有分配，冷热交换生效，原位置读取和内容正确，
结果链/计数对账通过；最终活进程 swapoff、Slot 全归还。复用 Stop/句柄关闭/重新 Start 检查，
不增加失败矩阵。

## 3. 双节点 BFS：关闭逐页 Trace 的覆盖报告

同一 Session 再 swapon 既有 80 GiB，重新 Start。64G 图、OMP_NUM_THREADS=40、24 GiB 物理限制，
1 次计时轮次，保留 Ligra 预热。采样器位于 workload cgroup 外。
`--placement --interval 1 --window 20 --warmup 20`，不开 pages/全程逐页 Trace。
使用 pool-monitor 收集 policy_stats/stats/mapping/placement。

- BFS/采样器退出 0，一次算法计时，四池成功读写，无新增错误。
- 得到真实 Adaptive 热/冷、无快照、无均值、PFN miss 次数与分母明确的比例。
- 同时报 Read/Write/总带宽、占用、均值/快照年龄、扫描超期/上传耗时。
- Stop 后累计统计仍可读；Start/Stop/swapon 不清零，模块重载不混算。
- 不设热度命中率门槛，不把监控 known 页数当成内核 Store 命中率。

不默认增加 BFS A/B；若需要效果比较，另行定义对照配置/时间窗口。

## 收尾与证据

Server 在线 → swapoff → mapping 全空/策略 Inactive → 最后 settled 快照 →
停止 collector/Trace/采样器 → 卸载 Client → 停止 Server，恢复原 Client 配置及 HMT/THP。
保存单/双配置、产物 hash、console/exit、summary/publication、短 Trace/buffer stats、
samples.jsonl、正式报告和新增 dmesg，转存数据盘。不得直接复用 Patch 10 的旧 run 控制器。
实机未发生的异步/错误/全池交错不宣称覆盖；BFS 完成不等于算法结果或性能认证。

## 2026-11-23 实机结果

**核心验收 PASS**。操作者运行 `/tmp/hermit-p11-core/host.py run` 执行特权操作，
助手根据实际热度快照推进 FIFO 关卡、重启 Server、执行正式分析及收尾检查。
Client 1.11 / #8 内核；单节点 server02，双节点 endpoint 0=node04、endpoint 1=server02，
各 16 GiB DRAM＋32 GiB PMem。全部 Server 的两 Tier NUMA 检查为 misplaced=0，
每次连接注册 6 个 MR，断开后均确认资源释放、服务进程退出。

### 三个可控工作集运行

每个运行是 32 MiB / 8192 页，交换冷热区域执行两轮，逐 word 校验；
READY_FINAL 时活进程 swapoff 后再做最终内容校验。三次均 PASS。
单节点 Warmup 专项配置为 600 秒，以保证所有实际换出都在 Warmup 内。

| 运行 | Warmup Store | 热决策→DRAM | 冷决策→PMem | 实际分配 |
| --- | ---: | ---: | ---: | --- |
| 单节点 Warmup | 16384 | 0 | 0 | Server 0 DRAM 8191 / PMem 8193；静态 Interleave 首选校验通过 |
| 单节点 Adaptive | 0 | 8188 | 8196 | 全部 Server 0，与上传快照逐 PFN/ppm 对应 |
| 双节点 Adaptive | 0 | 8192 | 8192 | 四池各 4096，目标 Tier 内 RR 校验通过 |

单节点第二轮中 4 页仍被快照判冷，允许历史热度和关联近似；实际放置与快照完全一致。
每个运行均有 16384 条成功分配、16384 条 post、16384 条 WC 和 98304 条映射事件，
正式工具通过 generation/publication、阈值、位置、结果链及回收验证。
48 CPU 的 Trace buffer 丢失检查均通过，无分配/提交/WC 错误；最终 live/retired/reference 全为 0。
本轮未触发容量 Fallback，不据此宣称覆盖了硬件耗尽或写失败后的磁盘回退。

### BFS 集成与实际覆盖

既有 80 GiB swap、64G 图、40 线程、24 GiB memory cgroup，memsw 无有限配额；
采样器在 workload cgroup 外，1 秒间隔、20 周期窗口、20 秒 Warmup，
不采集逐页文件和全程逐页 Trace。BFS 和采样器退出均为 0，算法计时行恰好一条
（0.251 秒，仅算法计时，不是端到端耗时）；采样器运行 222.773 秒。

- 成功 Write **24759149** 页，成功同步 Read **18603542** 页，四池均有读写。
- 判热 **857791**，判冷 **6562474**，PFN miss **17338884**；
  热度覆盖 / 有快照时 PFN 命中均为 **29.9698%**，分母均为 24759149 次 Adaptive Store 决策。
- 本轮没有实际 Warmup Store、无快照/无均值决策；Warmup Store 已由上面的单节点专项验证。
- 未知 PFN 使用 Interleave；全部首次候选成功，无容量 Fallback、分配错误、提交错误或 WC 错误。
- 33 轮采样、33 代上传，Warmup 10 轮、Observe 23 轮；21 轮超期，
  最长扫描 **16918.994 ms**、最长上传 **2188.705 ms**。不设覆盖率/开销门槛，不进行扫描优化。
- 有快照决策的年龄分桶：<=1s 1661397、(1,5]s 6347873、(5,20]s 16749879、>20s 0。

以下是从 collector 首个基线到 swapoff 后最后快照的 223.452 秒窗口，
按成功 WC 统计的逻辑载荷带宽；包含启动/收尾，不是介质峰值或策略收益比较。

| Server / Tier | Write MiB/s | Read MiB/s | 总 MiB/s |
| --- | ---: | ---: | ---: |
| 0 / DRAM | 79.253 | 70.790 | 150.043 |
| 0 / PMem | 137.158 | 91.867 | 229.025 |
| 1 / DRAM | 79.269 | 70.756 | 150.024 |
| 1 / PMem | 137.145 | 91.804 | 228.949 |

BFS 未做逐页内容/算法 ground truth 验证；不把进程成功等同于算法正确性认证。
本轮异步 Read 计数为 0，不宣称新增异步硬件交错覆盖。

### 累计计数、隔离和收尾

- `pool-status`、`pool-monitor`、`pool-report --settled` 均实际执行；四个运行的正式报告通过。
  单节点 Warmup→Stop/swapoff/swapon→Adaptive 后，累计决策从 16384 保留并增至 32768。
- 合并四组原始样本后，报告准确分为 **2 个模块生命周期**；重新加载后不混算，
  双节点可控运行→BFS 的累计决策为 16384＋24759149。
- 两次模块生命周期均通过提前 Adaptive 的 EAGAIN、手动切换、显式 Stop、
  旧句柄 ESTALE、重新 Start、上传句柄关闭自动 Stop 检查。
- 所有运行结束策略 Inactive、所有池 capacity=free、live=retired=0；最终无活动 swap、
  Client 已卸载、测试 cgroup/Trace instance 已删除、Server 均已退出。
- HMT 各开关、THP=always、page-cluster=3、readahead_win=0 及原双节点配置均恢复。
  与启动前 dmesg 前缀对比，包含最终卸载阶段均无新增 BUG/WARNING/相关错误。

### 证据与复核

证据归档到 `remoteswap/tests/artifacts/patch11-core-20261123/`（沿用项目的 artifacts 忽略规则）：

- `SUCCESS`、`results.json`、`files.json`：总结果与文件清单。
- `reports/{single-warmup,single,dual}/`：正式 Trace/热度报告、逐决策 TSV、累计计数对账。
- `reports/bfs/`：正式 pool 报告、采样开销、实际 Store 覆盖率与 I/O 计数。
- `reports/module-lifetimes.json`：模块重载分段、跨 Stop/area 累计验证。
- 每个 phase 下的 `pool/samples.jsonl`、`heat/`、console/exit、debugfs 前后快照；
  小工作集另有 Trace 与 buffer stats。全部 Server 日志和配置、产物 hash、控制器及复核脚本均保留。
- `restored.json`、`finish.ok`、`reports/dmesg.final.new`：参数恢复与最终内核日志证据。

复核示例（仅普通日志，不需 sudo；替换目录以适应归档位置）：

```bash
python3 remoteswap/tools/rswap_pool.py report \
  --samples remoteswap/tests/artifacts/patch11-core-20261123/bfs/pool/samples.jsonl \
  --settled --heat-dir remoteswap/tests/artifacts/patch11-core-20261123/bfs/heat
```
