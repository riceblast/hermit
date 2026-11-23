# Patch 9 复核修复与最小核心验收

## 当前范围与协作方式

助手负责修复、构建、本地回归、测试程序与控制、日志核对；操作者执行所有需要 sudo 的命令，
以及内核重启和 Server 启动。每阶段只执行助手当时给出的具体操作，不提前启用 swap。
本文件包含验收方案和执行记录。**2026-11-23 内核更新后的双节点两项核心验收均通过，
已完成正常收尾；实测结果与证据位置见末节。**

按最新要求，只做两项双节点硬件检查，取消此前单节点矩阵、硬件故障注入、全池压力及完整 Trace：

| 核心测试 | 配置 | 目标 |
| --- | --- | --- |
| 页面内容＋并发＋生命周期 | 两台各 DRAM 16 / PMem 32 GiB，总池 96 GiB；已有 48 GiB swap；4 GiB 工作集、2 GiB 物理限制、4 线程、2 代数据 | 多线程同时读取相同地址范围，每页每字正确；无 SIGBUS；活进程 swapoff 正常；Slot 全部归还 |
| BFS 集成＋同 Session 再启用 | 同一组连接，80 GiB swap，64G 图、40 线程、24 GiB 物理限制，1 次计时轮次 | 正常退出，四个 Server/Tier 有成功读写，错误无新增；最终 swapoff/卸载成功 |

修复的特殊错误交错用本地确定性回归覆盖，不为硬件验收专门注入读错误或刻意复现竞态。
多线程程序稳定运行只能证明该次核心工作负载通过，不能宣称所有线程交错已在硬件上覆盖。
本阶段保持 Interleave，不验收 Adaptive、冷热效果、吞吐提升或 DRAM 带宽高于 PMem。
不重新采集全量逐页热度，不重跑 Patch 8 全套。

## 修复内容

- `linux-5.14-rc5/mm/memory.c`：读取结束后若页面无效，在 PTE 锁下复核原 swap PTE。
  已变化则丢弃旧故障结果，仍相同才 SIGBUS；先于 KSM、rmap 和 swap 记账退出。
  成功读取路径及 Frontswap 接口不变。
- `rswap_mapping.c`：第一个元数据数组分配失败后立即回滚，不再尝试申请第二个大数组。
- `rswap_rdma_ops.c`：澄清旧 Store 的身份检查不是原子 compare-and-invalidate；
  保持现有页面锁及 Store 发布契约，不扩展内核接口。
- `test_patch9_fault.py`：编译实际故障/PTE 复核和清理代码段，覆盖仍相同/已变化的 PTE、
  I/O 等待顺序和锁/引用收尾。新回归在修复前 HEAD 上失败、修复后通过。
- `swap_shared_pattern.c`：独立实机校验程序。每代填充后暂停，4 个共享地址空间线程
  在相同批次读取相同地址，并逐字核对；两代使用不同内容和相反遍历方向。

## 1. 助手先完成构建和本地验证

```bash
make -C linux-5.14-rc5 -j12 bzImage modules
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
cc -std=gnu11 -O2 -Wall -Wextra -Werror -pthread \
  remoteswap/tests/swap_shared_pattern.c -o /tmp/hermit-p9-fix/swap_shared_pattern
bash -n remoteswap/tests/patch9_install_kernel.sh
git diff --check
```

日志和已校验构建产物清单放在 `/tmp/hermit-p9-fix/` 及 `/tmp/hermit-p9-fix-*.log`。
助手只在内核/Client 构建、本地回归和产物验证成功后生成 `/tmp/hermit-p9-fix/READY`。
未出现 READY 时安装脚本拒绝执行。

### 本轮本地结果

- 内核 `bzImage + modules` 完整构建通过，新构建为 `#8 SMP Mon Nov 23 11:25:57 CST 2026`；
  release 和 `.config` 保持现有部署值，managed Frontswap v2 符号存在。
- Client 完整重编译通过，版本 1.9；59 项回归通过，新增错误路径检查包含 18 组状态组合。
- 新故障回归拒绝修复前的 HEAD；修复后的实际代码段通过 ASan/UBSan（LeakSanitizer 关闭）。
- 四线程校验程序编译及 16 MiB / 2 代 / 4 线程本地自检通过，不视为已发生真实远端换页。
- 安装脚本语法、差异检查和 `/tmp/hermit-p9-fix/artifacts.sha256` 全部通过，READY 已生成。
- 调整构建并行度时曾发生生成文件冲突，已隔离受影响产物并完成重新构建。
  最终有效内核日志是 `/tmp/hermit-p9-fix-kernel-verified-build.log`；早前两份失败构建日志保留，
  不作为成功证据。Client 日志为 `/tmp/hermit-p9-fix-client-build.log`，
  最终回归日志为 `/tmp/hermit-p9-fix-regression-final.log`。
- 以上为实现阶段的本地结果；该阶段结束时尚未安装内核或执行硬件验收。
  后续实机结果见末节。原 `/tmp/hermit-p9-fix/` 构建交接文件已被重启清理，
  不应直接重跑依赖该 READY 的安装脚本；下次重建需重新准备构建交接。

## 2. 操作者安装内核，再重启

**先更新内核，再做实际换页测试。** 当前机器为 Client optane / 10.0.0.11。
安装脚本只覆盖当前已经部署的 `5.14.0-rc5-hermit-syscall-v2` 版本，不改变 release 名称，
不重装 OFED，以保留已有 OFED 模块的 release/ABI 路径。

从没有活动 swap、Client 模块已卸载的状态执行：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
sudo bash remoteswap/tests/patch9_install_kernel.sh
```

脚本核对产物校验和，先把当前四个 `/boot` 文件、GRUB 配置及整个旧模块目录备份到数据盘：
`/mnt/sdb3/data/yxr/projects/hermit-kernel-backups/p9fix-.../`。
旧模块目录约 6.3 GiB，复制可能耗时。备份失败不开始安装；任何安装步骤失败则停止并保留日志，
不要继续重启。脚本不会自动 reboot，也不会修改 GRUB 默认启动项。

看到 `INSTALL PASS` 后，按原来方式重启并选择 Hermit 内核：

```bash
sudo reboot
```

重启后回到对话告知已重启，暂不加载 Client、不启用 swap。助手接着检查：

- `uname -r` 与内核 release 一致，`uname -v` 与构建的 `include/generated/compile.h` 完全一致；
  不能只凭相同的 release 字符串判断已更新。
- 已安装 `/boot/vmlinuz-...` 与当前 bzImage 校验一致。
- managed Frontswap v2、HCA/RDMA 设备和固定 CPU 集合正常，无活动 swap。
- Client 构建产物版本 1.9，随后才安排连接。

若引导/OFED 异常，先停止验收；备份用于恢复原镜像、initrd 和模块目录。
本次没有改 Server 或 RDMA 协议。

## 3. 双节点准备与页面测试

助手确认构建/运行内核后，给出本阶段需要操作者执行的有限 sudo 操作：
准备两个测试 cgroup、加载 Client、启用既有 swap，以及启动只读日志采集。
Server 由操作者按已经验证的本机 NUMA 参数启动，node04 为 endpoint 0，server02 为 endpoint 1，
每台 16+32 GiB，Client 48 核；沿用仓库配置的总池 96 GiB。

页面测试仅开启 `bypass_swapcache` 来覆盖相关并发换入路径，`lazy_poll`、`batch_account`、
批量换出、speculative_io 等其余优化保持关闭。测试前保存，结束后恢复开关。
仅用物理内存限制制造压力，memsw 不设有限配额，THP 禁用，不读取既有 cgroup swap 记账验收。

操作者按助手给出的命令启动需要 root 的任务；助手通过日志和受控 stdin 管道推进：

1. 运行 `swap_shared_pattern 4096 2 4`，仅子进程进入 2 GiB cgroup。
2. `READY generation=0` 时确认实际远端占用和成功写入，再由助手释放第一次 ENTER。
3. 四线程完成第一代读回，出现 VERIFIED；程序填充第二代并停在 `READY generation=1`。
4. 操作者执行助手给出的解除物理限制和活进程 swapoff 命令。
5. swapoff 成功、每池 `live=retired=0` 且 `free=capacity` 后，助手释放第二次 ENTER。
6. 确认两代 VERIFIED、最终 PASS、退出码 0，四池有成功 I/O，无新增 WC/提交/未归属错误及 BUG/WARN。

这两个 READY 是同步点，不把暂停当作卡死，也不依赖跨 sudo/PTY 的键盘输入注入。
不需要操作者持续手动按键、盯带宽或计算统计。

## 4. BFS、再次启用和收尾

不重启 Server、不重新连接 Client。在同一 Session 上重新启用既有 80 GiB swap，
本项恢复此前 BFS 基线（包括关闭 bypass），只用 24 GiB 物理限制运行：
64G 图、40 线程、`-rounds 1`，保留 Ligra 自带预热。
操作者只执行需要 sudo 的启动/配置命令；助手读取日志、统计和退出状态。

通过目标：退出码 0、一条计时输出、四池成功读写、错误无新增。
最后操作者先 swapoff，助手在 Client 仍加载时核对全部 Slot 归还、最终 stats 和内核日志；
停止采集、恢复开关后，操作者再卸载 Client。确认无活动 swap 且模块已卸载后才停止 Server。

不自动重试失败的内核/换页任务，不强制卸载或提前停止 Server。失败时先保留现场和日志，
由助手定位后再给出下一条操作。

## 2026-11-23 实机核心验收结果

两项通过，正常收尾完成。本轮使用 Client optane / 10.0.0.11，48 个连续 CPU，
运行内核 `5.14.0-rc5-hermit-syscall-v2 #8 SMP Mon Nov 23 11:25:57 CST 2026`；
启动镜像与已构建 bzImage 的 SHA256 均为
`519aa7b09f05ce90427439e32dfaa46babd081809e230acc5c7c8d17eb930609`。
Client 1.9，协议/stats v2，总池 96 GiB，映射主体 288 MiB。

助手通过 SSH 以普通用户启动并最终停止两台 Server；两台 memlock 均为 unlimited。
node04 的 DRAM/PMem Node 为 0/1，server02 为 0/2，均为 16+32 GiB。
两台完整物理放置查询 `misplaced=0`，各 6 个 MR 注册成功，最终均输出 `Server resources released`。
用户执行了挂载/系统 swap 停用，以及 prepare、live-swapoff、bfs、finish 四个 root 阶段。
助手通过 stdin FIFO 精确释放两次 READY；未使用 PTY 键盘注入。

| 核心目标 | 实际结果 |
| --- | --- |
| 内容与并发 | `swap_shared_pattern 4096 2 4`，2 GiB 物理限制，bypass 开启；两代各 1048576 页均由 4 线程逐页逐字校验，最终 PASS，退出码 0，无 SIGBUS |
| 实际远端访问 | 第一处 READY 时约 2 GiB 已在远端；四池均有成功读写，不能用程序正常退出替代发生换页的证据 |
| 活进程 swapoff | 第二处 READY 期间解除物理限制并成功 swapoff，四池 `live=retired=0`、`free=capacity`；随后第二代最终内容仍正确 |
| 同 Session 再启用 | 不重启 Server、不重载 Client，在原连接上从 48 GiB 切换至 80 GiB swap，成功运行 BFS |
| BFS | 64G 图、40 线程、24 GiB 限制、`-rounds 1`，正常退出 0，一条 `Running time : 0.277` 输出；该值是算法计时输出，不是端到端运行时长 |
| 错误与日志 | 两项四池 WC/提交错误及 unrouted_error 增量均为 0；本轮新增 dmesg 未见相关 BUG/WARN/Call Trace/WC/rswap 错误 |
| 最终资源收尾 | 80 GiB swapoff 成功，Slot 全部归还，Client 卸载，两个测试 cgroup 清理；全部 HMT 开关、THP、readahead_win、page-cluster 恢复并读回原值；两台 Server 停止 |

每次成功请求为 4096 字节；以下为原 stats 的成功 WC 次数，重复读写重复计数，不代表唯一页面数：

| 测试 | Write 总次数 | Read 总次数 | WC/提交/未归属错误增量 |
| --- | ---: | ---: | --- |
| 四线程页面测试 | 2623624 | 5338971 | 全为 0 |
| BFS | 24952570 | 18708048 | 全为 0 |

本轮 Read 为同步路径，异步 Read 为 0。不声称硬件覆盖了完整异步组合、故障分支、
全池容量或 TB 级规模；特殊失败/PTE 交错由本地回归覆盖，没有在硬件上注入或认证该竞态。
BFS 不作为算法结果或性能改善的证明。没有启用 Adaptive，也不比较 DRAM/PMem 性能。

原始日志在 `/tmp/hermit-p9-core/`；为避免再次重启丢失，已将常规文件转存数据盘：
`remoteswap/tests/artifacts/patch9-core-20261123/`（Git 忽略）。
其中 `results.json`、`pattern.check.log`、`bfs.check.log`、`SUCCESS` 为摘要；
另保留两项 console/exit/stats/mapping、完整设置读回、内核日志、两台 Server 日志和本轮控制器。
`host.py` / `observe.py` 是此次实验控制器，不是生产 API；不在旧 run 目录直接重复 prepare。

最终无活动 swap。重启时自动启用的 `/swap.img` 本轮停用后未重新开启；fstab 未修改。
`sdb2` / `sdb4` 本轮挂载保留，未格式化或修改已有测试文件。
