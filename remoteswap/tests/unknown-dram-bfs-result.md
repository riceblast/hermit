# Client 1.13：单次双节点 BFS 带宽观察

## 结论

按操作者要求跳过独立功能测试，只运行一轮双节点 BFS；同一进程先 Interleave，再切 Adaptive，
没有额外对照、调参或重跑。结果符合本轮观察目标：Interleave 两 Tier 带宽近似相等；
Adaptive 的 DRAM 总带宽为 PMem 的 **2.64 倍**，读、写均较高。
这描述成功 RDMA 的逻辑访问流量，不代表介质峰值或 BFS 性能提高 2.64 倍。

## 配置与阶段

- Client **1.13**，placement_rule=1，上传 ABI v2，配套 #8 内核。
- Server 0=node04/10.0.0.13，DRAM NUMA 0、PMem NUMA 1；Server 1=server02/10.0.0.20，DRAM NUMA 0、PMem NUMA 2。
- 每台 16 GiB DRAM＋32 GiB PMem，总池 96 GiB。四个 NUMA 校验均 misplaced=0，每台注册 6 个数据 MR。
- 既有 64 GiB swap，使用该拓扑 Interleave 均衡前缀；不新建或格式化 swap。
- 64G 文本图、40 线程、24 GiB memory cgroup、一次 BFS 计时轮次并保留程序自带预热；memsw 无有限配额。
- 采样器在 workload cgroup 外，capacity-mean、reserve=1 GiB、interval=1s、window=4、warmup=20s、manual。
- 进程启动约 **29.07s** 首次发生远端 I/O；观察约 **45.25s** 实际 Interleave I/O 后，
  在启动约 **74.33s** 切 Adaptive。当时有新鲜的容量裁剪阈值，T=0，原始 mean=1657 ppm。

## 完整稳定阶段带宽

单位 MiB/s，两 Server 按 Tier 求和，按成功 WC 页数×4096/实际时间计算。
使用内核 state/run_id 划分相邻样本；跨状态边界单列 transition。
完整阶段包含无 I/O 的时间，按实际采样时长加权，不平均瞬时速率。

| 阶段 | 操作 | DRAM | PMem |
| --- | --- | ---: | ---: |
| Interleave | Read | 132.96 | 132.94 |
| Interleave | Write | 253.46 | 253.46 |
| Interleave | 合计 | 386.42 | 386.39 |
| Adaptive | Read | 219.04 | 116.24 |
| Adaptive | Write | 305.21 | 82.57 |
| Adaptive | 合计 | 524.25 | 198.81 |

稳定阶段时长为 Interleave **73.32s**、Adaptive **166.07s**；它们与绝对切换时刻的差别来自
状态边界单列及样本时间戳。Interleave 总带宽差约 **0.0057%**；Adaptive DRAM 的
Read/Write/总带宽分别高 **88.44% / 269.62% / 163.69%**。
Adaptive 142 个有 I/O 的采样窗口中，141 个 DRAM 总带宽较高，不宣称每个瞬时窗口都成立。
末尾约 **29.08s** 的稳定窗口（含无 I/O 时间）DRAM/PMem 总带宽为 **567.80/188.86 MiB/s**。

两台 Server 表现近似对称：Adaptive Server 0 DRAM/PMem 为 **262.11/99.42 MiB/s**，
Server 1 为 **262.14/99.39 MiB/s**。

## 原因与边界

Adaptive 稳定窗口内，按原因累计成功 Store WC 观察到：

| 类别 | 成功写页数 | 写流量占比 | 首选策略 |
| --- | ---: | ---: | --- |
| PFN miss／未知 | 9388540 | 56.95% | DRAM RR |
| 已知零热度 | 7020559 | 42.59% | 原 Interleave |
| 已知正热度 >=T | 76780 | 0.47% | DRAM RR |
| 已知正热度 <T | 0 | 0.00% | PMem RR |

因此主要变化来自未知页默认进入 DRAM；不是识别出了大量已知热页。
未知页与已知正热页约占 57.41% 写流量，剩余零热度近似一半写入各 Tier，
推算 DRAM 写份额约 78.71%，与实际 Write 带宽比例一致。
生效阈值 T=0，因此没有正热度低于阈值的 Store；零热度仍走显式 Interleave 分支。
上述比例只归因 Write；Read 未保存原始放置原因，不能据此声称 56.95% 的总读写来自未知页。

Adaptive Read 仍可能读取 Warmup 时已放置的页面；没有主动迁移。
这可以解释 Read 的 DRAM/PMem 比值小于 Write，但本轮未逐条追溯 Read 来源，属于机制上的解释。
全程同 Tier/跨 Tier Fallback 均为 0，分配错误为 0；采样到的每台 DRAM 占用峰值约 **14.92 GiB**，
未满 16 GiB，因此本轮未覆盖容量不足行为。

同一 BFS 内抓取一次 1 秒 decision 窗口，共 **158096** 条成功分配决策：
55830 条未知全部实际进入 DRAM，102266 条零热度按原映射分布到 DRAM/PMem（51110/51156）。
新规则、共享 RR、实际 publication/阈值关联及保存的 per-CPU buffer loss 检查通过。
这是短决策观察，没有全程 mapping/post/WC 链或逐页热度文件，不能代替内容及 PFN 身份验收。

采样器约 **240.92s** 完成 30 轮/30 代发布，22 轮超期；最长扫描 **17.47s**、
最长上传 **1.97s**。日志保留了开销和阈值继承信息，没有为此追加优化实验。

## 退出、收尾与证据

BFS/采样器退出码 0，一条算法 `Running time : 0.203` 输出；该值不是端到端时间。
WC/提交/未归属错误增量为 0，新增内核日志无相关 BUG/WARNING/错误。
swapoff 后四池 live=retired=0、free=capacity，累计计数 settled 检查通过。
Client 卸载、两台 Server 正常退出并释放资源，本轮保存的 THP/HMT/换页参数及原配置均已恢复。

完整证据位于 `artifacts/unknown-dram-bfs-20261123/`（Git 忽略）：
`summary.json`、`bfs/results.json`、`bfs/intervals.tsv`、原始 samples、热度摘要/分布/publication、
短 Trace、Server/内核日志、产物 hash、控制器及分析脚本。

[本轮带宽与策略时间序列图](artifacts/unknown-dram-bfs-20261123/bfs/bandwidth-hotness.png)
