# Patch 11：累计策略统计与正式监控分析

## 范围与状态

Client **1.11**，Server 协议 v2、stats v2、managed Frontswap v2、上传 ABI v1 不变。
新增固定数量累计计数、Store 结果关联和只读报告工具，不改 Server、#8 内核、采样算法、
策略阈值、Slot 所有权或 Completion 收尾机制。不新增逐页元数据或 run 历史表。
已通过编译、76 项本地回归，以及 2026-11-23 的
[单节点＋双节点核心实机验收](patch11-core-smoke.md)。

按操作者要求补单节点实机，不新增 Store 写失败后的磁盘回退验证。
不做扫描优化、覆盖优化、TTL/cooldown、迁移、Dashboard 或 DRAM 带宽提升门槛。
原有失败路径回归保留，不把它解释为新增磁盘回退硬件认证。

## policy_stats v1

新增只读 `/sys/kernel/debug/rswap/policy_stats`，与 stats v2 共用 instance_ns。
计数跨 Stop、swapoff/swapon 保留，模块重载清零。每项原子读取，整组不是原子事务。

```text
version=1 instance_ns=... sample_ns=... nr_servers=1 page_size=4096
reason <id> <decisions> <post_ok> <post_error> <wc_ok> <wc_error>
pool <server> <tier> <preferred> <allocated>
fallback <0..3> <count>
age <0..3> <count>
allocation_error <count>
```

- reason：0 Inactive、1 Warmup、2 无快照、3 均值未知、4 PFN miss、5 热、6 冷。
- 决策从 choose 成功后尝试分配开始计数，包含分配失败；area/CPU/THP 等提前拒绝
  仍由原 stats 描述，不冒充已查询热度。
- pool 是累计首选/实际分配次数。fallback 沿用 Patch 9 四候选次序，2/3 表示跨 Tier。
- age 仅统计有快照的 Adaptive 决策：<=1s、(1,5]s、(5,20]s、>20s，不触发过期行为。
- Adaptive 热度覆盖率 = (热+冷)/(无快照+均值未知+PFN miss+热+冷)。
- 有可查询快照时 PFN 命中率 = (热+冷)/(PFN miss+热+冷)。零分母输出 null。
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

status 组合拓扑和 Tier 比例、capacity/live/retired/free、状态/均值/快照年龄、
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
- 缺少逐页文件不能严格验证 PFN。BFS 默认摘要采集，用 pool-report 和热度摘要。
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
### 2026-11-23 本地结果

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

### 2026-11-23 后续实机结果

单节点 Warmup、单节点 Adaptive、双节点 Adaptive、双节点 BFS 四组核心验收均通过。
三个可控运行各 16384 条完整 Store 结果链与逐快照热度验证通过，内容校验及活进程
swapoff 正常；BFS 成功完成，实际 Adaptive 热度覆盖率 29.9698%，无新增 I/O/内核错误。
模块生命周期隔离、跨 Stop/area 的计数保留、正式 CLI 与所有 Slot/系统参数收尾均通过。
详细数字、范围和证据见 [实机结果](patch11-core-smoke.md#2026-11-23-实机结果)。
