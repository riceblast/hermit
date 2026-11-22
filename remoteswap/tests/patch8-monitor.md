# Patch 8：窗口热度监控与 Store PFN 关联

## 范围与设计

本 Patch 实现独立用户态监控、可控冷热程序和最小 Client 观测点。
Client 版本 **1.8**；控制协议仍为 **v2**，stats 仍为 **v2**，沿用 managed Frontswap v2 内核。
Server、内核核心、静态 Interleave、RDMA Completion 与页面生命周期不变。
尚未实现动态 Slot、热度上传、Adaptive 放置或迁移；本 Patch 不要求 DRAM 带宽高于 PMem。

实现位置：

- `tools/rswap_hotness.c/.h`：PID/启动命令、pagemap/Idle Tracking、窗口与日志。
- `tools/rswap_hotness_check.py`：离线关联此前快照与实际 Store 提交。
- `client/rswap_trace.h`、`rswap_rdma_ops.c`：默认关闭的 `rswap:store_sample`。
- `tests/hotness_pattern.c`：两个线程共享三个匿名区域，前后交换冷热角色。
- `tests/test_patch8_*.py`：实际生产 C 与离线工具的无特权回归。

### 采样语义

每轮记录 `1=活跃`、`0=已观测未访问`、`?=无效/不可观测`。
最近 N 轮的热度为 `active / valid`，N 为 **1..64 轮**，默认 20。
更新周期默认 1 秒，允许 0.001..86400 秒；Warmup 默认 20 秒，允许 0..86400 秒。
有效历史用两个 64 位 Bitmap 保存，不增加独立 TTL 或 cooldown。

例：`[1, 0, ?, ?]` 的热度是 1/2；`[0, 0, ?, ?]` 是已知冷页；
`[?, ?, ?, ?]` 是未知。无效轮次也推进窗口，历史不会无限保留。
初次看到页面只建立 Idle 基线，不把首次观测计为冷。
窗口按实际完成的采样轮次推进；扫描或日志输出超过周期时不补造采样，
记录 `overrun=1` 和真实时间，所以 N 轮不承诺严格等于 N×周期秒。

按 VA 保存历史；同一 VMA 内 PFN 改变，跳过变化轮次并重新建立 Idle 基线，保留此前 VA 历史。
可检测的 VMA 描述符变化（含边界改变）或未映射/未触页 PTE 会清除历史。
换出页保留窗口内历史并参与均值，但 `last_pfn` 只表示历史，不能当作当前 PFN。
平均值对已知页面等权，包含热度 0、排除未知；当前 PFN 或 swap entry 的别名只取一个代表，
所有 VA 仍保留在逐页日志中。映射变化中暂时无法去重的历史属于近似误差。

扫描目标 PID 的可读 VMA，每个共享地址空间只扫描一次，不逐 TID 重复扫描，也不跟踪 fork 子进程。
使用 kpageflags 的 SWAPBACKED 筛选普通匿名及 shmem/tmpfs 页面，排除普通文件缓存、
THP/compound、hugetlb、zero page、unevictable、offline 等。
只有符合条件的 LRU 页才标 Idle；检测到映射变化则该轮无效。
PFN 按物理顺序分组：kpageflags 按 512 页块读取，Idle Bitmap 按 64 页字读取，
写入只包含目标 PFN 的置位 mask，不清除其他页的位，也不扫描整个宿主机物理内存池。

本机内核 `mm/page_idle.c` 仍可能在竞争中跳过页面；前后 PFN 检查不能排除 ABA、
PFN 复用、COW、共享页被其他进程访问等误差。采样也会操作 Accessed/Young 状态，
不能宣称完全无扰动。不锁定或 pin 目标页、不强制 fault-in 页面、不读取页面内容。
持久接口错误/PFN 隐藏明确报错并停止，不能伪装成长期未知。

### 运行与输出

```bash
make -C remoteswap/tools

# 输出目录必须不存在；启动命令时，在释放子进程执行前设置 CLOCK_MONOTONIC 起点。
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p8-example \
  --interval 1 --window 20 --warmup 20 --pages -- /path/to/program arg1

# 附加已有 PID 时，从附加时开始计时，不追溯程序真正启动时间。
sudo remoteswap/tools/rswap_hotness --out /tmp/hermit-p8-attach --pid 12345 \
  --interval 1 --window 20 --warmup 20
```

需要 4 KiB 系统页、目标进程 proc 访问权、CAP_SYS_ADMIN/PFN 可见性，
以及 `/proc/kpageflags` 读取与 `/sys/kernel/mm/page_idle/bitmap` 读写权限。
建议在宿主机运行，不在受限的容器里采样。

- `summary.tsv`：每轮提交一行，含 PID、代次、单调时间、warmup/observe、页数、
  本轮 active/idle/invalid、窗口内 known、均值、扫描耗时和 overrun。
  `pages`/本轮样本按 VA 计数；`known`/均值按去重后的代表计数。
- `--pages`：另写 `pages-NNNNNN.tsv`，包含 VA、当前/历史 PFN、状态、窗口计数、
  heat_ppm（未知=-1）、alias、swap type/offset。关闭该选项时仅输出摘要，不能做逐页离线关联。
- `run.tsv`：结束时保存 PID 身份、起止时间、配置、监控错误与退出状态。
- 控制台：每轮摘要，与目标程序原有输出共用 stdout；结构化文件不混入程序输出。

`ready_ns` 在逐页文件完全写入并关闭后记录。只有这时之前已完成的快照才能用于 Store 关联。
Warmup 结束后的状态叫 `observe`，仅表示继续观察，**不是已启用 Adaptive**。
采样工具处于受限 workload cgroup 外；CLI 启动的程序继承其凭据，程序自行产生的线程共享监控范围。

目标退出或 PID 身份改变时结束；正常启动模式返回目标退出状态。
Ctrl-C/SIGTERM 停止采样，启动模式同时转发信号给直接启动的子进程，附加模式不向目标发信号。
不管理进程树、不保证忽略信号的子进程有硬超时。每轮重新打开 proc 文件，支持启动包装中的 exec；
若 exec/地址空间变化竞争导致读取 EIO，清除 Idle 基线并重试一次，持续失败则明确退出。
采样失败时启动模式终止自己启动的直接子进程，以免程序继续在无人观察的状态下运行。

大量逐页 TSV 会消耗内存、CPU、磁盘空间与时间；只在需要关联的实验窗口启用。
内存主要是前后两份紧凑页面数组和临时 PFN 索引，随观察到的页数增长，
不按远端池容量分配用户态热度。没有固定性能门槛，但需记录扫描超期情况。

### 最小 Store 事件与分析口径

```text
store_sample: start_ns=123 pfn=456 swap_type=0 offset=789 submit_ret=0
```

`start_ns` 在 Store 提交前由 `ktime_get_ns()` 取得，与用户态 CLOCK_MONOTONIC 对齐；
不依赖 ftrace 当前选择的时钟。事件在提交返回后发出，禁用时不额外读取时钟/PFN。
`submit_ret=0` 只表示提交被接受；同步 Store 随后的 WC 等待仍可能失败。
事件不等于 Frontswap 有效位已经发布，也不等于最终写成功。
WC 成败仍使用既有 stats/placement，不在本 Patch 改其格式或含义。

分析器按 Store 入口时间排序，只使用 `ready_ns <= start_ns` 的最近快照，
不使用后来的采样补认先前热度；只在监控起止范围内关联。
报告提交失败、无先前快照、PFN 不在表中、未知热度、匹配热/冷以及快照年龄。
已换出页的 `last_pfn` 不会加入当前 PFN 查找表。

匹配比例分母是**采集窗口中监控运行期间的全部 Client 已接受 Store**，不是精确 BFS Store 数。
当前 PID 可能是 kswapd，不能作为页面 owner。PFN 匹配也不能证明物理页身份完全正确。
逐页日志中的 VA→swap type/offset 转换可辅助人工核对归属；无法归属的样本不冒充精确命中率。
没有额外的命中率百分比门槛，但硬件关联验收至少要有已知热度匹配样本。

```bash
sudo python3 remoteswap/tools/rswap_hotness_check.py \
  /tmp/hermit-p8-example/summary.tsv /tmp/hermit-p8-store.log \
  --buffer-stats /tmp/hermit-p8-buffer.stats --require-match \
  --detail /tmp/hermit-p8-matches.tsv
```

输出 JSON 摘要和口径说明。不会使用未来快照匹配；无事件、格式错误、不完整快照、异常监控退出、
丢失事件标记、非零 buffer overrun/drop 或 `--require-match` 下零匹配都返回失败。
可正常分析附加模式下由 Ctrl-C/SIGTERM 结束且监控无错误的完整快照。
不提供 buffer stats 时会明确说明没有认证缓冲区无丢失。

## 测试与验收

可直接复制的准备、冷热识别、双节点 Store 关联、BFS 摘要监控和正常收尾步骤，
以及实现阶段的本地检查记录，统一见 [Patch 8 核心测试](patch8-core-smoke.md)。
本文件只保留设计、接口和统计口径；硬件验收结果以实际执行日志为准。
