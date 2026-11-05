# Patch 2：统一静态地址解析与请求路由

完成日期：2026-11-05。

## 核心修改

- 新增 `client/rswap_mapping.h`，保存实际节点数、各节点容量页数和总页数，
  提供唯一的 `rswap_map_page()`。单节点直接映射；双节点的共同容量按页交错，
  较大节点的剩余容量接逻辑池尾段。节点编号取配置顺序，原始 swap offset 不减 1。
- 拓扑在所有选中 Session 连接成功、总容量匹配后发布；退出时清空。
  不保留 init 栈上 config 指针，不根据运行期间修改的配置重新解释页面位置。
- `client/rswap_rdma_ops.c` 新增 `rswap_resolve_remote_page()`，统一解析 Session、
  Chunk 和 Chunk 内偏移，检查 slot、Chunk 下标、MAPPED 状态、整页范围与地址溢出。
- Store、store_on_core、同步 Load、异步 Load 共用发送路径。队列、DMA device、
  PD/lkey、远端地址与 rkey 来自同一 Session。
- 保留 `rswap_rdma_send()` 原接口；内部发送函数可返回实际提交队列，普通 Store
  据此等待目标队列，不再固定等待节点 0。这里只修正等待目标，未完成 WC 状态传播。
- 同时修正发送路径中请求分配失败返回成功、普通 Store 失败漏 `put_cpu()` 的问题。
- 更新模块描述、启动提示与 README，明确当前阶段仍禁止 swapon。

没有修改内核、Server、消息格式、配置或管理脚本，没有新增运行时测试入口。
没有引入动态位置表或 Slot allocator。

## 编译与检查结果

| 检查 | 结果 |
| --- | --- |
| RDMA Client 编译 | PASS，无编译警告 |
| 实际映射函数的无特权检查 | PASS |
| `git diff --check` | PASS |

编译使用 GCC 9.5.0、现有 `5.14.0-rc5-hermit-syscall-v2` 内核构建目录及 OFED
头文件／符号。产物为 `client/rswap-client.ko`，构建日志保存在
`/tmp/hermit-patch2-client-build.log`。

`test_patch2_mapping.py` 编译并调用生产代码中的映射函数，不另写替代实现：

- 穷举单节点容量 1..64 页，以及双节点各 1..64 页的所有偏移，检查完整覆盖、
  无重复、无越界。
- 检查单节点 8/64GiB、双节点 8+8、8+16、16+8GiB 的预期映射，覆盖原始
  offset 0/1、共同段末尾、尾段起点、所有节点内 8GiB Chunk 边界及末页。
- 补充现有协议上限每端 3200GiB 的映射边界，以及池未发布、节点数非法和偏移越界。

这些检查验证页偏移到 Server/Slot 的计算；不代表 RDMA 数据读写、运行时 Chunk
校验错误路径或页面生命周期已通过硬件验证。本轮未加载模块、启停 swap 或运行 workload。

## Patch 3 交接

`frontswap_register_managed_ops()` 的 `max_pages` 仍为 0，继续主动拒绝 swapon。
不得仅删除该限制来启用换页。

尚需完成：CPU-only poll/peek 覆盖全部 Session、修复 `drain_all_rdma_queues()`
目标节点处理、区分 DIRECT/SOFTIRQ CQ、并发 polling 与背压、post 失败清理、
WC 错误与真实完成结果、DMA 收尾／页面解锁顺序，以及 area invalidation 收敛。
重复 Store、entry 复用、Frontswap 有效位发布和读取错误语义仍需按 Patch 1 交接审计。

本阶段没有消除上述已知缺口，尤其不能把普通 Store 当前返回 0 理解为已经确认远端写成功。
映射函数支持 1/2 节点；CPU 队列布局仍沿用现有实现，保持在线 CPU 集合固定。
