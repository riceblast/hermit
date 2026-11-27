# Patch 12：第一阶段内核修改与最小 Client 接入

本文是后续 Codex 对话的交接参考，记录截至 2026-11-28 的需求、实现、验证结果和待办。
仓库：`/mnt/sdb3/data/yxr/projects/hermit`。
第一阶段代码提交：`fcbf69e3e`（`kernel support for swap modification`）。

## 1. 当前阶段的目标和边界

整体修改分三个阶段，每个阶段完成后分别验证：

1. **当前阶段：内核修改与必要的最小 Client 接入。** 建立 swap entry 与目标地址空间、VPN 的关联，完成编译和第一阶段实机验证。
2. Client 冷热统计改为按 VPN 保存历史，实现新的采样周期、EWMA 和 `summary.tsv`，测试冷热识别。
3. 基于新热度和平均热度修改 Warmup/Adaptive 放置策略及 decision 输出。

本阶段解决的问题是：页面进入 Store 时，执行回收的线程可能不是页面所属进程，页面的物理地址也不能代表长期逻辑身份。因此，需要在内核仍能确定所属地址空间和虚拟地址时，记录 `(swap type, swap offset) → 目标 mm/VPN`，供 Client 查询。

**第一阶段只建立身份关联，没有实现 VPN 热度表、EWMA、新均值、新统计文件或新放置规则。** 现有热度上传 ABI、Store/Load 数据路径和放置决策仍沿用原逻辑。第二阶段开始前，先完成本阶段实机验收；遇到影响需求或设计的问题，先与用户确认。

当前实现限定为单个目标地址空间及其共享 `mm` 的线程，重点支持普通 4 KiB 匿名页。暂不扩展 fork、shmem/tmpfs、大页和跨监控运行的历史持久化；不增加 munmap 事件跟踪。

## 2. 已确认的后续需求

以下是后续阶段的约束，**不要误认为本阶段已经实现**。

- 长期热度按目标进程中的 VPN 保存。PFN 只用于当轮 Idle Page Tracking 采样；换入到新 PFN 后，同一个 VPN 继续使用原热度历史，随后采样新 PFN。
- 不使用页面生命周期代次来清除同 VPN 历史。不专门追踪 `munmap → 同地址重新 mmap`，同一监控运行内地址复用允许延续历史。
- `a_bit_window` 默认 5 秒，每个小周期记录是否访问的 0/1；`EWMA_window` 默认 4，表示每 4 个小周期更新一次热度。二者可配置。
- 每组小周期的热度 `S = 访问为 1 的次数 / EWMA_window`；每组结束后更新 `H = 0.8 × H_old + 0.2 × S`，初始热度和尚未发生的小周期均按 0 处理。默认每 20 秒更新一次 EWMA，不是每 5 秒重复更新。
- 未观察到访问、已换出、采样无效的周期均计 0，不保留“有效/无效窗口”的语义。
- Warmup 为 5 个 EWMA 周期，默认约 100 秒。Warmup 与 Adaptive 都更新平均热度。Warmup 优先 PMem，满后回退 DRAM，再满则回退磁盘。
- Adaptive 对监控范围内页面使用未舍入热度比较：`H >= mean_heat` 为热页，优先 DRAM；否则优先 PMem。相等也算热页。非监控进程不参与冷热决策；既有固定放置规则的优先关系需保留。
- Adaptive 的 decision reason 显示页面热度和平均热度，均保留两位小数，例如 `hot(0.81 > 0.52)`；分类以未舍入值为准，不能根据显示出来的小数重新分类。
- 热度采样输出只保留 `summary.tsv`：PID、EWMA 周期序号和时间（如 `0(0s)`、`1(20s)`）、平均热度（两位小数）、当前页面数，以及五档热度分布的数量和比例（比例保留一位小数）。分档边界按左闭右开，最后一档包含 1.0；列名和数据尽量对齐。

VPN 历史与当前统计范围按下面的口径处理：

| 页面状态 | 保留 VPN 热度历史 | 计入当前 mean、pages 和分布 |
| --- | --- | --- |
| 页面驻留本地 | 是 | 是 |
| 页面已换出，虚拟地址仍有效 | 是；未观察到访问的周期计 0 | 是 |
| 虚拟地址映射已取消 | 是；继续按 0 衰减 | 否 |

**swap entry 关联表与 VPN 热度历史表是两回事。** 本阶段的关联表在 swap entry 最终释放时必须删除，防止 offset 复用后指向旧 VPN；这不意味着后续要删除那个 VPN 的热度历史。

## 3. 修改文件与实现机制

### 3.1 内核

| 文件 | 修改内容 |
| --- | --- |
| `linux-5.14-rc5/include/linux/rswap_identity.h` | 新增同步 tracepoint 声明 `rswap_swap_bind`、`rswap_swap_free`，以及已有 swap PTE 的补扫接口 `rswap_identity_scan_mm()`。 |
| `linux-5.14-rc5/mm/rmap.c` | 在 `try_to_unmap_one()` 和 `hermit_try_to_unmap_one()` 成功安装普通匿名页 swap PTE 后通知 Client，传入 `mm`、虚拟地址、swap type 和原始 offset；定义并导出 tracepoint，增加补扫实现。 |
| `linux-5.14-rc5/mm/swapfile.c` | 在 `swap_range_free()` 中通知 swap entry 最终释放，清除旧关联，避免 offset 复用串页。 |

补扫持有 mmap/PTE 锁，只检查已有普通 swap PTE；不会触发换入，不修改 A 位，也不会为了扫描而拆分 THP。绑定时先发布目标并接收通知，再执行补扫，覆盖绑定前已经换出的页面，并减少补扫期间回收产生的遗漏。

通知是内核同步回调，不经过 `trace_pipe` 或用户态事件队列。回调在 VM 锁保护的上下文中运行，不能睡眠，不能长期保留 VMA/PTE 指针。

### 3.2 Client

| 文件 | 修改内容 |
| --- | --- |
| `remoteswap/client/rswap_identity.c`、`.h` | 新增单目标 `mm` 绑定、关联表、查询、补扫接入、释放和 debugfs 控制接口。 |
| `remoteswap/client/rswap_rdma_ops.c` | 在 Client 初始化/退出时注册、注销回调；在 swapon/swapoff 生命周期中启用或撤销关联。没有把身份查询接入新的热度或放置决策。 |
| `remoteswap/client/rswap_stats.c` | 在原 debugfs 根目录下注册 `identity` 和 `identity_status`。 |
| `remoteswap/client/Makefile` | 编译并链接 `rswap_identity.o`。 |
| `remoteswap/client/rswap_client.c` | Client 模块版本更新为 `1.17`。 |

关联表是目标上下文内的 XArray：`swap offset → 不重复的 VPN 链表`，上下文同时记录目标 `mm` 和 swap type。允许同一个 entry 对应目标地址空间内多个 VPN。查询目前返回最低 VPN 和别名数量；**最低 VPN 只是第一阶段的验证接口行为，不代表第二阶段已经确定了多别名热度的聚合规则。**

RCU 保护目标上下文的发布和撤销；XArray 的 IRQ-safe 锁保护关联链表与计数。查询只复制数值，不向外暴露表项或 `mm` 指针。长期只持有 `mm_count` 引用，不持续持有 `mm_users`，避免阻止进程退出时释放地址空间。

回调分配使用 `GFP_ATOMIC`，失败会累计 `alloc_errors`。初始补扫失败或补扫期间出现分配错误时，绑定失败并撤销；后续回调的分配错误也必须在实机测试中检查，不能默认为关联完整。

### 3.3 测试与文档

- `remoteswap/tests/test_stage1_identity.py`：新增生产 Client 身份关联代码的用户态测试，以及内核挂钩位置、扫描过滤的静态检查。
- `remoteswap/tests/test_patch3_completion.py`：为原测试桩增加最小兼容定义。
- `remoteswap/tests/stage1-identity.md`：本阶段接口和测试说明。
- `remoteswap/tests/stage1_install_kernel.sh`：可选安装辅助脚本；**用户选择手动安装，不要求执行该脚本。**
- `remoteswap/README.md`：同步版本和说明。

## 4. bind、exec 和控制 fd：必须注意

**`bind` 绑定的是当时的地址空间，不只是 PID。**

例如，启动脚本执行 `exec ./BFS`：进程的 PID 不变，但原来属于 bash 的地址空间会被替换成 BFS 的地址空间。如果之前已经绑定，绑定仍指向旧的地址空间，因此需要重新绑定。

实际使用时，**等真正的测试程序启动后再绑定即可。普通的页面换入、换出和 PFN 变化都不需要重新绑定。** 测试程序可以在完成 exec、建立测试区域后发出 READY，再由控制程序执行 bind；需要测试初始补扫时，应让它在 bind 前先产生部分换出页。

当前不会自动追随 exec。确实发生 exec 时，需要关闭旧控制 fd，重新打开 `identity` 再 bind。第二阶段需要处理采样器/启动器与目标程序启动的衔接，这部分尚未实现。

其他控制接口约束：

- 先按原流程启动 Server、加载匹配的新 Client、启用 swap，再 bind；当前只允许一个目标绑定。
- `identity` 的 fd 要在整个测试期间保持打开；关闭它就会撤销属于它的绑定。不能用一次性 `echo 'bind PID' > identity` 来保持绑定。
- 每个控制 fd 只允许建立一次绑定；绑定已撤销、swapoff 后，重新测试应重新打开 fd。旧 fd 关闭不会撤销后来由其他 fd 建立的新绑定。
- `identity` 为 root/CAP_SYS_ADMIN 控制接口；`identity_status` 只读，用于观察全局状态。
- 查询使用 pagemap 中的**原始 swap offset，不减 1**，并核对 swap type。不要把 Server Slot、PFN 或 VPN 当成 swap offset。
- 关联的清理时机是 entry 最终释放；不能假设页面一换入，entry 就必然已经释放，swap cache 仍可能持有它。

最小接口示例（在有权限的控制进程中运行，`target_pid` 和 `swap_offset` 由测试程序提供）：

```python
import os

fd = os.open('/sys/kernel/debug/rswap/identity', os.O_RDWR)
try:
    os.write(fd, f'bind {target_pid}\n'.encode())
    print(os.pread(fd, 512, 0).decode())
    # 此时让目标进程保持运行，并保证待查询 entry 尚未最终释放。
    os.write(fd, f'lookup {swap_offset}\n'.encode())
    print(os.pread(fd, 512, 0).decode())
    # 实际控制程序在这里继续运行测试，测试结束后才进入 finally。
finally:
    os.close(fd)
```

`identity` 返回 `binding_id`、PID、attached、found、type、offset、VPN、VA 和 aliases。
`identity_status` 返回 bound、entries、aliases、bind_events、free_events、alloc_errors 等。
其中 `entries` 是有记录的 swap entry 数，`aliases` 是关联 VPN 数；二者都不是第二阶段的 `summary.tsv pages`。

## 5. 已完成的构建与本地验证

- 完整内核构建通过，新构建编号为 `#9`。
- Client 使用该内核目录构建通过，模块版本为 `1.17`。
- 全部 **105 项用户态回归测试通过**，其中第一阶段新增 5 项测试覆盖别名和过滤、entry 释放复用、fd 所有权和 swapoff、分配/注册/补扫失败、并发查询与撤销，以及内核挂钩检查。
- `git diff --check`、安装脚本语法和新增文件的 checkpatch 检查通过。
- 后续复查中，4 个生产 Client 测试场景通过 ASan/UBSan 检查；运行时关闭了 LeakSanitizer，不能据此声称完成了内存泄漏检查。

内核链接日志存在旧汇编目标相关的 `.note.GNU-stack` 和 RWX 段警告；不能把构建结果描述成完全没有警告。本阶段修改的源码编译和 Client 构建未发现新增编译错误或警告。

本地测试使用 VM/XArray/RCU 替身，**不能替代真实内核锁与生命周期、RDMA/DMA、swap 数据完整性和实机并发测试**。

构建命令，供需要重新构建时使用：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
make -C linux-5.14-rc5 LOCALVERSION= -j12
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
git diff --check
```

交接时构建日志和产物校验清单在 `/tmp/rswap-stage1-work/`：`kernel-build.log`、`client-build.log`、`regression.log`、`artifacts.sha256`、`READY`。`/tmp` 可能在重启后清空；本文记录的验证结果不依赖这些临时文件长期存在。`READY` 中“未安装”的文字描述的是最初构建交接时的状态，不能代替当前系统检查。

## 6. 内核安装、当前状态与重启确认

### 6.1 写本文时实际观察到的状态

- 当前运行内核：`uname -r` 为 `5.14.0-rc5-hermit-syscall-v2`，`uname -v` 仍为 `#8 SMP Mon Nov 23 11:25:57 CST 2026`。
- 整理文档期间，`/boot/vmlinuz-5.14.0-rc5-hermit-syscall-v2` 已更新，并经 `cmp` 确认与新编译的 `arch/x86/boot/bzImage` 完全一致。
- 已安装的 `System.map` 包含 `rswap_identity_scan_mm` 和两个新 tracepoint；模块索引、initramfs 的更新时间也已变化。
- 最近一次检查时 `/proc/swaps` 无活动 swap，`rswap_client` 未加载。
- **尚未观察到重启进入新内核，实机验收尚未完成。** 上述文件检查不能代替核对完整安装命令是否成功退出。新对话开始时必须重新检查实时状态，不要机械重复安装或再次 swapoff。

### 6.2 手动安装的核心步骤

用户选择手动安装，不执行 `stage1_install_kernel.sh`。流程是：编译完成后，安装模块和内核，再重启选择启动项。**仅编译后直接重启不会自动使用源码目录中的新镜像。**

如果尚未完成安装：先结束实验，保持 Server 在线并正常 swapoff，再卸载不再使用的 Client。由于新旧 release 同名，安装前备份 `/boot` 中该 release 的启动文件和 `/lib/modules/5.14.0-rc5-hermit-syscall-v2`，并保留可用的恢复启动方式。

```bash
cd /mnt/sdb3/data/yxr/projects/hermit/linux-5.14-rc5
sudo make LOCALVERSION= INSTALL_MOD_STRIP=1 modules_install
sudo make LOCALVERSION= install
```

本机 `/etc/kernel/postinst.d` 包含 initramfs 和 GRUB 更新钩子。确认安装及钩子执行成功后再重启，选择原来的 Hermit 启动项。若已经完成安装并核对产物一致，不需要为了本文重复安装。

新内核 release **仍为 `5.14.0-rc5-hermit-syscall-v2`，没有改为 v3**，目的是保留当前 OFED 模块的 release 路径。重启后不能只看 `uname -r`，要检查构建编号和新接口：

```bash
uname -r
uname -v
sudo grep -w rswap_identity_scan_mm /proc/kallsyms
```

预期 `uname -v`：

```text
#9 SMP Sat Nov 28 00:02:37 CST 2026
```

如果后续重新编译，编号和时间可能变化，应与 `include/generated/compile.h` 核对。新 Client 需要新内核导出的接口，不能因为旧 `#8` 的 release 名字相同就认为能够加载。

## 7. 待完成的第一阶段实机测试

先确认已启动正确的新内核，再按既有流程准备 Server、Client 和 swap。使用可控的小型普通匿名页工作集，禁用 THP，确保测试访问的是 4 KiB 页，且能稳定制造换出；首次不直接使用完整大规模 BFS 代替可控测试。

| 测试 | 需要验证的结果 |
| --- | --- |
| 新接口与模块加载 | 新导出符号存在，Client 1.17 正常加载，debugfs 文件可用，无 unknown symbol、Oops 或新增 I/O 错误。 |
| bind 前已有换出页 | 程序完成 exec 后先产生部分换出页，再 bind；补扫能建立其身份关联。 |
| bind 后新增换出页 | 后续回收产生的新 swap entry 能由回调建立关联，无需再次 bind 或重复全量扫描。 |
| VPN 与 entry 的对应 | 由目标 `/proc/PID/pagemap` 解出 swap type/offset，查询返回的 VPN/VA 与原地址一致；其他地址空间的页面不混入。 |
| 换出、换入与 PFN 变化 | 多轮往返后，新的 entry 仍关联到原 VPN；观察到 PFN 改变时身份仍正确，逐 word 校验页面数据无损坏。此项只验证身份，不代表 EWMA 已实现。 |
| entry 释放与复用 | 确认 entry 已最终释放后旧关联消失；offset 复用后只返回新的关联，不残留旧 VPN。 |
| 并发和分配错误 | 回收、查询、退出、撤销并发时无崩溃或异常卡住；`alloc_errors` 保持 0，发现非 0 需先分析。 |
| fd 与生命周期 | 持有 fd 时绑定有效；关闭控制 fd 撤销；旧 fd 不误撤销新绑定；swapoff 后状态清理正确。 |
| swapoff 和卸载 | 正常 swapoff、关闭控制 fd 后可以卸载；远端 Slot 全部归还，原有 I/O 错误计数无新增。 |

实机验收时保留页内容校验结果、身份对应样本、`identity_status`、Client/Server 状态和相关内核日志。不能只根据 `bind_events` 增长就判定 VPN 对应正确，也不能只凭用户态测试通过宣告实机验收通过。

## 8. 新 Codex 对话从哪里继续

先读本文、`stage1-identity.md`、`test_stage1_identity.py` 和对应生产代码；需要追溯旧机制时再读 `task_summary/` 与 Patch 8–11 的说明。用户后续的明确需求优先于历史文档。

开始时检查当前提交和工作区、`uname -v`、Client 版本、swap/Server 实际状态。当前优先事项是**确认已重启新内核并完成第一阶段实机测试**，随后向用户汇报，再按用户指示进入第二阶段。不要因文档列出了 EWMA 和放置需求就直接开始同时改写第二、三阶段。

此前 Codex 会话的可写工作区只包含 `NPB3.4.4` 和 `/tmp`，Hermit 仓库修改需要额外的沙箱授权；Linux 目录权限本身允许用户写入。新会话应在 VS Code 中打开或添加本仓库，并选择 `hermit` 为对话工作目录，避免把 `chmod` 当作工作区权限问题的解决办法。
