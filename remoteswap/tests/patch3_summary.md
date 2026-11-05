# Patch 3：完成、轮询与换页生命周期

## 实现范围

保留 Patch 2 已验证的 1/2 节点静态映射、实际容量发现和单 swap area 限制。
未修改 Server 或消息格式，也没有修改操作者现有的 `rswap_client.conf`。
本轮支持 4KiB 基础页，原型验收 workload 应禁用 THP。

### Client

- CPU-only poll/peek 遍历全部活动 Session。peek 汇总终止前剩余请求；返回 0
  表示请求已终止，不代表所有 WC 成功，读取成功仍由页面状态表示。
- DIRECT CQ 的处理由队列 `cq_lock` 串行化；SOFTIRQ CQ 不进行 direct polling。
  drain 不再根据初始 pending 快照提前退出；正常等待与销毁 QP 的 drain 分开。
- 统一发送与轮询的 CPU 索引，去掉固定 `% 48`。初始化要求 4KiB PAGE_SIZE，
  在线 CPU ID 从 0 连续编号，运行期间不支持改变在线集合。
- 提交失败回滚 pending、DMA 和请求引用；成功提交后由 Completion 处理 DMA。
  每个在途请求持有页面引用，防止页面在 DMA 收尾前被释放。
- 同步 Store 等待自身真实 WC 结果；失败保持页面锁，允许现有磁盘写回退。
  异步 Store 失败先重新标脏、清理远端有效位，再解锁，防止回收唯一的本地副本。
- Store 使用两个引用，分别对应 CQ 完成和 Frontswap 有效位发布。两个事件无论
  何种顺序发生，只有最后一个引用才能完成解锁和回收。
- 同步 Load callback 只处理数据／错误状态，由 Hermit 上层解锁；异步 Load callback
  完成 DMA 收尾后解锁。WC 错误不再设置 Uptodate，并记录 Session 错误以拒绝新提交。
- 共享延迟数组替换为请求内时间戳，原延迟 trace 增加 server ID；未增加监控框架。
- area invalidation 先停止接收该 area 的新请求，再收敛全部 Session 队列。
  提交入口用短临界区登记活跃提交，关闭入口后先等待这些提交退出；WR 占用和
  尚未完成发布／回收的 Store 分开计数，避免背压与生命周期互相阻塞。
  健康 swapoff 保留 QP；模块退出继续复用 Patch 1 的最终 drain/destroy 顺序。
- managed 注册容量改为实际池总页数，允许显式 swapon；管理脚本仍不操作 swap。

### 必要的内核修复

- `frontswap.h` / `frontswap.c`：增加可选的 managed Store prepare/commit 对；
  只有成功 prepare 后才发布有效位，随后 commit，整个调用受 SRCU 保护。
  旧的非 managed Store 接口仍保留。
- managed 注册入口改名为 `frontswap_register_managed_ops_v2`，拒绝新旧 Client／内核
  混用；未保留旧注册导出作为别名。DRAM backend 同步适配新符号，但仍保持原来的
  `max_pages=0` 调试阶段限制。
- managed Load 已有远端副本但读取失败时，返回专用错误；page I/O 显式完成错误，
  不尝试读取可能过期的磁盘副本。位图未命中仍允许正常磁盘读取。
- `swap_readpage()` 增加内部 `rdma_cpu` 输出：RDMA／软件错误完成由 Hermit polling
  管理，磁盘 I/O 返回 -1，由自己的完成路径管理页面锁。磁盘 I/O 不再运行在外层
  `get_cpu()` 的禁止抢占区间。全部内部调用点和无 SWAP stub 已同步修改。
- `swap_state.c`：推测读取失败时，先等待 I/O，再释放 entry／页面；避免磁盘回退后
  因 cpu=-1 再次提交同一页读取。
- `memory.c`：在 KSM、rmap 和 swap 记账之前完成必要读取并检查 Uptodate，失败
  返回 SIGBUS，避免晚检查导致错误退出时记账失衡。lazy polling 仍存在，但等待点
  收紧到上述操作之前，不保证原先全部延迟重叠收益。
- `ksm.c`：复制之前的 polling 不再提前解锁原页面。
- `swapfile.c`：swapoff 使用页面／安装 PTE 前检查 Uptodate，失败返回 EIO。
- 拒绝整 THP 的远端 Store，并失效全部 constituent 的旧远端副本后交给磁盘写；
  整 THP Load 只要有任一 constituent 在远端，就显式报错，不能混读磁盘旧数据。
- managed 模式禁止 writethrough 和 exclusive-get，避免绕过上述锁与失效契约。

## 验证记录

编译使用 GCC 9.5.0、现有内核配置和 OFED 头文件／符号。

| 检查 | 结果 |
| --- | --- |
| 内核 bzImage + modules 编译 | PASS |
| RDMA Client 编译 | PASS，无编译警告 |
| DRAM backend 兼容性编译 | PASS，无编译警告；最终产物已恢复 RDMA |
| Patch 3 实际函数逻辑检查 | PASS，2 项，含 200 轮并发发布／完成及 area 收尾 |
| Patch 2 映射回归 | PASS |
| 管理脚本回归 | PASS，7 项 |
| Client 逻辑检查 ASan / UBSan | PASS |
| shell 语法、git diff --check | PASS |

ASan 检查中关闭了 LeakSanitizer，因为当前 ptrace 环境不支持它；映射／解除映射、
请求释放次数与页面引用另外由断言检查。上述 sanitizer 结果仅适用于用户态 stub 检查。

完整内核重编译暴露了原有告警：`arch/x86/mm/fault.c` 的未使用变量、关闭性能统计时
`swap_stats.h` 空 stub 的参数类型不一致，以及 GNU-stack/RWX 链接告警。
对应代码不属于本轮修改范围；性能统计的该分支为空函数。本轮修改的源文件没有新增编译告警。

产物为 `linux-5.14-rc5/arch/x86/boot/bzImage` 和 `remoteswap/client/rswap-client.ko`。
日志在 `/tmp/hermit-patch3-kernel-build.log`、`/tmp/hermit-patch3-kernel-final-build.log`、
`/tmp/hermit-patch3-client-final-build.log` 和 `/tmp/hermit-patch3-dram-build.log`。
最终模块版本为 1.3，引用新 v2 注册符号。

本轮没有安装内核、加载模块、连接 RDMA、启停 swap 或运行 workload。

`test_patch3_completion.py` 在用户态提供 RDMA／页面 stub，编译并调用当前生产文件中
提取的实际 C 函数体，覆盖：

- 实际远端解析的尾段、描述符范围与溢出；
- Store 的完成先于发布、发布先于完成、同步 WC 失败，以及并发发布／完成；
- 分配、DMA map、post 失败后的计数、引用及 DMA 清理；
- 只有节点 1 有请求，以及两个节点交错完成时的 CPU-only poll/peek；
- 同步读保持页面锁、异步读由 SOFTIRQ 路径完成、失败读不标有效；
- 实际 Frontswap 发布／commit 顺序、未命中与远端已有副本失败的区别；
- 整 THP 写拒绝／全范围失效、远端仅有 THP 尾页时禁止磁盘回退；
- Hermit 对磁盘和 RDMA 页面锁的职责区分。

这些是无特权逻辑检查，不能替代真实 HCA、软中断调度、DMA 一致性及内核并发验证。
没有新增产品运行时故障注入或自检入口。

## 部署与剩余验证边界

- 必须同时部署新内核和 Client，不能只替换 `.ko`。内核 release 字符串未改变；
  需按新 v2 符号确认版本，不能仅比较 `uname -r`。
- 活动 swap 期间模块仍应由绑定引用保护；真实 swapoff 读回、再次 swapon、卸载保护、
  单／双节点逐页内容和不等容量尾段，均等待操作者硬件验收。
- 不实现运行期扩缩容、迁移、重连、数据重放或副本恢复。Session 错误不自动清除。
- 在途 DMA 必须等终止 Completion；不承诺设备／驱动失常时的硬超时回收。
- 现有 Frontswap 计数是接受请求的逻辑计数，不应当作 WC 成功计数。
