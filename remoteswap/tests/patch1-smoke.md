# 单/双节点连接测试（Patch 1）

本轮只测试模块装载、连接、容量发现和回滚，不启用远端 swap，不运行 Workload。
支持 1 或 2 个 Server；每端实际暴露容量为正的 8GiB 整数倍，总和必须等于配置。

这次只更新 Client 模块、配置和脚本，内核与 Server 无改动。如果之前已经启动了
包含 managed Frontswap 接口的新内核，无需再次安装内核。若不确定，可检查：

```bash
sudo grep -w -E 'frontswap_(register|unregister)_managed_ops' /proc/kallsyms
```

应存在两个函数。Client 构建如下：

```bash
make -C remoteswap/client -j12
```

## 配置和通用流程

编辑 `remoteswap/client/rswap_client.conf`，这是管理脚本的唯一配置来源。
环境变量不再覆盖配置，因此 `POOL_GIB=32 ./manage_rswap_client.sh install`
不再是容量异常测试的方法；请编辑文件或直接使用 insmod。

```bash
NR_SERVERS=2
POOL_GIB=16
CONNECT_TIMEOUT_MS=15000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"
SERVER1_PORT=9400
```

- 单节点只读取 SERVER0_*；SERVER1_* 可以保留、留空或省略。
- 两节点都必须连接成功，没有自动降级。
- POOL_GIB 是实际容量总和，不能填每端容量或大于实际容量的值。
- timeout 是每个 Session 完整初始化的期限，不是两个 Session 共享的总期限。
- install 会打印生效配置；status 显示已加载模块的实际参数。
- uninstall/status 不要求配置文件存在或有效。

测试前确认 SSH 和 RDMA 链路正常。若失败，停止并反馈。已有 rping 脚本参数是
IP 末段数字：

| Server 终端先执行 | Client 终端随后执行 |
| --- | --- |
| `bash /home/yxr/scripts/r_server.sh 13` | `bash /home/yxr/scripts/r_client.sh 13 11` |
| `bash /home/yxr/scripts/r_server.sh 20` | `bash /home/yxr/scripts/r_client.sh 20 11` |

仅需对参与本轮测试的节点执行，正常完成 10 次交换即可。rping 不替代模块测试。

在 Client 查看在线 CPU 数：

```bash
getconf _NPROCESSORS_ONLN
```

每个 Server 启动参数的核数都必须使用这个数，而不是各 Server 自己的核数。
保持 Client 的在线 CPU 集合不变。Server 的命令格式（把 `<N>` 换成上一步的数）为：

```text
cd /home/yxr/projects/hermit/remoteswap/server
./rswap-server <该Server的IP> 9400 <该Server的容量GiB> <N>
```

服务端容量参数严格填写 8 的整数倍。原 Server 会把非整数倍向下截断，Client
只能核对实际暴露的容量。等 Server 预分配完成再连接。

模块加载前 Client 必须没有活动 swap。由操作者明确停用指定的 SSD swap：

```bash
swapon --show
sudo swapoff /swap.img
swapon --show
```

若 swap 已停用，不重复执行 swapoff。若 swapoff 失败，停止本轮测试。
脚本不会创建/删除/格式化 swapfile，也不会自动 swapoff。

每个成功用例都执行：

```bash
cd /mnt/sdb3/data/yxr/projects/hermit/remoteswap/client
./manage_rswap_client.sh install
./manage_rswap_client.sh status
sudo dmesg | tail -100
```

预期 install 返回 0，显示参与节点各自的实际容量和总池容量；没有 Oops/BUG/WARN。
然后卸载：

```bash
./manage_rswap_client.sh uninstall
./manage_rswap_client.sh status
sudo dmesg | tail -100
```

预期参与节点的资源已释放，模块未加载。每次重新连接前，重启参与的 Server 进程。
不要在模块加载期间执行 swapon；Patch 1 仍会拒绝它。

## T1：单节点 8GiB

只在 node04（10.0.0.13）启动 8GiB 的 Server。配置：

```bash
NR_SERVERS=1
POOL_GIB=8
CONNECT_TIMEOUT_MS=15000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
```

执行通用 install/status/uninstall 流程。预期只有 server=0 连接日志：

```text
server=0 ready ... capacity=8 GiB regions=1
pool ready servers=1 total=8 GiB (connection-only)
```

不应尝试连接另一个节点。可选：把 SERVER0_IP 改成 10.0.0.20，并在 server02
启动新的 8GiB Server，再做一轮；任何物理节点都可作为单节点的逻辑 Server 0。

## T2：双节点 8+8GiB

node04、server02 各启动新的 8GiB Server。使用本文开头的双节点配置。
执行通用流程。预期两条 `capacity=8 GiB regions=1`，以及：

```text
pool ready servers=2 total=16 GiB (connection-only)
```

## T3：双节点不等容量 8+16GiB

node04 启动 8GiB，server02 启动 16GiB，仍使用 Client 的同一个在线 CPU 数。
先确认 Server 机器有足够可用内存。配置为 NR_SERVERS=2、POOL_GIB=24，endpoint
保持 node04 在 SERVER0、server02 在 SERVER1。执行通用流程。预期：

```text
server=0 ready ... capacity=8 GiB regions=1
server=1 ready ... capacity=16 GiB regions=2
pool ready servers=2 total=24 GiB (connection-only)
```

## T4：总容量不匹配、回滚与重试

两台 Server 仍分别提供 8 和 16GiB，均重新启动。把配置的 POOL_GIB 改成 32，执行：

```bash
./manage_rswap_client.sh install
echo "exit_code=$?"
./manage_rswap_client.sh status
sudo dmesg | tail -100
```

预期返回非零，模块未加载；两端连接成功后才检查总和，因此可能看到两端 ready：

```text
capacity mismatch: configured=32 GiB discovered=24 GiB servers=2
initialization failed (-28); rolling back 2 selected session(s)
```

之后应有资源释放日志。insmod 显示 `No space left on device` 是 ENOSPC 的通用提示，
具体含义看上述容量日志。不要用 rmmod 修复失败插入。

把 POOL_GIB 恢复 24，重启两台 Server，成功 install/uninstall 一次才算回滚验证通过。

若要复测断连回滚：NR_SERVERS=2 时只启动 SERVER0，预期第二端失败后整体回滚；
NR_SERVERS=1 时只需要所选节点，另一个节点停止不会影响连接。

## 测试结束和记录

确认模块已卸载后，恢复原来的 SSD swap：

```bash
sudo swapon /swap.img
swapon --show
```

保存每轮配置、退出码和本轮 Client/Server 日志，不清空全局 dmesg。初始化等待的
期限不等于驱动清理的硬性时间保证；若明显卡住，停止其他测试并反馈日志。
本轮不做网卡热拔插、页面读写或掉线数据恢复测试。

后续数据路径将按已确认的规则实现不等容量映射：共同容量交错，较大节点剩余容量
接逻辑池尾段。该映射尚未在本轮实现。
