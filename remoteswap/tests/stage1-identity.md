# 第一阶段：内核 VPN 关联与最小 Client 接入

本阶段增加普通 4 KiB 匿名页的 swap entry → 目标地址空间/VPN 关联。
Client 版本为 1.17；热度采样、上传 ABI、阈值、放置和 decision 展示仍是原有行为。
不增加 munmap 跟踪，不在此阶段实现 EWMA 或新的 Warmup/Adaptive 规则。

## 改动

- `linux-5.14-rc5/mm/rmap.c`：普通和 Hermit 的匿名页 swap PTE 安装路径同步通知 Client；
  增加 `rswap_identity_scan_mm()`，在附加已运行进程时补建已有 swap PTE 的关联。
  扫描持 mmap/PTE 锁，不换入页面、不采样 A 位、不为扫描拆分 THP。
- `mm/swapfile.c`：swap entry 释放时通知清理关联，覆盖未成功 Store 的 entry。
  Frontswap 重复 Store 的 invalidate 不删除该关联。
- `include/linux/rswap_identity.h`：声明两个同步 tracepoint 和补扫接口。
  Client 直接订阅内核回调，不依赖 trace_pipe 或事件缓冲区。
- `client/rswap_identity.c/.h`：单目标 mm 绑定、稀疏 XArray、别名列表、值拷贝查询、
  fd/swapoff/卸载清理，以及两个只供本阶段验证的 debugfs 文件。

关联仅保留 mm_count 引用，不持续持有 mm_users，目标进程退出仍可释放地址空间。
多个线程共享 mm，自然被覆盖；fork、shmem/tmpfs 和大页不属于本轮验收范围。
当前绑定明确指向 bind 时的 mm，exec 后需关闭旧 fd 并重新 bind；第二阶段将由采样器
完成启动/exec 的绑定衔接。内核里没有长期保存 VMA/PTE 指针。

## 构建与本地验证

在仓库根目录运行：

```bash
make -C linux-5.14-rc5 LOCALVERSION= -j12
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
git diff --check
```

新增本地检查直接编译 Client 生产代码，覆盖范围过滤、初始补扫、别名、entry 复用、
分配/注册失败、旧 fd 关闭、swapoff 与并发查询清理。VM/XArray/RCU 使用用户态替身，
不替代真实内核、RDMA/DMA 和页面内容验收。

## 安装并切换内核

完整构建和本地检查通过后，交接目录为 `/tmp/rswap-stage1-work/`，包含构建日志、
`artifacts.sha256` 和 `READY`。安装脚本核对这些产物，在数据盘备份原内核和完整模块目录，
随后安装模块、内核及 initramfs。它不自动 swapoff、不卸载 Client、不重启。

内核 release 沿用 `5.14.0-rc5-hermit-syscall-v2`，避免改变本机 OFED 模块的 release 路径。
安装会替换这一 release 的启动文件；备份必须成功后才安装。用构建的 `uname -v`
以及新符号确认启动版本，不能只看 `uname -r`。

操作者先按现有顺序保持 Server 在线、正常 swapoff，并卸载已停止使用的 Client，再执行：

```bash
sudo bash remoteswap/tests/stage1_install_kernel.sh
```

然后手动重启到惯用的 Hermit 启动项，检查：

```bash
uname -r
uname -v
grep -w rswap_identity_scan_mm /proc/kallsyms
```

新 Client 需要这个内核的新增导出符号；不能在旧 #8 内核上加载。

## 切换内核后的核心测试

按原流程准备 Server/Client/swap 和普通匿名页小工作集，禁用 THP。
阶段一验证进程需已经完成 exec，才能 bind。控制文件要由同一个进程保持打开：

```python
import os
fd = os.open('/sys/kernel/debug/rswap/identity', os.O_RDWR)
os.write(fd, f'bind {target_pid}\n'.encode())
print(os.pread(fd, 512, 0).decode())
# 在目标程序仍有该 swap entry 时查询原始 swap offset，不减 1。
os.write(fd, f'lookup {swap_offset}\n'.encode())
print(os.pread(fd, 512, 0).decode())
# 结束测试才关闭；关闭会撤销自己的绑定。
os.close(fd)
```

`identity_status` 显示绑定、entry/alias 数、建立/释放事件数和分配错误数。
`identity` 查询返回最低 VPN 及别名数量；不据此代表后续多别名热度聚合规则。

实机验收重点：

1. 目标 PID 的 pagemap swap offset 与查询返回的 VPN 对应；其他地址空间不混入。
2. bind 前已有换出页能补建，bind 后新增换出能由回调建立。
3. 重复换出换入以及观察到 PFN 改变后，entry 仍能关联到正确 VPN；逐 word 内容正确。
4. entry 最终释放并复用后不返回旧关联，alloc_errors 保持 0。
5. 控制 fd 关闭、活进程 swapoff、卸载生命周期正常，所有远端 Slot 归还，I/O 错误无新增。

阶段一编译通过不代表上述实机项目已完成；这些通过后再进入第二阶段。
