# 第二阶段：VPN 热度与 EWMA

2026-11-28，Client **1.18**。第一阶段已完成双节点与真实 BFS 验证，
见 [Patch 12](patch12-kernel-swap.md)。本文记录第二阶段实现与本地检查；
初次本地验证后已进行双节点核心和原始 BFS 测试：受控专项通过，
BFS 完成三次遍历计时和三组 EWMA，但采样器在目标退出时误报 ESTALE，
因此整轮实机验收尚未通过，现场保留。退出修复及本地验证见文末。

## 范围与统计口径

生产采样器 `tools/rswap_hotness` 为一个固定目标 mm 保存 VPN 历史。
PFN 只用于本轮 Idle Page Tracking，不作为长期身份。换入到不同 PFN、
VMA 合并/拆分或描述变化、同一运行中 `munmap` 后同地址重映射，都保留 H。
每次重新启动采样器建立新历史；不自动追踪 fork、子进程或后续 exec。

| 当前 PTE/页面 | 当前 pages、均值、分布 | VPN 历史 |
| --- | --- | --- |
| 普通 4-KiB 匿名 resident | 计入 | 保留 |
| 普通匿名 swapped | 计入；本轮访问为 0 | 保留 |
| 未触碰的预留匿名地址，PTE 为空 | 不计入 | 已有历史继续保留、衰减 |
| `MADV_DONTNEED` 后空 PTE，VMA 仍有效 | 不计入 | 保留、衰减，重新出现后沿用 |
| 映射已取消 | 不计入 | 保留、衰减，同地址再次出现后沿用 |
| 文件页、共享 shmem、huge/compound 等 | 不计入 | 已有同 VPN 历史保留、按 0 衰减 |

按 VPN 计数：两个 VPN 即使共用 PFN 或 swap entry，也分别计入统计。
只有物理 Idle bitmap I/O 可以合并，不能据此去重均值的分母。
resident 要求 private 映射、KPF_ANON、KPF_SWAPBACKED，排除 compound/huge 等；
private 文件映射中的普通 COW 匿名页可以计入。
本仓库内核启用了 MIGRATION、MEMORY_FAILURE，未启用 DEVICE_PRIVATE，
普通 swap type 为 0..28，29..31 为特殊 entry，不计入。换用不同配置的内核时需同步该边界。

## 采样与更新

- `--a-bit-window` 默认 **5 秒**，指扫描、重设 Idle、输出/发布完成后再等待的时间。
  下一轮先读取 Idle，再将本轮仍可采样的 PFN 写 1，建立下一轮基线。
  不另设定时线程，也不承诺精确的 5 秒访问窗口。
- `--ewma-window` 默认 **4**，完成 4 个不重叠的小周期后更新一次：
  `S = hits / EWMA_window`，`H = 0.8 * H_old + 0.2 * S`。
  参数范围：等待 0.001..86400 秒，组大小 1..64。
  `--interval`、`--window` 是相应参数的别名。
- 初始扫描只建立基线，输出 `0(0s)`、H=0，不作为已经观察到的一个小周期。
  新出现 VPN、swapped、缺失或采样无效的小周期均按 0；分母始终是整组大小。
  组内中途出现的页面也不缩小分母。所有保留历史在组结束时更新，含已不在当前统计内的 VPN。
- H 使用 Q32，1.0 对应 `2^32`，每次更新四舍五入；均值保留 `sum_heat/pages`，
  显示时才保留两位小数。分档使用最接近精确 0.2/0.4/0.6/0.8 的 Q32 边界，
  不按两位显示结果分档。持续访问的前两组 H 约为 0.20、0.36。
- 能观察到的 PFN 变化会使本轮访问按 0，并重新建立 Idle 基线，历史 H 不清除。
  **不新增换页序号/生命周期代次**；两次检查间换出又换入、恰好回到同 PFN 的情况
  可能影响采样，按用户接受的 KISS 近似处理。共享物理页的访问也不能完全归因到单个 VPN。
- 实际组时长包含扫描、输出/发布和等待。若每轮扫描 16 秒、等待 5 秒，
  一组约 84 秒，输出应显示实际时间；不补造样本，不静默修改等待参数，不因扫描超过 5 秒失败。
  第三阶段可以依据成功发布的窗口编号判断完成了 5 个 EWMA 组；本阶段不改变 Warmup/Adaptive 策略。

proc maps/pagemap fd 在同一运行中保持打开；目标退出后正常结束采样，
仍运行的目标因 exec 导致旧 mm 消失时明确报错，不重新打开新 mm 来混用历史。
`--publish` 还通过绑定 ID 与 `mm_current` 检查目标 mm。
启动模式先等 `exec` 成功，再绑定；需要监控 BFS 时应 attach **实际 BFS 可执行进程 PID**，
不能把另行 fork BFS 的脚本 PID 当作工作负载。没有自动暂停目标进程，启动/attach 与扫描仍有竞态。

## 输出和运行方式

仅创建一个新的目录和其中的 **summary.tsv**，不再输出逐页文件、raw、run、placement、distribution 日志。
九列为：`pid`、`EWMA_window`、`mean_hotness`、`pages`，以及五列
`dist[0 - 0.2]`、`dist[0.2 - 0.4]`、`dist[0.4 - 0.6]`、`dist[0.6 - 0.8]`、`dist[0.8 - 1.0]`。
最后一档包含 1.0，单元格为 `数量(比例%)`，比例一位小数。
字段间一个 TAB，数据字段内可能有对齐空格，读取时 strip。
`pages=0` 时均值 `0.00`，各档 `0(0.0%)`。
窗口示例 `1(84.012s)`；时间保留毫秒，初始行固定 `0(0s)`。

以下为实机测试的命令示例，sudo 操作由用户执行：

```bash
make -C remoteswap/tools
# 默认仅观察，不需要 Client；需要 root 访问 pagemap/PFN 和 Idle bitmap。
sudo remoteswap/tools/rswap_hotness --out /tmp/vpn-heat-NEW --pid ACTUAL_PID
# 发布到已连接、已 swapon 的 Client 1.18；不启用新的放置策略。
sudo remoteswap/tools/rswap_hotness --publish --a-bit-window 5 --ewma-window 4 \
  --out /tmp/vpn-publish-NEW --pid ACTUAL_PID
# 直接启动目标可执行文件，或使用 --pid attach；不可同时指定。
sudo remoteswap/tools/rswap_hotness --out /tmp/vpn-launch-NEW -- /path/to/executable args
sudo /usr/bin/python3 remoteswap/tools/rswap_hotness_check.py /tmp/vpn-heat-NEW/summary.tsv
```

`rswap_hotness_check.py` 自动识别新 summary，检查列数、组序号、时间、人数和比例；
无法从精简 summary 重新证明每页热度或 Store 分类，不接受旧 PFN trace 关联参数。
`pool-report --heat-dir` 支持新格式的概要。旧 PFN 详细关联只适用于历史日志。
旧 `--placement`、`--raw-logs`、`--pages` 等参数不再可用；旧指南中的这些命令属于历史版本。

## Client 接口

新增 `rswap_vpn_heat_uapi.h`、Client `rswap_vpn_heat.c/.h`，debugfs `vpn_heat`，权限 0600。
保留原 `hotness` PFN 上传 ABI 和全部现有放置实现。
新接口暂只提供观察快照与复制值查询，**Store/Load 不读取它，Server 协议和内核核心无修改**。

- 先保持 `identity` fd 并 `bind PID`，取得 `binding_id`。
  identity 文本增加 `mm_current`：该 PID 当前 mm 仍为绑定 mm 时为 1。
- 新热度 fd 只能 BIND 一次；BEGIN 声明组编号、历史记录总数、当前 pages 与 Q32 heat_sum。
  DATA 每批最多 4096 条，按 VPN 严格递增；每条为 VPN、H、是否计入当前统计。
  包含暂不在统计内的历史。COMMIT 检查条数、当前 pages、sum 全部一致才原子发布。
- 序号 0 为初始基线；后续序号严格递增。半包、乱序、重复 VPN、范围错误、部分提交、
  分配或拷贝失败不会取代旧完整快照；这些错误会丢弃暂存数据，可重新 BEGIN。
- QUERY + pread 返回组号、记录数、当前 pages/sum、目标 VPN 的 H。
  当前绑定内缺少历史返回 H=0、found=0；错误/失效绑定返回 ESTALE。
  读取复制值，不向调用者暴露快照指针。RCU 保证替换与并发读取的安全。
- identity fd 关闭、目标 exec/退出、swapoff 后旧绑定不能用于有效查询/发布。
  新绑定可接替失效的旧发布者；旧 fd 关闭不会撤销新绑定。热度 fd 关闭及 swapoff 释放快照。
  目标退出/exec 后若热度 fd 仍留着，旧存储暂留到关闭/接替，但已不能查询为当前热度。

当前实现用户态每条历史约 32 字节，Client 每条快照记录 24 字节，另有排序、当前页列表、
替换旧快照和容量预留的临时开销。历史按本次运行曾出现的 VPN 数增长，不按当前 pages 缩小；
大 BFS 的扫描时长和内存开销需要实测。

## 本地验证

执行命令：

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
make -C remoteswap/tools
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
clang --analyze -std=gnu11 -Wall -Wextra -Werror remoteswap/tools/rswap_hotness.c -o /tmp/hermit-stage2-hotness.plist
```

第二阶段新增 15 项测试（使用实际生产函数、无特权文件/VM/RCU 夹具和 UBSan）：

| 测试 | 目的 |
| --- | --- |
| VPN 采样/历史 | 初始基线、冷热、相同 PFN 别名分别计数、swapped、空 PTE、MADV_DONTNEED、PFN 变化、VMA 文本变化、unmap/remap 续用历史 |
| EWMA/统计 | 非重叠分组、新页固定分母、Q32 精度、五档边界、均值、0 页、单文件、实际时间及格式校验 |
| 完整 CLI 循环 | 16 秒模拟扫描加 5 秒等待，四轮后实际 84 秒；每组只更新一次；旧 mm EOF 不重开/混用历史 |
| 实际发送端→实际 Client | 批量发布超过 4096 条、初始/后续/空快照、当前统计和保留历史分开、查询缺失与失效绑定 |
| 事务/生命周期/并发 | 半提交、乱序/重复/非法记录、分配/拷贝/权限失败保留旧快照；exec、重新绑定、旧 fd、swapoff；500 轮并发替换/查询 |

历史 PFN 发送端保存在 `tests/fixtures/rswap_hotness_v1.c/.h`，仅供保留的旧 ABI、
放置规则和归档日志的兼容测试使用，不作为新采样器的正确性证据。

本次结果：**134 项测试全部通过，耗时 20.745 秒**；生产采样器在
`-Wall -Wextra -Werror` 下编译通过，Clang 静态分析无诊断；
Client RDMA 模块使用现有 `linux-5.14-rc5` 构建通过，模块版本为 **1.18**，本次构建日志无警告。
日志和文件校验见 [本地验证记录](artifacts/stage2-local-20261128/README.md)。

以上 134 项为首次本地验证；随后实机结果和退出修复如下。第三阶段放置策略仍未接入。

## 首轮实机结果与退出修复

原始证据及失败时的源码保存在
[failed-exit 目录](artifacts/stage2-core-bfs-20261128-failed-exit/STATUS.txt)。
该轮使用双 Server 各 16 GiB DRAM + 32 GiB PMem，Client 1.18、#9 内核，
实际 BFS 脚本参数不变：16 线程、64G 图、24 GiB 本地限额、三次计时。

- 受控专项完成 6 组 EWMA，8192 个目标 VPN 的历史完整保留。
  热页抽查 32/32 符合 0.20、0.36，冷页 32/32 为 0；
  4096 页换入后 PFN 变化，内容及历史验证通过。
  DONTNEED 时目标当前页数 8192→4096，refault 后恢复 8192，历史仍按原值延续。
- 共完成 7 份受控快照和 4 份 BFS 快照（含初始基线），800 次已存历史 VPN 的内核查询通过，
  没有观察到 unknown；全量上传记录均验证 H 在 Q32 的 [0,1] 范围。
  空 PTE 对应未建立历史的查询返回 0；错误绑定拒绝，未混作目标冷页。
- BFS 三次遍历计时为 0.462、0.331、0.325 秒。
  `ligra.h` 在 `readGraph` 之后才开始遍历计时；进程总时长还包括文本读取、解析/建图、
  回收换页及析构。监测记录约 220 秒，退出失败前未得到完整端到端退出码/耗时。
  此前同参数阶段一全流程 231.46 秒，属于参考，不能据此推导采样器的性能影响。
- BFS 远端观测峰值 42.386 GiB；退出失败前的快照累计远端写 71.678 GiB、
  同步读 58.489 GiB（已减去受控负载的各 16 MiB），异步读为 0。
  未见 WC/submit/unrouted、分配错误或新增相关内核错误。
- 采样器观测峰值用户态 RSS 1472.523 MiB，约 139.33 CPU 秒。
  这些数值不包含 Client 的内核热度表开销；没有做性能 A/B。

| BFS EWMA 组 | 相对基线时间 | 当前 pages | 未舍入 mean | summary 显示 |
| --- | --- | --- | --- | --- |
| 1 | 35.091 s | 6453306 | 0.0220449100 | 0.02 |
| 2 | 98.604 s | 14450780 | 0.0134577940 | 0.01 |
| 3 | 163.696 s | 16835325 | 0.0112926375 | 0.01 |

0.10 是受控负载首组约半数页为 0.20、半数页为 0 的整体均值，并非 BFS 的结果。
BFS 的低均值符合当前口径：大量换出/无访问/新出现的 VPN 贡献 0，计入全部当前页的分母；
PFN 变化会重新建立采样基线，三组 EWMA 也尚未收敛。
实际遍历很短，当前均值不能单独代表遍历阶段的稳态冷热。
需要稳态观测时应让相同图上的重复遍历持续多个实际采样组，不能仅靠延长空闲或读图时间提高 H。

退出失败与修复：Linux `do_exit()` 先设置 PF_EXITING，再执行 `exit_mm()`，之后才进入 zombie。
原采样器只检查 Z/X，可能在任务仍显示 R 时将已释放 mm 的 ESTALE 当作异常。
现在读取 `/proc/PID/stat` 第 9 字段识别 PF_EXITING；在绑定检查/发布返回 ESTALE 或 ESRCH 时，
重新确认目标是否已结束或 PID 已复用，仅这些结束情形正常收尾。
仍存活的 exec、绑定撤销、swapoff 和 I/O/协议错误保持报错；不追随新 mm/PID，不补写未完成组。
测试观察器处理 COMMIT 成功后目标退出导致的查询失效，控制器也接受 PF_EXITING，并等待实际 BFS 退出。

修复后全套 **156 项本地测试通过**，其中新增 18 项生产 CLI 退出竞态回归，
覆盖循环检查、BEGIN/DATA/COMMIT/ACK、扫描中退出、PID 复用及真实错误不被吞掉；
观察器使用真实 Client 接收代码验证提交后退出会丢弃未验证的观测。
采样器和观察器严格编译通过，Clang 静态分析无诊断。
日志和校验值见 [退出修复验证记录](artifacts/stage2-exit-fix-20261128/README.md)。
本次仅修改用户态采样器和测试工具，无须重编或重载 Client/内核；
尚未重跑 BFS 或清理保留现场，不能把本地修复验证说成实机复测通过。
