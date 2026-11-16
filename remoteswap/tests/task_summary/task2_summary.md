# Task 2 总结：多节点 DRAM／PMem 页面交错与运行观测

## 核心目标

在 Task 1 单／双节点远端内存池基础上，构建每 Server 的 DRAM／PMem 异构池，
按 Swap Offset 实现 Server 间、Server 内两级静态页面交错，并提供 Tier 级统计与可选 Placement Trace。
保持 8 GiB Chunk、4 KiB Page，复用现有 RDMA Session、Completion 和 Frontswap 生命周期。
本任务不实现任意策略放置、动态 Slot 分配、冷热识别或主动页面迁移。

## 主要修改

| 阶段 | High-level 修改 |
| --- | --- |
| [Patch 5](../patch5-server.md) | Server 分别预分配 DRAM／PMem 匿名范围，触页前设置单节点 NUMA 绑定，逐页查询验证物理放置；按 DRAM 在前、PMem 在后组织 Chunk 并注册 MR。控制协议升级为 v2，报告两类 Chunk 数；全部 MR 成功才宣告可用，串行复用控制 SEND 缓冲区，补充分配／注册失败清理与构建依赖。 |
| [Patch 6](../patch6-client.md) | Client 适配协议 v2，校验跨 QP／消息的拓扑一致性及 MR 范围，保存每 Session 的 Tier 容量。扩展统一映射为 Offset → Server → Tier → Chunk；两层均交错共同容量、较大一侧接尾段，使用 Tier-local Slot 路由现有 RDMA 请求，支持不等容量。 |
| [Patch 7](../patch7-client.md) | 将统计扩展为 Server/Tier × Write／同步 Read／异步 Read 的成功、WC 失败和提交失败；debugfs stats 升级为 v2，Python monitor 显示 Tier 带宽及 Server 汇总。增加默认关闭的 `rswap:placement` 完成事件，以及逐条验证静态映射、汇总 I/O 分布的离线工具和 CLI。 |

主要改动位于 `remoteswap/server/`、`client/`、共享协议 `utils.h` 及 `tools/`。
本任务没有新增内核核心修改，继续使用 Task 1 的 managed Frontswap v2 接口与既有数据 I/O 收尾机制。

## 验证情况

- Server／Client 构建通过；最终 Client 1.7 的 31 项无特权回归通过，覆盖协议、两级映射、
  实际地址解析、快速完成与页面生命周期、Tier 计数、带宽换算及 Trace 校验。
- 操作者完成两台 Server 的 NUMA 分配与各 6 个 MR 注册；Client 完成 v2 容量／描述符发现。
  每台 DRAM 16 GiB＋PMem 32 GiB，对应 2＋4 个 Chunk；单节点池 48 GiB，双节点池 96 GiB。
- Patch 6 单／双节点核心测试完成。双节点使用 48 GiB swap、4 GiB 工作集、2 GiB 物理限制，
  两轮逐页／逐字校验通过；活进程 swapoff 后内容仍正确，随后正常卸载。
- Patch 7 同配置双节点页面测试完成：两轮各 1048576 页校验通过，四个 Server/Tier 均有成功读写，
  WC／提交失败及未归属错误增量为 0。保存的 706963 条 Placement Trace 全部匹配静态映射，
  48 个 CPU 的 overrun／commit overrun／dropped events 计数均为 0。
- BFS 验收：双节点 96 GiB 池＋80 GiB swap，64G 图、40 线程、24 GiB 物理限制、
  1 个计时轮次（保留 Ligra 预热）；退出码 0，有一条 Running time 输出。
  四个池均有成功读写和非零带宽样本，采集期间错误增量为 0；最终 swapoff／卸载完成，
  保存的本轮新增 dmesg 中未见相关 WARNING／BUG／RDMA 错误。
- 3–3.5 TiB 总容量及现有协议上限已做字段范围和生产映射／resolver 边界检查，
  **尚未做 TB 级物理分配、MR、全容量 swap 或 workload 硬件验收**。

以上为核心功能验证，不作为性能对比、BFS 算法结果验证、全部远端地址／尾段覆盖或故障恢复的证明。
实际日志位于本次 Client 的 `/tmp/hermit-p6-core/dual/live/`、`/tmp/hermit-p7/`，未纳入 Git；
流程见 [Patch 6 核心测试](../patch6-core-smoke.md) 和 [Patch 7 核心测试＋BFS](../patch7-core-smoke.md)。

## 注意事项与保留边界

- 继续限定 **4 KiB、1／2 个 Server、一个活动 swap area**；测试禁用 THP。
  两个 Tier 都须为正的 8 GiB 整数倍；每台 DRAM＋PMem 合计最多 400 Chunks／3200 GiB，
  两台最多 6400 GiB。3.5 TiB＝3584 GiB 总池须分布到两台；配置总量必须等于实际发现总量，
  swap area 页数不得超过池容量。完整启用 3.5 TiB swap 约需 2.73 GiB 基础 Client 元数据，另有管理开销。
- PMem 已转换为 system-ram；Server 按本机已验证 Node 配置分配，CPU-less 识别只是部署约定。
  使用新命名启动参数，旧位置参数不兼容；`run_server.sh` 的 IP／Node 必须按各台机器检查。
  Client 在线 CPU ID 仍须从 0 连续且固定，Server 核数参数取 Client 在线核数；QP 保持串行建连。
- Server 编号取 endpoint 顺序，原始 offset **不减 1**。两层各自处理尾段；
  16＋32 GiB 的双节点池前 64 GiB 逻辑范围四路交错，后 32 GiB 仅在两台 PMem 间交错，
  不是全程按 1:2 加权。DRAM／PMem 地址无需连续或按地址大小排序；拓扑在连接期间不可变。
- 最终使用 Client **1.7**、控制协议 **v2**、debugfs stats **v2**；三者与内核符号
  `frontswap_register_managed_ops_v2` 是不同版本标识。新 Client 须配套新 Server；统计工具不兼容 stats v1，
  沿用 Task 1 已部署的配套内核即可，不需为本任务重新安装内核。
- 统计按模块生命周期累计、重载清零；成功带宽为 WC 成功页数据的逻辑带宽。
  Placement Trace 默认关闭，记录终止 WC，需短窗口持续采集并检查丢失；
  计数／Trace 描述 I/O 分布，不是实时驻留页数或容量使用率。异步 Read 为 0 允许，不能据此宣称覆盖异步硬件路径。
- 静态位置跟随 Linux entry／Frontswap invalidate，换入后不立即释放。
  正常顺序仍为 **Server 在线 → swapoff 成功 → 停止采集／monitor 并卸载 Client → 停止 Server**；
  重连前重启参与的 Server。不实现副本、自动重连或故障迁移，也不承诺 PMem 在进程／机器重启后恢复 swap 数据。
- **既有 cgroup swap 记账 bug 未修复**；仅用物理限制制造压力，解除有限 memsw 配额，
  `batch_account` 保持关闭，不以 cgroup swap 记账作为验收依据。其余错误与生命周期边界沿用 [Task 1](task1_summary.md)。

参考主要实现提交：`3054d27d2`、`f559ab59d`、`1017bf93d`、`52cb699c3`；
核心测试与工具见 `92acc1847`、`a47868a19`。
