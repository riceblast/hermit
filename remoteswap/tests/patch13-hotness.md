# Patch 13：VPN 热度、EWMA、双节点验证与退出修复

更新时间：2026-11-28。Client **1.18**；总结基于提交 `2918f04a6` 的代码和本轮日志。

**当前结论：第二阶段代码已实现，编译与 156 项本地测试通过；双节点受控专项通过，
真实 BFS 完成三次遍历计时和三次正式 EWMA 更新，已检查内容未出现 unknown。
BFS 退出阶段暴露的采样器 ESTALE 误报已修复并完成本地验证。
用户明确表示“不用复测了”，因此不再重跑 BFS；原实机记录保留退出错误，不改写为整轮 PASS。**

截至本文核对，环境清理尚未完成：Client 1.18 仍加载，64 GiB 测试 swap 仍激活，
没有 `cleanup.complete.json`。清理需要用户执行 sudo，见第 9 节。

## 1. 阶段与代码边界

| 阶段 | 当前状态 |
| --- | --- |
| 第一阶段：swap entry 与目标 mm/VPN 关联 | 已实现，双节点、Hermit、生命周期和真实 BFS 验证通过，见 [Patch 12](patch12-kernel-swap.md) |
| 第二阶段：VPN 历史、采样、EWMA、均值和 summary | 本 Patch 已实现并完成下述验证 |
| 第三阶段：依据 VPN 热度调整 Warmup/Adaptive 放置 | **尚未实现** |

第二阶段新增的 VPN 热度快照只用于发布、查询和验证，**Store/Load 尚未消费它**。
原 PFN 上传 ABI、已有固定放置规则和旧放置逻辑保留；本阶段没有修改 Server 协议或内核核心。
不能将“新热度表已经发布”理解成“数据已按新热度放置”。

相关提交：

- `b357e20df`：第二阶段主实现，Client 1.18。
- `6f264a36d`：实机测试工具、测试记录及退出处理修复。
- `2918f04a6`：文档更新与只做恢复的清理脚本。

## 2. 已确认的统计口径

历史以同一次监控、同一个目标 mm 中的 **VPN** 为键。共享 mm 的线程属于同一目标。
换入后的 PFN 变化、VMA 描述变化和同地址重映射不清空历史。
不增加用来清除 H 的页面生命周期代次，不跟踪 munmap 事件；不自动追踪 fork 或后续 exec，
不跨采样器运行持久化历史。

| 页面当前状态 | 保留已有 VPN 历史并参与后续更新 | 计入当前 pages、mean 和分布 |
| --- | --- | --- |
| 普通 4-KiB 匿名 resident PTE | 是 | 是 |
| 普通匿名 swapped PTE | 是，当轮无法观察访问时按 0 | 是 |
| 仅预留、尚未触碰，PTE 为空 | 已有历史保留，新地址不必建立记录 | 否 |
| MADV_DONTNEED 后 PTE 为空、VMA 仍有效 | 是，后续缺失观测按 0，页面回来后沿用 | 否 |
| 映射已经取消 | 是，后续缺失观测按 0，同地址再次出现后沿用 | 否 |
| 文件页、共享 shmem/tmpfs、huge/compound 等排除对象 | 已有同 VPN 历史保留，后续排除观测按 0 | 否 |

每个小周期产生一个 0/1 样本；swapped、缺失、采样无效均按 0，不采用旧的 unknown/有效窗口分母。
同一组中此前已经记录的访问仍有效，页面随后消失不会追溯删除本组已累计的 hits。

**逻辑统计按 VPN 计数，不按 PFN 或 swap entry 去重。** 两个 VPN 即使共享物理页也分别计算，
只允许合并物理 Idle bitmap 的 I/O。resident 页检查 private 映射及 KPF_ANON/KPF_SWAPBACKED，
排除大页等；private 文件映射中的普通 COW 匿名页可以计入。
当前配套内核配置下普通 swap type 为 0..28，29..31 是特殊 entry，换内核配置时需同步检查这一边界。

## 3. 采样、EWMA 与输出

- `--a-bit-window` 默认 **5 秒**：一轮扫描、重设 Idle、输出/发布完成后，再等待 5 秒。
  下一轮先读旧 Idle 结果，再将可采样 PFN 的 Idle 位写 1，建立下轮基线。
- `--ewma-window` 默认 **4**：完成四轮不重叠的小周期后更新一次。
  `--interval`、`--window` 是两个参数的别名；可配置范围分别为 0.001..86400 秒、1..64 轮。
- 初始扫描只建立基线并输出 `0(0s)`、H=0，不计作一次已完成的观测。
- 每组计算 `S = hits / N`、`H = 0.8 × H_old + 0.2 × S`，其中 `N = EWMA_window`。
  新出现的页面也使用完整组分母；所有保留历史在组结束时更新，含当前不在统计范围内的历史。
- 内部使用 Q32：`1.0 = 2^32`，每次更新四舍五入。当前均值保留未做两位显示舍入的总和与页数，
  输出时才取两位小数；五档边界使用最接近精确 0.2/0.4/0.6/0.8 的 Q32 表示。
- PFN 仅服务当前物理采样。观察到变化时该次访问计 0、重新建立基线，H 保留。
  **按用户接受的 KISS 方案，不增加换页序号**；两次检查间换出又换入、恰好返回同 PFN 的误差允许存在。
  共享物理页的访问也不能完全归因到单个 VPN。

5 秒是工作完成后的等待时间，不是精确墙钟窗口。实际每组时长还包含扫描和发布；
不补造缺失轮次，不静默延长配置，不因扫描超过 5 秒判失败，也不做按 elapsed 修正的 EWMA。
例如每轮扫描 16 秒、等待 5 秒，四轮约 84 秒。

生产采样器仅输出 **summary.tsv**，九列如下：

```text
pid  EWMA_window  mean_hotness  pages  dist[0 - 0.2]  dist[0.2 - 0.4]  dist[0.4 - 0.6]  dist[0.6 - 0.8]  dist[0.8 - 1.0]
```

实际用一个 TAB 分列，数据字段内部可有对齐空格，解析时 strip。
分布为 `数量(比例%)`，比例一位小数，最后一档包含 1.0。
`pages=0` 时 mean 为 `0.00`、各档为 `0(0.0%)`。
不再产生逐页、raw、run、placement 或 distribution 日志；测试工具的查询证据另存到测试目录，
不改变生产采样器的单文件输出要求。

## 4. Client 快照与 unknown 的含义

新接口是 debugfs `vpn_heat`，权限 0600，定义见 [UAPI](../rswap_vpn_heat_uapi.h)。

1. 保持 `identity` fd，bind 实际目标 PID，取得 binding_id；`mm_current` 表示 PID 的当前 mm 仍匹配绑定。
2. 热度 fd 只能 BIND 一次；BEGIN 声明组号、保留历史数、当前 pages 和 heat_sum。
3. DATA 按 VPN 严格递增，每批最多 4096 条；每条含 VPN、H、是否属于当前统计。
4. COMMIT 验证条数、当前页数和热度总和后，使用 RCU 原子替换快照。
   半包、重复/乱序记录、非法值、分配/拷贝失败不会取代上一份完整快照。
5. QUERY + pread 复制出快照元数据和指定 VPN 热度，不暴露内部指针。

有效 binding_id 内，尚无历史的 VPN 返回 **H=0、found=0**；已存历史查询应 found=1。
错误或失效 binding_id 返回 **ESTALE**，不能把它统计成目标冷页。
因此“本轮未出现 unknown”指已验证的目标观测和查询，不表示失效绑定也应返回有效热度。

identity fd 关闭、目标退出/exec、swapoff 都会使旧绑定不能再用于当前查询/发布。
旧 fd 关闭不能撤销新绑定。热度 fd 关闭、swapoff 或新绑定接替时释放相应快照。
用户态每条历史约 32 字节，Client 快照每条记录 24 字节，另有排序、容量预留及替换时的临时开销；
历史随本次运行曾出现的 VPN 数增长。

## 5. 修改入口与运行方式

| 文件 | 职责 |
| --- | --- |
| `tools/rswap_hotness.c/.h` | VPN 历史、Idle 采样、分组 EWMA、summary、可选发布、进程退出识别 |
| `client/rswap_vpn_heat.c/.h`、`rswap_vpn_heat_uapi.h` | 新快照事务、RCU 查询、fd 所有权和撤销 |
| `client/rswap_identity.c/.h` | 检查绑定 mm 是否仍为目标当前 mm，增加 mm_current |
| Client Makefile、stats、rdma_ops、版本声明 | 注册接口、连接 swap 生命周期，版本更新至 1.18 |
| `tools/rswap_hotness_check.py`、`rswap_analysis.py` | 新 summary 校验/概要，保留历史 PFN 日志读取 |
| `tests/stage2_host.py`、`stage2_pattern.c`、`stage2_sampler_audit.c` | 双节点控制、受控页面访问与内容校验、实际发布/query 检查 |
| `tests/test_stage2_*.py` | 生产函数、CLI 循环、发布/接收与退出竞态的本地回归 |
| `tests/stage2_cleanup.py` | 仅恢复本轮测试环境，不执行负载或复测 |

旧 PFN 发送端保存在 `tests/fixtures/rswap_hotness_v1.c/.h`，仅用于旧 ABI、放置和归档日志兼容测试，
不能作为新生产采样器的验证证据。

```bash
# 默认只观察；读取 PFN 和 Idle bitmap 仍需特权。
sudo remoteswap/tools/rswap_hotness --out /tmp/vpn-heat-NEW --pid ACTUAL_PID
# 发布到已连接并 swapon 的 Client 1.18，不启用第三阶段放置策略。
sudo remoteswap/tools/rswap_hotness --publish --a-bit-window 5 --ewma-window 4 \
  --out /tmp/vpn-publish-NEW --pid ACTUAL_PID
sudo /usr/bin/python3 remoteswap/tools/rswap_hotness_check.py /tmp/vpn-publish-NEW/summary.tsv
```

输出目录必须新建。支持 `-- COMMAND...`，先等首次 exec 成功再绑定；BFS 脚本会另起实际负载，
因此应 attach **实际 BFS 可执行进程 PID**，不能只监控脚本 PID。
旧 `--placement`、`--pages`、`--raw-logs` 等选项不再可用于当前采样器。

## 6. 本地构建与回归结果

| 检查阶段 | 结果 |
| --- | --- |
| 第二阶段首轮本地验证 | 134 项通过，20.745 秒；其中新增第二阶段测试 15 项 |
| 实机测试工具准备 | 137 项通过，22.276 秒 |
| 退出修复后完整回归 | **156 项通过，23.497 秒**，含 18 项新增退出竞态测试 |
| 更新 PID 复用夹具后 | 单独重跑 18 项退出测试通过，0.856 秒 |
| 编译与静态检查 | 采样器、Client RDMA 模块构建通过；采样器/观察器严格编译及 Clang 分析通过 |

Client 使用当前 `linux-5.14-rc5` 构建，模块版本 1.18；成功构建日志未见新增警告。
退出修复只涉及用户态采样器和测试工具，不需要重编/重载 Client 或内核。
本地验证使用实际生产函数、无特权文件/VM/RCU 夹具及 UBSan；不把夹具验证等同于实机覆盖。

## 7. 双节点实机结果

| 节点 | RDMA IP | Hermit 目录 |
| --- | --- | --- |
| Optane，Client | 10.0.0.11 | `/mnt/sdb3/data/yxr/projects/hermit` |
| node04，内存池 Server 0 | 10.0.0.13 | `/home/yxr/projects/hermit` |
| server02，内存池 Server 1 | 10.0.0.20 | `/home/yxr/projects/hermit` |

两台 Server 各 16 GiB DRAM + 32 GiB PMem，总池 96 GiB；端口 9400，Client 在线 CPU 数 48。
使用 #9 内核 `5.14.0-rc5-hermit-syscall-v2`，构建标识为
`#9 SMP Sat Nov 28 00:02:37 CST 2026`。测试期间禁用 THP 和 HMT 优化。

### 受控专项：通过

- 8192 个普通匿名目标页，前半持续访问、后半保持冷；完成 6 组 EWMA，共 7 份快照。
- 前两组抽查热页 32/32 符合 **0.20、0.36**，冷页 32/32 均为 0。
- 实际换出/换入 4096 页，每个 Server/Tier 各完成 1024 页写和同步读，共写/读各 16 MiB。
  4096 页换入后 PFN 变化，内容校验和 VPN 历史延续通过。
- MADV_DONTNEED 后目标当前页数 **8192→4096**，历史记录数保持 8192，H 继续衰减；
  refault 后当前页数恢复 8192，沿用旧历史，内容校验通过。
- 672 次已存历史 VPN 的内核查询通过；summary 页数、均值和分布与提交快照一致。
  关闭 identity 后，保留的旧热度 fd 查询按预期返回 ESTALE。

### 原始 BFS：功能观测完成，退出环节暴露问题

原样执行 `/mnt/sdb2/yxr/K_R_benchmarks/ligra/scripts/cgroup_run_bfs.sh`：
16 个 OpenMP 线程、`rMat_64G.adj`、`-rounds 3`，一次预热加三次计时。
本地 memory cgroup 24 GiB、swappiness=100、memsw 无限，使用既有
`/mnt/sdb4/yxr/swapfile/swap_64g.img`，采样器在 BFS cgroup 外运行。

| 正式 EWMA 组 | 相对基线累计时间 | 当前 pages | 未舍入 mean | summary 显示 |
| --- | --- | --- | --- | --- |
| 1 | 35.091 s | 6453306 | 0.0220449100 | 0.02 |
| 2 | 98.604 s | 14450780 | 0.0134577940 | 0.01 |
| 3 | 163.696 s | 16835325 | 0.0112926375 | 0.01 |

- 共 4 份快照（含基线），128 次已存历史 VPN 查询通过。加上受控专项，合计 **800 次查询，未发现 unknown**。
  全量上传记录验证 H 在 Q32 [0,1] 内；这是本次检查范围的结论，不认证所有可能并发情形。
- 三次遍历计时为 **0.462、0.331、0.325 秒**。监测记录约 220 秒；退出错误导致没有完整端到端退出码/耗时，
  不能记作整轮正常退出。BFS 本身没有输出算法结果 checksum。
- 远端观测峰值 **42.386 GiB**。退出失败前累计远端写 **71.678 GiB**、同步读 **58.489 GiB**，
  已减去受控负载的各 16 MiB；异步读为 0。两个 Server 的四个 Tier 池均发生实际读写。
- 未见 WC、submit、unrouted、分配错误或新增相关内核异常。旧 PFN 放置保持 inactive，
  no_snapshot/no_mean/pfn_miss 等旧策略计数未增长；不能将这一点当成新策略已经验证。
- 采样器峰值用户态 RSS **1472.523 MiB（约 1.44 GiB）**，采样到的 CPU 时间约 **139.33 秒**。
  不含 Client 内核热度表开销，也没有性能 A/B。

### 对运行时间和低均值的解释

Ligra 在 `readGraph` 后才开始输出遍历计时，进程全程还包括文本读取、解析/建图、换页及销毁。
64 GiB 级峰值在 24 GiB 限额下产生大量远端 I/O；此前同参数第一阶段全流程 231.46 秒可作参考，
不能据此推导采样器的额外性能成本。

BFS 的 mean 是 **0.02→0.01→0.01**；**0.10 来自受控负载**，约半数页 H=0.20、半数为 0。
BFS 均值包含大量按 0 采样的换出页、新页和无访问页，且 H 从 0 起步、仅完成三次分组更新。
相邻实际组间隔约 35.091、63.513、65.092 秒，亚秒级的三次遍历不能提供持续多组的稳态访问观测。
没有发现参数传错；延长进程空闲不会自动提高 H。若以后需要研究遍历阶段，应让同一图的重复遍历持续多个实际采样组。

## 8. 退出处理问题与修复

原采样器 stderr 为 `rswap hotness: Stale file handle`，控制器按约定停止并保留现场。
从日志和代码判断，问题出在目标退出与采样检查的交错：Linux 先设置 PF_EXITING，
随后 `exit_mm()` 清除 mm，再进入 zombie；只判断进程 Z/X 状态不足以识别正在退出的目标。

修复包括：

- 读取 `/proc/PID/stat` 第 9 字段的 PF_EXITING，正常结束对正在退出目标的观察。
- 绑定检查或发布遇到 ESTALE/ESRCH 后，复核目标是否已结束或 PID 已复用，再决定正常收尾。
- 仍存活的 exec、绑定撤销和 swapoff 保持报错，运行中的 I/O/协议错误不因修复而被忽略。
- 不追随新 PID/mm；未完成或未确认的观测不补写 summary。
- 测试观察器处理 COMMIT 成功后目标退出、query 失效的情况；控制器接受 PF_EXITING，等待实际 BFS 收尾。

18 项新增退出回归覆盖循环检查、BEGIN/DATA/COMMIT/ACK、扫描中退出、PID 复用、
活进程的失效绑定、不可读进程状态、真实 I/O/协议错误等路径。
**修复仅完成本地验证，用户明确取消实机复测；后续接手不要自动重跑 BFS。**

## 9. 证据、清理与后续边界

原始测试目录：`/tmp/hermit-stage2-core-bfs-20261128`。
以下归档在本机存在，当前未被 Git 跟踪；跨机器交接时需另外复制，不能假定 clone 后具备：

- [首次本地验证](artifacts/stage2-local-20261128/README.md)：134 项测试、构建和 SHA-256。
- [实机原始失败记录](artifacts/stage2-core-bfs-20261128-failed-exit/STATUS.txt)：日志、快照和失败时源码。
- [退出修复验证](artifacts/stage2-exit-fix-20261128/README.md)：156 项回归、18 项专项和修复文件校验。
- [详细接口说明](stage2-vpn-hotness.md)。

截至本文写入：未见 `cleanup.complete.json`；Client 1.18 仍加载，64 GiB 测试 swap 仍激活。
**尚不能记录为环境已恢复。** 已提供仅清理脚本，由用户在 Optane 执行：

```bash
sudo /usr/bin/python3 /mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/stage2_cleanup.py \
  /tmp/hermit-stage2-core-bfs-20261128
```

脚本通过模块实例、进程归属、启动时间和 pidfd 约束清理范围，结束本轮测试进程，
关闭测试 swap、确认远端池为空、卸载 Client，恢复原 `/swap.img` 和相关设置。
原配置包括 THP=always、page-cluster=3、swap/readahead_win=0，Hermit 开关均为 N；
测试新建的 `hermit-test` cgroup 需要恢复/移除。保留日志和既有 64 GiB swap 文件，不重新格式化或删除它。
清理成功写 `cleanup.complete.json`，不创建本轮测试的 `SUCCESS`。

第三阶段仍待后续指示：按成功完成的 5 个 EWMA 组处理 Warmup，而非固定 100 秒；
实现 Warmup 优先 PMem、Adaptive 按未舍入 H 与当前 mean 比较（相等归热），以及容量回退，
并保留既有固定放置规则的优先关系。非监控进程不参与冷热分类；decision reason 显示页面热度和均值
（两位小数），分类使用未舍入值。多个目标 VPN 共享 swap entry 时的热度聚合仍需明确，
不能把第一阶段查询返回的最低 VPN 直接当作已确认的最终策略。

本轮未覆盖长时间压力、性能 A/B、算法 checksum、新放置策略或全部并发/故障情形。
