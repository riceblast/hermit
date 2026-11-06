# Task 1 总结：单／双节点远端内存池

## 核心目标

以最小修改扩展 Hermit，支持 1 个 Client 连接 1／2 个 Memory Server，将其组织为统一逻辑
Swap 池，完成跨节点 4 KiB 页面读写、正确的 I/O 收尾及基本运行观测。
本任务使用静态映射；PMem 分层、自由页面放置、冷热策略和迁移留待后续任务。

## 主要修改

| 阶段 | High-level 修改 |
| --- | --- |
| [Patch 1](../patch1_summary.md) | 支持单／双 Session，发现各节点实际容量；统一配置入口，完善初始化失败回滚、受控 Frontswap 注册／注销和模块引用保护。 |
| [Patch 2](../patch2_summary.md) | 统一 offset → Server／Slot／Region 解析。单节点直接映射；双节点共同容量按页交错，剩余尾段归较大节点。所有读写使用目标 Session 的队列、DMA device、PD 和密钥。 |
| [Patch 3](../patch3_summary.md) | 多 Session poll／peek／drain；串行处理 DIRECT CQ，保留 SOFTIRQ 完成方式；修复请求所有权、失败回滚、页面锁及 Store 有效位发布顺序。补充必要内核错误传播和 swapoff 检查后开放换页。 |
| [Patch 4](../patch4_summary.md) | 每 Server 分别统计 Write、同步／异步 Read 的成功、WC 失败和提交失败；增加只读 stats 接口及按成功页数计算带宽的 monitor，提供 Server／请求类型／延迟样本。 |

主要改动位于 `remoteswap/client/`，以及 Frontswap、page I/O、swap fault／swapoff 的必要内核路径；
Server 实现和消息格式保持不变。沿用现有 RDMA 与 Frontswap 机制，未引入动态 Slot 分配器。

## 验证情况

- 内核／Client 构建及 22 项本地逻辑回归通过，覆盖映射边界、完成与锁、错误清理和统计。
- 操作者已完成 Patch 1–4 功能测试，包括单／双节点换页、逐页校验及 swapoff／卸载生命周期。
- BFS 验收：单节点 48 GiB 池＋48 GiB swap；双节点 48＋32 GiB 池＋80 GiB swap；
  使用 24 GiB 物理内存限制、64G 图、40 线程、8 轮基线配置。
  保存的日志均有 8 轮 BFS 输出，参与节点均有成功读写延迟样本和非零带宽，采集期间错误计数差值为 0。
- 上述结果为功能验证，不作为性能对比、全部远端地址／尾段覆盖或故障恢复的证明。

## 注意事项与保留边界

- 仅支持 **4 KiB、1／2 个节点、一个活动 swap area**；测试禁用 THP。
  Server 容量按现有 8 GiB Region 管理，Client 配置总量必须等于实际发现总量；swap area 不得大于池容量。
- 节点编号取配置顺序，原始 swap offset **不减 1**；换入后不立即释放远端位置，生命周期跟随 Linux entry／Frontswap invalidate。
- 使用配套的 `frontswap_register_managed_ops_v2` 内核与 Client 1.4；内核 release 名称未变，不能仅凭 `uname -r` 判断是否更新。
  在线 CPU ID 须从 0 连续且运行期间不变，Server 启动核数使用 Client 在线核数。
- 正常顺序：**Server 保持在线 → swapoff 成功 → 卸载 Client → 停止 Server**；重连前重启参与的 Server。
  未接受的 Store 可回退磁盘，但远端独占页读取失败不能读取过期磁盘副本。
  无副本、自动重连或故障迁移；断连可能导致 SIGBUS、swapoff 失败或等待，在途 DMA 收尾不保证硬超时。
- **Hermit 原有 cgroup swap 记账 bug 未修复**。本轮只限制物理内存、解除有限 memsw 配额；不使用 cgroup swap 统计验收，`batch_account` 保持关闭。
- 统计按模块生命周期累计，跨 swapon 保留、重载清零；带宽为成功 RDMA 页数据的逻辑带宽，
  延迟包含 polling 等等待。基线异步 Read 为 0 允许，不能据此宣称该次 workload 覆盖了异步路径。

参考主要实现提交：`8bd555744`、`555b5c7cc`、`0c1deadfb`、`f3f33b61e`、`526832de1`；
最终 BFS 测试方案见提交 `1a8002ee3` 及 [Patch 4 BFS 流程](../patch4-bfs-smoke.md)。
