# 远端地址空间列表：核心实机验收

2026-11-25 核心验收 **PASS**。本轮仅连接双节点并验证新地址列表，未启用 swap、
未运行 BFS 或其他 workload，未追加单节点、大规模或故障测试。

## 配置与产物

- Client：optane，配套 #8 内核 `5.14.0-rc5-hermit-syscall-v2`，48 个在线 CPU（0–47）。
- Client 模块仍为 1.13，本次新增 `address_map` 只读接口及 `address-map` 脚本命令。
- 本轮模块 SHA256：`87958f68699d913c8700ed6f2cd1b9ae46c326e80ace857388a2b8f08dacebff`。
- Server 0：node04／10.0.0.13:9400，DRAM NUMA 0、PMem NUMA 1。
- Server 1：server02／10.0.0.20:9400，DRAM NUMA 0、PMem NUMA 2。
- 每台 DRAM 16 GiB＋PMem 32 GiB，总池 96 GiB；每台发现 6 个 MR、建立 144 个 QP。

操作者执行准备好的 sudo Client 脚本，完成模块加载、debugfs 读取和正常卸载；
助手负责无 sudo 的 Server 启动、日志收集、逐条校验和证据归档。

## 核心检查结果

| 检查 | 结果 |
| --- | --- |
| 展示结构 | DRAM、PMem 顺序正确；两种介质下各有 memory pool 0、1，endpoint 与配置一致 |
| 地址范围 | 双节点共 12 条 MR 与各 Server 本轮 `MR registered` 日志逐条匹配，介质、Chunk、起点、长度均正确 |
| 容量 | 每 Server：DRAM Chunk 0–1、PMem Chunk 2–5；各 MR 的 `end-start=bytes=8589934592` |
| 两种入口 | 管理脚本 `address-map` 与直接 cat debugfs 输出完全一致 |
| 重复读取 | 20 次重复读取与首次输出完全一致 |
| 权限 | `/sys/kernel/debug/rswap/address_map` 为 0400、root:root |
| 只读行为 | 四池 live/retired 为 0、free=capacity；数据 I/O 成功及错误计数均为 0，unrouted_error=0 |
| 卸载生命周期 | Client 正常卸载，新接口随之移除；两台 Server 均报告资源释放且进程退出 |
| 日志及配置 | dmesg 前缀保留，新增日志无相关错误／WARNING／BUG；Client 配置保持不变，全程无活动 swap |

以上验证当前双节点配置下的 MR 地址展示与读取／卸载生命周期，不代表逐页换页映射、
页面内容正确性或额外 I/O 路径验收。

## 证据

完整证据位于 [artifacts/address-map-core-20261125/](artifacts/address-map-core-20261125/)，
沿用 artifacts 的 Git 忽略规则：

- `results.json`：正式校验结果；`check.py`：离线逐条校验脚本。
- `address-map.direct.txt`、`address-map.script.txt`、`address-map.repeat.txt`：实际输出。
- `server-0.log`、`server-1.log` 及 `server-*.status.json`：远端 MR 日志与退出状态。
- `client_core.sh`、`client-console.log`、`install.log`、`uninstall.log`、`client.exit`：操作者脚本及结果。
- `mapping.txt`、`stats.txt`、`address-map.stat`、`dmesg.new`、`unloaded.ok`：容量、计数、权限及收尾证据。

原始运行目录为 `/tmp/hermit-address-map-20261125-SFZ0x9/`。可无 sudo 重新校验：

```bash
python3 remoteswap/tests/artifacts/address-map-core-20261125/check.py
```

离线检查还会核对当前模块 hash、Client 未加载和配置未改；后续构建或重新部署后，
应以保存的本轮结果及日志为准，不要求新状态与本轮收尾状态相同。
