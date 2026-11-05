# 单/双节点与容量配置扩展：检查记录（2026-11-05）

本轮只修改 Client、管理脚本、配置文件和说明；没有修改内核、Server 或消息格式。
已有 managed Frontswap 内核接口保持不变，不需要重新安装内核。

| 检查 | 结果 |
| --- | --- |
| RDMA Client 编译 `make -C remoteswap/client -j12` | 通过，无编译警告 |
| DRAM 调试后端编译 `make -C remoteswap/client -j12 BACKEND=DRAM` | 通过，无编译警告 |
| 最终再次构建 RDMA Client | 通过，最终 .ko 为 RDMA 版本 |
| `modinfo` | 版本 1.2，含 nr_servers 参数 |
| 脚本和配置文件 `bash -n` | 通过 |
| `python3 remoteswap/tests/test_manage_config.py` | 7 项通过 |
| `git diff --check` | 通过 |

脚本测试用临时配置、临时状态文件和 insmod/rmmod/sudo 替身执行，包含配置路径带空格的
情况，不会实际操作模块、swap 或 RDMA。覆盖：

- 配置文件覆盖同名环境变量，缺项不会使用环境变量补齐。
- 单节点不要求、不传递第二个 endpoint。
- 24GiB 等不等容量总量能够传递给模块。
- 非法节点数、缺项、容量未对齐、超大数值、端口和 timeout 错误在调用模块命令前退出。
- 配置缺失或存在活动 swap 时拒绝安装。
- 配置损坏不影响 status/uninstall，status 读取实际模块参数。
- insmod 失败时传播非零返回码，不打印初始化成功。

代码复查结论：

1. nr_servers 只允许 1/2；只校验、连接所选 endpoint，双节点拒绝重复地址/端口。
2. FREE_SIZE 的 Chunk 数在分配前检查 1..MAX_REGION_NUM；每 Chunk 是现有的 8GiB。
3. GOT_CHUNKS 必须与先前公布数量一致；地址、长度与溢出检查保持原有约束。
4. 每端使用自己报告的数量分配 Region 描述；各端最大 3200GiB，总量上限为 6400GiB，
   使用 unsigned int 累计不会溢出。脚本不复制 MR 上限，模块统一校验该限制。
5. 所选节点总量必须严格等于 rmsize，否则打印 configured/discovered 并整体回滚。
6. 实际节点数在任何可能失败的初始化前记录；失败和退出只清理所选 Session，按逆序
   销毁。原有 WR、CQ、DMA 和 CM 清理机制保持不变。
7. 配置仅在加载时生效。没有启用换页，也没有实现共同容量交错/剩余尾段的数据映射。

编译日志：`/tmp/hermit-config-client-build.log`、`/tmp/hermit-config-dram-build.log`。
产物：`remoteswap/client/rswap-client.ko`。

本轮没有执行 SSH、网络测试、insmod/rmmod、swap 操作或 Workload。
硬件运行验证请按 [单/双节点测试步骤](patch1-smoke.md) 的 T1–T4，由操作者执行。
