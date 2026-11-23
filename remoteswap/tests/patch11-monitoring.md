# Patch 11：累计策略统计与正式监控分析

## 当前进展：Client 1.13

Adaptive 无快照、无可用阈值和 PFN miss 均改为首选 DRAM，与已知热页共享 Server RR；
零热度继续原 Interleave，容量回退和 Slot/I/O 生命周期沿用原机制。
新 decision 带 `placement_rule=1`，工具按数字标识兼容历史未知 Interleave 日志。
本轮修改、验证及后续实机计划见 [未知页 DRAM 默认策略](unknown-dram-placement.md)。
随后仅运行一次双节点 BFS：Interleave DRAM/PMem 总带宽 386.42/386.39 MiB/s，
Adaptive 524.25/198.81 MiB/s（DRAM 约 2.64 倍），无新增错误或容量回退，正常收尾。
结果和归因见 [1.13 单次 BFS 报告](unknown-dram-bfs-result.md)；独立功能专项尚未执行。
后续单节点 16/32 GiB 池的一轮 BFS 未满足 DRAM>PMem：Adaptive 为 217.20/332.04 MiB/s，
DRAM 满池且发生 4995599 次 DRAM→PMem 容量回退。只运行该轮、正常收尾，
详见 [单节点报告](unknown-dram-single-bfs-result.md)。
随后按要求将单节点改为 32/32 GiB、使用 64 GiB swap，仅运行一轮：Interleave 总带宽
349.19/349.14 MiB/s，Adaptive 536.08/224.57 MiB/s（DRAM 约 2.39 倍），无容量回退。
本轮短 Trace 的 overrun=24150，完整逐决策验证未认证；累计 WC 带宽统计正常。
结果与限制见 [32/32 GiB 单节点报告](unknown-dram-single32-bfs-result.md)。

## 历史 1.12 阶段

Patch 11 在累计策略统计、Store post/WC 关联和只读 pool CLI 基础上，新增：

- **capacity-mean**：按有效本地容量裁掉预计留在本地的最热部分，用剩余样本均值作阈值 T；
  原始均值 μ 独立保留，global-mean 保留用于对照。
- **放置规则**：Warmup、未知和零热度仍 Interleave；Adaptive 已知正热度 >=T 优先 DRAM，
  较低正热度优先 PMem。零热度参与均值，未知不按零统计；RR/Fallback 沿用原机制，无主动迁移。
- **保护与观测**：cgroup 限额减可配置余量；低覆盖/样本不足/预测无换出时继承旧阈值，
  无历史则全局均值兜底。新增紧凑直方图、阈值来源/代次及 reason 7=zero_interleave。
- 本地上传 **ABI v2**、policy_stats/placement.tsv **v2**；新 Client/采样器须配套。
  Server RDMA 协议 v2、I/O stats v2、#8 内核和 Slot/Completion 生命周期不变。

实现与参数见 [容量裁剪策略](capacity-mean-placement.md)。

## 已验证、未执行与带宽目标

- **已完成**：1.11 单/双节点核心验收；1.12 编译和 **81 项本地回归**，以及一次双节点 BFS。
  新策略该次 Interleave 合计 DRAM/PMem 为 **390.10/390.06 MiB/s**，
  Adaptive 为 **351.69/346.88 MiB/s**，DRAM 高 **1.38%**；无新增 I/O 错误，正常回收/卸载。
- **尚未执行（新策略）**：单/双节点可控页的分类、逐 word 读回及活进程 swapoff 内容专项；
  主动构造容量变化/低覆盖/阈值继承的实机专项；同条件 global-mean/capacity-mean A/B 与重复性测试。
  旧版验收和本次 BFS 正常退出不能替代这些验证；这里只记录待验证项，不自动追加实验。
- **当前带宽目标**：有真实 I/O 时 Interleave 分布正常、无错误；进入 Adaptive 后，
  以完整稳定阶段、两 Server 求和的成功 RDMA **Read+Write** 逻辑带宽比较，期望 **DRAM > PMem**，
  同时分列 Read/Write。未设最低领先幅度；当前仅单次达到，未证明稳定收益。
  这是实验观察/优化目标，不是机制正确性的硬验收条件，不以瞬时峰值代替阶段均值。
- **归因边界**：本次 Adaptive 零/未知页合计占 **99.18% 写带宽**，正热度仅 0.82%；
  Read 未保存原始放置原因，不能把这一比例解释成总读写占比。

结果与证据见 [单次 BFS 报告](capacity-mean-bfs-result.md)，旧版验收见
[1.11 核心记录](patch11-core-smoke.md)。扫描/覆盖优化、主动迁移、故障注入、
Store 写失败后的磁盘回退和全池耗尽仍不纳入本轮范围。

## policy_stats v2（工具兼容历史 v1）

新增只读 `/sys/kernel/debug/rswap/policy_stats`，与 stats v2 共用 instance_ns。
计数跨 Stop、swapoff/swapon 保留，模块重载清零。每项原子读取，整组不是原子事务。

```text
version=2 instance_ns=... sample_ns=... nr_servers=1 page_size=4096
reason <id> <decisions> <post_ok> <post_error> <wc_ok> <wc_error>
pool <server> <tier> <preferred> <allocated>
fallback <0..3> <count>
age <0..3> <count>
allocation_error <count>
```

- reason：0 Inactive、1 Warmup、2 无快照、3 无可用阈值（名称保留 no_mean）、4 PFN miss、
  5 正热度 DRAM 优先、6 正热度 PMem 优先、7 零热度 Interleave。
- 决策从 choose 成功后尝试分配开始计数，包含分配失败；area/CPU/THP 等提前拒绝
  仍由原 stats 描述，不冒充已查询热度。
- pool 是累计首选/实际分配次数。fallback 沿用 Patch 9 四候选次序，2/3 表示跨 Tier。
- age 仅统计有快照的 Adaptive 决策：<=1s、(1,5]s、(5,20]s、>20s，不触发过期行为。
- Adaptive 热度覆盖率 = (热+冷+零)/(无快照+无阈值+PFN miss+热+冷+零)；
  positive_heat_coverage 分子只取热+冷。历史 v1 无法单独识别零热度，后者显示 null。
- 有可查询快照时 PFN 命中率 = (热+冷+零)/(PFN miss+热+冷+零)。零分母输出 null。
- 统计为全部 Client 决策，不是精确目标 PID 换出率；Warmup/Inactive 不算 PFN miss。

入口关闭且 I/O drain 后才能核对累计等式：

```text
decisions = allocated + allocation_error = sum(preferred)
allocated = post_ok + post_error = sum(fallback)
每个 reason: post_ok = wc_ok + wc_error
```

快速 WC 可早于 post_ok 增加，默认不对运行中快照强制等式；不据差值推断负在途数量。
`pool-report --settled` 只检查最后的累计值，不对任意区间差值套用等式。

## Store 结果关联

新增默认关闭、全数字的 `rswap:store_result`：

```text
alloc_seq=123 offset=10 server=0 tier=0 stage=0 status=0
```

stage=0 为 post 被接受或分配后/pre-post 失败；stage=1 为终止 WC。
序号复用 mapping alloc.seq/decision.alloc_seq，请求在 post 前复制序号和 reason。
不持可能已被 Stop 释放的 run 指针；快速 WC 可早于 stage=0，工具按序号关联。
分配失败没有有效 alloc_seq，由 decision.alloc_ret 和 allocation_error 表示。
WC 成功不表示当前 entry 仍有效。Load 不新增热度归因，沿用原位置查询和 Read 统计。

## CLI 与采集

```bash
bash remoteswap/client/manage_rswap_client.sh pool-status
bash remoteswap/client/manage_rswap_client.sh pool-monitor --interval 1 --out /tmp/p11-NEW
bash remoteswap/client/manage_rswap_client.sh pool-report \
  --samples /tmp/p11-NEW/samples.jsonl --settled --heat-dir /tmp/p11-heat
```

status/monitor 只读 debugfs，不加载模块或启停 swap；report 只读普通日志，不需要 sudo。
输出 JSON，monitor 每行一个区间，并保存原始 samples.jsonl 以供重算。
每次读取立即关闭文件，不在 sleep 时持模块引用；卸载前仍应停止 collector。

status 组合拓扑和 Tier 比例、capacity/live/retired/free、状态/均值/阈值/来源/快照年龄、
累计决策与 I/O。used=live+retired，含预留/退休位置，不是精确 Frontswap 有效页数。
带宽复用 rswap_stats 的成功 RDMA Read/Write/总 MiB/s，按 WC 完成时间计。
窗口同时输出计数差值和当前状态；endpoint_state 只是区间两端标签，不能归因全部 I/O。
Adaptive 也会读取 Warmup 已放置的页面；单 run 精确 Store 来源需使用短窗口 Trace。

不同 instance 在离线报告中分段；拒绝同实例计数倒退。同次采集中实例不一致明确失败，
不能混算。各接口顺序读取，保留采集起止与内核时间，不承诺同一时刻快照。
推荐 Start 前基线至目标退出为实验窗口，swapoff 收尾单列。

## 正式详细验证

```bash
bash remoteswap/client/manage_rswap_client.sh trace-check /tmp/p11-trace.log \
  16:32 --dynamic --adaptive --require-empty --store-results \
  --heat-dir /tmp/p11-heat --buffer-stats /tmp/p11-buffer.stats \
  --detail /tmp/p11-decisions.tsv --json
```

双节点 layout 为 `16:32,16:32`。采集 mapping/decision/store_result，global trace clock，
从空池首次 Store 前持续读取至 swapoff 完成；保留全部 CPU buffer stats。

- heat-dir 以实际 run_id/generation 关联 publication 和逐页快照，验证 PFN/ppm、阈值、
  未知回退与记录数；逐代处理一个 PFN 表，不用最近 ready_ns 替代实际代次。
- 允许 RCU 读者使用已取得的旧代；last_pfn 不当当前帧，均值在规定精度内交叉核对。
- store-results 要求每个成功分配的完整 post/预提交错误及 WC 链，检查序号/位置/重复记录。
  报告观察到的错误，不证明磁盘回退或当前 entry 有效性。
- buffer-stats 检查保存的 per-CPU 丢失计数，未提供时明确未认证。
- 1.12 可用紧凑直方图重算阈值及继承链；缺少逐页文件仍不能严格验证 PFN。
  BFS 默认摘要采集，用 pool-report 和热度摘要。
- 支持旧符号/数字日志；历史日志无 store_result 时不请求该项验证。

Trace 分析保留短窗口决策索引，不用于 BFS 全量海量追踪。PFN 关联仍是近似身份。
逐条 TSV 包含成功分配的决策（含无快照的分配），失败分配单列在摘要，无有效远端 Slot。
正式分析工具只认证其输入证据；完整实机验收还须内容校验、内核日志和资源收尾。

## 本地检查

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
make -C remoteswap/tools
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
modinfo -F version remoteswap/client/rswap-client.ko
```

回归调用真实 C 计数/formatter/请求路径，覆盖单/双节点格式、Stop 后保留、快速 WC、
请求序号/reason 复制、覆盖率分母、重载分段、完整/缺失/重复结果，以及实际代次、
热度篡改、last_pfn 排除和历史格式兼容。复用 Patch 10 归档验证新工具，不冒充 1.11 实机验收。
### 2026-11-23 历史 1.11 本地结果

- RDMA Client 编译通过，modinfo 为 **1.11**；用户态采样器构建检查通过。
- 全组 **76 项**无特权回归通过（原 67 项＋新增 9 项）。
- shell 语法与 git diff --check 通过。
- 正式 Trace/热度工具复核 Patch 10 有效运行：16384 个已知 PFN 决策、98304 条映射事件
  及全部回收通过，buffer 丢失检查通过，输出 16384 条逐决策 TSV。
- 正式摘要函数复核旧 BFS 的 32 轮/32 代上传及超期/耗时数据，不依赖逐页日志。
- 历史 1.10 日志无 store_result；本地用实际请求函数与专项日志验证新 post/WC 关联，
  不能据旧日志宣称 1.11 新观测接口已完成硬件验收。

日志：`/tmp/hermit-p11-client-build.log`、`/tmp/hermit-p11-tools-build.log`、
`/tmp/hermit-p11-regression.log`；归档复核报告 `/tmp/hermit-p11-archived-trace.json`，
明细 `/tmp/hermit-p11-decisions.tsv`。最后一次增量构建可能只显示产物已最新，
实现阶段已实际编译各修改的 Client 源文件。以上为本地检查记录；随后完成的
1.11 实机结果、范围与证据见 [核心验收结果](patch11-core-smoke.md#2026-11-23-实机结果)。

### 2026-11-23 历史 1.11 实机结果

单节点 Warmup、单节点 Adaptive、双节点 Adaptive、双节点 BFS 四组核心验收均通过。
三个可控运行各 16384 条完整 Store 结果链与逐快照热度验证通过，内容校验及活进程
swapoff 正常；BFS 成功完成，实际 Adaptive 热度覆盖率 29.9698%，无新增 I/O/内核错误。
模块生命周期隔离、跨 Stop/area 的计数保留、正式 CLI 与所有 Slot/系统参数收尾均通过。
详细数字、范围和证据见 [实机结果](patch11-core-smoke.md#2026-11-23-实机结果)。
