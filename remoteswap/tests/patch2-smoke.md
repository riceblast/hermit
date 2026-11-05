# Patch 2 最小测试流程

本轮验证静态映射计算和连接／卸载回归，仍不启用远端换页，不运行 workload。
内核和 Server 没有修改，不需要为 Patch 2 重装内核或重启 Client。

## 已完成的本地检查

在仓库根目录可复现，均不需要 sudo：

```bash
python3 remoteswap/tests/test_patch2_mapping.py
make -C remoteswap/client -j12
git diff --check
```

映射测试直接编译生产代码中的 `rswap_map_page()`，覆盖单节点、双节点等／不等容量、
不等容量两种顺序、共同段／尾段边界、节点内 8GiB 边界、末页与越界。
它不执行内核中的完整 Chunk 解析、WR 构造或 RDMA I/O；硬件连接成功也不能替代这些
数据路径的验证。真实页面内容、完成和生命周期验证在 Patch 3 进行。

## 硬件测试前提

- 使用已包含 Patch 1 managed Frontswap 接口的内核。
- 确认参与节点的 SSH／RDMA 链路正常；异常时停止并反馈。
- 每个 Server 使用 Client 在线 CPU 数作为最后一个启动参数，不用 Server 自身核数。
- 当前 Client 检查值为 48 个 CPU、编号 0–47；测试前重新读取，保持在线集合不变且连续。
- 编辑 `client/rswap_client.conf`，它是管理脚本唯一配置来源。容量填写正的 8GiB 整数倍，
  `POOL_GIB` 等于实际暴露容量总和。记录原配置，测试结束后按需要恢复。

Client 检查：

```bash
getconf _NPROCESSORS_ONLN
cat /sys/devices/system/cpu/online
swapon --show
```

装载前必须没有活动 swap。若已有 SSD swap，仅由操作者停用确认过的指定 area；
例如实际使用 `/swap.img` 时执行 `sudo swapoff /swap.img`，再检查 `swapon --show`。
若本来没有活动 swap，无需执行 swapoff。swapoff 失败时停止。

## T1：单节点 8GiB

只启动所选的节点 0。以下 Server 命令在各自仓库的 `remoteswap/server` 目录执行；
末尾 48 应替换为刚读取的 Client 在线 CPU 数：

```bash
./rswap-server 10.0.0.13 9400 8 48
```

等预分配完成，再配置 Client：

```bash
NR_SERVERS=1
POOL_GIB=8
CONNECT_TIMEOUT_MS=15000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
```

节点 1 配置可保留；本用例不应尝试连接节点 1。
随后执行下文的公共装卸流程。

## T2：双节点 8+16GiB

重新启动参与本轮测试的 Server 进程，确认各端可用内存足够。
两端分别执行：

```bash
./rswap-server 10.0.0.13 9400 8 48
```

```bash
./rswap-server 10.0.0.20 9400 16 48
```

配置 Client：

```bash
NR_SERVERS=2
POOL_GIB=24
CONNECT_TIMEOUT_MS=15000
SERVER0_IP="10.0.0.13"
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"
SERVER1_PORT=9400
```

随后执行公共装卸流程，并在模块已加载时完成一次下文的 swapon 拒绝检查。
本用例只确认新模块能发现并接收不等容量拓扑，不验证实际尾段页面读写。

## 公共装卸流程

在 Client 仓库的 `remoteswap/client` 目录执行：

```bash
./manage_rswap_client.sh install
./manage_rswap_client.sh status
sudo dmesg | tail -100
```

预期 install 成功，status 参数与本轮配置一致，没有 Oops、BUG、WARN 或异常卡住。
T1 应有节点 0 的 `capacity=8 GiB regions=1`；T2 应分别为 8GiB/1 Region、16GiB/2 Regions。
总池日志分别为：

```text
pool ready servers=1 total=8 GiB (static mapping ready; swapon disabled)
```

```text
pool ready servers=2 total=24 GiB (static mapping ready; swapon disabled)
```

模块启动还应打印：

```text
client ready; swapon remains disabled pending Patch 3
```

完成本轮检查后卸载：

```bash
./manage_rswap_client.sh uninstall
./manage_rswap_client.sh status
sudo dmesg | tail -100
```

预期参与节点各有资源释放日志、模块未加载、没有活动 swap。
每次重新连接前重启参与的 Server 进程；现有 Server 最终断连后会退出。

## 一次 swapon 拒绝检查

在 T2 模块已加载、尚未卸载时执行。仅使用原来已验证可正常启用的合法 swapfile／分区，
不要创建或重新格式化。以下假定该文件就是 `/swap.img`：

```bash
sudo swapon /swap.img
echo "exit_code=$?"
swapon --show
./manage_rswap_client.sh status
```

预期 swapon 返回非零，通常提示 `Operation not supported`；没有新增活动 swap，
模块仍已加载，随后应能正常卸载。
文件不存在、格式无效等错误不能算此项通过。
若意外启用成功，立即停止本轮测试，不运行 workload、不继续卸载，保留日志反馈。
若没有现成合法 swap area，则记录此项未执行，不为这项创建新文件。

## 收尾与结果记录

确认模块已卸载，再恢复测试前原本启用的 SSD swap。例如原 area 为 `/swap.img`：

```bash
sudo swapon /swap.img
swapon --show
```

原来没有活动 swap 时，不需要额外启用一个。
每轮保存配置、install/uninstall 结果和本轮 Client／Server 日志；不清空全局 dmesg。
任一步出现意外失败或卡住，停止后续用例并反馈，暂不开展故障恢复尝试。

Patch 1 已通过的等容量、容量不匹配和断连回滚，本轮不要求整套重跑。
8+8 与 16+8 的映射计算已由本地检查覆盖；其真实数据路径仍需 Patch 3 验证。
