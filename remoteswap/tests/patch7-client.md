# Patch 7：Server/Tier 统计与 Placement Trace

Client 模块 **1.7**，debugfs stats 格式 **2**；Server 控制协议仍为 **2**。
只修改 Client、用户态工具、测试和说明；不修改 Server、内核核心、两级静态映射，
不改变 Completion 的页面锁／引用计数／DMA 收尾、Frontswap 发布或 swapoff 语义。
本轮未加载模块、连接 Server、启停 swap 或运行真实换页 workload。

## 统计接口与口径

接口仍为 `/sys/kernel/debug/rswap/stats`，只读；新工具仅接受 v2，不兼容 v1。

```text
version=2 instance_ns=<模块实例> sample_ns=<单调时钟> page_size=4096
server tier op ok wc_error submit_error
```

后续每行是 `server tier op ok wc_error submit_error`，最后是 `unrouted_error=N`。
每个 Server 有 dram／pmem 两个 Tier，各有 write／read_sync／read_async 三行；
单节点共 6 行计数，双节点共 12 行计数。

- `ok`：成功数据 WC 次数；每次成功操作对应 4096 字节。
- `wc_error`：失败数据 WC 次数，不贡献成功带宽。
- `submit_error`：目标解析成功后的分配、DMA map、Session 拒绝、post 等提交失败，
  归属已选中的 Server/Tier，不重复计入 WC 错误。
- `unrouted_error`：目标未解析、area／type 拒绝等无法归属的提交错误。
- 控制消息、磁盘回退、swap cache 命中不属于成功 RDMA 数据 I/O。
- 计数按模块生命周期累计，swapoff/swapon 不重置，模块重载重置；快照逐项原子读取，
  不保证所有行同一时刻采样。计数只用于观测，不参与生命周期收尾。

沿用命令：

```bash
bash remoteswap/client/manage_rswap_client.sh stats
bash remoteswap/client/manage_rswap_client.sh monitor 1
```

monitor 按 Server/Tier 显示 MiB/s 和区间错误次数，另显示每 Server 的 `all` 汇总。
`all` 由两个 Tier 相加得到，内核不维护重复计数。带宽为
`Δok × page_size / Δsample_seconds / 1024²`，Read 总量为两类 Read 相加。
首个快照和模块重载只建立基线，不把累计值当带宽；每次读取立即关闭 debugfs 文件。

指标表示**成功 I/O 分布**；重复读写重复计数，不表示独立页面数、当前有效驻留页数、
已用容量、链路线速或应用吞吐量。未增加页面表／Allocator／invalidate 计数。
计数器数量固定，Trace 使用有界 ftrace 缓冲区，不按 3–3.5 TiB 总页数新增元数据。

## 可选 Placement Trace

新增模块内事件 **`rswap:placement`**，默认关闭，按标准 ftrace event enable 控制；
不新增 Hermit 开关或模块参数。每个终止数据 WC 在释放请求前记录一次：

```text
placement: swap_type=0 offset=123 server=1 tier=pmem op=read_sync status=0
```

offset 是原始 4-KiB swap 页偏移，不减 1；Server 编号沿用 Client endpoint 顺序。
status 为 0 或 -EIO，分别表示成功／失败 WC，不是原始 HCA WC code。
提交前失败没有 WC 事件，仍按提交错误统计。WC 成功不表示 Store 有效位发布／解锁已完成。
请求的 Tier／swap type／offset 在 post 前保存，避免快速 Load 完成后访问已释放请求。

使用独立 ftrace instance，仅采集新事件，避免混入原有延迟 `trace_printk`；
原延迟日志保持原样。以下是手动采集方式，涉及 sudo 的命令由操作者执行：

1. 加载 Client 1.7 后，创建全新的测试 instance 并准备缓冲区：

```bash
sudo mkdir /sys/kernel/debug/tracing/instances/rswap_p7
echo 0 | sudo tee /sys/kernel/debug/tracing/instances/rswap_p7/tracing_on
echo 1024 | sudo tee /sys/kernel/debug/tracing/instances/rswap_p7/buffer_size_kb
```

2. 另一个终端先启动持续读取，输出到普通日志文件，随后在 workload 有实际 I/O 时开启：

```bash
sudo cat /sys/kernel/debug/tracing/instances/rswap_p7/trace_pipe > /tmp/hermit-p7-placement.log
```

```bash
echo 1 | sudo tee /sys/kernel/debug/tracing/instances/rswap_p7/events/rswap/placement/enable
echo 1 | sudo tee /sys/kernel/debug/tracing/instances/rswap_p7/tracing_on
```

3. 只采集短窗口。停止事件后，让读取器排空缓存，再 Ctrl-C 结束 cat：

```bash
echo 0 | sudo tee /sys/kernel/debug/tracing/instances/rswap_p7/tracing_on
echo 0 | sudo tee /sys/kernel/debug/tracing/instances/rswap_p7/events/rswap/placement/enable
sudo sh -c 'cat /sys/kernel/debug/tracing/instances/rswap_p7/per_cpu/cpu*/stats' > /tmp/hermit-p7-trace-buffer.stats
```

本 instance 是新建的，检查保存的各 CPU 统计中 overrun／commit overrun／dropped events
均为 0，日志中没有 LOST EVENTS。读取器排空前的 entries 非零不等于丢失。
采集可能影响运行速度，本轮不做性能对比，不用逐页 Trace 计算全局带宽。
卸载 Client 前关闭该事件、停止读取器和 stats monitor；测试结束可移除空闲 instance。

## 离线校验与分布汇总

在仓库根目录，用**实际发现的容量**描述拓扑，每台写 `DRAM_GiB:PMem_GiB`：

```bash
bash remoteswap/client/manage_rswap_client.sh trace-check /tmp/hermit-p7-placement.log 16:32,16:32
```

工具逐行验证 Offset 对应的 Server/Tier，按 Server/Tier/op 汇总成功记录；
错误位置、非法记录、混合 swap type、失败 WC、丢失事件标记或无事件均不判 PASS。
匹配关系保留 Patch 6 的两层共同段交错和尾段规则；支持单 Server 和大容量拓扑。
工具流式读取日志，不保存逐页映射；重复访问累计为多次 I/O。

Trace 只验证采集窗口；不要求短窗口条数等于整个 workload 的累计计数，也不声称所有
Chunk／尾段均被访问。工具只能发现文本中的丢失标记，不能代替上述内核缓冲区统计检查。

## 核心验证计划

包含操作者追加的 64G BFS 测试、可直接复制的步骤见
[Patch 7 核心测试＋BFS](patch7-core-smoke.md)。

本次本地验证：RDMA Client 编译通过，无新增编译告警；整组 **31 项无特权回归通过**，
shell 语法和 `git diff --check` 通过。产物为 `client/rswap-client.ko`（1.7），
完整构建日志位于 `/tmp/hermit-patch7-client-build.log`。

本地检查：Client 编译，实际回调／失败路径的 Server/Tier 归属、快速完成与 Trace 字段，
formatter 与 Python parser 的 v2 一致性、成功带宽及汇总、重载基线、Trace 校验器。
Trace 校验器另与生产 C 映射在共同段／尾段及 3.5 TiB 布局边界对照。

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
```

硬件只跑一轮双节点：沿用每台 16＋32 GiB 池和已有 48 GiB swap；
4 GiB 工作集、2 GiB 测试物理限制、两轮 `swap_pattern`，保持 Hermit 开关不变。

1. Trace 关闭时确认四个 Server/Tier 的成功 Write／Read 增长，错误增量全为 0；
   monitor 显示各 Tier 成功带宽和正确 Server 汇总，不强求四个池计数严格相等。
2. 同一 workload 中短窗口采集 Trace，确认上述无丢失条件，运行 trace-check PASS。
3. 两轮逐页校验 PASS，操作者解除测试物理限制并执行活进程 swapoff，再校验内容，
   最后停止采集／monitor 并正常卸载。异步 Read 为 0 允许，不据此宣称覆盖异步硬件路径。

操作者后续追加一次 64G 图 BFS 正常运行验证，具体使用 40 线程、24 GiB 物理限制、
1 个计时轮次；不重跑全部单节点、故障或 TB 级压力测试。完整硬件结果等待操作者验收。
