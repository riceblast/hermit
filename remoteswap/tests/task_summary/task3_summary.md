# Task 3 总结：页面冷热识别、动态映射与自适应放置

## 核心目标

在 Task 1／2 的单／双节点异构远端内存池基础上，连接指定进程的用户态冷热观测、
Client 动态 Slot 管理和 Swap-out 放置策略，实现按 4 KiB 页面选择 Server／Tier，
并提供热度、容量、放置决策及成功 RDMA 带宽的运行观测。
保持现有 Frontswap、RDMA Session 和 Completion 机制；仅对新 Store 决策，不主动迁移已有页面。

## 主要修改

| 阶段 | High-level 修改 |
| --- | --- |
| [Patch 8](../patch8-monitor.md) | 新增独立 `rswap_hotness` 工具，通过 pagemap＋Idle Page Tracking 观察指定 PID 及共享地址空间线程，以有效采样周期中的活跃比例维护窗口热度和均值。输出摘要／可选逐页日志；新增默认关闭的 Store PFN 观测事件和离线关联工具，区分已知零热度与未知。 |
| [Patch 9](../patch9-mapping.md) | 用稠密 offset→Slot 索引、每 Server／Tier 空闲链表和 Slot 引用计数替代静态物理位置。Store 分配，Load 查既有映射，Invalidate 摘除索引，最后一个在途引用退出后回收；完善重复 Store、发布、失败回滚及 swapoff 收尾。新增容量／映射 Trace，并修复 `mm/memory.c` 中读取失败后先复核 PTE、再决定重试或报错的路径。 |
| [Patch 10](../patch10-placement.md) | 新增 `rswap_placement.c/.h`，用户态按代上传 PFN 热度和阈值，staging／COMMIT／RCU 整代发布，Store 二分查询。实现显式 Start／Stop、Inactive／Warmup／Adaptive、自动／手动切换、Tier 内 Server RR 和容量 Fallback；新增 decision Trace。将 mapping／placement 文本改为数字字段，规避本机 5.14 Trace 字符串校验告警。 |
| [Patch 11](../patch11-monitoring.md) | 新增模块生命周期累计 `policy_stats`、以 alloc_seq 关联的 Store post／WC 结果，以及只读 pool-status／monitor／report CLI；复用 Tier I/O 计数计算带宽。扩展逐 publication／热度／映射／结果链校验。后续加入 capacity-mean、阈值保护与来源记录，并完善零热度 Interleave、未知页 DRAM 默认策略及历史日志兼容。 |

主要改动位于 `remoteswap/client/`、`tools/`、共享 `rswap_placement_uapi.h` 和测试／分析工具，
另有 Patch 9 必要的内核 fault 路径修复；Server 实现、RDMA 协议和既有 CQ／poll／drain 机制不变。

最终使用 Client **1.13**，当前规则见 [未知页 DRAM 默认策略](../unknown-dram-placement.md)：

| 状态／热度 | 首选位置 |
| --- | --- |
| Inactive／Warmup | 原 offset 两级 Interleave |
| Adaptive，无快照／无可用阈值／PFN miss | DRAM，目标 Tier 内 Server RR |
| Adaptive，已知热度 0 | 原 Interleave，不消耗 RR ticket |
| Adaptive，已知正热度 `h >= T` | DRAM，目标 Tier 内 Server RR |
| Adaptive，已知正热度 `h < T` | PMem，目标 Tier 内 Server RR |

未知与已知热页共享 DRAM RR；首选池不足依次尝试另一 Server 同 Tier、首选 Server 另一 Tier、
剩余池，单节点跳过不存在的候选。原 Interleave 只决定首选 Server／Tier，实际 Slot 均动态分配。
策略作用于全部 Client Store，包括监控地址空间以外的 PFN；未知保持独立原因，不算作已知热页。

阈值默认使用 **global-mean**；显式启用的 [capacity-mean](../capacity-mean-placement.md)
根据有效本地容量估计换出群体，从已知分布中裁掉预计留在本地的最热部分，以剩余样本均值为 T。
原始均值独立保留；零热度参与均值，未知不按零计入。覆盖不足、样本过少或预测无换出时，
优先继承同一 run 的旧阈值，无历史则用全局均值兜底，并记录来源、状态和原始计算代次。

## 验证情况

- Client／用户态工具构建通过；最终 1.13 的 **84 项无特权回归**通过，覆盖窗口／阈值、
  实际上传事务与状态控制、动态映射、共享 RR／Fallback、Store／Load／失败收尾及新旧日志兼容。
  生产函数的用户态 stub／部分 sanitizer 检查不替代内核、HCA、DMA 或完整并发硬件验收。
- Patch 8 实机冷热切换由 1.0／0.0 变为 0.0／约 0.999817；522162 条已接受 Store
  匹配到此前已完成快照中的已知热度。另完成双节点页面内容和 BFS 集成，证明观测链可用，
  不把 PFN 近似匹配解释为精确页面身份或访问次数。
- Patch 9 在修复后的 #8 内核完成双节点四线程两代逐页／逐 word 内容校验、活进程 swapoff、
  同 Session 再 swapon 及 BFS；Slot 全部归还，无新增相关 I/O／内核错误。
- Patch 10 完成双节点可控冷热／内容／生命周期及 BFS；可控运行有 16384 次放置决策、
  热冷各 8192 次及 98304 条完整映射事件，实际快照／阈值／位置和最终回收校验通过。
- Patch 11 的 **1.11 历史核心验收**完成单节点 Warmup、单节点 Adaptive、双节点 Adaptive 和 BFS。
  三个可控运行各有 16384 条完整 Store post／WC 链及 98304 条映射事件，逐 word 内容、
  活进程 swapoff、累计统计、模块／运行生命周期及 buffer 零丢失检查通过。
  这些证据对应当时策略，不替代后续 1.12／1.13 规则的专项验收。
- 1.12 capacity-mean 完成 81 项本地回归及一轮双节点 BFS；Adaptive DRAM／PMem
  总带宽为 351.69／346.88 MiB/s。随后 1.13 按要求跳过独立功能专项，分别运行以下三轮 BFS：

| 1.13 BFS 配置 | swap | Interleave DRAM／PMem | Adaptive DRAM／PMem | 容量回退 |
| --- | ---: | ---: | ---: | --- |
| [双节点，各 16／32 GiB](../unknown-dram-bfs-result.md) | 64 GiB | 386.42／386.39 | 524.25／198.81 | 0；DRAM 约为 PMem 的 2.64 倍 |
| [单节点 16／32 GiB](../unknown-dram-single-bfs-result.md) | 48 GiB | 336.51／375.59 | 217.20／332.04 | 4995599 次 DRAM→PMem；未达到 DRAM 更多的目标 |
| [单节点 32／32 GiB](../unknown-dram-single32-bfs-result.md) | 64 GiB | 349.19／349.14 | 536.08／224.57 | 0；DRAM 约为 PMem 的 2.39 倍 |

表中为成功 RDMA **Read＋Write** 逻辑带宽，单位 MiB/s，双节点按 Tier 求和。
三轮均为 64G 图、40 线程、24 GiB 物理限制、1 次计时轮次并保留程序预热，
使用 capacity-mean、1s／4 轮窗口及最短 20s Warmup，在有实际 I/O 后手动切 Adaptive。
按完整稳定阶段和实际间隔加权，跨状态窗口单列；各轮池容量、swap 及实际切换时机不同，不是严格 A/B。

三轮 BFS／采样器均退出 0，无新增 WC／提交／未归属或分配错误，swapoff 后所有 Slot 归还，
Client／Server 正常退出，原配置和保存的系统参数恢复。单节点 16／32 GiB 的 DRAM 满池，
大量首选请求回退 PMem；32／32 GiB 的 DRAM 峰值约 29.82 GiB，无回退。
双节点和 32／32 GiB 的写流量偏好主要由未知页默认进入 DRAM 驱动，不是大量已知热页的证明。

32／32 GiB 本轮短 decision Trace 的 **overrun=24150**，完整逐决策验证未认证；
其带宽及分类统计来自独立累计 WC／policy 计数，不依赖该 Trace。其余两轮短 Trace 零丢失，
但均不是全程逐页／映射／post／WC 验收。原始日志、图表及分析脚本见各报告链接的 artifacts 目录。

## 注意事项与保留边界

- 继续限定 **4 KiB、1／2 个 Server、一个活动 swap area**，测试禁用 THP；每 Tier 为正的 8 GiB
  整数倍，配置总量须等于发现容量，swap 不大于总池。节点编号、原始 offset 不减 1、固定在线 CPU
  等约束沿用 [Task 2](task2_summary.md)。Interleave 仍为共同容量交错、较大一侧接尾段，不是全程加权均分。
- 动态映射跟随 Frontswap entry 生命周期：Load／Swap-in 不释放位置；Invalidate 摘除索引后，
  最后一个在途请求退出才回收 Slot。重复 Store 由现有前端先 invalidate，新 Store 可重新放置；
  Stop、快照更新和阈值变化均不移动既有页面，Load 始终查询实际位置。
- 使用配套 **#8 内核**及 managed Frontswap v2，不能只凭 `uname -r` 区分旧内核；
  Client **1.13**、Server RDMA 协议 **v2**、本地上传 ABI／policy_stats／placement.tsv **v2**、
  I/O stats **v2** 和 `placement_rule=1` 是不同标识。ABI v1 上传者不兼容；分析工具按标识兼容历史策略。
- 动态索引／Slot 主体元数据按**整个池容量**预分配，约 **12 bytes/page**：96 GiB 池为 288 MiB，
  3.5 TiB 池为 10.5 GiB；另有 Linux swap、页表和热度快照开销。PFN 上传记录为 16 bytes／条，
  替换时可同时存在新旧两代。尚未做 TB 级物理分配、MR、全容量换页或 workload 硬件验收。
- 采样只跟踪一个地址空间及其线程，不递归跟踪 fork。需 pagemap PFN 可见性、Idle Tracking 及相应权限；
  热度是有效周期内活跃比例，窗口 1..64 轮，未知不等于零。PFN 复用、过时快照及关联误差只能影响放置质量。
  不设独立 cooldown／TTL；内核可继续使用旧快照，扫描超期时不保证每秒发布，也不保证监控无扰动。
- Start 才激活策略；启动程序在 exec 前计时，附加 PID 不追溯原启动时刻。手动 Warmup 到期后仍需显式切换。
  Stop、上传 owner 最终关闭或 swapoff 清除本轮策略；新 swapon 需重新 Start。卸载前停止持有模块引用的采样器／collector。
- `mapping` 的 used=live＋retired，包含预留／退休位置，不是精确 Frontswap 有效页数；累计策略计数跨 Stop／swapoff
  保留、模块重载清零。各计数读取不是整组事务，只在入口关闭且 drain 后核对 settled 等式。
  decision 只表示分配尝试，不等于 post 接受、WC 成功或 entry 仍有效；正式 Trace 验收须完整窗口且无丢失。
- **DRAM 带宽高于 PMem 是观察目标，不是正确性保证**；容量不足、零热度 Interleave、偏移尾段及旧映射读流量
  都影响结果。BFS 正常退出与带宽达标不证明算法结果、逐页内容或稳定性能收益；Read 未保存原始热度原因，
  不能把 Store 分类比例当作总读写比例。
- **尚未完成**新阈值／未知默认规则下独立可控页分类、逐 word 内容及活进程 swapoff 专项，
  受控容量／覆盖变化与阈值继承专项、同条件阈值策略 A/B 和重复性测试；已有实机跨 Tier 回退观察
  不代表全池耗尽、Store 写失败后的磁盘回退或完整异步／故障注入验收。不实现主动迁移、副本或自动重连。
- 正常顺序保持 **Server 在线 → swapoff 成功 → 停止采集并卸载 Client → Server 退出**，重连前重启 Server。
  **既有 cgroup swap 记账 bug 未修复**；仅限制物理内存、解除有限 memsw 配额，`batch_account` 保持关闭。
  其他错误与故障边界沿用 [Task 1](task1_summary.md)。

参考主要实现提交：`46117f95c`、`9195bd9a0`、`c326edf2b`、`dfc7dead9`、`ba6d90de8`、
`eb0237108`、`66749c72c`；核心流程见 [Patch 9](../patch9-core-smoke.md)、
[Patch 10](../patch10-core-smoke.md)、[Patch 11](../patch11-core-smoke.md) 与 [BFS 手动指南](../bfs-end-to-end-guide.md)。
