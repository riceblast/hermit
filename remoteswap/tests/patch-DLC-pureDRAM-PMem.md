# Patch DLC：pureDRAM／purePMem 应用放置策略

## 目标与结果

为独立应用选择固定首选远端介质，支持多个应用并发，并统一输出逐页 placement choice。
Client 1.15 引入应用策略，1.16 接入统一 `rswap:decision`；最终版本为 **Client 1.16**。
内核源码、Server 和 RDMA 协议未修改，现有带宽统计工具继续使用。

## 主要实现

- **应用识别**：每个应用使用独立 memory cgroup。注册时由 PID 获取 memcg 并持有引用；
  Store 持有页面锁时，通过 `page_memcg(page)` 匹配策略，覆盖线程及留在同组内的子进程。
- **放置规则**：仅提供 `pureDRAM`、`purePMem`，不依赖冷热采样或 Warmup。每应用独立轮询
  Server，先尝试所有 Server 的首选介质，再回退另一介质；全池耗尽沿用磁盘 Swap 回退。
  因此 pure 表示固定首选，不保证最终全部页面只使用一种介质。
- **启动工具**：新增 `tools/rswap_run.py`，先阻塞子进程，再加入 cgroup、注册并确认策略，
  最后放行 exec。工具在组外等待主进程及组内后代退出，转发终止信号并回收子进程。
- **生命周期**：新增 `client/rswap_app.c/.h` 和本地 ABI `rswap_app_uapi.h` v1。
  每个控制 fd 管理一个绑定；关闭时通过 RCU 安全注销，swapoff 撤销全部绑定。
  工具被 SIGKILL 后，存活任务的后续 Store 恢复原默认策略。
- **已有数据**：Load 继续查询实际映射；注销和策略变化不主动迁移旧页面。
  未绑定页面继续执行现有 Interleave／Adaptive，原 Slot 和 RDMA 收尾机制保留。

## Placement choice 输出

统一启用 `events/rswap/decision/enable`，正文仍为六字段，每项后带逗号：

```text
req_type: store, pfn: 0x12345, placement_policy: pureDRAM, reason: fixed, server: 0, target_mem: dram,
req_type: store, pfn: 0x12345, placement_policy: purePMem, reason: capacity_fallback, server: 1, target_mem: dram,
```

`placement_policy` 为配置策略，`server`／`target_mem` 为实际分配位置。
同介质切换 Server 仍显示 `fixed`，跨介质回退显示 `capacity_fallback`。
只记录成功分配 Slot 的 Store，不记录 Load 或分配失败；分配成功不等于 RDMA 提交或 WC 成功。
原 Interleave／Warmup／Adaptive 正文不变；详细固定策略事件保留为 `rswap:app_decision`。

## 验证与保留边界

- Client 1.16 编译通过，无新增编译告警；完整 **100 项无特权回归通过**，diff 检查通过。
  覆盖 memcg 匹配、独立 RR、容量回退、注销并发、Store／Load／失败收尾、启动同步及输出兼容。
- 本次没有加载模块、启停 Swap 或执行 BFS；真实 memcg、多应用多轮换入换出和 RDMA 硬件验收
  由操作者继续执行。本地 stub 回归不能替代这些检查。
- 沿用 4 KiB、1／2 Server、一个活动 swap area，测试禁用 THP。仅支持命令启动，暂不支持
  运行中附加、嵌套 cgroup 策略继承或容量预留；共享／继承页遵循实际记账归属。
- **既有 cgroup Swap 记账 bug 未修复**：保持 `batch_account=N`，目标及生效祖先无有限 memsw
  配额；不使用 cgroup Swap 计数验收。工具不自动创建／删除 cgroup 或修改系统配置。
- 原 RDMA Read／Write 带宽及错误统计包含固定策略；原 `policy_stats` 和 `decision_raw`
  仍只描述 Interleave／Adaptive，不提供每应用独立带宽。

## 核心用法

按原流程加载新 Client 并启用 swap，预先准备空的 cgroup-v1 memory 组后运行：

```bash
sudo env OMP_NUM_THREADS=40 python3 remoteswap/tools/rswap_run.py \
  --policy pureDRAM --memory-cgroup "$BFS_CGROUP" -- \
  /path/to/BFS -s -rounds 1 /path/to/graph
```

改成 `purePMem` 即切换首选介质，无需手动传 PID。
`app_policies` 查看当前绑定，原 `monitor` 查看带宽，`rswap:decision` 查看放置选择。
完整 BFS 配置、采集和收尾命令见 [应用放置说明](app-placement.md)。
