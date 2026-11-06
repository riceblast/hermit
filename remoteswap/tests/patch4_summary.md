# Patch 4：每 Server 计数与成功 RDMA 数据带宽

## 范围与口径

只增加 RDMA Client 的累计统计和用户态差分查看工具。Server、消息协议、静态地址映射、
CQ/polling、页面锁、Frontswap 生命周期及内核核心代码均沿用 Patch 3。
未修改 cgroup swap 记账，不读取其统计作为本功能的数据源。

每个 Server 的 Write、同步 Read、异步 Read 各有三个 64-bit 原子计数：

| 字段 | 定义 |
| --- | --- |
| `ok` | 数据请求收到成功 WC 的次数 |
| `wc_error` | 数据请求收到失败 WC 的次数 |
| `submit_error` | 目标解析成功后，请求分配、DMA map、Session 拒绝或 post 等提交阶段失败次数 |

未成功解析目标的失败，以及 area 关闭、swap type 不匹配等提交前拒绝，计入全局
`unrouted_error`，不猜测归属节点。失败总次数可以按 `wc_error + submit_error` 计算，
全局未归属失败单独显示。同步 Store 已提交后等待 WC 失败，只计 `wc_error`，不重复计提交失败。
控制消息、Frontswap bitmap 未命中的磁盘读取、swap cache 命中均不计入 RDMA 数据请求。
提交失败也不能解读为 SSD 写入已成功；本轮不增加磁盘完成统计。

计数从模块加载后、注册 Frontswap 前初始化；跨 swapoff/swapon 保留，模块重载重新开始。
没有 reset 接口，也不使用这些统计代替 WR 和 Store 生命周期计数。WC 计数表示收到了
终止 Completion，不保证 Store 发布／页面解锁已结束。

## 输出与查看

只读接口：`/sys/kernel/debug/rswap/stats`（RDMA backend）。例如：

```text
version=1 instance_ns=1000000000 sample_ns=2000000000 page_size=4096
server op ok wc_error submit_error
0 write 1000 0 0
0 read_sync 800 0 0
0 read_async 100 0 0
1 write 1001 0 0
1 read_sync 801 0 0
1 read_async 101 0 0
unrouted_error=0
```

`instance_ns` 标识本次模块实例，`sample_ns` 为内核单调时钟。快照中每个计数都是原子读取，
整组计数不做全局锁定；运行中采样存在很小的读取时差，不用于严格的在途请求对账。
debugfs 文件持有模块 owner；仅在模块进入 LIVE 状态后允许打开，退出先移除接口再释放 Session。

在 Client 仓库目录：

```bash
# 一次累计快照；不依赖配置文件，不改变 swap。
bash remoteswap/client/manage_rswap_client.sh stats

# 持续显示每 Server 带宽与区间错误次数，默认 1 秒。
bash remoteswap/client/manage_rswap_client.sh monitor

# 也可以指定正的秒数，Ctrl-C 退出。
bash remoteswap/client/manage_rswap_client.sh monitor 0.5
```

脚本沿用现有 sudo 包装。直接查看也可执行 `sudo cat /sys/kernel/debug/rswap/stats`。
debugfs 需要可访问；创建统计接口失败会使新模块初始化失败并回滚，避免加载成功却没有接口。
DRAM 调试 backend 不提供这个统计文件。

带宽输出列为：

```text
server write_MiB/s sync_read_MiB/s async_read_MiB/s read_total_MiB/s total_MiB/s wc_error_delta submit_error_delta
```

计算规则：`Δok × page_size / Δsample_seconds / 1024²`。
`read_total` 是两类 Read 之和，`total` 是 Read＋Write；不是单向链路利用率。
每区间另显示 `unrouted_error_delta`。成功带宽不包含失败请求、网络协议头或重传字节，
也不等于应用吞吐量。首次读取只建立基线，模块重载时重新建立基线，不输出伪造带宽。
模块卸载／接口不可用时清楚报错并退出；工具每次读取后关闭文件，不在采样睡眠时持有它。
卸载前建议先停止 monitor，避免与短暂的文件读取引用竞争。

## 编译与逻辑验证

RDMA 模块构建使用现有 `linux-5.14-rc5` 配置及 OFED；产物版本为 1.4，
仍使用 Patch 3 的 `frontswap_register_managed_ops_v2`。无需修改内核 release 或重新安装内核。

本地验证命令：

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
python3 remoteswap/tests/test_patch4_stats.py
python3 remoteswap/tests/test_patch3_completion.py
python3 remoteswap/tests/test_patch2_mapping.py
python3 remoteswap/tests/test_manage_config.py
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
```

Patch 3 实际 C 函数检查增加计数断言：快速完成、并发发布与完成、两个 Server 独立归属、
三类操作的提交失败／WC 失败、同步失败不重复计数、area 再次初始化不清零。
Patch 4 检查编译实际内核 formatter 并交给真实 Python parser，验证格式兼容性、
成功字节计算、错误差值、空闲区间、重载后计数更高也不能混算，以及无效输入处理。
管理脚本检查在临时 fixture 中验证 stats/monitor 不读配置、不改 swap、正确传递路径与间隔。

本轮结果：

| 检查 | 结果 |
| --- | --- |
| RDMA Client 构建 | PASS，无编译警告；最终产物为 RDMA backend、版本 1.4 |
| DRAM backend 兼容构建 | PASS，无编译警告；随后恢复 RDMA 产物 |
| 全部无特权回归 | PASS，22 项（管理脚本 10、映射 1、完成／锁及计数 3、统计工具 8） |
| shell 语法、git diff --check | PASS |

构建日志为 `/tmp/hermit-patch4-client-build.log`、
`/tmp/hermit-patch4-client-final-build.log` 和 `/tmp/hermit-patch4-dram-build.log`。

这些均为无特权逻辑验证，不能替代真实 HCA 的统计核对。新模块没有在本轮加载；
真实 RDMA 计数和运行中的带宽输出仍待操作者验收。
