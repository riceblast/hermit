# Client 1.13：单次单节点 BFS 带宽观察

本文保留 16/32 GiB 池的结果。随后按要求完成的 32/32 GiB 单节点运行满足 DRAM>PMem，
新配置、带宽和证据限制见 [后续报告](unknown-dram-single32-bfs-result.md)。

## 结论

仅运行一轮 node04 单节点 BFS，同一进程先 Interleave 再 Adaptive，没有追加实验或调参。
**本轮不满足 Adaptive 阶段 DRAM 带宽高于 PMem 的观察目标。**
完整 Adaptive 阶段 DRAM/PMem 总带宽为 **217.20/332.04 MiB/s**，DRAM 约为 PMem 的 **0.65 倍**。
16 GiB DRAM 满池后发生大量容量回退，首选 DRAM 的请求实际进入 PMem；
末尾约 29 秒也仍是 PMem 更高。

## 本轮配置

- Client 1.13 / placement_rule=1 / 上传 ABI v2 / #8 内核，与上一轮双节点相同产物。
- Server 0=node04，10.0.0.13；DRAM NUMA 0、16 GiB，PMem NUMA 1、32 GiB；总池 48 GiB。
- 两 Tier 均 NUMA misplaced=0，6 个数据 MR 注册成功；本轮不启动 server02。
- 使用既有 48 GiB swap，不创建或格式化文件。
- 64G 文本图、40 线程、24 GiB memory cgroup、一次 BFS 计时轮次并保留程序预热；memsw 无有限配额。
- 采样器在 workload cgroup 外；capacity-mean，reserve=1 GiB，interval=1s、window=4、warmup=20s、manual。
- 首次远端 I/O 约在启动后 **29.06s**；观察约 **48.18s** 实际 Interleave I/O 后，
  在启动后 **77.25s** 切 Adaptive。切换时有新鲜容量裁剪阈值 T=0，原始 mean=25456 ppm。

单节点按既有每台 16/32 GiB 配置运行，远端 DRAM 总量是上一轮双节点的二分之一；
本轮不是保持池容量相同、仅改变 Session 数的严格性能对照。

## 带宽

单位 MiB/s，按成功 RDMA WC 页数×4096/实际时间计算。
按内核 state/run_id 划分完整稳定窗口，跨状态边界单列 transition；
包含阶段内无 I/O 的时间，按实际间隔加权，不平均瞬时速率。

| 阶段 | 操作 | DRAM | PMem |
| --- | --- | ---: | ---: |
| Interleave | Read | 118.95 | 119.81 |
| Interleave | Write | 217.56 | 255.78 |
| Interleave | 合计 | 336.51 | 375.59 |
| Adaptive | Read | 99.18 | 144.16 |
| Adaptive | Write | 118.02 | 187.88 |
| Adaptive | 合计 | 217.20 | 332.04 |

稳定阶段时长：Interleave **76.25s**、Adaptive **225.01s**。
Interleave PMem 总带宽比 DRAM 高约 **11.61%**，没有复现双节点几乎严格 1:1 的分布。
Adaptive DRAM 总带宽比 PMem 低约 **34.59%**；205 个有 I/O 的窗口中，仅 17 个 DRAM 更高。
末尾约 **29.23s** 的稳定窗口（含无 I/O 时间）DRAM/PMem 为 **144.69/191.12 MiB/s**。

## 原因分析

### 1. DRAM 首选生效，但 16 GiB 容量不足

Adaptive 采样到 DRAM 使用峰值为完整 **16.00 GiB**，运行中多次只剩十余至几十个空闲 Slot。
PMem 峰值约 **24.25 GiB**。累计/稳定阶段跨 Tier Fallback 均为 **4995599** 次，约占
Adaptive 分配的 **28.35%**；没有分配错误、同 Tier 节点回退或 I/O 错误。

| Adaptive 分配 | DRAM | PMem |
| --- | ---: | ---: |
| 首选次数 | 11793542 | 5826510 |
| 实际分配次数 | 6797943 | 10822109 |

DRAM 首选减去实际分配恰为 4995599，PMem 相应增加同样数量，说明本轮跨 Tier 回退方向为 DRAM→PMem。
首选策略本来偏向 DRAM，容量回退使实际写入反而偏向 PMem。
后续 Read 还会读取既有 Warmup/Adaptive 位置；没有主动迁移，不能靠新策略搬走已在 PMem 的数据。
本轮未逐条保存 Read 的原始放置原因，因此不把全部 Read 差异精确归因于某类热度。

同一 BFS 内的一次 1 秒短 Trace 共 **106273** 条成功分配决策，全部为 PFN miss，
首选全部是 Server 0 DRAM；其中 **74463** 条实际进入 PMem、fallback=2，另 **31810** 条进入 DRAM。
新规则、RR ticket、publication/阈值关联和 per-CPU buffer loss 检查通过。
该短窗口直接观察到未知默认策略后的容量回退，不表示整个阶段都是这一比例。

### 2. 零热度与未知的比例

按 Adaptive 稳定窗口的成功 Store WC 统计：

| 类别 | 成功写页数 | 占写流量 |
| --- | ---: | ---: |
| 未知 PFN | 7813192 | 44.34% |
| 已知零热度 | 9583699 | 54.39% |
| 已知正热度 >=T | 223161 | 1.27% |
| 已知正热度 <T | 0 | 0.00% |

未知和正热度首选 DRAM，零热度保持原 offset Interleave；T=0，因此没有正热度 PMem 分类。
但“首选 DRAM”不是容量保证，不能用热度比例推断最终 Tier 带宽。

### 3. 单节点 48 GiB swap 的 Interleave 并非全范围均分

16 GiB DRAM＋32 GiB PMem 下，前 32 GiB 逻辑偏移两 Tier 交错，后 16 GiB 仅首选 PMem。
本轮 Interleave 已记录 PMem 首选更多，且该阶段没有容量 Fallback；
这与可用偏移范围中的 PMem 尾段偏置一致。未记录完整 Warmup 逐 offset Trace，
不独立分解尾段与工作负载 offset 访问分布各自贡献。
上一轮双节点 64 GiB swap 全部处于均衡前缀，因此两轮不能默认有相同的 Interleave 比例。

## 退出、收尾与限制

BFS/采样器退出码 0，只有一条 `Running time : 0.466`，这是算法计时而非端到端时间。
采样约 **302.91s**，28 轮/28 代发布，25 轮超期；最长扫描 **19.83s**，最长上传 **2.01s**。
保留这些开销，没有另做优化实验。

新增 WC/提交/未归属错误均为 0，分配错误为 0，新增内核日志无相关 BUG/WARNING/错误。
swapoff 后 DRAM/PMem 均 live=retired=0、free=capacity，累计计数 settled 检查通过；
Client 卸载，node04 正常释放资源并退出，原双节点配置及保存的系统参数恢复。

本轮只验证一次 BFS 的机制观测与带宽分布，不代替独立逐页内容验收或性能重复性结论。
完整证据位于 `artifacts/unknown-dram-single-bfs-20261123/`（Git 忽略）：
`summary.json`、原始 samples、热度摘要/分布/publication、短 Trace、Server/内核日志、产物 hash 和执行/分析脚本。

[单节点带宽与策略时间序列图](artifacts/unknown-dram-single-bfs-20261123/bfs/bandwidth-hotness.png)
