# Client 1.12：单次 BFS 分阶段带宽结果

## 本轮结论

按要求仅运行一轮 BFS，直接观察 Interleave 和 capacity-mean Adaptive；
没有独立功能测试、额外对照轮次或后续参数实验。
**Interleave 流量分布正常；完整可归类 Adaptive 采样窗口中，DRAM 合计带宽高于 PMem 约 1.38%。**
这是一次运行的观察结果，差距较小，不代表稳定性能提升或介质峰值比较。

## 固定配置与执行

Client 1.12 / 上传 ABI v2 / #8 内核，原双节点配置：Server 0=node04 / 10.0.0.13、
Server 1=server02 / 10.0.0.20，各 16 GiB DRAM＋32 GiB PMem，总池 96 GiB。
两 Tier NUMA 均 misplaced=0，各注册 6 个 MR。

使用既有 **64 GiB swap**，其逻辑偏移处于本布局的 Interleave 均衡前缀内。
64G 文本图、OMP_NUM_THREADS=40、BFS 一次计时轮次并保留自带预热；
仅 BFS 进入 24 GiB memory cgroup，memsw 无有限配额；采样器在外。
`--placement --manual --threshold-policy capacity-mean --interval 1 --window 4 --warmup 20`，
容量来源显式指定本轮 cgroup，reserve=1 GiB，有效本地容量 **23 GiB**。
不启用全量逐页日志，只保留摘要、精确直方图、累计采样及一段 1 秒决策观察。

首次远端 I/O 约在启动后 **29.07 秒**；
在有真实 Interleave I/O 且获得新鲜容量阈值后，于约 **72.24 秒**手动切换 Adaptive。
切换时 global mean=80317 ppm（约 8.03%），容量裁剪阈值 **T=0**，
来源为该代真实裁剪计算；模型 population=8585868 页、已知样本=2159281 页、候选=642955 页。

## 带宽

单位 MiB/s，**两 Server 按 Tier 相加**，仅计成功 RDMA WC 的 4 KiB 逻辑载荷。
按内核 placement.state 划分，相邻样本跨状态/跨 run 的窗口归入 transition，
不混入任一稳定阶段；包含阶段内无 I/O 的时间，按真实间隔加权，不平均每秒速率。
Interleave 实际为手动 Warmup，不能拿用户态 summary 的 observe 标签判定 Adaptive。

| 阶段 | 操作 | DRAM | PMem |
| --- | --- | ---: | ---: |
| Interleave | Read | 136.74 | 136.71 |
| Interleave | Write | 253.36 | 253.35 |
| Interleave | 合计 | 390.10 | 390.06 |
| Adaptive | Read | 160.89 | 159.18 |
| Adaptive | Write | 190.80 | 187.70 |
| Adaptive | 合计 | 351.69 | 346.88 |

稳定阶段采样窗口时长：Interleave **71.23 秒**、Adaptive **170.48 秒**；
切换边界窗口另存，因此它们不等于从进程启动计算的绝对切换时刻。
Interleave 四个 Server/Tier 均有成功 Read/Write，两 Tier 总流量近似 1:1；
有 I/O 窗口的均值另存 JSON，不把启动时无 I/O 解释为连接异常。

Adaptive DRAM 的 Read、Write、合计分别高 **1.07%**、
**1.65%**、**1.38%**。
这些是时间阶段的访问流量；Adaptive 的 Read 仍可能读取 Interleave 阶段已经放置的页。
本轮没有 A/B 对照，不把差异全部归因于容量裁剪阈值。

## 热度与放置观察

Adaptive 稳定窗口中约 16518741 次 Store 决策：

- 未知 PFN → Interleave：**9025181**，约 **54.64%**。
- 已知热度 0 → Interleave：**7357345**，约 **44.54%**。
- 已知正热度 → DRAM 优先：**136215**，约 **0.82%**。
- 已知正热度低于阈值 → PMem 优先：**0**，约 **0.00%**。

实际生效阈值为 0：最冷候选样本的加权均值落在零热度桶。
正热度页因此优先 DRAM，零和未知仍保持原 Interleave，与用户确认的规则一致。
在均衡逻辑偏移范围内，大部分 Interleave 流量维持两 Tier 接近平衡，
少量正热度 Store 额外偏向 DRAM，与本轮 DRAM Write 略高的观察一致。

1 秒短 Trace 共 116731 条决策：零热度 Interleave 75856、未知 Interleave 40462、
正热度 DRAM 413。检查了实际阈值 publication、首选/实际 Server/Tier 和 RR ticket，
buffer 丢失检查通过。未保存全量逐页历史，不宣称独立认证了所有 PFN 的真实访问频率或内容完整性。

采样器运行约 **243.40 秒**，28 轮采样/28 代上传，
23 轮超期；最长扫描 **17.49 秒**、最长上传 **1.98 秒**。
保留了扫描开销，不追加优化实验。28 代阈值来源为 9 次容量计算、17 次继承、2 次初始全局兜底；
每代阈值已通过紧凑直方图离线重算/继承链校验。
BFS 和采样器退出均为 0，一条算法计时 `Running time : 0.174`；该值不是端到端耗时。

## 收尾和证据

提交/WC/unrouted 错误增量为 0，无分配错误或容量 Fallback。
BFS 完成后 swapoff，四池 live=retired=0、free=capacity，累计计数 settled 检查通过；
Client 卸载，两个 Server 均释放资源并退出，原配置及本轮保存的 HMT/THP/换页参数恢复。
最终无活动 swap，新增内核日志无相关 BUG/WARNING/错误。

证据：`remoteswap/tests/artifacts/capacity-mean-bfs-20261123/`（Git 忽略）。
`summary.json` / `bfs/results.json` 为汇总，`bfs/intervals.tsv` 为分阶段时间序列，
另含原始 samples、heat summary/publication/distribution、短 Trace、正式报告、
Server/内核日志、配置、产物 hash 和本轮控制器。

[本轮带宽、阈值和分类时间序列图](artifacts/capacity-mean-bfs-20261123/bfs/bandwidth-hotness.png)


## 现有数据补充：Adaptive 写带宽归因

仅离线分析原样本，没有重新运行任何实验。以同一组 Adaptive 稳定窗口（170.48 秒）为口径，
按每个 reason 的成功 Store WC ×4096 字节计算，两个 Server 合计：

| 类别 | 写入 GiB | 写带宽 MiB/s | 占全部写带宽 |
| --- | ---: | ---: | ---: |
| 零热度 → Interleave | 28.07 | 168.58 | 44.54% |
| 未知 → Interleave | 34.43 | 206.80 | 54.64% |
| 两者合计 | 62.49 | 375.38 | 99.18% |
| 已知正热度 → DRAM | 0.52 | 3.12 | 0.82% |

这些是成功写入的逻辑载荷，不是用观测页数推断 I/O。
policy_stats 与 I/O stats 分别读取，窗口求和有 5 页（20 KiB）差异，不影响上述两位小数。

Read 没有保存原始放置 reason，当前数据无法把它归到零/未知/正热度三类，
也不能严格计算每类各占多少 DRAM/PMem 流量。两者的**写流量**已占全部 Read+Write 的约
**53.74%**，这是其合计总 I/O 占比的下界，不能说它们占总读写带宽 99.18%。
完整派生结果见归档的 `bfs/bandwidth-attribution.json`。
