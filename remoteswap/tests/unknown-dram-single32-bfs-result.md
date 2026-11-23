# Client 1.13：单节点 32/32 GiB BFS 带宽观察

## 结论

按要求将 node04 单节点池改为 **32 GiB DRAM＋32 GiB PMem**，只运行一轮 BFS，
同一进程先 Interleave、再 Adaptive，没有追加功能测试、对照或调参重跑。
**本轮满足带宽观察目标：Interleave 两 Tier 近似相等，Adaptive DRAM 总带宽约为 PMem 的 2.39 倍。**
DRAM 使用峰值约 29.82 GiB，没有容量回退，读写流量均偏向 DRAM。

本轮 1 秒短 decision Trace 有缓冲区丢事件，不能认证该窗口的完整逐决策行为；
正式逐决策校验保留失败/未认证状态，没有通过忽略丢失标记宣称通过。
带宽、分配及原因比例均来自独立的累计 stats/policy_stats，不依赖该 Trace。

## 固定配置与执行

- Client **1.13**、placement_rule=1、上传 ABI v2、#8 内核，沿用此前模块及采样器产物。
- Server 0=node04 / 10.0.0.13；DRAM NUMA 0、PMem NUMA 1，各 32 GiB，总池 64 GiB。
- 两 Tier NUMA misplaced=0，4+4 共 **8 个数据 MR** 注册成功；本轮不启动 server02。
- 既有 **64 GiB swap**，覆盖范围内两 Tier 均衡 Interleave，无 PMem 单独尾段；不创建或格式化 swap。
- 64G 文本图、40 线程、24 GiB memory cgroup、一次 BFS 计时轮次并保留程序预热；memsw 无有限配额。
- 采样器在 workload cgroup 外；capacity-mean、reserve=1 GiB，interval=1s、window=4、warmup=20s、manual。
- 启动后约 **29.07s** 首次远端 I/O；观察约 **69.37s** 实际 Interleave I/O 后，
  于启动后约 **98.44s** 切 Adaptive。当时有新鲜容量裁剪阈值 **T=0**，原始 mean=25641 ppm。

与此前单节点 16/32 GiB 运行相比，DRAM 从 16 增到 32 GiB、swap 从 48 增到 64 GiB；
实际切换时机也因等待新鲜阈值而不同。本轮不是只改变单一变量的严格性能 A/B。

## 完整稳定阶段带宽

单位 MiB/s，按成功 RDMA WC 页数×4096/实际时间计算。
按内核 state/run_id 划分相邻样本，跨状态边界单列 transition；完整阶段包含无 I/O 时间，
按实际间隔加权，不平均每秒瞬时速率。

| 阶段 | 操作 | DRAM | PMem |
| --- | --- | ---: | ---: |
| Interleave | Read | 118.22 | 118.17 |
| Interleave | Write | 230.98 | 230.98 |
| Interleave | 合计 | 349.19 | 349.14 |
| Adaptive | Read | 222.92 | 139.15 |
| Adaptive | Write | 313.16 | 85.43 |
| Adaptive | 合计 | 536.08 | 224.57 |

稳定阶段时长：Interleave **97.43s**、Adaptive **139.06s**。
Interleave 两 Tier 总带宽差约 **0.0145%**；Adaptive DRAM 的 Read/Write/总带宽分别高
**60.20% / 266.58% / 138.71%**。Adaptive 的 **126 个有 I/O 窗口全部是 DRAM 总带宽更高**。
末尾约 **29.17s** 稳定窗口（含无 I/O 时间）为 DRAM **575.50**、PMem **221.31 MiB/s**。
这些是访问流量，不代表介质峰值或 BFS 加速倍数。

## 分布、容量与解释

Adaptive 稳定窗口中按原因累计的成功 Store WC：

| 类别 | 成功写页数 | 写流量占比 | 首选策略 |
| --- | ---: | ---: | --- |
| 未知 PFN | 7996790 | 56.36% | DRAM |
| 已知零热度 | 6079069 | 42.84% | 原 Interleave |
| 已知正热度 >=T | 113228 | 0.80% | DRAM |
| 已知正热度 <T | 0 | 0.00% | PMem |

未知页是主要的 DRAM 偏好来源。未知与正热度约占 57.16% 写流量，其余零热度近似两 Tier 均分，
因此 DRAM 写流量约占 78.58%，与记录的 Write 带宽比例一致。
阈值 T=0，零热度仍走显式 Interleave，故没有正热度 PMem 分类。
累计接口分别读取，窗口计数之间存在少量在途/读取时序差异，不把几页差异解释为数据错误。

DRAM/PMem 采样占用峰值分别为 **29.82/19.39 GiB**，各自低于 32 GiB。
全程同 Tier/跨 Tier Fallback 和分配错误均为 **0**，首选池与实际分配池计数一致。
本轮较大的 DRAM 容量允许 DRAM 首选实际落地；均衡池也消除了此前单节点映射的 PMem 尾段。
Read 会访问已有 Warmup/Adaptive 映射，没有主动迁移，未保存 Read 原始热度原因；
不能把上述成功写入分类比例直接扩展为总读写分类比例。

## 短 Trace 的限制

本轮一次 1 秒短 Trace 保留 **236114** 条 decision 文本记录（未知 161368、零热度 74746）。
保存的 per-CPU buffer stats 汇总 **overrun=24150**，commit overrun/dropped events 均为 0；
文件也含 LOST EVENTS 标记。完整性检查明确拒绝，未进行正式逐决策/逐 publication 认证。
这些保留条数不是窗口全部决策，也不能作为总体放置比例。

原始失败分析日志、原始脚本及随后用于容纳“不完整 Trace”结果的离线汇总脚本全部保留。
没有修改生产校验器的拒绝逻辑，没有清除原始丢失标记，也没有重新采样或重跑 BFS。

## 收尾与证据

BFS/采样器退出码 0，仅一条 `Running time : 0.174`，该值不是端到端时间。
采样约 **237.64s**，33 轮/33 代发布，22 轮超期；最长扫描 **17.27s**，最长上传 **1.95s**。
新增 WC/提交/未归属错误均为 0，分配错误为 0，新增内核日志无相关 BUG/WARNING/错误。
swapoff 后两池 live=retired=0、free=capacity，累计计数 settled 检查通过。
Client 卸载，node04 正常释放资源并退出，原双节点 Client 配置及保存的系统参数恢复。

完整证据位于 `artifacts/unknown-dram-single32-bfs-20261123/`（Git 忽略），包括
`summary.json`、原始 samples、热度摘要/分布/publication、Trace 与 buffer stats、
`trace-buffer-loss.json`、Server/内核日志、产物 hash、控制器及分析脚本。

[本轮带宽与策略时间序列图](artifacts/unknown-dram-single32-bfs-20261123/bfs/bandwidth-hotness.png)
