# Patch 3 核心硬件测试流程

按顺序执行：部署 → 单节点 → 双节点与生命周期 → 三项优化 → 不等容量尾段。
先完成前四节，再继续后面的用例；不用重跑 Patch 1/2 的全部连接异常组合。
本文没有任何已经执行硬件测试的含义；需要操作者在宿主机上执行 sudo 命令。

## 0. 约定与停止条件

- Client 仓库路径假定为 `/mnt/sdb3/data/yxr/projects/hermit`。
- Server 0：`10.0.0.13:9400`，8 GiB；Server 1：`10.0.0.20:9400`，16 GiB。
- 当前 Client 为 48 个在线 CPU（0–47）。Server 命令最后的 48 必须使用测试时的
  **Client 在线核数**；运行期间不改变在线 CPU 集合。
- 本文使用已有合法 `/swap.img`（8 GiB），不重新格式化。它足以完成基础测试，
  无法覆盖双节点 16 GiB 以后的尾段。不要把占用 8 GiB swap 当成尾段测试通过。
- 只使用一个活动 swap area。不要执行 `swapoff -a`、强制卸载 `rmmod -f`，
  或在还有远端数据时重启 Server／卸载 Client。
- 除专门验证“卸载应被拒绝”外，命令非零退出、内容 FAIL、进程意外退出、
  新的 WC/DMA 错误、Oops/BUG/WARN、持续卡住时，停止后续步骤并保存日志反馈。
  不要反复 swapon/swapoff 或重启 Server 来尝试恢复。

本文用三个 Client 终端：A 运行交互校验程序，B 操作／观察，C 收集 RDMA trace。
每个终端需要用变量时，先执行以下公共设置：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
P3_RUN=/tmp/hermit-p3
P3_CG=/sys/fs/cgroup/memory/hermit-p3
mkdir -p "$P3_RUN"
```

每次完整重测前，另存旧日志；下文相同文件名会覆盖旧结果。

## 1. 安装匹配的新内核和 Client

编译产物已经准备好。需要重编译时，在 Client 仓库根目录执行：

```bash
make -C linux-5.14-rc5 -j12 bzImage modules
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
```

预期均成功；产物为 `linux-5.14-rc5/arch/x86/boot/bzImage` 和
`remoteswap/client/rswap-client.ko`。无需重新复制 config、修改内核编号或名称。

沿用仓库 README 的安装方式。安装会覆盖同名内核文件：先用你现有的备份方式保留
当前 `/boot` 中对应版本的镜像、initrd、System.map、config，以及
`/lib/modules/5.14.0-rc5-hermit-syscall-v2`；确认另一个可启动内核可用于回退。
随后执行：

```bash
sudo make -C linux-5.14-rc5 LOCALVERSION= INSTALL_MOD_STRIP=1 modules_install
sudo make -C linux-5.14-rc5 LOCALVERSION= install
```

安装／initramfs 生成成功后，通过原来的启动方式重启到该 Hermit 内核。
不要为本轮重新安装 OFED。重启后检查：

```bash
uname -r
grep -w frontswap_register_managed_ops_v2 /proc/kallsyms
getconf _NPROCESSORS_ONLN
cat /sys/devices/system/cpu/online
swapon --show
```

预期：release 仍为 `5.14.0-rc5-hermit-syscall-v2`，存在新 v2 注册符号，CPU
仍连续编号。**只有 uname 相同不算成功**；v2 符号不存在时不要加载模块。
若 OFED／HCA 在新内核下无法正常工作，也先停止反馈。

## 2. 一次性准备：日志、基础开关、cgroup、校验程序

先执行第 0 节公共设置。保存当前状态及原 Client 配置：

```bash
cp remoteswap/client/rswap_client.conf "$P3_RUN/rswap_client.conf.before"
sudo dmesg > "$P3_RUN/dmesg.before"
swapon --show > "$P3_RUN/swaps.before"
```

确认宿主机 memory cgroup 可写。当前会话看到的挂载为只读，需在真正可写的
Client 宿主机环境执行，不能假设加 sudo 就能写入只读挂载：

```bash
findmnt -T /sys/fs/cgroup/memory
sudo mkdir "$P3_CG"
ls "$P3_CG"/memory.limit_in_bytes "$P3_CG"/memory.memsw.limit_in_bytes
```

预期目录创建成功且两个 limit 文件存在。本文采用当前机器的 cgroup-v1；若不符合，
停止并反馈挂载情况，不自行改成 cgroup-v2 命令或重挂载系统目录。

设置 2 GiB 物理内存限制，6 GiB 内存＋swap 限制：

```bash
echo 2147483648 | sudo tee "$P3_CG/memory.limit_in_bytes"
echo 6442450944 | sudo tee "$P3_CG/memory.memsw.limit_in_bytes"
echo 100 | sudo tee "$P3_CG/memory.swappiness"
```

关闭 Hermit 优化，固定预读窗口为 1；保存原值供测试后恢复：

```bash
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    sudo cat "/sys/kernel/debug/hermit/$flag" > "$P3_RUN/$flag.before"
    echo N | sudo tee "/sys/kernel/debug/hermit/$flag"
done
cat /sys/kernel/mm/swap/readahead_win > "$P3_RUN/readahead_win.before"
cat /proc/sys/vm/page-cluster > "$P3_RUN/page-cluster.before"
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
sudo cat /sys/kernel/debug/tracing/tracing_on > "$P3_RUN/tracing_on.before"
```

预期文件均存在且写入成功。不要修改 `reclaim_mode` 等其他参数。
校验程序自行对其匿名映射使用 `MADV_NOHUGEPAGE`，不要求修改全局 THP 策略。

编译附带的用户态校验程序：

```bash
gcc -std=gnu11 -O2 -Wall -Wextra -Werror remoteswap/tests/swap_pattern.c -o "$P3_RUN/swap_pattern"
```

它逐个 4-KiB 页、逐个 8-byte word 校验，数据包含进程 seed、页号、页内位置及版本。
每个版本写完后打印 `READY generation=N` 并等待 ENTER。按一次 ENTER 即完整校验，
打印 `VERIFIED`；若还有下一轮，改写为下一版本并再次暂停。最后应打印 `PASS` 并返回 0。
**EOF、Ctrl-C、被 OOM kill 或只到 READY 都不算通过。**

## 3. T1：单节点基础换页

### 3.1 Server 与 Client

在 Server 0 自己的仓库 `remoteswap/server` 目录启动，末尾核数按第 1 节结果替换：

```bash
./rswap-server 10.0.0.13 9400 8 48
```

等预分配完成，保留 Server 输出。编辑 Client 的
`remoteswap/client/rswap_client.conf`，将 `NR_SERVERS=1`、`POOL_GIB=8`；
Server 0 地址仍为 `10.0.0.13:9400`。其他原配置保留，脚本不读取环境变量覆盖值。

Client 先检查可用内存足够读回原 SSD swap，再停用明确选定的原 swap：

```bash
free -h
sudo swapoff /swap.img
swapon --show
```

原本没有活动 swap 时跳过 swapoff。预期最后没有活动 swap；若还有其他 area，
先停止处理，不带着它们加载模块。

```bash
bash remoteswap/client/manage_rswap_client.sh install
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg | tail -100
sudo swapon /swap.img
swapon --show
```

预期：节点 0 `capacity=8 GiB regions=1`；
`pool ready servers=1 total=8 GiB (4-KiB swap ready)`；
`client ready; 4-KiB swap enabled for one swap area`；swapon 成功。
不能再按 Patch 2 文档期待 swapon 被拒绝。

### 3.2 触发换页并检查内容

终端 A：

```bash
set -o pipefail
sudo bash -c 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 2' bash "$P3_CG" "$P3_RUN/swap_pattern" | tee "$P3_RUN/t1-pattern.log"
echo "workload_exit=$?"
```

第一次看到 `READY generation=0` 时暂不按 ENTER，在终端 B 查看：

```bash
cat "$P3_CG/memory.usage_in_bytes"
grep -E '^(swap|total_swap|pgmajfault|total_pgmajfault) ' "$P3_CG/memory.stat"
swapon --show
```

预期：物理内存大致受 2 GiB 限制，**cgroup swap 明显非零**（通常 GiB 量级），
系统 `/swap.img` 的 Used 增加。若 swap 为零，这轮没有验证远端换页。
在 A 按一次 ENTER，等待 `VERIFIED generation=0` 和 `READY generation=1`；
再按一次 ENTER，预期 `VERIFIED generation=1`、最终 `PASS`、退出码 0。

### 3.3 T1 收尾

程序正常退出后执行：

```bash
sudo swapoff /swap.img
bash remoteswap/client/manage_rswap_client.sh uninstall
sudo dmesg > "$P3_RUN/t1-dmesg.after"
```

预期 swapoff、卸载成功，无新增相关错误。下一轮重连前重启参与的 Server 进程。

## 4. T2：双节点、活进程 swapoff 与再次 swapon

### 4.1 重新连接并采集实际 I/O

两端各自启动全新的 Server 进程：

```bash
# Server 0
./rswap-server 10.0.0.13 9400 8 48
```

```bash
# Server 1
./rswap-server 10.0.0.20 9400 16 48
```

Client 配置改回 `NR_SERVERS=2`、`POOL_GIB=24`，地址与原配置一致。
重复 T1 的 install/status/swapon，预期发现 8 GiB＋16 GiB，
`pool ready servers=2 total=24 GiB (4-KiB swap ready)`。

在独立终端 C（先执行第 0 节公共设置），开启短时 trace 收集，再去 A 运行 workload：

```bash
echo 1 | sudo tee /sys/kernel/debug/tracing/tracing_on
sudo awk 'match($0, /server: [01] type: [012]/) {
    key = substr($0, RSTART, RLENGTH)
    if (seen[key]++ < 8) { print; fflush() }
}' /sys/kernel/debug/tracing/trace_pipe | tee "$P3_RUN/t2-rdma.trace"
```

这会占住 C。完成一轮写入和校验后在 **C** 按 Ctrl-C 停止采集；不要在 A 中断 workload。
每个 Server／type 只保存前 8 条样本，避免大量 trace 占满日志空间；后面的计数是样本数，
不是总 I/O 数。不清空全局 dmesg。此方法适用于没有其他 tracing 使用者的测试机。

### 4.2 双节点内容校验与保持活页

A 执行（3 个版本）：

```bash
set -o pipefail
sudo bash -c 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 3' bash "$P3_CG" "$P3_RUN/swap_pattern" | tee "$P3_RUN/t2-pattern.log"
echo "workload_exit=$?"
```

按 T1 方法确认有实际换页。分别在 `READY generation=0` 和 `READY generation=1`
时各按一次 ENTER。等待 `READY generation=2` 后暂停，**不要按第三次 ENTER**。

停止 C 的 trace 采集，在 B 检查：

```bash
grep -E 'server: [01] type: [012]' "$P3_RUN/t2-rdma.trace" | head -30
for server in 0 1; do
    for type in 0 1; do
        grep -c "server: $server type: $type " "$P3_RUN/t2-rdma.trace"
    done
done
```

预期两个 Server 的 type=0（Write）、type=1（同步 Read）都出现；type=2 是异步 Read。
基线不要求 type=2。若没有捕获到读写，先检查采集窗口是否覆盖校验阶段，不能仅凭连接成功判通过。

### 4.3 卸载保护和活进程读回

保持 A 停在 `READY generation=2`，先在 B 再确认 cgroup swap 非零，再执行：

```bash
sudo rmmod rswap_client
echo "rmmod_exit=$?"
test -d /sys/module/rswap_client && echo "module remains loaded"
```

预期 rmmod 非零，提示模块在使用中，模块仍在。此处直接使用普通 rmmod；
管理脚本自身会拒绝活动 swap，不能用它证明内核模块引用保护有效。

为活页读回留足空间：**先提高 memsw 上限，再提高物理内存上限**，然后 swapoff：

```bash
echo 12884901888 | sudo tee "$P3_CG/memory.memsw.limit_in_bytes"
echo 8589934592 | sudo tee "$P3_CG/memory.limit_in_bytes"
sudo swapoff /swap.img
swapon --show
```

预期 swapoff 返回 0，列表为空，A 仍存活，未重启 Server，Client 模块仍加载。
现在回到 A 按第三次 ENTER。预期 `VERIFIED generation=2`、最终 `PASS`，退出码 0。
这一步同时验证 swapoff 读回后的内容，而不是仅检查命令退出成功。

### 4.4 同一连接再次 swapon

不卸载模块、不重启 Server，重新收紧 cgroup：

```bash
echo 2147483648 | sudo tee "$P3_CG/memory.limit_in_bytes"
echo 6442450944 | sudo tee "$P3_CG/memory.memsw.limit_in_bytes"
sudo swapon /swap.img
```

运行 T1 的 4096 MiB／2 轮命令，日志另存为 `t2-reopen-pattern.log`。
确认重新产生 cgroup swap，两个版本均通过。然后：

```bash
sudo swapoff /swap.img
bash remoteswap/client/manage_rswap_client.sh uninstall
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg > "$P3_RUN/t2-dmesg.after"
```

预期 swap 列表为空、模块未加载、资源正常释放。

## 5. T3：双节点上的必要优化回归

重启两端 Server 并重新 install/swapon。cgroup 使用前述 2 GiB／6 GiB 限制。
每小项独立运行 4096 MiB／2 轮内容校验并保存日志；只有前一项 PASS 后才进行下一项。
开关只在 workload 已退出时切换，不在运行中切换。

| 用例 | 相对基线的设置 | 预期 |
| --- | --- | --- |
| T3a 异步 Store | `batch_swapout=Y`、`batch_io=Y` | 多版本内容 PASS，写出与完成不会卡住 |
| T3b 异步预读 | 恢复上述两个为 N；`bypass_swapcache=Y`、`readahead_win=8`、`vm.page-cluster=3` | 内容 PASS，短 trace 出现 type=2 |
| T3c lazy poll | 窗口恢复 1；`bypass_swapcache=Y`、`lazy_poll=Y` | 内容 PASS，无页面锁卡住 |

写开关的方式为：

```bash
echo Y | sudo tee /sys/kernel/debug/hermit/batch_swapout
echo Y | sudo tee /sys/kernel/debug/hermit/batch_io
```

其他开关同理，窗口写 `/sys/kernel/mm/swap/readahead_win`。
T3b 另执行 `echo 3 | sudo tee /proc/sys/vm/page-cluster`。
当前 cache 路径的窗口固定为 1；需通过 bypass 路径实际计算预读窗口，且 page-cluster
不能为 0。修正后的这些设置才有机会触发异步 Read。若内容 PASS 但始终未捕获 type=2，
该项记录为“数据通过，异步路径未确认”，不要算异步验收通过。
仅开启 `batch_io` 或仅开启 `lazy_poll`，不能保证覆盖目标路径。
三项均无需开启 `speculative_io`、batch accounting、vaddr swapout 等其他优化。
并发项开始前将 bypass_swapcache、lazy_poll 恢复 N，窗口恢复 1。

再做一轮小规模并发：基线设置、同一 cgroup 内同时运行两个 **2048 MiB／2 轮**程序，
分别用不同的 CPU，例如在上述 exec 命令中改为
`exec taskset -c 0 "$2" 2048 2` 和 `exec taskset -c 1 "$2" 2048 2`，在两个终端分别操作。
两个进程应均有完整 PASS；总工作集仍约 4 GiB，无需把每个都扩大到 4 GiB。

`speculative_io=Y` 在当前 Hermit 中初始并不会立即激活实际路径，约 1000 万次相关调用后
才首次启用。此次短测不把它算作硬件通过；刚修复的锁路径已有实际函数的 24 组合逻辑回归。

结束后按 T1 收尾：workload 退出 → swapoff → 卸载；保留 `t3-*` 日志。

## 6. T4：不等容量尾段（单独执行一次）

完成基础项后再做，恢复全部优化为 N、窗口 1。当前 `/swap.img` 只有 8 GiB，
需要操作者另行准备一个合法、约 24 GiB 的测试 swap area；本文不提供对现有文件
执行 mkswap 的命令。不要格式化或覆盖当前 `/swap.img`。

1. 保持双节点 8＋16 GiB；无活动 swap 时加载 Client，只启用该较大的测试 area。
2. 将 cgroup memory 上限设为 4 GiB，memsw 上限设为 32 GiB；提高限制时先提高 memsw。
3. 运行 `swap_pattern 24576 2`（24 GiB 工作集，两个版本），按前文方式加入该 cgroup。
4. READY 时必须观察到**该 cgroup 的实际 swap 超过 16 GiB**；最好接近 20 GiB。
   仅“申请了超过 16 GiB 内存”不够。swap 超过 16 GiB 才能确认占用了尾段 offset。
5. 两轮逐页校验 PASS；程序结束后，swapoff 该测试 area，再卸载。

这确认共同交错段之外、Server 1 尾段的实际读写，不要求重复所有优化组合。
没有较大合法 swap area 时将 T4 记录为未执行，不能声称尾段硬件验收完成。

## 7. 恢复与反馈

确认所有校验进程已退出、测试 swap 已停用、Client 已卸载，再恢复：

```bash
cp "$P3_RUN/rswap_client.conf.before" remoteswap/client/rswap_client.conf
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    sudo tee "/sys/kernel/debug/hermit/$flag" < "$P3_RUN/$flag.before"
done
sudo tee /sys/kernel/mm/swap/readahead_win < "$P3_RUN/readahead_win.before"
sudo tee /proc/sys/vm/page-cluster < "$P3_RUN/page-cluster.before"
sudo tee /sys/kernel/debug/tracing/tracing_on < "$P3_RUN/tracing_on.before"
sudo rmdir "$P3_CG"
```

若原来 `/swap.img` 是活动的，确认模块已卸载后再 `sudo swapon /swap.img` 恢复 SSD swap。
原来没有活动 swap 则不额外启用。恢复原 tracing_on，结束 trace 采集终端。

反馈时按下表记录，并附对应日志；不需要发送整份 trace：

| 项目 | 需要反馈 |
| --- | --- |
| 环境 | uname、v2 符号、在线 CPU、Server 容量、实际 Client 配置 |
| T1 | 是否实际换页、两个 VERIFIED、最终 PASS、swapoff／卸载结果 |
| T2 | 两节点 type=0/1 计数、rmmod 被拒绝、活进程 swapoff 后 PASS、再次 swapon 后 PASS |
| T3 | 三项分别结果、异步 Read type=2 是否出现、双进程是否均 PASS |
| T4 | 实际最大 cgroup swap、两轮 PASS，或明确未执行 |
| 异常 | 第一处错误、相关 Client dmesg／Server 输出；进程是否仍存活 |

**现在先执行第 1–3 节。T1 成功后再做第 4 节，不需要一次跑完所有用例。**
