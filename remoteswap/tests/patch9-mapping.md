# Patch 9：动态 Slot 与映射生命周期

## 目标、边界与状态

在已完成的 Task 1/2 与 Patch 8 上，解除 swap offset 与远端物理位置的固定关系：
Store 分配位置，Load 查询映射，Frontswap invalidate 结束映射，最后一个请求退出后归还 Slot。
Client 为 **1.9**；Server 控制协议 **v2**、带宽 stats **v2**、managed Frontswap **v2** 不变。
初版不修改内核核心；复核后增加 `mm/memory.c` 的失败读取/PTE 复核修复，
**实机验收前需要安装并重启到重编译内核**。Server、CQ 类型、QP 布局、polling/drain 机制及冷热采样语义不变。
不上传热度、不启用 Adaptive、不做迁移、重连、故障恢复或 PMem 持久化恢复。

本轮已实现并进行本地构建、逻辑回归和用户态 stub sanitizer 检查。
**2026-11-23 已完成内核复核修复后的双节点核心实机验收：四线程页面内容、活进程 swapoff、
同 Session 再次启用及一次 BFS 均通过；Slot 全部归还、Client 卸载、Server 停止。**
配置、证据及覆盖边界见 [核心测试执行记录](patch9-core-smoke.md#2026-11-23-实机核心验收结果)。

## 下一阶段交接

**当前进度：Patch 8 的冷热观测与 Patch 9 的动态位置管理均已具备，但两者尚未接通。**
本阶段完成了逐页分配/查找、Invalidate 与在途引用共同决定的安全回收、容量 Fallback，
以及必要的容量/生命周期观测。生产 Store 仍按 Interleave 选择首选 Server/Tier。

- **Patch 10 尚需实现**：用户态热度/均值整代上传与发布、Start/Stop 及
  Inactive/Warmup/Adaptive 状态机、热度查询与目标选择。可复用
  `rswap_mapping_alloc_preferred()` 分配机制，在新 Store 时选择目标；Load 始终读取既有位置，
  有效映射不因策略更新而改变，不做主动迁移。策略沿用已确认的 `h > mean` 放 DRAM、
  否则 PMem，目标 Tier 内跨 Server RR，未知热度回退 Interleave，容量不足使用已有 Fallback。
  采样窗口和更新语义承接 Patch 8；不能假定每秒都有完成的快照。
- **Patch 11 尚需完成**：热度、阈值、策略状态和放置原因的决策记录及综合效果验收。
  当前 `mapping` Trace 只验证分配/引用生命周期，stats 只描述成功 RDMA I/O，不能替代冷热效果证据。
- **验证边界**：59 项本地回归及双节点核心页面/BFS 验收通过；特殊失败/PTE 交错有本地回归，
  未做故障注入、TB 级或完整异步组合的硬件验收。后续优先核心验证，不默认重跑 Patch 1–8 全矩阵。
- **部署起点**：Client 已使用修复后的内核 **#8**，release 名称仍与旧 #7 相同；
  下一会话先核对 `uname -v`，无新增内核修改时无需重复安装。当前无活动 swap，Client/Server 均停止，
  测试开关已恢复，THP 为原来的 always；下轮 4 KiB 测试需重新禁用 THP。
  继续保持 batch_account 关闭、memsw 无有限配额；sudo 由操作者执行，其余由助手主导。

## 已确认约定

- 按 3–3.5 TiB 接近满池设计，接受以 Client 内存换取操作时间；保持现有容量上限。
- 继续 4 KiB、1/2 Server、一个活动 swap area、静态拓扑和固定在线 CPU 集合。
- 原始 offset 不减 1；Server 编号由配置顺序确定，Slot 为 Tier-local。
- 首选 Server/Tier 保留两级 Interleave；实际 Slot 从对应空闲链表分配。
- 容量 Fallback：首选 Server/Tier → 另一 Server/同 Tier → 首选 Server/另一 Tier → 剩余池。
  单 Server 跳过不存在的候选。这不是失败 Session 的自动故障转移。
- 不能分配 Slot 时本次 Store 失败，允许现有磁盘写回退，不阻塞等待 Slot。
  远端已有副本的 Load 失败仍按已有远端错误语义处理，不能读取过期磁盘数据。
- 一次 invalidate 结束当前映射。现有内核重复 Store 前会 invalidate，因此后续 Store
  可以获得新位置；有效映射期间不重放置，Load 不主动释放位置。
- 最小分配/回收观测纳入 Patch 9，完整冷热决策与效果验收留到 Patch 10/11。

## 数据结构与内存预算

`rswap_mapping.c` 在所有 Session 描述符发现成功后、Frontswap 注册前预分配：

| 结构 | 大小 | 含义 |
| --- | --- | --- |
| `u32 index[pool_pages]` | 4 bytes/page | offset → pool-global Slot ID + 1；0 为没有映射 |
| `struct rswap_slot slots[pool_pages]` | 8 bytes/page | `refs` 高位为索引所有权，其余位为请求引用数；`link` 在占用时保存 owner offset，空闲时保存下一空闲 ID + 1 |
| 每 Server/Tier 的池描述符 | 固定数量 | Slot 基址、空闲链表头及 capacity/live/retired/free |

当前协议最多 6400 GiB / 1677721600 页，可由 32-bit ID 和 offset 表示。
地址、容量计算、Tier-local Slot、MR 字节偏移仍以 64 位处理；初始化验证总页数和各池数量。
请求持有 `mapping_id` 引用期间，该 ID 不会复用，所以无需额外的逐页 generation 对象。

主体数组按**发现的整个池容量**分配，不按工作集或较小的 swap area 缩小：

| 池容量 | 新增主体元数据 |
| --- | --- |
| 48 GiB | 144 MiB |
| 96 GiB | 288 MiB |
| 3 TiB | 9 GiB |
| 3.5 TiB | 10.5 GiB |
| 6400 GiB | 18.75 GiB |

以上不包含 vmalloc 页表、分配粒度、既有 Linux swap 元数据、请求及用户态采样器。
分配失败使 Client 初始化失败并回滚，不能宣告部分容量可用。已做大容量字段/预算检查，
没有实际分配 10.5/18.75 GiB 元数据或进行 TB 级 MR、swap、工作负载验收。

分配和归还使用空闲链表，接近满池无需扫描 Bitmap；索引查询直接访问数组。
每次操作最多检查四个池，使用一个短 `spin_lock_irqsave` 临界区。
锁内不 post、poll、等待 Completion、不分配内存、不调用 Frontswap。
初始化为 O(pool_pages)；正常 swapoff 已逐页失效时 reset 无需扫描整个索引。
有残留映射才分批扫描，批间可调度。数组保留至卸载，可在原 Session 上再次 swapon。

## 所有权与发布

1. **Store 准备**：Interleave 选择首选池，分配 Slot，安装索引，并取得一个 Store 请求引用。
   映射在 prepare 成功返回前准备好；Frontswap 之后先发布有效位，再调用 commit。
2. **Store 收尾**：沿用原 `store_refs` 的 WC/commit 双所有者；只有最后一个所有者退出才
   释放映射请求引用和页面锁。WC 先于 commit、commit 先于 WC 都必须成立。
3. **Load**：在同一映射锁下查索引并增加引用；请求保存实际 ID，使用对应 Session/MR。
   终止 WC 和 DMA unmap 后释放映射引用。Load 成功后索引所有权仍在。
4. **Invalidate**：摘除索引、去掉所有权，live → retired；若请求引用为 0 则立即归还。
   有引用时不等待，最后一次 put 才归还。重复 invalidate 无操作。
5. **回滚**：req 分配、MR 解析、DMA map、post 失败均撤销本次 Store 映射并释放请求引用。
   同步 WC 失败未发布 Frontswap bit，也必须由后端主动撤销映射。
6. **旧请求**：abort 仅在当前索引仍指向被该请求持有的 ID 时摘除；旧读可在新位置建立后
   收尾。异步 Store 失败沿用先标脏再失效的语义，另检查 ID；同 Entry 的正常 Store
   仍依赖既有页面锁串行化，ID 检查不取代内核的锁/发布契约。
7. **area 结束**：关闭提交入口 → 等待入口提交者 → drain 所有 Session → 清除残留映射，
   检查所有池已全部空闲。最终模块退出在 QP/CQ 销毁后释放数组。

有效位已经存在但映射缺失时，不恢复静态地址。由 managed Load 路径显式报远端读取错误。
额外提供 `rswap_mapping_alloc_preferred()` 作为机制接口，供后续策略选择 Server/Tier；
当前生产 Store 只调用 Interleave 包装函数，不新增指定位置 CLI。

## 最小观测接口

带宽继续用原 `stats` / `monitor`，格式 v2 不变。新增只读接口：

```bash
bash remoteswap/client/manage_rswap_client.sh mapping
# 等价于 sudo cat /sys/kernel/debug/rswap/mapping
```

```text
version=1 pages=25165824 metadata_bytes=301989888 sequence=0
server tier capacity live retired free
```

后续每行对应一个 Server/Tier，单位都是 4 KiB Slot：

- `live`：仍在索引中的位置，包含已预留、尚在 Store prepare 中的位置；不等同于 Frontswap 有效位数。
- `retired`：索引已失效但仍有请求引用的位置。
- `free`：允许立即分配的位置。每池始终满足 `capacity = live + retired + free`。
- 整组数量在同一映射锁下获取，`metadata_bytes` 只统计两个主体数组。
- 无活动 swap 且完成收尾后，每池应为 `live=retired=0`、`free=capacity`。

新增默认关闭的 `rswap:mapping`，动作是 alloc/get/put/invalidate/free，包含：
`seq action offset server tier slot users live fallback`。
`users` 不包含索引所有权；Fallback 编码 0/1/2/3 对应四个候选顺序。
事件在映射锁内发出，seq 在模块生命周期内递增，便于检查跨 CPU 顺序和事件缺口。

动态验证：

```bash
bash remoteswap/client/manage_rswap_client.sh trace-check /tmp/p9-mapping.log 16:32,16:32 --dynamic --require-empty
```

必须从**所有池均为空、第一次 Store 之前**开启采集，并使用 ftrace `global` trace clock；
持续读取 trace_pipe，不能对只保留尾部的环形缓冲区宣称完整验收。
`--require-empty` 要求采集覆盖最终回收；不加该参数允许结束时仍有 live/retired，并报告数量。
同时检查各 CPU overrun/commit overrun/dropped events 为 0，文本无 LOST EVENTS。
该工具流式验证唯一所有权、引用变化、实际占用容量、Interleave/Fallback 和安全释放；
内存按采集窗口中未回收的 Slot 数增长，不保存全部历史事件。
它不验证页面数据或 WC 成功，须配合内容校验和原有 stats。
原来的默认静态 trace-check 保留给历史记录；发生合法 Fallback 的新记录不能用静态规则验收。

## 本地验证

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
make -C remoteswap/tools
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
modinfo -F version remoteswap/client/rswap-client.ko
```

新增专项测试调用实际生产 allocator 和 RDMA 函数，而非另写分配算法：

- 小容量单/双节点及异容量穷举、满池、重复分配、再次使用。
- 四候选 Fallback、所有空闲位置都被旧 I/O 引用时 ENOSPC。
- Invalidate 后旧读仍在途，同 offset 新 Store 使用不同 Slot；旧清理不删除新映射。
- 显式选择不同于静态首选的位置后，实际 Load 使用新 Session/Tier/MR。
- 8 线程共 24000 轮分配/读取引用/失效/重新分配，100000 步接近满池随机操作。
- 初始化两处内存分配失败回滚、大容量边界与元数据预算。
- 实际 C 生命周期 Trace 与 Python 校验器一致，拒绝提前释放、重用、过时查找和丢失记录。
- 原有快速 WC、并发 WC/commit、DMA/post 失败、页面锁、统计和内核 Frontswap 契约回归。

ASan/UBSan 检查适用于用户态 stub，不能替代内核/HCA/DMA 验收。
本轮关闭 LeakSanitizer，释放状态另外由断言核对。

### 2026-11-23 本地执行结果

- RDMA Client 编译通过，无新增编译告警；`modinfo` 确认产物版本 **1.9**。
- 用户态工具构建检查通过；整组 **58 项**无特权回归通过（原有 42 项＋新增 16 项）。
- 实际 allocator 的 8 组场景，以及实际新旧 I/O 生命周期检查，均通过 ASan/UBSan。
- shell 语法和 `git diff --check` 通过。
- 未执行硬件验收；不把用户态 pthread/RDMA stub 的结果解释为内核/HCA 并发验证。

本机日志（未纳入 Git）：`/tmp/hermit-p9-client-build.log`、
`/tmp/hermit-p9-regression.log`、
`/tmp/hermit-p9-sanitizer-final-hzqem6dw/results.log`。
最后一次增量构建日志可能仅显示产物已是最新；此前实现阶段已完成各修改文件的实际编译和链接。

## 待执行的实机验收方案

以下矩阵是初版计划。按用户最新要求，复核修复后的验收收缩为双节点核心页面检查＋一次 BFS，
具体目标、更新内核和协作流程以 [Patch 9 核心验收](patch9-core-smoke.md) 为准。
需要 sudo 的宿主机命令仍由操作者执行，不修改 Server；不要直接复制要求版本 1.8 的 Patch 8 核心脚本执行。

| 阶段 | 配置/操作 | 必须看到的结果 |
| --- | --- | --- |
| 单节点 | server02 16+32 GiB，Client 单 Session，已有 48 GiB swap | 1.9 ready、协议 v2，mapping 主体 144 MiB；初始全部空闲 |
| 单节点内容 | 4 GiB swap_pattern、2 GiB 物理限制、2 轮 | 每轮逐页/逐字校验通过，有远端读写；stats 错误增量为 0 |
| 单节点生命周期 | 活进程期间解除物理限制并 swapoff，再完成最终内容校验 | swapoff 成功，mapping 全部空闲；同 Session 再 swapon/小工作集/swapoff 成功 |
| 双节点 | node04+server02 各 16+32 GiB，Client 总池 96 GiB | 两 Session 正确发现，mapping 主体 288 MiB |
| 双节点内容 | 同样的 4 GiB/2 GiB/2 轮，活进程 swapoff，再次启用 | 内容正确，四个池均有成功 I/O，无新增错误，最终全部空闲 |
| 完整动态 Trace | 在空池上开启 rswap:mapping，使用较小页面程序，如 128 MiB/64 MiB 物理限制，采集至 swapoff | 无 Trace 丢失，动态检查 --require-empty 通过；短测试不能声称覆盖全部 Chunk |
| BFS 回归 | 双节点 96 GiB 池、80 GiB swap，64G 图、40 线程、24 GiB 限制、1 次计时轮次 | 正常退出，四池成功 I/O，无新增错误，swapoff/卸载成功 |

沿用已验证的 Server Node 配置、THP=never、batch_account 关闭和无有限 memsw 配额；
只对测试进程施加物理限制，不以既有 cgroup swap 记账作为验收依据。
单/双节点切换前遵守 swapoff → 卸载 Client → 停止 Server，重连前重启参与的 Server。
正常全过程 Server 保持在线，直到 Client 完成 swapoff 和卸载。

Trace 建议单独做小工作集完整采集，不为 4 GiB 页面测试或 BFS 全程产生逐页日志：
建新 instance → 设置 `trace_clock=global` → 开启读取器 → 开启 mapping 事件和 tracing →
开始程序 → 内容验证/活进程 swapoff → 停止 tracing/事件并排空读取器 → 保存 buffer stats → 校验。
大池满容量、失败注入和异步 CQ 在途竞争主要先由本地生产函数测试覆盖；
若实机没有发生某路径，不能宣称已完成该路径的硬件验收。
本阶段不要求 DRAM 带宽高于 PMem，也不宣称 BFS 性能改善。
