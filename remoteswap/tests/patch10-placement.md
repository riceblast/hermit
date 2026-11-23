# Patch 10：热度快照上传与 Adaptive 放置

## 目标与边界

连接 Patch 8 用户态观测和 Patch 9 动态位置管理，仅对新的 Store 决策。
Client **1.10**；Server 协议 **v2**、stats **v2**、managed Frontswap **v2** 不变。
不修改 Server、内核核心、RDMA Completion/poll/drain、映射所有权或 Slot 回收契约。
当前 Client 已部署 Patch 9 修复内核 #8，本 Patch 不需要再次安装内核。

已完成实现和本地验证，随后按操作者要求开展实机核心验收。
实机计划与执行记录见 [核心验收](patch10-core-smoke.md)。

## 采用的语义

- 默认 Inactive；只有采样器显式 `--placement` 才 Start，且要求已有活动 swap area。
- 启动程序模式在释放 exec gate 前完成内核 Start，并以 ACK 的 CLOCK_MONOTONIC 起点计时。
  附加 PID 从附加时 Start，不追溯程序真正启动时刻。仍仅观察一个地址空间及其线程。
- Warmup 内 Interleave；自动模式到期后无需等扫描完成即可进入 Adaptive。
  手动模式到期后仍为 Warmup，直到 `adaptive`；提前请求返回 EAGAIN。
- Adaptive 中 `h > mean` 首选 DRAM，`h <= mean` 首选 PMem；每 Tier 独立 RR。
  RR 按已知热度的选择尝试推进，失败不回滚；每次 Start 清零。
- 无快照、未知均值或 PFN 未命中时 Interleave。状态可为 Adaptive 而全部 Store 仍走未知回退。
- 热度、均值整代更新，每代有效期间阈值固定。不新增 cooldown、TTL 或心跳。
  扫描卡住但句柄仍在时，旧快照继续有效；status 显示年龄，不保证每秒发布。
- Stop、上传句柄最终关闭、swapoff 清除本轮策略。已有位置不动，Load 不查热度。
  Stop/swapoff 后原采样器下一次上传收到 ESTALE，关闭句柄并继续纯观察，不自动重新 Start。
  真正的采样/上传错误延续 Patch 8 错误退出语义；启动模式终止直接子进程。
- 新 swapon 不继承旧策略，需要重新 Start；不支持运行中退回 Warmup，使用 Stop/Start。
- 首选池容量不足沿用 Patch 9 四候选 Fallback，不是失败 Session 故障转移。
  全池不可用时 Store 失败，沿用磁盘写回退，不等待 Slot。
- 不主动迁移；一次 invalidate 结束映射，后续 Store 可以重新放置。
  DRAM 带宽高于 PMem 是期望，不是硬性验收条件。

## 快照内容、精度与开销

继续使用 Patch 8 的 active/valid 窗口。均值来自全部已知且非 alias 的代表页，
包含历史尚有效的换出页；上传表只含当前 resident、PFN 非零、非 alias、已知热度的页面。
不上传 VA、last_pfn 或以 swap offset 猜测的热度；不按 current PID 筛选回收线程。
PFN 关联仍是近似，复用/竞争只能改变放置质量，不能改变实际 Load 地址。

页热度为 `floor(active * 1000000 / valid)`；内核均值为
`floor(原有未舍入比例均值 * 1000000)`。两者都以 ppm 判定，相等归冷。
未知均值为 -1，已知 0 有效。原 summary 浮点均值保留，因此边界处与旧浮点离线分类
可能存在小于 1 ppm 的精度差；本 Patch 决策以记录的 ppm 为准。

`rswap_placement.c` 保存 PFN 排序数组，Store 二分查询；不在换出路径扫描或分配热度内存。
写者由 mutex 串行，构建 staging 后用 RCU 发布。读侧只复制决策，退出 RCU 后才分配/提交 RDMA。
写者在进程上下文等待 grace period，再释放旧数组，避免堆积多代退休快照。
空表也须提交，完整取代旧 PFN；不允许因没有已知页而无限保留旧表。

每条记录 16 bytes；内核每代另有固定头。1000 万条单代约 153 MiB，更新时两代约 305 MiB。
另有用户态两份采样数组、排序/上传数组等。记录数按 Client 物理页数和 size_t 上限检查，
不按远端容量或 96 GiB 写死，也不静默截断；分配失败明确报错。
`upload_ms` 包含用户态构表、排序、传输及等待旧读者，不包含采样扫描。

## 本地接口

原纯观察模式完全保留。以下命令必须在操作者完成 Client/Server/swap 准备后使用：

```bash
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p10-example \
  --placement --interval 1 --window 4 --warmup 4 -- /path/to/program args

# 手动模式：Warmup 到期后再执行管理脚本 adaptive
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p10-manual \
  --placement --manual --interval 1 --window 4 --warmup 4 --pid PID

bash remoteswap/client/manage_rswap_client.sh placement
bash remoteswap/client/manage_rswap_client.sh adaptive
bash remoteswap/client/manage_rswap_client.sh placement-stop
```

debugfs `/sys/kernel/debug/rswap/` 新增：

| 文件 | 内容 |
| --- | --- |
| `placement` | 状态、area、run_id、计时、自动/手动模式、generation、publish_ns、age_ns、上传记录数、均值贡献页数、mean_ppm、当前/staging 内存 |
| `hotness` | 独占上传运行的二进制命令/ACK；文件可多开，但同时只允许一个运行 owner |
| `placement_control` | `adaptive` / `stop`；关闭此临时控制文件不影响 owner 生命周期 |

上传者使用 O_CLOEXEC，fork 子进程在 exec gate 前另行关闭继承副本。
最终关闭 owner fd 即 Stop；旧 owner 的 release 不会停止新运行。
持久句柄会持模块引用，卸载前须停止采样器；显式 Stop 后仍可能等当前扫描结束才关闭旧句柄。

本机二进制 ABI 位于 `rswap_placement_uapi.h`，版本 1，native endian：
40-byte header、16-byte record、32-byte ACK。每个 write 是一个完整命令：
START → BEGIN(generation/count/known/mean) → DATA(每批最多 4096 条) → COMMIT。
记录须严格按 PFN 递增。完整性、范围或内存错误丢弃 staging，不改变已发布代。
COMMIT 后只读 ACK 可获取 run_id/start_ns/generation/publish_ns。
每个 fd 只允许一次 Start，下一轮须新开 fd；Stop 后旧命令 ESTALE。
BEGIN/COMMIT 的代次严格大于已发布代，不允许跨运行复用。

原 `summary.tsv`、`pages-NNNNNN.tsv`、`run.tsv` 保留 Patch 8 格式；summary 的 observe
仍只是观察标签，不是内核状态。启用 placement 额外生成 `placement.tsv`，记录采样代、
内核运行标识、内核代、发布时间、记录数、known、mean_ppm 和 upload_ms。
内核 generation = sample_generation + 1；0 表示还没有快照。
上传不依赖 `--pages`，BFS 默认只写摘要和 publication 日志。

## 最小决策 Trace 与校验

新增默认关闭的 `rswap:decision`，在 Store 尝试分配之后、MR 解析和 post 之前记录。
包含 PFN/offset、run/generation、decision_ns/publish_ns、状态/原因、heat/mean ppm、
首选与实际 Server/Tier/Slot、Fallback、RR ticket、alloc_ret 和 alloc_seq。
它表示分配决策，不表示提交接受、Frontswap bit 发布或 WC 成功；后续失败仍由 mapping 回滚。

原因编码：0 Inactive，1 Warmup，2 无快照，3 均值未知，4 PFN 未知，5 热，6 冷。
实机复核后 mapping/placement 文本改为数字字段，避开本机 5.14 字符串校验在
seq 缓冲区已满时不推进 va_list 的告警路径：action=0/1/2/3/4 分别为
alloc/get/put/invalidate/free，tier=0/1 为 DRAM/PMem，op=0/1/2 为 Write/同步 Read/异步 Read。
解析器同时接受历史符号格式；二进制事件字段及 stats 格式不变，不需要修改内核。
Fallback 编码沿用 Patch 9 的 0/1/2/3 四候选次序。失败分配 alloc_seq=0；
成功 alloc_seq 对应 `rswap:mapping` 的 alloc.seq，避免仅靠 offset 错配多次生命周期。
Trace 在分配锁外发出，可晚于其他 CPU 事件；校验器通过序号关联，而非依赖 decision 行顺序。

```bash
bash remoteswap/client/manage_rswap_client.sh trace-check /tmp/p10-trace.log \
  16:32,16:32 --dynamic --adaptive --require-empty
```

必须同时采集 mapping 和 decision，从空池第一次 Store 前至最终回收，使用 global trace clock
和持续 trace_pipe 读取，并另查所有 CPU buffer loss 为 0。旧 `--dynamic` 专用于 Interleave，
不能验收合法 Adaptive 分配。新模式校验已记录的阈值/首选、RR ticket、Fallback 容量和映射引用；
不独立认证输入 PFN 身份或用户态采样准确率，不等于 WC/内容验证。
当前校验为两遍文件读取，并保存短窗口决策索引；不能用于 BFS 全量长期追踪。
完整冷热 publication 关联、分布/带宽效果分析留给 Patch 11。

## 构建和本地检查

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
make -C remoteswap/tools
cc -std=gnu11 -O2 -Wall -Wextra -Werror -pthread \
  remoteswap/tests/adaptive_pattern.c -o /tmp/hermit-p10-adaptive-pattern
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
modinfo -F version remoteswap/client/rswap-client.ko
```

新增生产 C 检查覆盖：自动/手动状态、无快照/无均值/未知/相等、单/双节点 RR、
完整/空/失败上传、非法记录/超量/内存失败、owner 竞争、旧句柄释放、Stop/swapoff、
四读线程与 200 次快照替换，以及真实用户态上传器连接真实内核命令函数的跨批传输。
实际策略另接实际 RDMA Store/Load，验证跨策略更新的原位置读取、重复 Store 和旧读晚完成。
这些是用户态 kernel/RDMA stub 回归，不代替内核/HCA/DMA 验收。

### 2026-11-23 本地执行结果

- RDMA Client 编译通过，无新增编译告警；modinfo 确认产物版本 **1.10**。
- 用户态工具及新可控冷热程序以 `-Wall -Wextra -Werror` 编译通过。
- 整组 **65 项**无特权回归通过（原有 59 项＋新增 6 项；其中上传/状态测试含四组专项场景）。
- `bash -n` 和 `git diff --check` 通过。
- 没有加载模块、连接/启动 Server、启用 swap 或执行 MADV_PAGEOUT 硬件测试。
  没有新一轮 ASan/UBSan 或内核/HCA 并发认证；不得把 stub RCU 锁模拟解释为实机 RCU 验收。

日志位于 `/tmp/hermit-p10-client-build.log`、`/tmp/hermit-p10-tools-build.log`、
`/tmp/hermit-p10-regression.log`；新程序产物 `/tmp/hermit-p10-adaptive-pattern`。
早期编译/测试适配错误已修正，最终上述构建和整组回归均成功。

### 实机发现的 Trace 输出问题及修复后本地检查

首次实机页面内容、16384 次决策和 98304 条映射事件检查通过，但 trace_pipe 读取器
触发 `trace_check_vprintf` WARNING；该次运行未作为完整通过，保留现场后暂停进入 BFS。
本机实际 `trace_seq_vprintf()` 在 seq 已满时不消费 va_list，字符串校验器继续读取
`%s` 参数时可能读到前面的数值参数。新增回归直接编译本内核的实际校验函数，
在 full seq 条件下复现旧格式告警，并验证数字格式不经过该字符串校验分支。

修复只修改 mapping/placement 的文本格式和 Python 兼容解析，不改放置、映射或 RDMA。
重新编译 Client 1.10 成功，整组 **67 项**回归通过。源代码和二进制格式均不需要新内核。
旧运行安全 swapoff/卸载、恢复设置后，重启两台 Server，以新 SHA256 产物重新开始核心验收。
修复后的 module SHA256：`8ada6b878c32959f7654b8d753d6d960de8254c6772ba54d029b2f2489899f79`。
旧 WARNING 为 WARN_ONCE，因此不能单凭后续没有新增告警证明问题消失；
本轮还以实际函数复现、移除 `%s` 的格式检查和实际数字格式 Trace 完整校验作为证据。

### 最终交接

修复后的双节点可控冷热/内容/生命周期和 BFS 集成均通过，操作者完成 root 控制器启动，
其余同步推进、Server 生命周期和日志核对由助手完成。
完整数字和覆盖限制见 [核心测试执行记录](patch10-core-smoke.md#2026-11-23-实机核心验收结果)。
当前无活动 swap、Client/Server 均停止，测试配置已恢复。

Patch 11 接手完整监控分析、BFS 实际冷热/未知覆盖与效果比较；
本轮不要求 DRAM 带宽高于 PMem，不宣称 BFS 性能提升或精确页面身份。
