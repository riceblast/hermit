# BFS：Interleave / Adaptive 阶段带宽排查

## 结论与范围

2026-11-23，在 Client 1.11 / #8 内核、双节点各 16 GiB DRAM＋32 GiB PMem 上完成四轮核心 BFS 实验。
**四轮完整 Adaptive 阶段的 DRAM 合计带宽均低于 PMem。** 部分短时窗口 DRAM 更高，不能解释为整个阶段达标。
所有轮次 BFS/采样器正常退出，最终 swapoff/Slot 回收/累计计数检查通过，无新增 I/O 或内核错误。

本轮没有修改 Client/Server/BFS 逻辑或热度算法，没有主动迁移。
仅改变手动切换流程、采样窗口、本地物理内存限制和 BFS 计时轮次数。
Server 0=node04 / 10.0.0.13，Server 1=server02 / 10.0.0.20，使用既有 80 GiB swap、64G 文本图、OMP_NUM_THREADS=40。
只运行 BFS 核心实验，没有重复可控页完整性测试或进行故障注入。

## 流程与统计口径

用户态采样器从 BFS 启动开始运行，interval=1s、最短 Warmup=20s，使用 `--placement --manual`。
先保留真实 Interleave I/O 至少 15 秒，等待上传记录 >=100000、快照年龄 <=3 秒再请求 Adaptive；
最多等待实际 Interleave I/O 75 秒后切换。保存每轮实际切换依据和时间。

这解决了原先 20 秒自动切换时 BFS 可能尚未发生 swap 的观察问题。
手动模式下，summary 的 `phase=observe` 只表示采样器已过最短 Warmup；
**阶段划分依据内核 placement.state，不能依据 summary 标签。**

- 两 Server 按 Tier 求和，按成功 WC 的 4 KiB 载荷计算 Read/Write/合计 MiB/s。
- 表格为完整阶段的时间加权均值，包含该阶段无 I/O 的时间；另在 JSON 保留只计有 I/O 窗口的均值。
- 状态切换交界窗口单独归入 transition，不混入任一稳定阶段。
- Adaptive 中的 Read 可能读取 Interleave 期间已经放置的页；这是时间阶段带宽，不是每条 Read 的策略来源追踪。
- 带宽反映访问流量，不是 DRAM/PMem 硬件峰值测试。各轮切换时刻、工作集演进和缓存状态不同，
  下表不能用来证明某参数造成性能提升。

## 参数和完整阶段带宽

单位均为 MiB/s。D/P 为 DRAM 合计 / PMem 合计。

| 轮次 | 本地物理限制 | 热度窗口 | BFS 计时轮次 | 首次 I/O（秒） | 切入 Adaptive（秒） |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 24 GiB | 20 轮 | 1 | 30.09 | 100.32 |
| short-window | 24 GiB | 4 轮 | 1 | 30.08 | 80.22 |
| pressure | 16 GiB | 4 轮 | 1 | 31.09 | 59.30 |
| tight-memory | 8 GiB | 4 轮 | 8 | 15.04 | 31.09 |

| 轮次 | 阶段 | DRAM Read | DRAM Write | DRAM 合计 | PMem Read | PMem Write | PMem 合计 | D/P |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | interleave | 147.1 | 279.1 | 426.2 | 147.1 | 279.1 | 426.2 | 1.000 |
| baseline | adaptive | 154.7 | 82.0 | 236.7 | 189.7 | 256.7 | 446.4 | 0.530 |
| short-window | interleave | 149.3 | 279.8 | 429.1 | 149.3 | 279.8 | 429.0 | 1.000 |
| short-window | adaptive | 166.0 | 119.4 | 285.4 | 197.1 | 258.8 | 455.9 | 0.626 |
| pressure | interleave | 81.5 | 216.4 | 297.9 | 81.5 | 216.4 | 297.8 | 1.000 |
| pressure | adaptive | 203.8 | 160.3 | 364.1 | 236.4 | 267.6 | 504.0 | 0.722 |
| tight-memory | interleave | 0.0 | 89.7 | 89.7 | 0.0 | 89.7 | 89.7 | 1.000 |
| tight-memory | adaptive | 182.9 | 178.3 | 361.2 | 206.6 | 243.1 | 449.7 | 0.803 |

最后一轮八次算法计时合计约 **70.01 秒**，采样端到端约 **396.93 秒**；
算法计时没有覆盖全部输入、初始化和退出过程，不能把完整 Adaptive 标签等同于纯算法阶段。

## 热度覆盖与实际 Store

以下仅统计稳定 Adaptive 窗口的决策；覆盖率=(热+冷)/(无快照+无均值+PFN miss+热+冷)。

| 轮次 | 判热 | 判冷 | PFN miss | 已知热度覆盖 | 已知页中判热占比 | 最长扫描（秒） | 采样超期/总轮数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 93441 | 4836644 | 5849636 | 45.73% | 1.90% | 16.90 | 22/31 |
| short-window | 67236 | 4561967 | 8224848 | 36.01% | 1.45% | 16.65 | 21/27 |
| pressure | 118674 | 3091005 | 19041047 | 14.43% | 3.70% | 14.90 | 27/33 |
| tight-memory | 1489907 | 3422011 | 34511415 | 12.46% | 30.33% | 11.04 | 48/58 |

这些是当前 Client 的实际 Store 分类，不是监控 known 页数，也不证明精确 PID/VA 所有权。
PFN 复用、VMA 变化和采样竞争的近似边界仍然存在。

短 Trace（short-window）确实看到：

| PFN | 热度 ppm | 阈值 ppm | 结果 |
| --- | ---: | ---: | --- |
| 203053937 | 250000 | 18785 | reason=5，Server 0 / DRAM / Slot 668477 |
| 203727067 | 0 | 18785 | reason=6，Server 0 / PMem / Slot 2967481 |

这是内核 Store 时使用的热度与分配位置；没有保存 BFS 全量逐页快照，
不把它解释为独立验证了该 PFN 的全部真实访问次数。短 Trace buffer 丢失检查通过，
完整 RDMA 成功/错误仍由累计 WC 统计认证。

## 为什么没有达到预期

1. **实际换出的已知页以冷页为主。** 当前 `rswap_placement.c:rswap_placement_choose()`
   使用热度严格大于均值才判热，否则判冷。规则不保证半数以上 Store 判热；上表显示实际冷热比例。
   没有容量 Fallback 或错误路由的证据。Linux 回收倾向先处理不活跃页；
   热页留在 Client 本地时，也不会产生远端 DRAM 流量。这是对访问分布的机制解释，未逐页标记回收原因。
2. **文本读图的大量内存可能成为冷换页来源。** 本机 Ligra `ligra/IO.h` 中
   `readStringFromFile()` 将整个文本读入匿名缓冲区，`stringToWords()` 创建字符串指针表，
   `readGraphFromFile()` 中 `W.del()` 被注释，两个输入解析对象保留到进程退出。
   当前文件 17713974470 字节（16.50 GiB），顶点 33554432、边 2048484756；
   在本机 64 位指针下，词指针表约 15.51 GiB，二者合计约 **32.01 GiB**，尚未计图结构和其他临时对象。
   它们本身已超过 24/16/8 GiB 配额，说明实验从初始化阶段就有大量匿名内存压力。
   “某个冷 PFN 一定属于这些对象”没有逐页证据，不作这样的断言。
3. **快照覆盖和时效受到限制。** 大工作集扫描明显超过 1 秒目标周期；
   有些轮次的可上传 PFN 从百万下降到几十，随后恢复。4 轮窗口不等于 4 秒，
   缩短窗口没有消除 VMA/PFN 变化或扫描耗时。未命中页继续静态 Interleave，均衡前缀趋近 1:1、尾段行为见下一项；
   额外判冷页再把流量推向 PMem。
4. **未知页的 Interleave 本身存在 PMem 尾段。** `rswap_mapping.h:rswap_interleave_pair()`
   对共同容量交错，再追加大 Tier 的尾部。当前每台 16:32 GiB、两台合计 32:64 GiB，
   因此逻辑偏移前 64 GiB 均衡交错，64 GiB 之后首选 PMem。
   本轮使用 80 GiB swap；未知 PFN 的回退也可能进入这一尾段，即使所有容量 Fallback 计数为 0，
   首选流量仍可能偏向 PMem。这里不是 allocator 耗尽回退，也不是 RDMA 错误。
5. **热度更新不会重放置旧页。** Load 根据既有映射读原位置；没有主动迁移。
   页面后来变热，并不会自动把已有 PMem 副本改到 DRAM；当前阶段 Read 也不能全部归因于当前阶段 Store。

## 本轮尝试与下一步方案

已尝试手动阶段切换、20→4 轮窗口、24→16→8 GiB 本地限制、1→8 次计时轮次。
这些改动获得了完整的分阶段数据和更多已知热页样本，但未令完整 Adaptive 合计带宽反转。
Interleave 近似 1:1 的微小差异、短窗口略高的样本不作为达成预期的证据。

下一轮最直接的参数试验是：核对并使用现有 **`swap_64g.img` 替代 80 GiB swap**，
其逻辑偏移留在当前池的均衡前缀内，减少未知页的 PMem 尾段倾斜，不改任何放置逻辑。
四轮按周期样本观察到的远端 Slot 最高占用分别约 46.00 / 44.73 / 50.33 / 58.59 GiB，
因此 64 GiB 是有依据的候选；周期观测不是严格峰值保证，仍需监测余量。
**本轮尚未执行 64 GiB swap 试验，不能声称它能令 DRAM 带宽超过 PMem**：已知冷页仍会首选 PMem。

另一个流程方向是**改变施加压力的时机**：先在较宽内存限额下完成读图和热度学习，
再在重复 BFS 计算期间降低限额，先记录有 I/O 的 Interleave，再切 Adaptive。
这可以减少“第一轮 Store 发生时尚无可靠热度”的问题，仍然不保证 DRAM 合计带宽一定更高。
应保留完整过程，同时明确标记读图与 Compute 边界，单列稳定计算阶段；不能只挑 DRAM 更高的窗口。

如果允许单独的 benchmark 变体，可考虑 Ligra 已有的二进制输入路径，或评估释放解析缓冲区。
后者涉及应用内存管理，本轮没有修改。减少临时内存后要重新匹配本地限额，否则可能不再产生 swap。

若需要进一步提高放置质量，再单独评估采样范围/扫描代价、VMA 变化时的历史保留及 PFN 关联覆盖。
这些属于监控实现改动，本轮没有把它们混入参数实验。

如果最终目标升级为“保证 DRAM 总流量高于 PMem”，仅靠当前均值规则无法保证；
需要重新定义放置目标和约束，不能把它当作一个已定位的 RDMA 路由 bug。
更直接的策略质量指标还包括已知热 Store 的 DRAM 放置率、Fallback、数据正确性和同条件下应用耗时。

## 图、原始证据与收尾

证据位于 `remoteswap/tests/artifacts/bfs-tier-bandwidth-20261123/`，沿用 artifacts 的 Git 忽略规则。
每轮保存 `results.json`、`intervals.tsv`、`samples.jsonl`、heat summary/publication、
切换前后快照及理由、console/exit、正式 settled 报告和内核日志。

- [baseline 带宽与热度时间序列](artifacts/bfs-tier-bandwidth-20261123/baseline/bandwidth-hotness.png)
- [short-window 带宽与热度时间序列](artifacts/bfs-tier-bandwidth-20261123/short-window/bandwidth-hotness.png)
- [pressure 带宽与热度时间序列](artifacts/bfs-tier-bandwidth-20261123/pressure/bandwidth-hotness.png)
- [tight-memory 带宽与热度时间序列](artifacts/bfs-tier-bandwidth-20261123/tight-memory/bandwidth-hotness.png)

图中带宽是相邻采样窗口均值，热度点是实际完成的采样，不把超期轮次伪装成每秒一次。
`summary.json` 和 `summary-table.md` 为汇总；`provenance.json` 保存测试时的源码 commit、
内核和产物 hash，记录测试开始时源码工作区干净。

每轮结束均关闭 swap，验证所有 Slot 归还、累计计数收敛；最后统一卸载 Client，
确认两个 Server 释放资源并退出；本轮保存的 HMT/THP/换页设置和原 Client 配置均恢复。
原先已有的 `hermit-bfs-manual` cgroup 不属于本轮资源，没有删除。
