# DLS：VPN 放置实机验收与运行补充

更新时间：2026-11-28。补充 [Patch 14](patch14-vpn-placement.md) 之后的实机记录与运行工具；
Client 仍为 **1.19**，放置规则和 Server 协议沿用原文。
当前采样文件格式以 [DLC 补充](patchDLC-hotness.md) 为准。

## 双节点已验证内容

沿用 Optane Client、node04／server02 双 Server，每台 16 GiB DRAM + 32 GiB PMem，总池 96 GiB。
证据目录：`/tmp/hermit-stage3-core-bfs-20261128-r3`；core／edges 通过证据由 r2 接续并记录来源。

| 专项 | 实机结果 |
| --- | --- |
| 真实采样与阶段切换 | 5 秒／4 轮采样；Warmup 4096 次 Store 优先 PMem；Adaptive 4096 hot + 4096 cold Store，post／WC 与内容校验通过，unknown=0 |
| 边界、固定策略与停止 | 相等归热、全零、固定策略优先、非目标 Interleave、停止策略及内容校验通过 |
| 容量与回退 | 40 GiB 负载填满合计 32 GiB DRAM，验证跨层回退；释放指定 Slot 后验证同层另一 Server 回退；停止策略后 Load 和全量内容校验通过 |

96 GiB 池全满后的磁盘回退仍只有本地验证，未在本轮实机强行填满整个池。

验收工具的重要修正：

- 重复 pageout 前逐页 volatile 写回原值，强制产生新 Store；仅读后再次换出可能复用干净 swap 副本，
  不能据此判断 Adaptive 没有放置热页。
- 阶段间在自有负载／控制 fd 退出后，对测试 swap 执行 swapoff／swapon，再严格检查空池。
  内核 per-CPU swap slot 缓存会延迟 invalidate，单纯等待仍有少量 live Slot 不足以证明泄漏。
- trace 过滤器使用实际声明字段 `ret`，不是打印标签 `alloc_ret`；启动负载前读取事件 `format`
  并验证内核接受过滤器。采样 trace 不作为完整分配回放，受控专项另检查 Store post／WC。

## BFS 实际观测与结果边界

本轮原始 BFS 使用 **6 GiB cgroup、16 线程、64G 图、`-rounds 3`**。
采样器在 BFS cgroup 外运行，使用 **10 秒／2 轮**；基线 0 不计入 Warmup。
第 5 组提交后进入 Adaptive，再完成 10 组，即总第 15 组时暂停 BFS 和采样器，等待用户选择。
继续则每增加 10 组再次暂停；连续 15 分钟无新组完成也暂停，不自动重试。

| 观测点 | BFS 累计时间 | 审计 mean（约） | DRAM／GiB | PMem／GiB | DRAM:PMem |
| --- | --- | --- | --- | --- | --- |
| 第 5 组，Adaptive 入口 | 140 秒 | 0.001746 | 0.000 | 45.445 | 0:1 |
| 第 15 组，后续 10 组完成 | 439 秒 | 0.000701 | 0.498 | 58.087 | 1:116.7 |

实际每组约 30 秒，包含扫描和发布开销。期间 hot／cold Store 均持续发生，unknown 始终为 0，
未观测到 BFS 跨层容量回退；末组热页约 2.86%，冷页约 97.14%，冷页 H 均为 0。
Warmup 的 PMem 存量不会自动迁移，Adaptive 只影响新 Store，容量比例不能直接当成新 Store 的冷热比例。

控制器的 `bfs.ewma.tsv`／`bfs.ewma.jsonl` 保存逐组未做显示舍入的均值及其变化、
当前页数、冷热／零热页数、五档分布、DRAM／PMem 容量、比例和份额，以及 hot／cold Store 与跨层回退增量。
容量口径为 **Client 全池 occupied Slots = live + retired**，不是带宽，也不是严格的目标进程独占容量。
观测匹配同一 binding／window 的提交后采集；目标退出导致缺测时不以清理后的零占用代替。

**本轮不能记作完整 BFS／自动收尾 PASS。** 用户在第 15 组选择停止后，采样器已关闭并确认旧绑定失效，
但控制器等待 BFS 启动包装脚本退出超过 120 秒，保留 `failure.txt`，没有生成最终 `results.json`／`SUCCESS`。
用户随后确认已手动关闭全部测试进程并卸载 Client；未获得控制器完整恢复验证，不能据此认证所有设置恢复。
该包装脚本等待超时问题未在本轮修复，也不能声称 BFS 完成了三轮计时或算法正确性校验。

## 本机便捷运行

Optane 的仓库外脚本位于 `/mnt/sdb2/yxr/K_R_benchmarks/ligra/scripts/adaptive_run/`：

1. 先确保两 Server、Client 1.19 和目标 swap 就绪，再执行 `bash setup_cgroup.sh`。
2. 终端 1 执行 `bash cgroup_run_bfs.sh`。
3. BFS 启动后，终端 2 执行 `bash enable_hotness_adaptive.sh`。

当前脚本统一使用 `hermit-bfs64-4g` cgroup（**4 GiB**），16 线程、64G 图、三轮 BFS；
此配置区别于上述 6 GiB 实机记录。
enable 使用 `pgrep -n -x BFS` 获取实际 PID，适用于仅运行一个 BFS；多个 BFS 时应按 cgroup 和可执行路径筛选。
不要将脚本或 `sudo` 包装进程的 PID 作为目标，必须在 BFS 完成 exec 后绑定。

enable 以 `--placement --a-bit-window 10 --ewma-window 2` 启动采样，
每次新建 `/tmp/hermit-adaptive-时间戳/` 并打印输出路径；读取文件需 sudo。
`--placement` 隐含发布并启用放置，不需要另外传 `--publish`。
Client 断开后再次连接前，应重启参与的 Server。
