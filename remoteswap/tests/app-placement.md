# Client 1.16：应用固定首选介质与统一决策输出

`pureDRAM`／`purePMem` 只决定应用新 Store 的首选远端介质，不使用热度或 Warmup。
两台 Server 都有位置时，按应用独立的 RR 计数轮询；先尝试两台同类介质，再尝试另一类介质。
全池耗尽时沿用原有磁盘 Swap 回退。名称不表示容量不足时仍保证所有页面只使用一种介质。
Load 查询已有映射；策略注销、快照更新不迁移旧页面。未绑定页面仍使用原 Interleave／Adaptive。

## 实现与边界

- 新 `client/rswap_app.c/.h` 保存 memcg → 策略绑定，注册时从 PID 取得在线 memcg 引用，
  Store 持有页面锁时用 `page_memcg(page)` 精确匹配。RCU 保护查询，写操作使用 mutex；
  注销先摘除，再等待读者退出，最后释放引用。请求只复制决策，不保存绑定指针。
- 使用新的本地 `rswap_app_uapi.h` v1：每个 `app_policy` fd 最多成功注册一次，
  关闭即注销自己的绑定。重复绑定同一个 memcg、根组、cgroup-v2、无活动 swap area 均拒绝。
  swapoff 撤销全部绑定，旧 fd 不能注册到下一次 swapon；需重新启动工具。
- `tools/rswap_run.py` 支持命令启动，不提供运行中 PID 附加。先 fork 并阻塞子进程，
  父进程加入 cgroup、注册、校验 ACK 后才放行 exec；子进程提前关闭控制 fd。
  工具保持在受限组外，用 subreaper 回收孤儿后代，等待主进程和组内任务全部退出再注销。
  退出码沿用主命令；SIGINT／SIGTERM／SIGHUP 转发给任务，5 秒后仍未退出则 SIGKILL。
  工具被 SIGKILL 时 fd 自动关闭，存活任务的后续 Store 恢复原默认策略。
- 每应用使用外部预先准备的空 cgroup-v1 memory 组，不创建子 cgroup、不在运行中迁组，
  不递归继承对子 cgroup 的策略；线程及留在同组内的子进程自然覆盖。
  工具不创建／删除 cgroup，不改内存限制、swap、THP 或 Hermit 开关。
- 已有 cgroup Swap 记账问题未修复。工具要求 `batch_account=N`，并检查目标与可见祖先
  无有限 memsw 配额；不读取 Swap 用量判断归属、容量或回收。共享／继承页遵循实际记账归属，
  不承诺按所有使用者各自的策略放置同一个物理页。不以 cgroup 删除成功证明旧引用问题解决。
- 多个应用共享远端池，不预留容量、不按容量加权。沿用 4 KiB、1／2 Server、一个活动 swap area，
  测试禁用 THP。无需修改或重新安装内核，无 Server 改动；需重新加载 Client 1.16。

## 统计与只读检查

现有 `stats`、`monitor`、`mapping`、`pool-status`、`pool-monitor` 工具及其格式不变。
Server／Tier 的成功 RDMA Read／Write 和错误计数包含所有应用，没有应用级带宽。
原 `policy_stats` 及 `decision_raw` 只统计原 Interleave／Adaptive 路径，不包含固定策略；
不能把其决策总数当作本轮全部 Store 数。

Client 1.16 将固定策略接入原 `rswap:decision` 展示事件，仍为六字段、十六进制 PFN、
每项后带逗号。`placement_policy` 表示所配置的策略，`server`／`target_mem` 表示实际位置：

```text
req_type: store, pfn: 0x12345, placement_policy: pureDRAM, reason: fixed, server: 0, target_mem: dram,
req_type: store, pfn: 0x12345, placement_policy: pureDRAM, reason: capacity_fallback, server: 1, target_mem: pmem,
req_type: store, pfn: 0x12345, placement_policy: purePMem, reason: fixed, server: 1, target_mem: pmem,
req_type: store, pfn: 0x12345, placement_policy: purePMem, reason: capacity_fallback, server: 0, target_mem: dram,
```

同介质切换 Server 仍显示 `fixed`，只有跨介质回退显示 `capacity_fallback`。
与原行为一致，只在 Store 成功分配 Slot 后输出；Load、分配失败不输出该展示事件。
成功分配不表示 RDMA 提交或 WC 成功，原 Interleave／Warmup／Adaptive 输出内容保持不变。

`sudo cat /sys/kernel/debug/rswap/app_policies` 显示当前绑定编号、注册 PID 和策略。
PID 只是注册来源，退出／复用不改变 memcg 绑定；工具退出、swapoff 后相应绑定消失。

默认关闭的新事件 `rswap:app_decision` 记录固定策略的分配尝试：binding、policy（1=DRAM、
2=PMem）、RR、首选／实际 Server／Tier、alloc_seq 和 alloc_ret。只有 alloc_ret=0 时
实际位置有效；分配成功不等于 post／WC 成功。alloc_seq 可关联既有 store_result 和 mapping。
原冷热离线 `trace-check --adaptive` 不支持此新事件，不用于固定策略验收。

## BFS 核心步骤

在仓库根目录构建：

```bash
make -C remoteswap/client -j8
make -C remoteswap/tools
```

随后按现有流程启动 Server，连接新 Client 并显式 swapon；不要在有活动 Swap 时卸载旧 Client。
重连前仍须重启参与的 Server。以下假设 cgroup-v1 memory 已挂载且祖先没有有限 memsw 配额。

```bash
export BFS_CGROUP=/sys/fs/cgroup/memory/hermit-bfs-pure
sudo mkdir "$BFS_CGROUP"
echo -1 | sudo tee "$BFS_CGROUP/memory.memsw.limit_in_bytes"
echo 4294967296 | sudo tee "$BFS_CGROUP/memory.limit_in_bytes"
echo 100 | sudo tee "$BFS_CGROUP/memory.swappiness"
echo N | sudo tee /sys/kernel/debug/hermit/batch_account
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled

sudo env OMP_NUM_THREADS=40 python3 remoteswap/tools/rswap_run.py \
  --policy pureDRAM --memory-cgroup "$BFS_CGROUP" -- \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/apps/BFS -s -rounds 1 \
  /mnt/sdb2/yxr/K_R_benchmarks/ligra/inputs/generated/rMat_sym_32G.adj
```

改成 `--policy purePMem` 可运行另一种策略。工具取代原脚本中的 rswap_hotness 和加入 cgroup
的 Bash 包装器，不必手工传 PID，不需要 `--placement`、阈值、采样间隔或 Warmup。
若另外需要冷热观测，可以独立附加采样器，但不需要它启动 Adaptive 放置。
并发运行时每个应用使用不同的空 cgroup；不要同时改变同一个组的成员或绑定。

另一个终端观察：

```bash
sudo cat /sys/kernel/debug/rswap/app_policies
bash remoteswap/client/manage_rswap_client.sh monitor 1
bash remoteswap/client/manage_rswap_client.sh mapping
```

需要逐页 placement choice 时，加载新模块后创建独立 Trace instance；在 BFS 运行期间读取：

```bash
TRACE=/sys/kernel/debug/tracing/instances/rswap_pure_choice
sudo mkdir "$TRACE"
echo 1 | sudo tee "$TRACE/events/rswap/decision/enable"
echo 1 | sudo tee "$TRACE/tracing_on"
sudo cat "$TRACE/trace_pipe"
```

Ctrl-C 退出查看后关闭采集，释放 instance：

```bash
echo 0 | sudo tee "$TRACE/tracing_on"
echo 0 | sudo tee "$TRACE/events/rswap/decision/enable"
sudo rmdir "$TRACE"
```

运行中应看到注册策略、实际 RDMA 读写和零新增错误；首选介质满池后另一介质有流量是预期行为。
应用退出后绑定消失。旧远端位置按原 Frontswap 生命周期回收，必要时按原流程 swapoff 后确认
所有 Slot 空闲，再卸载 Client，最后停止 Server。不要用 cgroup Swap 计数验收。

## 本地验证

Client 1.15 构建通过，无新增编译告警；现有采样器构建检查通过。
完整 **98 项无特权回归通过**（原 88 项＋新增 10 项），`git diff --check` 通过。

Client 1.16 统一 decision 输出扩展已编译通过，无新增编译告警；完整 **100 项无特权回归通过**。
新增检查覆盖两种固定策略的精确六字段格式、跨 Tier 与同 Tier Server 回退、失败不展示、
Load 不展示，以及原 Interleave／Warmup／Adaptive 输出兼容；5.14 full-seq 格式校验继续通过。

编译 Client 与现有采样器；运行 `python3 -B -m unittest discover -s remoteswap/tests -p 'test_*.py'`。
新回归直接编译生产注册、RCU 查询、分配器和 RDMA 请求代码，覆盖独立 memcg／RR、
注销／swapoff、PID 复用、错误注册、同 Tier／跨 Tier 回退、在途 Slot 耗尽、Load 和失败收尾。
启动工具测试使用实际 fork／exec／信号／孤儿后代，cgroup 与模块操作用替身，不需要特权。

上述测试不替代 HCA／DMA、真实 memcg 记账、多应用多轮换入换出或 BFS 硬件验收；
本次没有加载模块、启停 Swap 或运行实际 BFS。
