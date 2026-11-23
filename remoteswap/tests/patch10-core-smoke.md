# Patch 10：待执行的核心实机验收

## 目标与执行边界

本轮只完成代码、构建和本地检查。下面是下一阶段计划，**尚未执行**。
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
