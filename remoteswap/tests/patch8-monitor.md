# Patch 8：窗口热度监控与 Store PFN 关联

## 本轮目标与完成状态

本轮完成的是 Task 3 的 **Patch 8：冷热观测基础**：能够针对指定进程采集页面活跃度，
输出窗口热度和均值，并把此前生成的热度与实际发生的 Client Store 做离线关联。
核心功能已实现，2026-11-22 完成本地回归之后的三项实机检查，Client 侧已正常收尾。

| 交付目标 | 本轮完成内容与证据 |
| --- | --- |
| 热度监控 | 支持启动程序/指定 PID、共享地址空间线程、逐页热度、均值、Warmup 与日志 |
| 热度变化 | 可控程序交换冷热区域后，第一/第三段热度从 1.0/0.0 变为 0.0/约 0.999817 |
| 实际 Store 关联 | 522162 条已接受提交在先前已完成快照中匹配到已知热度，均为冷页；平均快照年龄约 1.002 秒 |
| 兼容现有换页 | 双节点 4 GiB/2 GiB 页面测试两轮各 1048576 页校验通过，活进程 swapoff 后内容仍正确；四个 Server/Tier 错误无新增 |
| BFS 集成 | 64G 图、40 线程、24 GiB 物理限制、一次计时轮次正常完成，摘要监控通过；随后 swapoff/卸载成功 |

这证明冷热观测与 Store 离线关联可以工作；**尚未把热度交给内核决定远端位置**。
Task 3 的动态映射、Adaptive 放置及其效果验收仍在后续 Patch 中完成。
本轮实机检查的配置、原始日志位置、有效运行与中止运行的区别，见
[核心测试的实际执行结果](patch8-core-smoke.md#2026-11-22-实机核心验收结果)。

## 已确认的需求与设计决策

以下记录本轮讨论后采用的约定，后续开发应保持这些语义；未实现的部分在文末单列。

| 事项 | 采用的约定 |
| --- | --- |
| KISS 与修改边界 | 用户态独立采样，Client 只补一个最小 Store 观测点；复用已有 I/O，不引入监控框架、Server 修改或新 RDMA 协议 |
| 监控对象 | 单个指定进程的可读地址空间，包含其中共享地址空间的线程；均值针对可参与 swap 的目标页，不把普通文件缓存/代码页混入，不递归跟踪 fork 子进程 |
| 热度含义 | 统计一个采样周期内是否活跃，以活跃周期占有效周期的比例近似频率；不计精确访问次数，不使用 PEBS |
| 不可观测的周期 | 已换出、PFN 变化或本轮无法可靠观测时记无效，不直接记冷；窗口仍推进，窗口内无有效样本后变未知 |
| VA/PFN 历史 | 同一 VMA 内 VA 换了 PFN，保留 VA 既有历史，只跳过变化轮并重建 PFN/Idle 关联；检测到 VMA 变化或未映射则清除相应历史 |
| 平均值 | 每个有有效窗口历史的目标页等权，包含已知热度 0，排除未知；当前物理页/交换条目的别名尽量去重 |
| 配置与更新 | 采样、窗口热度和用户态均值按同一轮更新；配置更新周期、热度窗口和 Warmup，不再增加独立 cooldown、TTL 或复杂快照过期机制 |
| 起始计时 | CLI 启动目标程序时开始，覆盖 BFS 读图、初始化、Ligra 预热和计算，不等待算法计时阶段；附加 PID 仅从附加时开始 |
| Warmup 的本 Patch 含义 | Warmup 内继续采样，到时改为 observe 标签；两阶段的 Client 都保持 Interleave，不产生真正的 Adaptive 切换 |
| 提前加入 Store 观测 | 最小 PFN Trace 纳入 Patch 8，便于验证热度与实际换出页的关联；完整放置决策 Trace 留给后续 Patch |
| 允许的近似 | 接受过时热度、PFN 复用、采样竞争及一定 VA/PFN 关联误差；这些信息只能影响将来的放置质量，不作为读取地址或数据正确性的依据 |
| 规模 | 延续 Task 2 的容量边界，不把实现写死为当前 48/96 GiB；监控内存按目标进程观察到的页数增长，本轮没有新增 TB 级硬件验收 |
| 核心验收 | 独立冷热切换＋双节点小工作集关联/内容校验＋一次 BFS 摘要集成；不扩展单/双节点组合矩阵、故障注入或性能门槛 |

不设置独立 TTL **不等于无限保留页面热度**：历史仍由同一个采样窗口淘汰。
保留换出页历史也不表示旧 PFN 仍属于该页；离线查找只使用快照中的当前 resident PFN。

## 范围与设计

本 Patch 实现独立用户态监控、可控冷热程序和最小 Client 观测点。
Client 版本 **1.8**；控制协议仍为 **v2**，stats 仍为 **v2**，沿用 managed Frontswap v2 内核。
Server、内核核心、静态 Interleave、RDMA Completion 与页面生命周期不变。
尚未实现动态 Slot、热度上传、Adaptive 放置或迁移；本 Patch 不要求 DRAM 带宽高于 PMem。

实现位置：

- `tools/rswap_hotness.c/.h`：PID/启动命令、pagemap/Idle Tracking、窗口与日志。
- `tools/rswap_hotness_check.py`：离线关联此前快照与实际 Store 提交。
- `client/rswap_trace.h`、`rswap_rdma_ops.c`：默认关闭的 `rswap:store_sample`。
- `tests/hotness_pattern.c`：两个线程共享三个匿名区域，前后交换冷热角色。
- `tests/test_patch8_*.py`：实际生产 C 与离线工具的无特权回归。

关键实现入口：

| 入口 | 职责 |
| --- | --- |
| `rswap_hotness.c:main()/launch()` | 解析配置、启动/附加 PID、建立计时起点及运行生命周期 |
| `collect_process()/collect_round()` | 每轮重新打开 proc 文件，完成映射枚举、页面筛选、Idle 读取、映射复核、窗口推进与下一轮标记 |
| `rswap_hotness.h:heat_push()/heat_ppm()` | 用有效/活跃位图维护最近 N 轮，区分未知和已知 0 |
| `snapshot()/run_metadata()` | 输出同代逐页热度、均值、就绪时间及运行起止/退出结果 |
| `rswap_rdma_ops.c:rswap_store_prepare()` | 仅在观测事件启用时保存入口时间/PFN，提交返回后记录 `store_sample` |
| `rswap_hotness_check.py:read_summary()/read_pages()/correlate()` | 验证快照完整性与均值，只用此前就绪的快照关联 Store 并统计原因 |

### 采样语义

每轮记录 `1=活跃`、`0=已观测未访问`、`?=无效/不可观测`。
最近 N 轮的热度为 `active / valid`，N 为 **1..64 轮**，默认 20。
更新周期默认 1 秒，允许 0.001..86400 秒；Warmup 默认 20 秒，允许 0..86400 秒。
有效历史用两个 64 位 Bitmap 保存，不增加独立 TTL 或 cooldown。

例：`[1, 0, ?, ?]` 的热度是 1/2；`[0, 0, ?, ?]` 是已知冷页；
`[?, ?, ?, ?]` 是未知。无效轮次也推进窗口，历史不会无限保留。
初次看到页面只建立 Idle 基线，不把首次观测计为冷。
逐页 `heat_ppm=-1` 表示未知；整轮没有已知页时 `mean=-1`，不是把均值当成 0。
窗口按实际完成的采样轮次推进；扫描或日志输出超过周期时不补造采样，
记录 `overrun=1` 和真实时间，所以 N 轮不承诺严格等于 N×周期秒。

按 VA 保存历史；同一 VMA 内 PFN 改变，跳过变化轮次并重新建立 Idle 基线，保留此前 VA 历史。
可检测的 VMA 描述符变化（含边界改变）或未映射/未触页 PTE 会清除历史。
换出页保留窗口内历史并参与均值，但 `last_pfn` 只表示历史，不能当作当前 PFN。
平均值对已知页面等权，包含热度 0、排除未知；当前 PFN 或 swap entry 的别名只取一个代表，
所有 VA 仍保留在逐页日志中。映射变化中暂时无法去重的历史属于近似误差。
一个快照中的逐页热度与均值来自同一轮窗口状态；已换出但窗口历史仍有效的页也参与均值。

扫描目标 PID 的可读 VMA，每个共享地址空间只扫描一次，不逐 TID 重复扫描，也不跟踪 fork 子进程。
使用 kpageflags 的 SWAPBACKED 筛选普通匿名及 shmem/tmpfs 页面，排除普通文件缓存、
THP/compound、hugetlb、zero page、unevictable、offline 等。
只有符合条件的 LRU 页才标 Idle；检测到映射变化则该轮无效。
PFN 按物理顺序分组：kpageflags 按 512 页块读取，Idle Bitmap 按 64 页字读取，
写入只包含目标 PFN 的置位 mask，不清除其他页的位，也不扫描整个宿主机物理内存池。

本机内核 `mm/page_idle.c` 仍可能在竞争中跳过页面；前后 PFN 检查不能排除 ABA、
PFN 复用、COW、共享页被其他进程访问等误差。采样也会操作 Accessed/Young 状态，
不能宣称完全无扰动。不锁定或 pin 目标页、不强制 fault-in 页面、不读取页面内容。
持久接口错误/PFN 隐藏明确报错并停止，不能伪装成长期未知。

### 运行与输出

```bash
make -C remoteswap/tools

# 输出目录必须不存在；启动命令时，在释放子进程执行前设置 CLOCK_MONOTONIC 起点。
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p8-example \
  --interval 1 --window 20 --warmup 20 --pages -- /path/to/program arg1

# 附加已有 PID 时，从附加时开始计时，不追溯程序真正启动时间。
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p8-attach --pid 12345 \
  --interval 1 --window 20 --warmup 20
```

需要 4 KiB 系统页、目标进程 proc 访问权、CAP_SYS_ADMIN/PFN 可见性，
以及 `/proc/kpageflags` 读取与 `/sys/kernel/mm/page_idle/bitmap` 读写权限。
建议在宿主机运行，不在受限的容器里采样。

- `summary.tsv`：每轮提交一行，含 PID、代次、单调时间、warmup/observe、页数、
  本轮 active/idle/invalid、窗口内 known、均值、扫描耗时和 overrun。
  `pages`/本轮样本按 VA 计数；`known`/均值按去重后的代表计数。
- `--pages`：另写 `pages-NNNNNN.tsv`，包含 VA、当前/历史 PFN、状态、窗口计数、
  heat_ppm（未知=-1）、alias、swap type/offset。关闭该选项时仅输出摘要，不能做逐页离线关联。
- `run.tsv`：结束时保存 PID 身份、起止时间、配置、监控错误与退出状态。
- 控制台：每轮摘要，与目标程序原有输出共用 stdout；结构化文件不混入程序输出。

`ready_ns` 在逐页文件完全写入并关闭后记录。对某次 Store，只能使用
`snapshot.ready_ns <= store.start_ns` 的最近快照；这是用户态快照就绪时间，不是内核上传完成时间。
Warmup 结束后的状态叫 `observe`，仅表示继续观察，**不是已启用 Adaptive**。
采样工具处于受限 workload cgroup 外；CLI 启动的程序继承其凭据，程序自行产生的线程共享监控范围。

目标退出或 PID 身份改变时结束；正常启动模式返回目标退出状态。
Ctrl-C/SIGTERM 停止采样，启动模式同时转发信号给直接启动的子进程，附加模式不向目标发信号。
不管理进程树、不保证忽略信号的子进程有硬超时。每轮重新打开 proc 文件，支持启动包装中的 exec；
若 exec/地址空间变化竞争导致读取 EIO，清除 Idle 基线并重试一次，持续失败则明确退出。
采样失败时启动模式终止自己启动的直接子进程，以免程序继续在无人观察的状态下运行。

大量逐页 TSV 会消耗内存、CPU、磁盘空间与时间；只在需要关联的实验窗口启用。
内存主要是前后两份紧凑页面数组和临时 PFN 索引，随观察到的页数增长，
不按远端池容量分配用户态热度。没有固定性能门槛，但需记录扫描超期情况。

### 最小 Store 事件与分析口径

```text
store_sample: start_ns=123 pfn=456 swap_type=0 offset=789 submit_ret=0
```

`start_ns` 在 Store 提交前由 `ktime_get_ns()` 取得，与用户态 CLOCK_MONOTONIC 对齐；
不依赖 ftrace 当前选择的时钟。事件在提交返回后发出，禁用时不额外读取时钟/PFN。
`submit_ret=0` 只表示提交被接受；同步 Store 随后的 WC 等待仍可能失败。
事件不等于 Frontswap 有效位已经发布，也不等于最终写成功。
WC 成败仍使用既有 stats/placement，不在本 Patch 改其格式或含义。

分析器按 Store 入口时间排序，只使用 `ready_ns <= start_ns` 的最近快照，
不使用后来的采样补认先前热度；只在监控起止范围内关联。
报告提交失败、无先前快照、PFN 不在表中、未知热度、匹配热/冷以及快照年龄。
已换出页的 `last_pfn` 不会加入当前 PFN 查找表。

匹配比例分母是**采集窗口中监控运行期间的全部 Client 已接受 Store**，不是精确 BFS Store 数。
当前 PID 可能是 kswapd，不能作为页面 owner。PFN 匹配也不能证明物理页身份完全正确。
逐页日志中的 VA→swap type/offset 转换可辅助人工核对归属；无法归属的样本不冒充精确命中率。
没有额外的命中率百分比门槛，但硬件关联验收至少要有已知热度匹配样本。

```bash
sudo python3 remoteswap/tools/rswap_hotness_check.py \
  /tmp/hermit-p8-example/summary.tsv /tmp/hermit-p8-store.log \
  --buffer-stats /tmp/hermit-p8-buffer.stats --require-match \
  --detail /tmp/hermit-p8-matches.tsv
```

输出 JSON 摘要和口径说明。不会使用未来快照匹配；无事件、格式错误、不完整快照、异常监控退出、
丢失事件标记、非零 buffer overrun/drop 或 `--require-match` 下零匹配都返回失败。
可正常分析附加模式下由 Ctrl-C/SIGTERM 结束且监控无错误的完整快照。
不提供 buffer stats 时会明确说明没有认证缓冲区无丢失。

## 本轮验收结论与已知限制

本地编译、42 项无特权回归及采样核心 ASan/UBSan 检查已通过，三项实机核心检查也通过。
本轮没有改 Server、内核核心、Completion 契约或实际放置规则；最终无活动 swap，Client 已卸载，
测试 cgroup/Trace instance 已清理，原配置未改。完整证据统一保存在核心测试文档所列路径。

验收结论保留以下边界：

- Store 匹配的 100% 只针对本次采集窗口，分母为全部被采到且在监控运行期间接受的 Client Store。
  本次匹配均为冷页；这不是目标 PID 的精确身份准确率，也没有证明热页 Store 的硬件覆盖。
  该结果证明用户态先有快照、随后能离线查到 Store 的 PFN 热度；不等于已经测得内核热度查询命中率。
- BFS 本轮仅保存摘要，验证正常完成、Warmup/observe、已知热度和均值输出，没有做 BFS 逐页 PFN 关联。
  它也不验证 BFS 算法结果或应用性能提升。
- BFS 24 轮采样中 20 轮超期，最大扫描约 17.56 秒。配置周期 1 秒没有在大工作集下按时完成，
  窗口仍按实际轮次推进；不能把默认 20 轮解释成固定 20 秒。
- BFS 大工作集阶段已知历史页数曾低至 49，随后达到约 985.5 万，覆盖有明显波动。
  VMA 描述符变化会按现实现重置历史，换页/采样竞争也会减少有效样本；
  本轮没有独立量化各原因的贡献。均值仅代表当时有有效历史的目标子集。

上述采样耗时、覆盖变化和逐页日志量是后续接策略时需要保留的工程限制，
本轮按功能验收，没有新增性能门槛或为提高匹配数字修改采样规则。

## 后续 Patch 的已确认约束与待明确事项

以下是本轮已经确认、但 **Patch 8 尚未实现** 的约束：

- **映射生命周期跟随当前 Frontswap invalidate。** 一次 invalidate 结束当前映射生命周期；
  后续 Store 可重新选择位置。无需为“同一 Linux swap entry 永远不换位置”增加内核接口。
  当前内核重复 Store 前会失效旧副本，因此重复 Store 允许建立新位置。
- **仅在新的 Store 放置决策中使用热度。** 有效映射期间位置保持，Load 根据既有映射取数据，
  不因换入立即释放远端位置，不做主动迁移。动态 Slot 需等 invalidate 且无在途引用后才回收。
- **冷热识别和 Placement 分离。** 用户态扫描不进入 Swap-out 路径；Client 将来查询已发布的热度。
  未知页按 Interleave，Adaptive 中严格 `h > mean` 选择 DRAM，`h <= mean` 选择 PMem。
  单 Server 选对应 Tier；双 Server 在目标 Tier 内 RR，目标 Tier 容量不足允许 Fallback。
- **保留现有规模和兼容性边界。** 继续基于 4 KiB、1/2 个 Server、一个活动 swap area 和已发现的静态拓扑，
  不把 96 GiB 当作算法常量；不新增副本、故障迁移、运行中扩缩容或持久化恢复语义。
- **后续实验期望启用 Adaptive 后更多远端访问落到 DRAM。** DRAM 带宽高于 PMem 是后续实验期望，
  不是 Patch 8 的验收条件；本轮明确不设吞吐提升或监控开销数值门槛。

后续设计仍需在对应 Patch 中明确：

| 阶段 | 仍待具体设计/确认的内容 |
| --- | --- |
| Patch 9：Dynamic Mapping | 映射对象/Bitmap 的具体组织、锁和引用关系、提交失败回滚、并发 invalidate/旧 I/O 收尾、实际元数据预算 |
| Patch 10：Adaptive Placement | 用户态到 Client 的传输接口、热度/均值整代发布、Start/Stop 与采样器异常退出时的内核行为、自动/手动切换的具体接口、RR/Fallback 尝试顺序 |
| Patch 11：效果验收 | 带宽比较采用读、写还是读写总量，跨 Server 汇总与统计时间窗，是否要求 BFS 本身满足 DRAM 高于 PMem，以及 BFS 的实际热度/Store 覆盖 |

这些具体方案不能从 Patch 8 的文件输出或 observe 标签推断为已经存在。
未来继续 Interleave 指保留首选 Server/Tier 选择规则，实际 Slot 交给动态分配器；
不能在 Adaptive 使用过池空间后假设 offset 算出的静态 Slot 仍空闲。

CPU 页访问热度也不等于远端 I/O 频率：热页可能一直驻留 Client，Warmup 放置的旧页不会主动迁移，
窗口活跃度还可能饱和。若所有已知页均为 1，严格大于均值的页为空，按既定规则都属于 PMem 候选。
因此后续带宽实验需同时解释实际 Store 覆盖及后续读写模式，不能仅凭热点识别宣称 DRAM 访问一定更多。
带宽继续复用 stats v2 的 WC 成功页数，按 `Δok × 4096 / Δt` 换算逻辑数据带宽；
用户态热度不计作 RDMA 流量，读/写/总量及比较窗口的最终验收口径在后续明确。

## 复测与协作执行约定

需要 sudo 的宿主机命令由操作者执行，助手负责准备具体脚本、读取日志与核对结果。
交互式页面程序的两个 READY 是测试同步点，不是进程卡住；监控摘要在暂停时仍持续输出。
手动复测可按核心文档在原终端发送 ENTER；自动续测应由控制器持有 stdin 管道，
第一次在 Trace 开启并有先前热度后释放，第二次在活进程 swapoff 成功后释放。
不把跨 sudo/PTY 的终端编号或输入注入当作可靠的自动化接口。

本次自动续测脚本位于 `/tmp/hermit-p8-core/continue_patch8.py`，只是实验控制器，
不是采样器的正式 API，也不是下一个 Patch 的运行依赖。首次未完成运行和有效验收运行的日志均保留，
具体路径及主动中止退出码的解释见核心测试文档。

## 测试与验收

可直接复制的准备、冷热识别、双节点 Store 关联、BFS 摘要监控和正常收尾步骤，
以及实现阶段的本地检查记录，统一见 [Patch 8 核心测试](patch8-core-smoke.md)。
本文件保留设计、已确认决策、接口口径与结论；具体操作和完整验收记录集中在核心测试文档。
