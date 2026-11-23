# Patch 10：核心实机验收与执行记录

## 目标与执行边界

以下为本轮核心验收方案；实际执行结果、首次 Trace 问题与修复重测另记于文末。
沿用 Client optane / 10.0.0.11 的修复内核 #8；Client 为新构建 1.10。
Server 和协议不变：endpoint 0=node04 / 10.0.0.13，endpoint 1=server02 / 10.0.0.20，
每台 DRAM 16 GiB + PMem 32 GiB，总池 96 GiB。
继续 THP=never、固定 CPU 0–47、batch_account 关闭、无有限 memsw 配额。

操作者执行 sudo 宿主机操作，助手准备阶段命令/控制器、观察结果和分析日志。
不直接运行 Patch 8/9 中硬编码旧模块版本的控制器，不在旧 run 目录重用 prepare。

## 核心测试一：可控冷热 → 实际 Store → 内容和生命周期

使用新 `tests/adaptive_pattern.c`，建议初次 **32 MiB**；无需靠大内存压力碰运气换出热页。
程序自身只操作其匿名映射，不配置 swap/cgroup：

1. 填充每页每字可验证内容，线程持续读取前半区域，后半区域保持冷。
2. `READY generation=0` 暂停；控制器保持 stdin FIFO，不使用 PTY 输入注入。
3. 采样器继续运行。确认 Warmup 到期、内核 Adaptive、完整 generation 已发布，
   并在已提交样本中确认两半热度确有差异后，才释放 ENTER。
4. 程序停访问线程，执行 MADV_PAGEOUT，然后读回所有页的每个 word。
   必须从 stats/decision 证明确有已知热页和冷页 Store；MADV_PAGEOUT 返回 0 不证明全部页都换出。
5. 第二代重写全部内容，改成后半区域热，再次 READY；等待窗口更新和新代实际发布，
   然后释放 ENTER，重复 pageout/内容验证。重写用于产生新的 Store 生命周期。
6. `READY_FINAL` 保留活进程，操作者成功 swapoff 后确认各池全空，再释放最后 ENTER，
   程序完成最终内容校验并 PASS。

启动采样示意（须先完成操作者的连接和 swap 准备，并由控制器持有 stdin）：

```bash
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p10-NEW/pattern/heat \
  --placement --interval 1 --window 4 --warmup 4 --pages \
  -- /tmp/hermit-p10-adaptive-pattern 32
```

MADV_PAGEOUT 会改变 Young/Referenced 状态。本轮决策只用 pageout 前已发布代解释，
不能以后来的采样补认此前热度；必要时检查 decision 的实际 generation，而非猜测每秒一次。
热页被监控到并不足够：必须有 reason=5 和 reason=6 的成功分配及相应远端成功写入证据。

从空池、第一次 Store 前启动独立 ftrace instance，global 时钟，持续读取，
同时启用 `rswap:mapping` 和 `rswap:decision`，采集至 swapoff 全部回收。
检查 per-CPU overrun/commit overrun/dropped events 全为 0，之后运行：

```bash
bash remoteswap/client/manage_rswap_client.sh trace-check /tmp/p10-trace.log \
  16:32,16:32 --dynamic --adaptive --require-empty
```

必须达成：内容全部正确；在空闲充足时热/冷分别落 DRAM/PMem；目标 Tier 内两台均有决策；
无新增 WC/submit/unrouted 错误；swapoff 成功、live=retired=0、free=capacity。
策略 Stop/更新期间已有映射仍可正常读回；上传句柄关闭或 swapoff 后状态 Inactive。
容量 Fallback 和全池不足主要由本地实际分配器回归覆盖，不为此次核心验收填满 96 GiB。

可在同一连接增加短控制检查：手动模式 Warmup 到期前 adaptive 返回 EAGAIN，
到期后仍 Warmup，显式 adaptive 后生效；placement-stop 后原进程和观察继续，
下一轮 Start 获得新 run_id。无需把这扩展为完整单双节点硬件矩阵。

## 核心测试二：BFS 摘要集成

同一 Session 上再次 swapon 既有 80 GiB swap，重新 Start；沿用 64G 图、40 线程、
24 GiB 物理限制、1 次计时轮次及 Ligra 自带预热。采样器位于受限 workload cgroup 外。
使用 `--placement`，不启用 `--pages`，不全程采集逐页 Trace。

验收：程序正常退出 0；有一次计时输出；能观察到 Warmup/Adaptive 和完整 publication；
记录扫描超期、upload_ms、快照年龄/记录数和均值变化；stats 中有成功远端读写，错误无新增。
覆盖不足或全部命中冷页应如实报告，不要求 BFS 必须产生热页 Store，也不要求 DRAM 带宽高于 PMem。

## 收尾与留存

Server 保持在线 → 成功 swapoff → 核对 mapping 全空及策略 Inactive → 停止采样器/Trace/monitor →
卸载 Client → 停止 Server；恢复原 THP/HMT/物理限制等测试配置。
保留 build、版本、console/exit、summary/publication、短窗口 trace/buffer stats、
前后 stats/mapping/status 和新增 dmesg。结果转存数据盘，不只放重启可能清理的 /tmp。

本次不认证 TB 级、故障注入、全部异步 CQ 组合、BFS 算法结果或性能提升。
完整效果比较及监控分析工具的扩充留给 Patch 11。

## 2026-11-23 实机核心验收结果

**修复后两项核心验收通过，正常收尾完成。** Client 使用现有 #8 内核、模块 1.10，
48 个连续在线 CPU；两台 Server 各 DRAM 16 / PMem 32 GiB，总池 96 GiB。
node04 的 Tier Node 为 0/1，server02 为 0/2，两台全部物理查询均 misplaced=0，
各 6 个 MR 注册成功。Server 没有修改代码或协议。

### 首次运行发现问题与重测

首次运行完成两代内容和全部映射回收，但读取 mixed decision/mapping Trace 时出现
`trace_check_vprintf` WARNING。因此该次没有作为完整通过，也没有继续启动 BFS。
先保留现场，再以本机实际内核校验函数复现：seq 缓冲区已满后 vprintf 未消费 va_list，
后续 `%s` 校验可能读到前面的数值。修复将 mapping/placement 文本改为数字字段，
工具兼容旧格式；不改数据路径、放置、内核或协议。

重新编译和 67 项本地回归通过后，操作者启动受控 restart：停止旧控制器、确认无活动
swap/映射、恢复设置并卸载旧 Client。助手确认两个旧 Server 均已释放资源，再启动新进程；
使用新的产物 SHA256 清单、全新 run 目录重测。内核无需重启。
详细修复与格式说明见 [Patch 10 设计](patch10-placement.md)。

### 修复后页面与策略验收

| 目标 | 实际结果 |
| --- | --- |
| 热度分离 | 32 MiB / 8192 页；第一轮前半 4096 页全为热、后半全为冷；交换后角色反转。ENTER 前验证已完成快照及内核 Adaptive |
| 内容 | 两代各 8192 页逐页逐 word 校验通过；活进程 swapoff 后最终内容仍正确，程序和采样器均退出 0 |
| 实际放置 | 16384 次成功分配均与内核实际使用的 generation/PFN/heat/mean 相符；热/冷各 8192 次，分别进入 DRAM/PMem |
| 两节点 RR | 四个 Server/Tier 各 4096 次分配及成功 Write、4096 次成功同步 Read；无容量 Fallback |
| Trace 生命周期 | 98304 条 mapping 事件完整通过；16384 次分配全部归还，无 live/retired/引用残留；48 个 CPU 的 overrun/commit overrun/dropped events 均为 0 |
| 控制接口 | 手动 Warmup 到期前切换 EAGAIN，到期后仍等待手动命令；显式切换、Stop、关闭 owner fd 自动 Stop、新 run_id 及旧 fd ESTALE 均通过 |
| 收尾 | 活进程 swapoff 成功，四池全部空闲、策略 Inactive；同 Session 再 swapon 80 GiB 成功，重新 Start 的 run_id 从 3 变为 4 |

页面阶段共 49 轮观察、33 代上传；swapoff 后采样器检测 ESTALE 并继续纯观察，直到程序正常退出。
该阶段没有采样超期，最大扫描 200.323 ms，最大上传 143.605 ms。
全部实际 Store 均在 Adaptive 阶段；不声称硬件覆盖了 Warmup Store 路由。
PFN 查表一致不等于消除了 PFN 身份复用/采样竞争误差。

### BFS 集成

同一组 Session，既有 80 GiB swap，64G 图、OMP_NUM_THREADS=40、24 GiB 物理限制，
仅目标程序进入 cgroup，采样器在外，memsw 无有限配额。
配置 `--placement --interval 1 --window 20 --warmup 20`，关闭逐页 TSV 和全程 Trace。

- BFS 和采样器退出码均为 0，有且仅有一条 `Running time : 0.253`；该值为算法输出，
  不作为端到端时长或性能提升证据。监控运行约 224.79 秒，包含输入/初始化/预热/退出检测等。
- 32 轮采样、32 代完整上传；summary 有 Warmup/observe，内核状态有 Warmup/Adaptive。
  最终采样器退出自动结束策略，swap area 尚活动时状态已为 Inactive。
- 四池全部有成功读写，WC/submit/unrouted 错误增量全为 0。
- 22/32 轮超期，最大扫描 **17.187 秒**，最大上传 **2.065 秒**；
  状态采样观察到的最大快照年龄 **18.295 秒**。不能将配置周期或窗口解释为固定秒数。
- 最大上传 resident 记录数 5088246，观测到的已发布热度表最大约 81.4 MB。
  该值不含 staging、用户态采样/排序数组及页表，不是总监控内存峰值。
- 大工作集阶段 known 曾降至 31，之后最大达到 8815797；VMA 变化及换页竞争等近似限制
  延续 Patch 8。BFS 未保存逐页快照，不宣称测得其内核 PFN 命中率或冷热策略收益。

每次成功操作为 4096 字节；下表重复 I/O 重复计数，不表示唯一页或驻留容量：

| 阶段 | 成功 Write | 成功 Read | WC/submit/unrouted 错误增量 |
| --- | ---: | ---: | --- |
| 可控冷热页面 | 16384 | 16384 | 全为 0 |
| BFS | 24833706 | 18380669 | 全为 0 |

本次 Read 全为同步路径。容量 Fallback、异步及失败竞争沿用本地实际函数回归，不声称
新增硬件认证；Inactive 后仍有效映射的 RDMA Load 由本地检查覆盖，本轮不单独认证该硬件交错。
DRAM 带宽高于 PMem 不作为验收条件。

### 最终状态与证据

80 GiB swapoff 成功，四池 live=retired=0、free=capacity，策略 Inactive；
Client 卸载，两台 Server 正常退出并输出 `Server resources released`。
HMT 全部开关、THP=always、page-cluster=3、readahead_win=0 恢复并读回，测试 cgroup 和
Trace instance 清理，原 Client 配置未改。修复后新增 dmesg 无相关 BUG/WARN/Call Trace/WC 错误。

当前无活动 swap，Client/Server 均停止。下次 4 KiB 测试需重新禁用 THP；
不要直接在旧 run 目录再次执行控制器。

原日志：`/tmp/hermit-p10-core/`（首次未接受运行）和 `/tmp/hermit-p10-core-fixed/`（有效重测）。
常规文件转存数据盘，Git 忽略：`remoteswap/tests/artifacts/patch10-core-20261123/`，
分别为 `first-run/` 和 `fixed-run/`。
有效运行的 `results.json`、`pattern.results.json`、`bfs.results.json`、`SUCCESS` 为摘要；
另保存完整 heat/publication、短窗口 Trace/buffer stats、console/exit、stats/mapping/policy、
内核日志、两台 Server 日志、产物校验和以及本轮 host/observe/check 控制器。
控制器只是本轮实验工具，不是生产 API。
