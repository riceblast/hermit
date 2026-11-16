# Patch 6：Client 协议 v2 与两级静态 Interleave

本 Patch 衔接已完成的 Patch 5 Server，修改 Client 协议解析、拓扑和地址映射。
Client 模块版本为 1.6；控制协议仍为 2，debugfs stats 仍为 1。
没有修改 Server 行为、内核核心、数据 Completion、polling／drain 或 Frontswap 生命周期。
本文件的硬件步骤是验收计划，不表示已经执行。

## 实现

- 每 Session 保存 DRAM／PMem Chunk 数；两类均须为正，总和最多 400。
- QUERY 发送版本 2 和零数量；REQUEST_CHUNKS 完整回显发现数量。
- 所有回复检查完整长度、版本、类型、数量；不同 QP 的 AVAILABLE、FREE_SIZE、
  GOT_CHUNKS 必须保持同一拓扑。GOT_CHUNKS 全部合法才绑定描述符；同一 Session 的
  有效 MR 范围不得重叠，不要求地址连续、按地址排序或 8 GiB 对齐。
- 所有选中 Session 连接、描述符和总容量验证成功后，一次发布不可变布局。
- `rswap_map_page()` 返回 Server、Tier、Tier-local Slot。两层均为共同容量逐页交错、
  较大一侧接尾段；第二层使用 Server-local Slot，原始 swap offset 不减 1。
- 每 Chunk 2097152 页。Tier-local Chunk 下标为 `slot >> 21`，页内字节偏移为
  `(slot & ((1ULL << 21) - 1)) << 12`；PMem 描述符下标加 `dram_chunks`。
  使用目标 Session 的现有 QP／PD／DMA device 和目标描述符的地址／RKey。

每 Server 16 GiB DRAM＋32 GiB PMem 时，单 Server 前 32 GiB 逻辑范围逐页交错，
后 16 GiB 为 PMem；两台同配置时，前 64 GiB 四路交错，后 32 GiB 只在两台 PMem
之间交错。这不是全程按 1:2 加权分配。这里 GiB 范围表示原始 offset × 4096，
不是应用累计换出量；头页位置仍保留。

## 3–3.5 TiB 总容量

- 容量配置单位为 GiB，3 TiB = 3072 GiB，3.5 TiB = 3584 GiB。
- 每 Server 的 DRAM＋PMem 合计最多 400 × 8 = **3200 GiB**；两台合计最多
  **6400 GiB**。3584 GiB 在现有双 Server 上限内，但不能全放在一台；两台不必等容量。
- 容量／Chunk 数使用的整数足够表示上述上限。先转换为 `u64` 再计算页数或字节数；
  布局、Slot、Chunk 字节偏移和远端地址均以 64 位计算。
- 3.5 TiB 为 939524096 页，低于本内核 swap header／`swap_info_struct.max` 的
  32 位页数上限；本项目配置为 x86-64，swap offset 编码也足够。
  当前 Client 的 L1TF 状态为 Not affected；最终启用容量仍以实际 swapon 结果为准。
- 一个活动 swap area 的页数不得大于池总页数。小 swap area 可以配大池，但不能据此
  宣称硬件覆盖全部远端空间；实际用满池需要足够大的合法 swap area。
- 大池的元数据消耗取决于启用的 swap area 大小。本内核开启 MEMCG_SWAP，若启用
  3.5 TiB swap，基础 swap_map 约 896 MiB，Frontswap bitmap 约 112 MiB，
  swap_cgroup ID 表约 1792 MiB，合计约 **2.73 GiB**，另有索引／cluster 等开销。
  这与应用物理内存配额分开考虑；不在本 Patch 修复既有 cgroup swap 记账问题。
- 上述是源代码范围检查及无特权地址计算验证，不是 3.5 TiB 全量 HCA／工作负载验收。
  大 MR 注册耗时还需在最终规模下核实；每 Session 总初始化超时可配置 100..300000 ms。

## 本地检查

本次 Client 构建通过，无新增编译告警；下列 23 项无特权回归全部通过。
产物为 `remoteswap/client/rswap-client.ko`（版本 1.6），构建日志为
`/tmp/hermit-patch6-client-build.log`。未加载模块、连接 Server 或操作 swap。

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
PYTHONDONTWRITEBYTECODE=1 python3 remoteswap/tests/test_patch6_protocol.py
PYTHONDONTWRITEBYTECODE=1 python3 remoteswap/tests/test_patch2_mapping.py
PYTHONDONTWRITEBYTECODE=1 python3 remoteswap/tests/test_patch3_completion.py
PYTHONDONTWRITEBYTECODE=1 python3 remoteswap/tests/test_patch4_stats.py
PYTHONDONTWRITEBYTECODE=1 python3 remoteswap/tests/test_manage_config.py
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
```

测试调用生产代码：v2 消息填写／解析及非法描述符、两级映射穷举无碰撞与完整覆盖、
各层尾段和所有 Tier 内 Chunk 边界、实际 RDMA WR 选择与已有生命周期回归。
容量用例包含 3072、3080、3328、3584 GiB 和现有 6400 GiB 协议上限；
实际 resolver 另外检查 3584 GiB 布局中每个 Tier 内每个 Chunk 的首末页，
协议测试覆盖单 Server 400 个描述符。所有检查不实际分配 TB 级数据内存。

## 最小硬件验收：先单 Server，再双 Server

不重装内核，沿用有 `frontswap_register_managed_ops_v2` 的配套内核。
先完成以下两轮；本阶段不要求重跑全部故障用例、性能对比或 BFS。

| 轮次 | Server 池配置 | Client 配置与发现预期 |
| --- | --- | --- |
| 单 Server | 10.0.0.20：DRAM 16＋PMem 32 GiB | NR_SERVERS=1，SERVER0_IP=10.0.0.20，POOL_GIB=48；发现 DRAM 2＋PMem 4 Chunks |
| 双 Server | 10.0.0.13、10.0.0.20：各 DRAM 16＋PMem 32 GiB | NR_SERVERS=2，原 endpoint 顺序，POOL_GIB=96；两台各 6 Chunks |

两轮 `CONNECT_TIMEOUT_MS=60000`。每轮步骤：

1. **Server：**按 Patch 5 新参数启动新进程；核数填写 Client 在线核数，Node ID 使用
   各自已验证的配置。`run_server.sh` 默认 IP 为 10.0.0.20，另一台须改为本机 IP／Node。
   等待 `listening on ...`；连接后记录各 6 个 MR 注册成功、QUERY、REQUEST_CHUNKS 流程结果。
   当前 Server 的成功发现没有逐条 QUERY 日志，Client `ready protocol=2` 表示整轮发现已完成。
2. **Client：**确认无活动 swap、旧模块已卸载；编辑实际配置文件后运行
   `bash remoteswap/client/manage_rswap_client.sh install`。
   检查加载版本 1.6、每 Server Tier 数量、总容量以及 `two-level page interleave` 就绪日志。
   此处未成功不继续 swapon。模块安装脚本不会自动启用 swap。
3. **数据：**选一个已经初始化的合法 swap area，页数不超过池容量；可以使用现有较小
   文件，不必为 96 GiB 池新建同样大的 swap。沿用已验证的基线开关和物理内存限制，
   关闭 batch_account 等优化，解除有限 memsw 配额；不读取 cgroup swap 记账作为依据。
   用 `swap_pattern.c` 跑 **4 GiB 工作集、2 GiB 物理限制、2 轮**，产生实际换页，
   每轮逐页校验成功。程序自行禁用该范围 THP。
4. **观察：**记录 workload 前后 `manage_rswap_client.sh stats`；参与 Server 必须有成功
   Write、Read，WC／提交错误和 unrouted_error 增量为零。保持当前 Server 级统计口径，
   不用它声称已经计数到每个 Tier；逐 Tier 观察在 Patch 7 验收。
5. **收尾：**最后一轮 READY 时保留进程，确认宿主机内存足够后解除测试物理限制，
   显式 swapoff 本轮文件，再让进程完成校验并输出 PASS；最后卸载模块。
   Server 保持在线直至 swapoff／卸载结束；重新连接前重启参与的 Server。

两轮验证协议、真实页面内容及健康 swapoff。小工作集不保证触及每个 Tier／Chunk／尾段；
这些边界先由实际映射和 resolver 检查覆盖，最终结合 Patch 7 placement trace 确认硬件覆盖。
如出现校验失败、非零 WC／提交错误、内核 BUG/WARN 或持续卡住，保存日志并停止后续步骤。
