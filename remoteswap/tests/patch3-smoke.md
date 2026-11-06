# Patch 3 核心硬件测试流程

按顺序执行：部署 → 单节点 → 双节点与生命周期 → 必要的异步路径 → 收尾。
先完成前四节，再继续后面的用例；不用重跑 Patch 1/2 的全部连接异常组合。
本文没有任何已经执行硬件测试的含义；需要操作者在宿主机上执行 sudo 命令。

## 0. 约定与停止条件

- Client 仓库路径假定为 `/mnt/sdb3/data/yxr/projects/hermit`。
- Server 0：`10.0.0.13:9400`；Server 1：`10.0.0.20:9400`。
  单节点示例使用 8 GiB；双节点使用 8＋16 或 16＋8 GiB。
- 当前 Client 为 48 个在线 CPU（0–47）。Server 命令最后的 48 必须使用测试时的
  **Client 在线核数**；运行期间不改变在线 CPU 集合。
- T1 使用已有合法 `/swap.img`（8 GiB）；T2 起使用你已准备好的
  `/mnt/sdb4/yxr/projects/swap_24g.img`（24 GiB），均不重新格式化。
  本轮小工作集不验收 16 GiB 以后的尾段。
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

设置 2 GiB 物理内存限制；解除旧的 memory＋swap 上限，避免已知记账 bug 触发配额 OOM。
本轮不验证 cgroup swap 用量、配额或解除记账，只使用物理内存限制制造换页：

```bash
echo -1 | sudo tee "$P3_CG/memory.memsw.limit_in_bytes"
echo 2147483648 | sudo tee "$P3_CG/memory.limit_in_bytes"
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
swapon --show
```

预期：物理内存大致受 2 GiB 限制，系统 `/swap.img` 的 Used 相比 workload 启动前
明显增加（通常 GiB 量级）。只看系统实际 swap 占用，不读取 cgroup 的 swap 统计。
系统实际 swap 用量没有增加时，这轮不能算验证了远端换页。
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

### 4.0 本轮范围与当前进度

**目标／内容：**验证两个 Session 的真实读写、重复改写后的内容、活动 swap 的卸载保护、
活进程 swapoff 读回，以及同一组 Session 再次 swapon。只用 cgroup 的物理内存限制制造压力。

**固定路径：**Client 仓库为 `/mnt/sdb3/data/yxr/projects/hermit`，日志目录为
`/tmp/hermit-p3`，cgroup 为 `/sys/fs/cgroup/memory/hermit-p3`，测试 area 为你已准备好的
`/mnt/sdb4/yxr/projects/swap_24g.img`。后文命令使用绝对路径，不依赖另一个终端的变量。
Server 0/1 容量可以是 16＋8 或 8＋16 GiB，总和必须是 24 GiB。

**已完成的部分不重跑：**如果本轮已得到两个 `VERIFIED generation=0/1`，
A 正停在 `READY generation=2`，且同一轮采样为 `8/8/8/8`，直接从 **4.3** 继续。
不要重启 Server、重新加载模块或再启动一个 workload；保留 A 中的活页。

本轮不测试或依赖以下内容：`memory.stat` 的 swap/total_swap、memsw usage/failcnt、
cgroup swap 配额与引用回收。解除 memsw 上限仅用于绕过已知配额 OOM，不表示 bug 已修复。
物理内存限制和父级限制仍然可能导致 OOM；发生 Killed 时停止，不计为通过。

### 4.1 新开一轮时：确认双节点与实际 swap 已启用

**目标／内容：**确认不是仅连接了 24 GiB Server，而是 Client 真正启用了合法 swap area。
**仅在没有正在暂停／运行的校验进程时执行本节。**当前已到 generation=2 时跳过。

**步骤 1：在 Client 终端 B 准备 workload 和物理内存限制。**

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
mkdir -p /tmp/hermit-p3
sudo mkdir -p /sys/fs/cgroup/memory/hermit-p3
gcc -std=gnu11 -O2 -Wall -Wextra -Werror remoteswap/tests/swap_pattern.c -o /tmp/hermit-p3/swap_pattern
# 一次性解除旧 memsw 上限；不要在后面的用例中再设置 6/12 GiB 上限。
echo -1 | sudo tee /sys/fs/cgroup/memory/hermit-p3/memory.memsw.limit_in_bytes
echo 2147483648 | sudo tee /sys/fs/cgroup/memory/hermit-p3/memory.limit_in_bytes
echo 100 | sudo tee /sys/fs/cgroup/memory/hermit-p3/memory.swappiness
# 各项优化先关闭。batch_account 在所有用例中保持 N。
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    echo N | sudo tee "/sys/kernel/debug/hermit/$flag"
done
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
```

预期编译和写入成功。cgroup 使用当前机器的 v1；父级也应没有有限的 memsw 上限。
前面若已保存 `.before` 文件，保留它们，不要用本轮设置覆盖原值备份。

**步骤 2：检查连接状态。**

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg | grep 'rswap:' | tail -15
swapon --show --bytes --output NAME,SIZE,USED
```

若模块已正确连接两个节点，配置为 `nr_servers=2`、`rmsize=24`，直接执行步骤 3。
否则按以下冷启动流程；每条命令失败都停止，不继续下一条：

- 在无校验进程时，先 `swapon --show` 确认活动 area。仅停用其中明确的测试 area，
  使用实际路径，例如 `sudo swapoff /mnt/sdb4/yxr/projects/swap_24g.img`。
  若仍是原 SSD `/swap.img` 活动，则停用的是 `sudo swapoff /swap.img`。
- `swapon --show` 必须为空。旧模块若加载，执行
  `bash remoteswap/client/manage_rswap_client.sh uninstall`。
- 在两端各自的 `remoteswap/server` 目录启动全新的 Server 进程。以下采用 16＋8 GiB，
  最后的 48 是 Client 在线 CPU 数，测试时若改变则替换：

```bash
# 只在 Server 0 执行
./rswap-server 10.0.0.13 9400 16 48
```

```bash
# 只在 Server 1 执行
./rswap-server 10.0.0.20 9400 8 48
```

Client 的 `remoteswap/client/rswap_client.conf` 使用以下文件内容；已有配置一致则不修改。
这是配置文件内容，不是在终端设置环境变量：

```text
NR_SERVERS=2
POOL_GIB=24
CONNECT_TIMEOUT_MS=15000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"
SERVER1_PORT=9400
```

```bash
# 回到 Client 仓库目录执行
cd /mnt/sdb3/data/yxr/projects/hermit
bash remoteswap/client/manage_rswap_client.sh install
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg | grep 'rswap:' | tail -15
```

**步骤 3：启用正确的 swap area。**

```bash
if swapon --show=NAME --noheadings | grep -Fxq /mnt/sdb4/yxr/projects/swap_24g.img; then
    echo 'test swap already active'
else
    sudo swapon /mnt/sdb4/yxr/projects/swap_24g.img
fi
swapon --show --bytes --output NAME,SIZE,USED
```

**预期结果：**两个 Server 容量合计 24 GiB，日志有
`pool ready servers=2 total=24 GiB (4-KiB swap ready)`；系统只启用上述一个约 24 GiB 的 area。
**列表为空不能运行 workload：加载模块不会自动 swapon。**不创建或重新格式化文件。

### 4.2 双节点逐页内容与实际 I/O

**目标／内容：**验证两节点都完成 Write／同步 Read，以及同一批页面改写后仍能正确读回。

**步骤 1：在终端 C 启动采集，再去 A 运行 workload。**

```bash
mkdir -p /tmp/hermit-p3
echo 1 | sudo tee /sys/kernel/debug/tracing/tracing_on
sudo awk -v since="$(cut -d ' ' -f1 /proc/uptime)" '
match($0, /[0-9]+\.[0-9]+: /) {
    stamp = substr($0, RSTART, RLENGTH) + 0
    if (stamp < since) next
    if (match($0, /server: [01] type: [012]/)) {
        key = substr($0, RSTART, RLENGTH)
        if (seen[key]++ < 8) { print; fflush() }
    }
}' /sys/kernel/debug/tracing/trace_pipe | tee /tmp/hermit-p3/t2-rdma.trace
```

C 持续等待是正常的。每节点／类型最多保存 8 条，且只接收采集启动后的时间戳，避免旧单节点
记录混入。文件应立即创建，可在 B 用 `ls -l /tmp/hermit-p3/t2-rdma.trace` 确认；
如有 Permission denied 或 trace WARNING，停止采集并反馈，不把它当成采集成功。
此采集方式只在没有其他 tracing 使用者的测试机使用。

**步骤 2：终端 A 写入 4 GiB，运行三个版本。**

```bash
set -o pipefail
sudo bash -ceu 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 3' bash /sys/fs/cgroup/memory/hermit-p3 /tmp/hermit-p3/swap_pattern 2>&1 | tee /tmp/hermit-p3/t2-pattern.log
echo "workload_exit=$?"
```

A 到达 `READY generation=0` 后先暂停，在 B 查看：

```bash
cat /sys/fs/cgroup/memory/hermit-p3/memory.usage_in_bytes
swapon --show --bytes --output NAME,SIZE,USED
```

预期物理内存约受 2 GiB 限制，系统该 area 的 USED 比 workload 前增加约 GiB 量级。
不读取 cgroup swap 统计。若没有实际换页，不算本项通过。

**步骤 3：在 A 分别按两次 ENTER，每次等待下一条 READY 后再操作。**

预期依次得到：

```text
VERIFIED generation=0 ...
READY generation=1 ...
VERIFIED generation=1 ...
READY generation=2 ...
```

**在 generation=2 保持暂停，不按第三次 ENTER。**

**步骤 4：在 C 按 Ctrl-C 停止采集，在 B 检查带标签的样本数。**

```bash
for server in 0 1; do
    for type in 0 1; do
        printf 'server=%s type=%s samples=' "$server" "$type"
        grep -c "server: $server type: $type " /tmp/hermit-p3/t2-rdma.trace
    done
done
```

**预期结果：**四类样本数均大于 0，通常均为 8；type=0 是 Write、type=1 是同步 Read。
8 是采样上限，不是总请求数。基线不要求 type=2。内容检查和四类实际 I/O 都满足才算本项通过；
当前 A 尚未退出，因此最终 PASS 留到下一项的 swapoff 后检查。

### 4.3 活动 swap 卸载保护与活进程 swapoff

**目标／内容：**验证活动 area 能阻止正常卸载，并验证进程仍存活时 swapoff 读回的数据。
前提是 A 仍暂停在本轮 `READY generation=2`，系统实际 USED 仍明显非零。

**步骤 1：终端 B 尝试普通卸载。**

```bash
sudo rmmod rswap_client
echo "rmmod_exit=$?"
test -d /sys/module/rswap_client && echo 'module remains loaded'
```

预期 rmmod 非零、提示正在使用，模块仍在。这是唯一一项预期失败的命令。
不要使用管理脚本代替它，也不要加 `-f`。

**步骤 2：仅提高物理内存限制，为 4 GiB 活页读回留足空间。memsw 不设有限上限。**

```bash
echo 8589934592 | sudo tee /sys/fs/cgroup/memory/hermit-p3/memory.limit_in_bytes
sudo swapoff /mnt/sdb4/yxr/projects/swap_24g.img
echo "swapoff_exit=$?"
swapon --show
```

预期 swapoff 返回 0，swap 列表为空；A 未退出，Server 和 Client 模块均未重启。

**步骤 3：回到 A 按第三次 ENTER。**

**预期结果：**`VERIFIED generation=2`、最终 `PASS`、`workload_exit=0`。
这才完成活进程读回内容的验证。任何 Killed、FAIL、非零退出都不能继续判通过。

### 4.4 同一连接再次 swapon

**目标／内容：**验证同一组 Session 的 area 关闭后可以重新启用并进行新的真实读写。
前提是上一程序已正常 PASS 退出，模块仍加载，swap 列表为空。

**步骤 1：终端 B 恢复 2 GiB 物理限制，重新启用 area。**

```bash
echo 2147483648 | sudo tee /sys/fs/cgroup/memory/hermit-p3/memory.limit_in_bytes
sudo swapon /mnt/sdb4/yxr/projects/swap_24g.img
swapon --show --bytes --output NAME,SIZE,USED
```

预期启用成功。不卸载模块、不重启 Server、不重新设置 memsw。

**步骤 2：终端 A 运行一个新版本。**

```bash
set -o pipefail
sudo bash -ceu 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 1' bash /sys/fs/cgroup/memory/hermit-p3 /tmp/hermit-p3/swap_pattern 2>&1 | tee /tmp/hermit-p3/t2-reopen-pattern.log
echo "workload_exit=$?"
```

READY 时在 B 用 `swapon --show --bytes --output NAME,SIZE,USED` 确认 USED 增加，
然后在 A 按一次 ENTER。

**预期结果：**`VERIFIED generation=0`、最终 `PASS`、退出码 0。
完成后可以直接进入第 5 节；保留模块、连接和活动 swap，避免无必要的 Server 重启。
如本次只完成基础验收，直接按第 6 节收尾。

## 5. T3：必要的异步路径回归

三项分别运行，均使用双节点、同一个 24 GiB area、2 GiB 物理限制和无限 memsw。
每项开始前，上一 workload 必须已正常退出。`batch_account`、`speculative_io` 等其余开关保持 N。
不做性能比较、不测 cgroup swap 统计、不做完整优化组合矩阵。

### 5.1 T3a：异步 Store 与多 Session 完成收敛

**目标／内容：**验证 `store_on_core` 路径、两节点写完成收敛，以及改写后的页面内容。

**步骤 1：终端 B 检查 swap 已启用，开启批量换出与异步 I/O。**

```bash
swapon --show --bytes --output NAME,SIZE,USED
echo Y | sudo tee /sys/kernel/debug/hermit/batch_swapout
echo Y | sudo tee /sys/kernel/debug/hermit/batch_io
echo N | sudo tee /sys/kernel/debug/hermit/batch_account
echo N | sudo tee /sys/kernel/debug/hermit/bypass_swapcache
echo N | sudo tee /sys/kernel/debug/hermit/lazy_poll
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
```

预期 area 仍活动、设置成功。不能只开启 batch_io 而不启用 batch_swapout。

**步骤 2：终端 A 运行两轮。**

```bash
set -o pipefail
sudo bash -ceu 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 2' bash /sys/fs/cgroup/memory/hermit-p3 /tmp/hermit-p3/swap_pattern 2>&1 | tee /tmp/hermit-p3/t3a-pattern.log
echo "workload_exit=$?"
```

每次 READY 时按一次 ENTER，等待下一条 READY 后再操作。

**预期结果：**实际系统 swap USED 增加，两个版本均 VERIFIED、最终 PASS、退出码 0；
提交／校验能够结束，没有 WC/DMA 错误或页面锁卡住。仅检查正确性，不要求带宽或时延阈值。

### 5.2 T3b：SOFTIRQ 异步 Read／预读

**目标／内容：**验证实际进入 type=2 的异步 Read 完成路径，并检查预读页面内容。

**步骤 1：终端 B 恢复同步换出，开启当前代码能够触发预读的配置。**

```bash
echo N | sudo tee /sys/kernel/debug/hermit/batch_swapout
echo N | sudo tee /sys/kernel/debug/hermit/batch_io
echo Y | sudo tee /sys/kernel/debug/hermit/bypass_swapcache
echo N | sudo tee /sys/kernel/debug/hermit/lazy_poll
echo 3 | sudo tee /proc/sys/vm/page-cluster
echo 8 | sudo tee /sys/kernel/mm/swap/readahead_win
swapon --show --bytes --output NAME,SIZE,USED
```

当前 cache 路径窗口固定为 1；需要 bypass 路径计算预读窗口，且 page-cluster 非零。

**步骤 2：终端 C 开始本项采集。**

```bash
mkdir -p /tmp/hermit-p3
echo 1 | sudo tee /sys/kernel/debug/tracing/tracing_on
sudo awk -v since="$(cut -d ' ' -f1 /proc/uptime)" '
match($0, /[0-9]+\.[0-9]+: /) {
    stamp = substr($0, RSTART, RLENGTH) + 0
    if (stamp < since) next
    if (match($0, /server: [01] type: 2/)) {
        key = substr($0, RSTART, RLENGTH)
        if (seen[key]++ < 8) { print; fflush() }
    }
}' /sys/kernel/debug/tracing/trace_pipe | tee /tmp/hermit-p3/t3b-rdma.trace
```

**步骤 3：终端 A 运行一轮，到 READY 后按一次 ENTER 完成校验。**

```bash
set -o pipefail
sudo bash -ceu 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 1' bash /sys/fs/cgroup/memory/hermit-p3 /tmp/hermit-p3/swap_pattern 2>&1 | tee /tmp/hermit-p3/t3b-pattern.log
echo "workload_exit=$?"
```

程序 PASS 后在 C 按 Ctrl-C 停止采集，在 B 检查：

```bash
grep 'type: 2 ' /tmp/hermit-p3/t3b-rdma.trace
```

**预期结果：**内容 PASS、退出码 0，并捕获本项 type=2 样本。没有 type=2 时记录
“内容通过，异步 Read 路径未确认”，不算该路径验收通过，也不据此宣称数据错误。

### 5.3 T3c：lazy poll

**目标／内容：**验证需求页延后 polling 时，页面在读取完成前不会被错误使用或解锁。

**步骤 1：终端 B 关闭预读窗口、保持 bypass，开启 lazy poll。**

```bash
echo N | sudo tee /sys/kernel/debug/hermit/batch_swapout
echo N | sudo tee /sys/kernel/debug/hermit/batch_io
echo Y | sudo tee /sys/kernel/debug/hermit/bypass_swapcache
echo Y | sudo tee /sys/kernel/debug/hermit/lazy_poll
echo 1 | sudo tee /sys/kernel/mm/swap/readahead_win
swapon --show --bytes --output NAME,SIZE,USED
```

**步骤 2：终端 A 运行一轮，到 READY 后按一次 ENTER。**

```bash
set -o pipefail
sudo bash -ceu 'echo $$ > "$1/cgroup.procs"; exec "$2" 4096 1' bash /sys/fs/cgroup/memory/hermit-p3 /tmp/hermit-p3/swap_pattern 2>&1 | tee /tmp/hermit-p3/t3c-pattern.log
echo "workload_exit=$?"
```

**预期结果：**实际系统 swap USED 增加，完整 VERIFIED、PASS、退出码 0，无持续锁等待。
仅开启 lazy_poll 而不开 bypass 不能保证进入目标路径。

本轮不额外安排双进程压力矩阵、大容量尾段、主动断连、推测读取和 cgroup swap 记账测试。
尾段的实际硬件覆盖仍标记“未验收”；不能因双节点小工作集 PASS 就宣称全部 24 GiB 已验证。

## 6. 收尾与需要反馈的结果

**目标／内容：**确认正常退出后，area 可以停用、模块可以卸载，恢复原机器设置。
前提是所有校验程序已正常退出；如仍有活页或出现异常，先保存日志，不直接卸载或重启 Server。

**步骤 1：终端 B 停用测试 area，再卸载模块。**

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
sudo swapoff /mnt/sdb4/yxr/projects/swap_24g.img
bash remoteswap/client/manage_rswap_client.sh uninstall
bash remoteswap/client/manage_rswap_client.sh status
sudo dmesg > /tmp/hermit-p3/t2-t3-dmesg.after
```

**预期结果：**swapoff、卸载都成功，状态显示模块未加载且没有活动 swap，无新增相关内核错误。

**步骤 2：若第 2 节已保存原设置，则恢复；缺少备份的项跳过，不猜测原值。**

```bash
cd /mnt/sdb3/data/yxr/projects/hermit
if [ -f /tmp/hermit-p3/rswap_client.conf.before ]; then
    cp /tmp/hermit-p3/rswap_client.conf.before remoteswap/client/rswap_client.conf
fi
for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account vaddr_swapout speculative_io speculative_lock lazy_poll apt_reclaim; do
    if [ -f "/tmp/hermit-p3/$flag.before" ]; then
        sudo tee "/sys/kernel/debug/hermit/$flag" < "/tmp/hermit-p3/$flag.before"
    fi
done
if [ -f /tmp/hermit-p3/readahead_win.before ]; then
    sudo tee /sys/kernel/mm/swap/readahead_win < /tmp/hermit-p3/readahead_win.before
fi
if [ -f /tmp/hermit-p3/page-cluster.before ]; then
    sudo tee /proc/sys/vm/page-cluster < /tmp/hermit-p3/page-cluster.before
fi
if [ -f /tmp/hermit-p3/tracing_on.before ]; then
    sudo tee /sys/kernel/debug/tracing/tracing_on < /tmp/hermit-p3/tracing_on.before
fi
```

不以 cgroup swap 记账是否清零、cgroup 删除是否成功作为本轮验收项。
保留已知问题的 cgroup，后续单独处理；不在收尾时重新设有限 memsw 上限。
若测试前原 `/swap.img` 是活动的，确认模块已卸载后才执行 `sudo swapon /swap.img` 恢复 SSD swap。
不要再启用远端测试文件来代替原 SSD area。原来没有活动 swap 则不额外启用。

**反馈表：**只需核心结果和第一处异常，不需要整份大 trace。

| 项目 | 通过标准／反馈 |
| --- | --- |
| 双节点内容与 I/O | generation 0/1 VERIFIED；本轮两节点 type 0/1 样本均非零 |
| 卸载保护 | 普通 rmmod 被拒绝，模块仍加载 |
| 活页 swapoff | swapoff 成功，随后 generation 2 VERIFIED、最终 PASS／退出 0 |
| 再次 swapon | 同一连接重新启用成功，新进程 PASS／退出 0 |
| 异步 Store | 两个版本 VERIFIED，PASS／退出 0，无新增完成错误 |
| 异步 Read | PASS／退出 0，且本项捕获 type=2；否则标记未确认 |
| lazy poll | PASS／退出 0，无持续锁等待 |
| 正常收尾 | swapoff／卸载成功，相关 dmesg 无新增错误 |

日志在 `/tmp/hermit-p3/t2-*.log`、`t3*-pattern.log`、`t2-rdma.trace`、`t3b-rdma.trace`
及 `t2-t3-dmesg.after`。cgroup swap 统计正确性和大容量尾段均不在本轮通过结论内。
