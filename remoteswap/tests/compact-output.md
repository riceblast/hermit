# Client 1.14：精简热度与 Store 决策输出

默认输出面向查看，保留原采样、去重、阈值、上传 ABI v2、Slot 和 RDMA 生命周期。
Client／采样器构建及 88 项本地回归通过。2026-11-25 完成单轮 32G 图／4 GiB 本地限制的
核心实机验收，结果 PASS；实际覆盖范围和未覆盖路径见末尾记录。

## 默认 TSV 与控制台

`rswap_hotness --out DIR --pages ...` 自动生成：

```text
summary.tsv:
version pid generation pages present swapped others mean scan_ms pages_file

pages-NNNNNN.tsv:
va pfn last_pfn state heat
```

实际文件用 Tab 分隔；summary 格式版本为 2。others=pages-present-swapped，当前主要是
扫描竞争中状态变为 changed 的页面，不等同于旧 invalid。均值仍按有有效历史的非 alias
代表页计算，删除 alias 列不改变去重，不能对展示文件所有行直接取平均复核。

所有展示热度使用 0～1、两位小数，未知显示 `-`；例如 0、125000、1000000 ppm 显示
0.00、0.13、1.00。显示使用四舍五入，内部 active/valid、整数 ppm、阈值及上传均不舍入。
默认控制台同步精简；placement.tsv 为 v3，distribution.tsv 为 v2，热度字段改为 mean／threshold／heat。
展示直方图按两位小数合并相同桶，原始直方图和实际阈值计算不变。

debugfs `placement` 升为展示 v2，mean／threshold 同样为两位小数；
`placement --raw`／`placement_raw` 保留原 v1 格式，pool 工具内部读取原始接口，
默认 status/report 中的策略热度也使用展示格式。

## 可选原始日志

```bash
sudo remoteswap/tools/rswap_hotness --out /tmp/heat-NEW \
  --pages --raw-logs --interval 20 --window 4 --warmup 30 \
  --placement -- /path/to/program args
```

`--raw-logs` 默认关闭，开启后额外在 DIR/raw/ 保存原始 summary.tsv、run.tsv，
配合 --pages 保存原始 pages-NNNNNN.tsv；启用 placement 时另保存原始 placement.tsv／distribution.tsv。
原始均值精度、时间、计数、去重标识和上传整数均保持旧格式。

完整关联／阈值校验工具会自动优先读取 raw/，兼容历史原始日志。只有展示文件时，
工具明确提示需要 --raw-logs，不用舍入数据冒充完整精度验证。历史文件不会被改写。

## ftrace decision

`rswap:decision` 默认关闭，只记录成功分配 Slot 的 Store，正文恰好六项且每项后有逗号：

```text
req_type: store, pfn: 0x12345, placement_policy: adaptive, reason: hot, server: 0, target_mem: dram,
```

| 模式 | placement_policy | reason |
| --- | --- | --- |
| Inactive／Interleave | interleave | interleave |
| Warmup | adaptive(warmup) | interleave(warmup) |
| Adaptive，最终 DRAM | adaptive | hot |
| Adaptive，最终 PMem | adaptive | cold |

hot/cold 是实际目标标签，包含未知页、零热度及容量回退，不改写 policy_stats 的原始分类。
PFN 是换出源页面的页框号，不乘页大小。Load 不新增事件。分配失败没有合法目标，
只留原始事件及错误计数；分配成功也不表示 post 或 WC 成功。

原始完整字段另保留为 `rswap:decision_raw`，独立默认关闭，包含分配失败；
完整 trace-check 使用此事件，并继续兼容历史 decision 文本。精简 decision 无法验证完整结果链。
原始 mapping、placement、store_result 事件和原有请求延迟 trace_printk 不变。

新正文在事件自己的 char 数组内格式化，再通过单个 `%s` 输出，避免 5.14 在 full seq
条件下对混合数值／字符串参数的校验告警。实际内核校验函数回归覆盖原告警及新格式。

## 本地检查

- Client 1.14、采样器构建通过，无新增编译告警。
- 88 项回归通过，包含实际采样器→内核上传精度与整代事务、精简／原始 TSV、
  原始校验兼容、Store／Load／失败事件路径、按实际 Tier 标记及 full-seq 字符串校验。
- shell 语法和 diff 检查通过。硬件验证仍以实际运行记录为准。

## 核心实机记录（2026-11-25）

原始运行目录：[compact-output-bfs32-4g-20261125-4spvlt32](artifacts/compact-output-bfs32-4g-20261125-4spvlt32/)。
这是本地忽略的实测产物，不随源码提交。配置为两个 16 GiB DRAM + 32 GiB PMem Server，
32G BFS 图、cgroup-v1 本地限制 4 GiB、40 线程、`-rounds 1`。采样间隔 20 秒、窗口 4、
Warmup 30 秒，开启 `--pages --placement --raw-logs`，没有执行性能对比或故障注入。

- BFS 退出码 0，采样器与应用的监控窗口约 100.49 秒，生成 5 轮快照。
  BFS 自身的单轮计时为 2.43 秒，与包含图加载、扫描和输出的监控窗口含义不同。
- summary 精简十列，`others=pages-present-swapped`，均值两位小数；最后一轮为
  `pages=8255360 present=503525 swapped=6805484 others=946351 mean=0.83`。
- 五份逐页文件均为五列，行数与对应 summary 相符，末轮 8,255,360 行。
  核对精简／原始文件的头部样本；原始发布元数据与精确阈值校验通过，
  展示直方图与原始直方图舍入合并后的结果一致。没有逐行关联校验全部 PFN 或页面内容。
- Warmup 短窗口捕获 77,809 条精简 Store decision，覆盖 DRAM 和 PMem；
  Adaptive 短窗口捕获 131,749 条，均为 DRAM／hot。两者正文均只有六项、PFN 为十六进制、
  每项包括最后一项均以逗号结尾，没有 Load decision；各 CPU 缓冲区无 overrun／丢失计数。
  本轮没有捕获 Adaptive PMem／cold，实际应用也没有覆盖 Inactive／Interleave 输出；
  这些格式分支由本地回归覆盖。`hot` 是最终 DRAM 目标标签，不能解释为原始热度命中。
- 两台 Server 的两种介质均有成功 RDMA Read／Write。全轮 allocation、submit、WC 错误均为 0；
  新增内核日志无告警。结束时 Slot 全空闲，Client 已卸载、swap 已关闭、cgroup 已删除，
  保存的系统参数已恢复，配置文件未变，两台 Server 均确认资源释放并退出。

结果与示例：

- [运行结果](artifacts/compact-output-bfs32-4g-20261125-4spvlt32/results.json)
- [输出核对结果与覆盖范围](artifacts/compact-output-bfs32-4g-20261125-4spvlt32/output-verification.json)
- [实际输出节选](artifacts/compact-output-bfs32-4g-20261125-4spvlt32/examples.txt)
- [summary.tsv](artifacts/compact-output-bfs32-4g-20261125-4spvlt32/heat/summary.tsv)
- [末轮 pages-000004.tsv](artifacts/compact-output-bfs32-4g-20261125-4spvlt32/heat/pages-000004.tsv)

可直接查看（在仓库根目录执行）：

```bash
run=remoteswap/tests/artifacts/compact-output-bfs32-4g-20261125-4spvlt32
cat "$run/examples.txt"
column -t -s $'\t' "$run/heat/summary.tsv"
head -n 10 "$run/heat/pages-000004.tsv" | column -t -s $'\t'
```

完整精度日志位于同一目录的 `heat/raw/`，本轮与默认精简日志同时保存。
