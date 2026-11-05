# Patch 1 总结与 Patch 2/3 交接

## 当前成果与边界

- 支持 `nr_servers=1/2`，最多两个独立 Session；单节点只使用 `sip/sport`，第二个 endpoint 不参与连接。
- 每端通过 `FREE_SIZE` 发现实际容量，检查 Chunk 数 `1..MAX_REGION_NUM`；`GOT_CHUNKS` 必须与其一致，并校验地址、8GiB 长度和溢出。两端无需等容量，实际总和必须严格等于 `rmsize`。
- 保留独立 QP/CQ、PD、控制消息和 Region 列表；任一步失败整体回滚，不自动降级。按实际节点数停止并逆序清理，包括部分初始化资源、控制 WR、DMA 映射、请求缓存和 CM 回调。
- 内核仅改 `include/linux/frontswap.h`、`mm/frontswap.c`、`mm/swapfile.c`：增加受控注册/注销、单 swap area 绑定及模块引用、SRCU 调用保护、swapon 错误回滚。恢复 Load 位图检查、重复 Store 失效处理和位图计数正确性。
- `remoteswap/client/rswap_client.conf` 是管理脚本的唯一配置来源，不接受环境变量覆盖。install 打印生效配置；status 读取已加载模块参数；配置损坏不妨碍 status/uninstall。
- **仍为连接测试版**：`rswap_register_frontswap()` 传入 `max_pages=0`，主动拒绝 swapon。旧数据路径仍有硬编码和错误处理缺口，不能直接解除限制。
- 编译及 7 项无 sudo 脚本测试已通过；用户反馈已完成本轮测试，此前已确认连接、回滚和 SSD swap 基础功能正常。**远端页面读写、活动 swap 的卸载保护及数据完整性尚不属于已验证成果。**

Server 和消息格式未改；Server 在最终断连后退出，重连前重启参与的 Server 进程。容量指进程实际暴露的空间，并非机器总内存；现有 Server 会把非 8GiB 整数倍的启动容量向下截断。

## Patch 2：统一静态地址解析

使用实际拓扑，不能再假定两个节点或每端容量等于 `rmsize/2`。节点编号取决于配置顺序，与 IP、物理机器名称、哪端更大无固定关系。

令 `o` 为原始 **swap 页偏移**，`C0/C1` 为各端容量的页数，`T` 为总页数；先拒绝 `o >= T`：

```text
单节点：server=0，slot=o

双节点：M=min(C0,C1)
  o < 2*M：server=o&1，slot=o>>1
  其余：server=容量较大的节点，slot=M+(o-2*M)

byte_offset = slot << PAGE_SHIFT
chunk_idx = byte_offset >> CHUNK_SHIFT
chunk_offset = byte_offset & CHUNK_MASK
remote_addr = selected_chunk.remote_addr + chunk_offset
```

这是已确认的“共同容量交错＋剩余尾段”：8+16GiB 的前 16GiB 交错，后 8GiB 属于较大节点；等容量没有尾段。全范围严格奇偶分配无法用满不等容量，不能混用两套规则。

- Store、store_on_core、同步 Load、异步 Load 共用一个解析函数，返回实际 Session/Chunk/区内偏移；选队列、DMA 设备、PD/lkey 和 remote rkey 必须来自同一个 Session。
- 替换 `rswap_rdma_send()` 对 `rdma_sessions[0]` 的硬编码；检查 slot、Chunk 下标、MAPPED 状态、`chunk_offset+PAGE_SIZE <= mapped_size` 和地址运算溢出。
- 保留节点数/总页数的模块内持久状态。`active_servers` 目前是 `rswap_rdma_ops.c` 内的 static；不能持有模块 init 栈上的 config 指针，也不能从后来编辑的配置文件重新解释旧页面位置。
- 单位统一为页、字节、GiB；先算页奇偶再左移。swap offset 0 通常是 header 保留项，**不要人为减 1**；池总页数含这个地址位置。
- 静态映射继续复用 Linux swap entry 的生命周期，无需动态位置表或 Slot allocator；Load 后不另行释放远端位置。保持单一 swap area，不能在忽略 swap type 的同时放开多个 area。
- 不同 CPU/QP 之间没有 RDMA 写顺序保证。重复 Store、invalidate 后复用 entry 时，要确认旧 I/O 已受页面锁/entry 生命周期约束，避免迟到的旧写覆盖新数据；不能只靠位图判断。

## Patch 3：完成、轮询和错误处理

主要未完成内容集中在 `remoteswap/client/rswap_rdma_ops.c`，需与 Patch 2 联动验收：

| 当前状态/风险 | 后续要求 |
| --- | --- |
| 普通 Store 的等待、poll/peek 仍只使用 Session 0；`drain_all_rdma_queues()` 忽略目标参数 | 普通 Store 等待实际目标队列；CPU-only poll/peek 没有 offset，必须覆盖所选全部 Session；peek 汇总剩余请求，不能提前返回“全部完成” |
| STORE、LOAD_SYNC 的 CQ 为 DIRECT；LOAD_ASYNC 为 SOFTIRQ | 保留不同完成上下文；背压和 drain 也必须遵守，不能把所有 CQ 都直接 poll；并发 direct polling 需正确串行化 |
| callback 已使用 `queue->rdma_session` 识别类型，但完整数据路径仍未修复 | DMA 同步/unmap、计数、缓存回收全部使用请求所属队列；不要退回全局 Session 的指针相减 |
| callback 在 WC 错误后仍可能 SetPageUptodate；先解锁/减 pending 再 unmap/free | 失败读不得标记有效；保证必要 DMA 可见性和资源处理先于对外宣布完成；pending=0 不能单独证明整个回调已退出 |
| 非 lazy 模式的同步 Load callback 与 `hermit_poll_read()` 可能重复解锁 | 明确唯一解锁者；优先在模块中让同步 Load callback 只完成状态/资源处理，由现有 Hermit 调用路径解锁；异步 Load、Store 按各自契约处理，逐个核对调用点和 lazy_poll 分支 |
| 分配请求失败可能返回 0；post_send 失败未完整撤销计数/资源；普通 Store 错误路径可能漏 put_cpu | 统一“未提交由调用者清理、已提交由 Completion 清理”的所有权；注意 `fs_build_rdma_wr()` 当前映射失败会自行 free，重构时避免双重释放；不能等待未提交请求的 Completion |
| Store poll/peek 使用 `% NR_WRITE_QUEUE`（固定 48），发送路径没有相同转换 | 统一发送与轮询的 CPU/队列索引规则并检查范围；本阶段保持在线 CPU 集合不变，不能假定 CPU ID 永远连续 |
| 旧 drain 使用 pending 快照和完成数提前退出 | 重新确定并发提交/轮询下的完成条件，避免未完成请求被报告为已 drain，也避免错误路径无限空转 |
| 延迟记录数组按 CPU/type 共用、部分锁被注释 | 双 Session 会增加容量和并发竞争；改成请求内时间戳或按 Session 隔离，暂时停用也应明确，不能忽略数组满/记录冲突 |

**错误回退语义要单独核对**：后端未存该页与“已存在远端的页面读取失败”不同。Load 返回失败可能触发磁盘读取，而远端独占页面未必有有效磁盘副本；不能把 session.error 一概转换为磁盘回退。WR 提交成功也不等于远端写成功。不实现副本或掉线自动恢复，但不能静默报告成功或读取旧数据。

## 与 Patch 1 生命周期的衔接

1. Patch 2/3 数据路径和完成处理就绪后，才将 managed 注册的 `max_pages=0` 改成**验证后的实际总页数**；同步更新连接测试提示和文档。内核比较的是 `sis->max`，注意 header 与页数边界，不能只删 swapon 错误分支。
2. `invalidate_area()` 当前为空。启用换页后必须保证该 area 的 I/O 已收敛，再让内核释放绑定的模块引用；网络资源仍由统一 exit 清理。正常轮询与销毁用 `ib_drain_qp()` 不同，后者会将 QP 置 ERR，不能据此认定连接还可直接复用。`invalidate_page()` 可能处于锁/原子上下文，不随意加入可睡眠分配或阻塞 drain。
3. Frontswap 控制 mutex 覆盖 init/area invalidation；回调中不能重入注册/注销。SRCU 保护的是同步后端调用，**不替代异步 RDMA Completion 的等待**；不能在同一 SRCU 读侧回调里等待该域的宽限期。
4. 退出顺序仍为：成功 swapoff（期间保持远端可读）→ 注销并等待同步调用 → stop/drain/destroy RDMA → 释放模块资源。保留 Patch 1 的停止标志、错误 WR 结算、`ib_free_cq()` 同步及设备引用释放顺序。
5. 当前 `ib_drain_qp()` 可能阻塞在驱动中；初始化的每 Session deadline 不是驱动清理硬超时，不能超时就释放仍被 DMA/回调使用的对象。

## 下一轮最小验证与约束

- 先单节点读写内容校验，再双节点等容量；随后验证 8+16 和 16+8 两种顺序。覆盖 `2*M` 尾段边界、8GiB Chunk 边界、末页及越界；不能只看进程是否退出或节点访问次数是否相等。
- 覆盖重复 Store、重复 Load、invalidate/entry 复用，以及两个 Session 交错请求、同步/异步完成和 lazy_poll。补最少的提交失败/WC 失败用例，确认不会伪成功、漏释放或永久锁页。
- 首次验证活动 swap 下 rmmod 被拒绝、swapoff 读回成功后能卸载；再确认 SSD swap 恢复。Patch 1 的 idle unload 测试不能替代这一项。
- 小 Workload 必须配合 cgroup 限额，确认确实发生远端写入和读回；BFS 正常退出不能替代页面内容校验。日志/计数以 Client 为主，一侧 RDMA 数据 I/O 不需要 Server 逐页处理。
- 继续 KISS：不加 PMem/Tier、动态放置、迁移、运行中扩缩容或复杂监控；不同时开启全部 Hermit 优化。涉及 sudo/Workload 前先给操作者步骤和预期结果；SSH/RDMA 基础连通性异常时停止并反馈。

参考：[操作测试步骤](../remoteswap/tests/patch1-smoke.md)、[配置扩展检查记录](../remoteswap/tests/configurable-client-build.md)、[位图修复独立审查](../remoteswap/tests/frontswap-validity-review.diff)。
