# Patch 14：VPN 热度驱动 Store 放置

更新时间：2026-11-28。Client **1.19**。本阶段实现第三阶段放置策略，
沿用 [Patch 12 身份关联](patch12-kernel-swap.md) 和 [Patch 13 热度统计](patch13-hotness.md)。
本轮只做本地验证与编译，没有加载新模块、执行 sudo 或重跑双节点/BFS。

## 放置规则

| 情况 | 首选位置 |
| --- | --- |
| 已注册 pureDRAM/purePMem 固定策略 | 继续优先执行固定策略 |
| 目标 mm，尚未成功提交第 5 个 EWMA 组 | PMem |
| 目标 mm，已提交第 5 组，H ≥ 当前均值 | DRAM |
| 目标 mm，已提交第 5 组，H < 当前均值 | PMem |
| 非目标页面 | 普通 Interleave |

- 基线编号为 0，不计入五个完成组；提交组号必须连续。BEGIN、部分 DATA、失败 COMMIT
  或单纯等待均不推进阶段。默认实际时间包含扫描、上传和每轮扫描后的 5 秒等待，可能超过 100 秒。
- Store 根据 **swap type/raw offset → 目标 mm/VPN** 查热度，不用执行 Store 的线程 PID 或 PFN 判断归属。
  同一 entry 对应多个目标 VPN，取最大 H；相同 H 时选最低 VPN 作为诊断标识。
  统计均值仍按当前范围内的每个 VPN 分别计数，不按 entry 去重。
- 已确认属于目标但没有历史的 VPN，H=0。零热度正常参与比较；H=mean=0 属于热页。
  当前统计 pages=0 时均值按 0 处理，与 summary 一致；保留的旧 H 仍可供重新出现的 VPN 使用。
- 一次决策在同一 RCU 快照中读取 H、sum_heat、pages 和组号，用
  `H_q32 * pages >= sum_heat` 比较。没有先转换成两位小数或 ppm，也没有内核浮点。
- 每层独立 Server round-robin；容量不足先试同层其他 Server，再试另一层，
  全满时沿用 Frontswap 错误返回和已有磁盘回退路径。
- 只影响新 Store。既有映射不迁移，Load 始终按已有 Slot 读取。

## 启动与停止

前提：匹配的采样器与 Client 1.19、第一阶段 #9 内核、已连接的池和已激活的 swap。
目标使用普通 4 KiB 私有匿名页；有关采样范围、历史和退出处理的约定仍按第二阶段执行。

```bash
# OUT 必须是新目录；PID 是 exec 后的实际负载进程。
sudo remoteswap/tools/rswap_hotness --placement --a-bit-window 5 --ewma-window 4 \
  --out /tmp/vpn-placement-NEW --pid ACTUAL_PID

# 仅读状态：placement 是两位小数显示，placement_raw 保留机器可读值。
sudo cat /sys/kernel/debug/rswap/placement
sudo cat /sys/kernel/debug/rswap/placement_raw
```

`--placement` 隐含发布，使用 VPN ABI v1 的新增操作 `BIND_POLICY=6`。
`--publish` 仍使用原 BIND=1，仅发布/查询，不启用放置；不带这两个参数则仅用户态采样。
命令/记录/ACK 大小和旧 PFN 上传 ABI 均未改变。旧 PFN 策略与 VPN 放置互斥，冲突返回 EBUSY。
每个上传 fd 只允许绑定一次，不能把观察 fd 原地升级成放置 fd。

```bash
# 停止未来的热度放置，采样器可继续以观察模式运行。
printf 'stop\n' | sudo tee /sys/kernel/debug/rswap/placement_control
```

停止、关闭上传 fd、目标 exec/身份失效或 swapoff 后，后续 Store 不再使用该 VPN 策略。
已经取得旧快照的并发 Store 可完成；已有 Slot 和在途 I/O 保留其生命周期。
重新启动需要新 fd/有效绑定，旧 fd 关闭不会停止新的 owner。

## 输出与证据

采样文件仍只有 `summary.tsv`，保持第二阶段的九列、分布和实际完成时间。
Client 状态新增 `source=vpn placement_rule=2`、`warmup_windows=5`、`completed_windows`、
binding、pages、sum_heat；状态中的 ppm/两位小数只用于显示。

`rswap:decision` 保持六字段格式，例如：

```text
req_type: store, pfn: 0x123, placement_policy: adaptive(warmup), reason: warmup, server: 0, target_mem: pmem,
req_type: store, pfn: 0x123, placement_policy: adaptive, reason: hot(0.75 >= 0.67), server: 0, target_mem: dram,
req_type: store, pfn: 0x123, placement_policy: adaptive, reason: cold(0.50 < 0.50); capacity_fallback, server: 1, target_mem: dram,
```

最后一行允许出现：精确 H 小于均值，但显示舍入后相同；PMem 满后实际落到 DRAM，仍保留 cold 分类。
人类可读事件仅在成功分配后发出，不等于 RDMA post/WC 成功。

新增独立的数值事件 `rswap:vpn_decision`，含最大 H 对应 VPN、aliases、binding、
window/publish_ns、Q32 H、pages/sum_heat、RR、首选与实际位置、回退和分配结果。
旧 PFN `decision_raw` 与固定策略 `app_decision` 保留。
`rswap_trace.py --dynamic --adaptive` 能校验新事件、精确比较、回退、映射及 Store 完成关联；
完整容量回放仍要求从空池开始且无丢事件。summary 本身不能独立证明逐页热度内容。
`rswap_pool.py` 提供 `classified_heat_coverage`；VPN 模式不再把分类覆盖率宣称为正热度或 PFN 命中率。

## 本地验证

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s remoteswap/tests -p 'test_*.py' -v
make -C remoteswap/tools -B
make -C remoteswap/client KDIR=/mnt/sdb3/data/yxr/projects/hermit/linux-5.14-rc5 -j12
```

新增专项覆盖：真实发送/接收与选择函数、第五组原子提交切换、别名最大热度、缺项与全零、
精确均值比较、非目标 Interleave、固定策略优先、旧策略互斥、exec/旧 fd/swapoff、
并发快照一致性、实际 Slot/Store/Load 路径、同层与跨层回退、在途 Read 保留容量、
池满返回 ENOSPC、trace/完成关联及舍入后的显示。
用户态测试以 fixture 代替内核同步、mm 和 RDMA 硬件，不能替代实机验收。

结果：**172 项测试全部通过（26.992 秒）**，其中新增第三阶段专项 16 项；
采样器和 Client 编译通过，`modinfo` 确认模块版本 **1.19**，构建日志无警告。
采样器 `clang --analyze` 和 `git diff --check` 均通过。

最终本地结果与构建日志见 [验证记录](artifacts/stage3-local-20261128/README.md)。
本次没有修改内核核心或 Server 源码/协议。双节点与真实 BFS 验收留待后续单独安排。
