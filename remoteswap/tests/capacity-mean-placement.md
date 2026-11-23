# 容量裁剪均值放置：Client 1.12

## 范围与规则

在现有页面热度上，新增 `capacity-mean` 阈值策略；保留 `global-mean` 作为对照。
默认 CLI 策略仍为 global-mean，容量策略须显式启用。两种新模式均遵循本轮确认的规则：

| 状态/热度 | 首选位置 |
| --- | --- |
| Inactive / Warmup | 原 Interleave |
| Adaptive，未知 | 原 Interleave |
| Adaptive，已知热度 0 | 原 Interleave，不消耗 Tier RR ticket |
| Adaptive，已知正热度 >= 阈值 T | DRAM，目标 Tier 内 Server RR |
| Adaptive，已知正热度 < T | PMem，目标 Tier 内 Server RR |

零热度仍参与热度分布与阈值计算；未知不按零计入均值。
容量 Fallback、Slot 所有权、RDMA 请求/Completion、Load 查询既有位置、Invalidate 回收均不变。
无主动迁移，不改 Server、RDMA 控制协议 v2 或 #8 内核。
两种 1.12 模式对零热度和正热度相等的处理相同，适合隔离比较阈值模型；
历史 1.11 的全局均值日志仅作历史参考，不能作为只改变阈值的严格对照。

## 计算模型

- `M`：本轮去重后的可管理 resident / swapped 页数，包含未知页。
  排除暂态 changed 页，避免把无法确认当前映射的页用于容量估计；不改变其原有历史观测。
- `K`：其中有有效热度历史的页数；包含有效零热度和换出页的历史。
- `B`：有效本地页数，`floor((本地字节限额 − 预留字节数) / 4096)`。
- `R = ceil(K × (M − B) / M)`：当 M>B 时，预计换出群体在已知样本中的数量。
- 从已知样本中按热度递增取 R 页，`T = floor(sum(heat_ppm × 页数) / R)`。

按比例裁剪 K，而不是从 K 中直接减去 B。例如 M=64 GiB、B=24 GiB、K=8 GiB，
候选样本为最冷的 5 GiB。裁剪只用于求阈值，不删除上传的任何已知 resident PFN。

使用 `(valid, active)` 固定直方图：至多 2144 个非空可能桶，合并相同整数 heat_ppm；
只排序少量桶，不按热度排序完整页表。边界桶按页数部分计入，无需选出具体 VA/PFN。
采用 128 位乘积，避免容量比例及加权求和在大容量下溢出。
阈值以页面已导出的整数 ppm 计算；原始真实均值仍按旧的 active/valid 统计并独立保留。
因此阈值模型样本与旧历史均值的贡献者可不同，不能把 threshold_sample_pages 当作原 known 字段。

模型假设已知分布能近似整体分布，且较热部分更可能留在本地，不保证精确预测 Linux 回收集合。

## 容量与保护参数

```text
--threshold-policy global-mean|capacity-mean    默认 global-mean
--memory-cgroup DIR                            显式指定 cgroup v1 memory 目录
--local-memory-gib GIB                         手动覆盖容量，优先于 cgroup
--reserve-gib GIB                              默认 1，允许 0
--threshold-min-pages N                        默认 1024
--threshold-min-coverage FRACTION               默认 0.05，范围 [0,1]
```

cgroup 模式读取指定目录及更紧的父级有限限额，每代重新读取，可观察运行中的限额调整。
显式路径避免启动包装器尚未进入受限组时误读根 cgroup。当前版本针对本机 cgroup v1，
不自动解释 cgroup v2 的 `memory.max`。无限限额、缺失文件、限额不大于余量明确报错，
不默默使用整机 MemTotal。手动覆盖适用于未设置 cgroup 的实验。

保护条件依次为：M<=B、K/M 低于覆盖门槛、K 或 R 小于最少页数。
触发保护时继承同一 run 上一份已成功发布的有效阈值，包括其原始计算代次；
还没有历史值时使用该轮全局均值。新 PFN 表仍正常发布。
Start 是新的工具/run 生命周期，不跨进程缓存旧阈值；Stop 后仍按原行为继续纯观察。

## 启动示例

先按 [BFS 手动指南](bfs-end-to-end-guide.md) 完成 Server、Client、swap 和 cgroup 初始化。
只展示采样器调用，不自行加载模块、启停 swap 或调整 cgroup：

```bash
sudo env OMP_NUM_THREADS=40 remoteswap/tools/rswap_hotness \
  --out /tmp/hermit-capacity-NEW --placement --manual \
  --interval 1 --window 4 --warmup 20 \
  --threshold-policy capacity-mean \
  --memory-cgroup /sys/fs/cgroup/memory/hermit-bfs-manual --reserve-gib 1 \
  -- bash -ec 'echo "$$" > "$1/cgroup.procs"; exec "$2" -s -rounds 1 "$3"' \
  bfs /sys/fs/cgroup/memory/hermit-bfs-manual \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_64G.adj
```

对照运行只把策略改为 `--threshold-policy global-mean`，去掉容量来源参数；
其他条件、swap 大小和手动切换时机保持一致。首次 Store 有 I/O 后再记录 Interleave 并手动切换，
不拿无 I/O 的 Warmup 当作带宽对照。

## 接口与监控

Client **1.12**，本地上传 **ABI v2**；Server RDMA 协议、stats v2、managed Frontswap v2 均不变。
上传头仍 40 字节，BEGIN 后新增 72 字节阈值元数据；DATA 的 16 字节 PFN 记录、32 字节 ACK 不变。
新采样器与 Client 必须匹配，ABI v1 写入明确拒绝；历史纯日志仍可由新分析工具读取。
阈值元数据和 PFN 表在同一 staging/COMMIT/RCU 快照内发布，Store 不读取用户态文件或 cgroup。

`placement` 额外展示 policy、threshold_ppm、threshold_source、threshold_generation、
threshold_status、population_pages、threshold_sample_pages、candidate_pages、local_pages。

- policy：0 global-mean，1 capacity-mean。
- source：0 直接全局均值，1 本轮裁剪均值，2 沿用历史，3 无历史时全局均值兜底。
- status：0 正常，1 覆盖不足，2 预测无换出群体，3 样本/候选数量不足。
- threshold_generation：阈值原始计算/兜底代次；PFN 表更新不改写继承值的来源代次。

`policy_stats` 升为 v2，新增 reason **7=zero_interleave**，其余 ID 保留。
原始 I/O stats 不变。pool 工具的已知热度覆盖率包含零热度；另给 positive_heat_coverage，
只统计进入正热度阈值分类的 Hot/Cold，避免把“已知但 Interleave”混成 PFN miss。
历史 policy_stats v1 无法单独识别零热度，因此 positive_heat_coverage 显示 null。
reason 3 在新格式中表示无可用阈值，名称保留 no_mean 供现有消费者辨识。

`summary.tsv` / 逐页热度格式继续 v1，真实均值含义不变。
`placement.tsv` 升为 v2，新增完整模型参数、阈值及来源；
`distribution.tsv` 保存每个采样代次的精确 ppm 桶及页数，-1 桶为模型群体中的未知页。
不启用 --pages 也能重算阈值。显式 Stop 时可能保留最后一次未发布的候选分布，
正式校验只关联成功 publication；不会把未提交阈值当作生效值。

decision Trace 继续使用全数字字段，新增实际阈值、policy/source/status 和阈值代次。
工具保留旧决策的严格 `heat>mean` 语义，新决策验证零热度 Interleave、正热度 `heat>=T`。
`--heat-dir` 可重算阈值、验证继承链；有 --pages 时再对照逐页分布与 PFN 输入。

## 本地验证与后续实机计划

实现阶段仅编译与无特权验证；随后按要求只运行了一次 BFS，结果见文末。
下列独立专项及同条件对照尚未执行，当前不自动追加实验。

本地检查覆盖：真实 C 比例裁剪/部分已知样本/桶内截断/全零/同值/大数乘积；
真实上传到内核 staging/COMMIT、旧 ABI 拒绝、非法阈值不影响已发布代；
正热度相等走 DRAM、零/未知保留 Interleave 及不消耗 RR；
实际 sampler→Client 桥接、直方图重算、历史阈值继承及逐页/Trace 关联。
复用全组生命周期、Completion、mapping、失败路径与旧日志回归。

尚未执行的新策略实机验证：

1. 小工作集构造含热/次热/零/未知的分布，确认 μ 与 T 分开、裁剪结果正确；
   验证零/未知仍按原 offset Interleave，正热度等于 T 进入 DRAM。
2. 改变本地限额或覆盖率，观察新阈值、保留旧阈值和首次全局兜底，核对来源代次。
3. 使用新版本控制器做逐 word 内容和活进程 swapoff，确认 Slot 全部归还、无新增错误。
4. BFS 对照 global-mean 与 capacity-mean，同一组内存/窗口/切换条件；
   建议核对并使用既有 64 GiB swap，避免 80 GiB 在当前 16:32 Tier 布局中的 PMem 尾段偏好。
   分别汇报 Interleave/Adaptive Read、Write、总带宽，零热度 Interleave、正热度分类及 Fallback。

当前期望完整 Adaptive 阶段两 Server 合计的成功 Read+Write 逻辑带宽满足 DRAM>PMem，
分列 Read/Write，未设最低领先幅度；这是实验观察目标，不是机制正确性的保证。
本次单轮达到 +1.38%，尚无同条件 A/B 或重复性结论；统一进展见 [Patch 11 总结](patch11-monitoring.md)。

### 2026-11-23 本地结果

- Client 编译通过，`modinfo` 为 **1.12**；用户态工具以 `-Wall -Wextra -Werror` 编译通过。
- **81 项**无特权回归通过（既有 76 项＋新增 5 项，专项内部覆盖多组边界与事务）。
- 新格式实际 sampler→内核桥接、紧凑直方图重算、逐页/阈值/PFN 关联通过；篡改阈值会被拒绝。
- 新 pool-report 成功复核历史 1.11 BFS 的累计统计/热度摘要，未要求历史日志补齐新字段。
- 管理脚本和 Markdown Bash 命令语法、`git diff --check` 通过。
- 本轮没有加载模块、启用 swap 或启动实机 BFS，硬件数据正确性和带宽效果留待上述验收。

日志：`/tmp/hermit-capacity-client-build.log`、`/tmp/hermit-capacity-tools-build.log`、
`/tmp/hermit-capacity-regression.log`、`/tmp/hermit-capacity-targeted.log`。
最终增量构建可能只显示产物已最新；实现阶段已实际编译修改过的 Client 源文件。
本地结果与产物 hash 另存 `artifacts/capacity-mean-local-20261123/`（Git 忽略）。


### 2026-11-23 后续单次 BFS 观察

按操作者要求跳过独立功能验收，仅执行一轮 BFS，不追加实验。
Interleave 分布正常，Adaptive DRAM 合计 351.69 MiB/s、PMem 346.88 MiB/s，
本轮 DRAM 高约 1.38%；所有 I/O 错误增量为 0，正常收尾。
此结果不等同于完整逐页功能验收或稳定性能提升。
详细配置、口径、热度及原始证据见 [单次 BFS 结果](capacity-mean-bfs-result.md)。
