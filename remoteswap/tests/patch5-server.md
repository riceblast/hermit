# Patch 5：Server 异构内存池与控制协议

范围：只修改 Server、共享协议和 Server 构建依赖。Client 解析与两级映射尚未修改，
本版本不兼容旧 Client。没有修改内核、Client Completion、Frontswap 或统计。

## 构建与启动

需要 C++11、libnuma 开发头文件／库，以及原有 RDMA CM／ibverbs／OFED 依赖。

```bash
make -C remoteswap/server
numactl --hardware
cd remoteswap/server
./rswap-server --ip 10.0.0.20 --port 9400 --client-cores 48 \
  --dram-node 0 --dram-gib 16 --pmem-node auto --pmem-gib 32
```

这里 DRAM node 0 是本次在 server02 上明确选择的节点；48 来自已有 Client 验收文档，
部署时仍须核对 **Client** 在线 CPU 数。另一台 Server 修改 IP，并根据本机拓扑选择 DRAM node。
不需要重新转换 PMem。旧的位置参数格式不再接受。

七个命名参数全部必填。两类容量都必须为正的 8 GiB 整数倍，合计最多
`MAX_REGION_NUM`（400）个 Chunk；不再静默向下截断容量。
`--pmem-node auto` 扫描在线、有内存且 cpulist 为空的节点，只有唯一候选时才自动选择；
多个候选必须显式指定 `--pmem-node N`。节点编号可以不连续。
DRAM 必须使用有 CPU 的内存节点，PMem 必须使用 CPU-less 内存节点，两者不能相同，
并且必须在进程允许的内存节点集合内。CPU-less 识别遵循本项目的硬件部署约定。
运行期间保持 NUMA 拓扑和允许节点集合不变。

Server 创建两个匿名范围，在触页前设置单节点 `MPOL_BIND` 并读回检查，设置
`MADV_NOHUGEPAGE` 后按 4 KiB 写入全部页面。随后通过 `move_pages(nodes=NULL)`
分批查询全部页面，仅查询、不迁移；任何查询错误、缺页或错误节点都使启动失败。
容量预检查不是内存预留；极端竞争下允许内核 OOM 终止，不能退回其他节点。

日志包含两个地址范围及逐范围的物理位置验证结果。16+32 GiB 应得到：

```text
NUMA verified tier=DRAM node=<DRAM_NODE> pages=4194304 bytes=17179869184 misplaced=0
NUMA verified tier=PMem node=<PMEM_NODE> pages=8388608 bytes=34359738368 misplaced=0
memory allocation done: protocol=2 dram_chunks=2 pmem_chunks=4 total_chunks=6 total_gib=48; MR registration pending Client connection
listening on <IP>:9400 client_cores=48 protocol=2 message_bytes=8024; waiting for matching Client to register MRs
```

Chunk 0–1 属于 DRAM，2–5 属于 PMem，每个长度 8589934592 字节。
两个 Tier 的虚拟地址无需连续，也无需按 8 GiB 对齐。
无 Client 的启动测试可用 Ctrl-C 停止；正常退出解除两个映射。
有活动 Client 时仍须先在 Client swapoff、卸载，最后停止 Server。

## 协议 v2 与 Client 适配约定

`utils.h` 定义 `RSWAP_PROTOCOL_VERSION=2`。保留原有三个描述符数组、`mapped_chunk`
和 `type`，在后面增加三个 `uint32_t`：`protocol_version`、`dram_chunks`、`pmem_chunks`。
当前 x86-64 ABI 中完整消息长度为 8024 字节，继续采用既有本机字节序／共享结构体布局，
不是跨架构序列化协议。

Server 发送的 AVAILABLE_TO_QUERY、FREE_SIZE、GOT_CHUNKS 都携带版本和完整数量。
FREE_SIZE 的描述符数组为零；GOT_CHUNKS 按 DRAM 在前、PMem 在后填入真实地址、长度和 RKey。
全部数据 MR 成功注册之前不发送 AVAILABLE_TO_QUERY，不发布部分容量。

Client 后续必须：

1. 所有接收检查 `wc->byte_len == sizeof(struct message)`、版本等于 2 和预期消息类型。
2. QUERY 在 queue 0 发送，三个 Chunk 数字段为零，版本为 2。
3. FREE_SIZE 校验两类数量均大于零，和等于 `mapped_chunk`，且总数不超过上限；
   在 `chunk_list` 保存 DRAM／PMem 数量，总容量仍为 `mapped_chunk * 8 GiB`。
4. REQUEST_CHUNKS 在 queue 0 发送，版本为 2，三个数量完整回显 FREE_SIZE 的值。
5. GOT_CHUNKS 的总数与两类数量必须和先前发现一致；逐项校验地址非零、4 KiB 对齐、
   长度恰为 8 GiB、地址加长度不溢出，并保存地址、RKey、长度和按索引确定的 Tier。
   RKey 是不透明值，不额外规定非零。
6. 所有选中 Server 发现成功、总容量与配置一致后才发布池；单台应为 48 GiB，两台应为 96 GiB。

顺序为 AVAILABLE_TO_QUERY → QUERY/FREE_SIZE → REQUEST_CHUNKS/GOT_CHUNKS。
AVAILABLE_TO_QUERY 仍按每个 QP 发送，其余控制请求仅使用 queue 0。
所有 SEND 回复排队，直到前一 SEND completion 后才复用共享发送缓冲区；
只在 queue 0 维持共享控制接收缓冲区，避免多个 QP 同时写入。

MR 继续在 Client 建连取得 PD 后注册，日志逐个报告 Chunk、Tier、Node、地址、长度和 RKey。
注册失败终止初始化，不宣告可用；停止 CQ 线程、销毁 QP 后注销已注册 MR，再解除映射。
清理操作本身失败时终止进程，剩余资源交由内核进程退出回收，避免继续释放可能仍被使用的内存。

## Client 代码交接：修改位置与数据结构

以下是后续 Client 适配要求，并不表示这些修改已经在本次 Server Patch 中实现。
仅同步 `utils.h` 并重新编译还不够：旧 `send_message_to_remote()` 只填写 `type` 和
`mapped_chunk`，新增字段仍为零，会被新版 Server 拒绝。

| Client 文件／函数 | 需要修改或保留的内容 |
| --- | --- |
| `rswap_rdma.h` / `struct chunk_list` | 在现有 `chunk_num`、`remote_mem_size` 旁增加 `uint32_t dram_chunks`、`uint32_t pmem_chunks`，分别保存每个 Session 发现的数量 |
| `rswap_rdma.c` / `send_message_to_remote()` | 每次完整初始化消息，填写版本；QUERY 数量为零；REQUEST_CHUNKS 回显该 Session 的三个数量 |
| `rswap_rdma.c` / `handle_recv_wr()` | 在读取新字段前校验完整长度，校验版本、类型、数量及各阶段拓扑一致性；全部检查成功后才推进 queue state |
| `rswap_rdma.c` / `rswap_query_available_memory()`、`init_remote_chunk_list()` | FREE_SIZE 发现成功后，继续在进程上下文分配描述符数组；不要在 CQ callback 内加入可睡眠分配 |
| `rswap_rdma.c` / `bind_remote_memory_chunks()` | 保留 Server 的描述符顺序，全部 GOT_CHUNKS 描述符校验成功后再绑定；Tier 可由索引与 `dram_chunks` 比较得到 |
| `rswap_rdma.c` / `rdma_session_connect()` | 保留串行 QP 建连、queue 0 容量查询，以及 Session 初始化截止时间；就绪日志增加每 Tier Chunk 数与容量 |
| `rswap_rdma_ops.c` / `rswap_client_init()` | 按实际发现的每 Server 容量构建布局；任何一个选中 Server 失败都整体回滚，全部成功后才发布池并注册 Frontswap |
| `rswap_client.c` / 模块参数与版本说明 | 保留现有参数接口和容量上限；可更新模块描述／版本以区分新版 Client，不把 48／96 GiB 写成唯一合法值 |
| `rswap_client.conf` 及说明模板 | 更新本轮 48／96 GiB 配置示例；保持现有 IP、端口和节点编号约定 |

`remote_mem_size` 现有单位是 **GiB**，`mapped_size[]` 单位是 **字节**，
`rswap_pool_layout.server_pages[]` 和 `total_pages` 单位是 **4 KiB 页数**。
每个 8 GiB Chunk 为 2097152 页；16／32／48 GiB 分别为
4194304／8388608／12582912 页，双 Server 96 GiB 为 25165824 页。
容量换算先提升到 `u64` 再移位，不能在 32-bit 整数中先计算 GiB 字节数。

`dram_chunks`／`pmem_chunks` 必须是每 Session 元数据，不能保存成所有 Server 共用的一组数量；
16+32 GiB 是部署配置，不是协议中固定的 2+4 常量，两台 Server 也允许不同容量。
保持拓扑在池存续期间不可变，不能通过修改配置文件重新解释已存页面。

不强制给每个 `remote_chunk` 再加 Tier 字段：最小实现可用
`chunk_idx < session->remote_mem_pool.dram_chunks` 判定 DRAM，否则为 PMem。
将来确需显式 Tier 标识时，可约定 Client 内部 `DRAM=0`、`PMEM=1`；这些不是本协议新增的线缆字段。

### 消息填写模板与接收校验

下面片段放在现有 `ib_dma_sync_single_for_cpu()` 之后、
`ib_dma_sync_single_for_device()` 之前；保留已有控制 RECV 预投递和 WR 计数处理。
调用者只允许在 queue 0 发送 QUERY 或 REQUEST_CHUNKS，并等待前一轮控制收发终止后复用缓冲区：

```c
struct message *msg = session->rdma_send_req.send_buf;

memset(msg, 0, sizeof(*msg));
msg->protocol_version = RSWAP_PROTOCOL_VERSION;
msg->type = type;
if (type == REQUEST_CHUNKS) {
    msg->mapped_chunk = session->remote_mem_pool.chunk_num;
    msg->dram_chunks = session->remote_mem_pool.dram_chunks;
    msg->pmem_chunks = session->remote_mem_pool.pmem_chunks;
}
/* QUERY retains zero in all three Chunk-count fields and all arrays. */
```

接收需要满足以下规则：

- 先同步 DMA 缓冲区到 CPU，再检查 `wc->byte_len`；长度错误时不读取新增字段。
  随后检查版本与 `queue->expected_message`，不通过返回 `-EPROTO`，沿用现有 Session 错误和回滚机制。
- 对所有 AVAILABLE_TO_QUERY、FREE_SIZE、GOT_CHUNKS 检查数量合法性。
  `mapped_chunk` 是有符号 `int`，必须先检查 `1..MAX_REGION_NUM`；两类数量均为正，
  以 `u64` 相加后与总数比较，避免先按 `uint32_t` 相加发生回绕。
- 第一个 AVAILABLE_TO_QUERY 可将数量暂存在本 Session 的 `chunk_list` 中；
  之后其他 QP 的 AVAILABLE_TO_QUERY、FREE_SIZE、GOT_CHUNKS 都必须与这组数量一致。
  数量只是暂存，直到描述符和所有 Session 都完成校验才发布整个池。
- AVAILABLE_TO_QUERY 和 FREE_SIZE 仅提供拓扑，描述符数组为零，不能套用 GOT_CHUNKS
  的非零地址校验，也不能将 AVAILABLE_TO_QUERY 当作已经收到地址或完整 Client 已就绪。
- GOT_CHUNKS 先校验全部有效条目，再调用 `bind_remote_memory_chunks()` 并发布 RECEIVED_CHUNKS；
  同一 Session 的有效地址范围还应互不重叠。只有 `[0, mapped_chunk)` 是有效条目，
  数组其余部分没有可用 MR，不能把 400 个数组元素全部作为容量。

不要要求相邻 Chunk 地址连续、按地址排序、按地址大小推断 Tier，或要求 Chunk 地址按 8 GiB 对齐。
DRAM／PMem 是两个独立 mmap，PMem 地址完全可能小于 DRAM 地址。
不同 Server 的虚拟地址或 RKey 可以相同；不能跨 Session 做全局唯一性检查。
实际 RDMA 请求必须使用选中 Session 的队列／PD／DMA device，以及该描述符的地址和 RKey。

### 线缆布局与缓冲区大小

两端共同使用 `remoteswap/utils.h` 和 `constants.h`，`MAX_REGION_NUM=400`、
`REGION_SIZE_GB=8` 必须一致，不复制一份私有协议结构，不增加 `packed`，不单独对新字段做字节序转换。
当前 x86-64 布局如下；这是诊断参考，代码中仍统一使用 `sizeof(struct message)`：

| 字段 | 字节偏移 | 字节数 |
| --- | ---: | ---: |
| `buf[400]` | 0 | 3200 |
| `mapped_size[400]` | 3200 | 3200 |
| `rkey[400]` | 6400 | 1600 |
| `mapped_chunk` | 8000 | 4 |
| `type` | 8004 | 4 |
| `protocol_version` | 8008 | 4 |
| `dram_chunks` | 8012 | 4 |
| `pmem_chunks` | 8016 | 4 |
| 尾部 ABI padding | 8020 | 4 |

旧消息为 8008 字节，新消息为 8024 字节。分配、DMA map／unmap、DMA sync 和 SEND／RECV SGE
长度都要使用新结构的 `sizeof`；现有 `rswap_setup_buffers()`、`rswap_setup_message_wr()`、
`post_control_recv()`、`rswap_free_buffers()` 等已经使用 `sizeof(struct message)`，
同步头文件后重新编译即可更新这些长度，避免另加硬编码常量。
Client 本地 kbuild 会跟踪共享头文件依赖；部署时必须使用重新构建的 `.ko`。

消息枚举数值不变：`AVAILABLE_TO_QUERY=11`、`QUERY=10`、`FREE_SIZE=4`、
`REQUEST_CHUNKS=8`、`GOT_CHUNKS=2`。
每个 Server 进程仅支持一次完整的容量／描述符发现。
Server 收到 REQUEST_CHUNKS 并发送 GOT_CHUNKS 回复时，内部设置 `expected_request=DONE`，仅表示发现流程结束；
**当前实现并未支持 DONE 消息处理**，Client 不应因此发送 DONE，
也不发送 STOP、ACTIVITY、REQUEST_SINGLE_CHUNK 或重复 QUERY 来做保活／重试。

## Client 建连与配置模板约定

每个 Server 进程服务一个 Client Session。每 Session 的 QP 总数为
`Client在线CPU数 * NUM_QP_TYPE`，`NUM_QP_TYPE=3`，顺序为 STORE、LOAD_SYNC、LOAD_ASYNC，
每类按 CPU ID 递增；索引为 `cpu + type * online_cores`。
Server 按连接请求到达顺序分配索引，协议中没有额外的 QP 编号握手，
因此不能把现有逐 QP 建连改成并发建连，也不能为发现额外创建一个控制 QP。
48 个在线 CPU 对应每 Server 144 个 QP；两端启动参数必须一致。
Client 在线 CPU ID 继续要求从 0 连续编号，并在模块运行期间保持不变。

Client 在 `rdma_connect()` 前预投递该 QP 的 AVAILABLE_TO_QUERY RECV；
等到该 QP 的通知完成后再连接下一个 QP。所有 QP 完成后，只在 queue 0 发 QUERY、REQUEST_CHUNKS。
这样继续满足每 Session 只有一份控制接收缓冲区、同一时间只有一个有效控制接收请求的约束；
不要给所有 QP 同时预投递到这份共享缓冲区。

先等待 Server 日志出现 `listening on ...` 再加载 Client；这表示预分配和物理位置验证已完成，
第一次 AVAILABLE_TO_QUERY 仍须等待全部 MR 注册。
Client 的 `CONNECT_TIMEOUT_MS` 是一个 Session 从开始建连到完成全部 QP 和发现的总截止时间，
不是每个 QP 单独重新计时。当前配置为 15000 ms，首次联合调试可先设为 60000 ms
（现有合法范围 100..300000），实际按 MR 注册与建连耗时调整。

`rswap_client.conf` 仍是管理脚本唯一配置来源，不新增远端 Node ID、Tier 容量或新模块参数；
这些信息由 Server 负责，其中 Node ID 不在线缆协议中报告，Client 只需 Tier 数量和 MR 描述符。
Client 的 Server ID 由配置顺序决定，与 Server 的 NUMA node ID 无关。

单 Server 使用 server02 的配置模板：

```bash
NR_SERVERS=1
POOL_GIB=48
CONNECT_TIMEOUT_MS=60000
SERVER0_IP="10.0.0.20"
SERVER0_PORT=9400
```

双 Server 保留当前 endpoint 顺序的模板：

```bash
NR_SERVERS=2
POOL_GIB=96
CONNECT_TIMEOUT_MS=60000
SERVER0_IP="10.0.0.13"  # node04
SERVER0_PORT=9400
SERVER1_IP="10.0.0.20"  # server02
SERVER1_PORT=9400
```

当前仓库配置中的 `POOL_GIB=80` 是 Patch 4 的 48+32 GiB 配置；当两端都以 16+32 GiB
启动时，必须改成 96。`POOL_GIB`／模块 `rmsize` 始终是选中 Server 的总和，不能填成每端容量，
也不能由 Client 假定两端相等后除以节点数。每端上限是两类合计 400 个 Chunk，不是每类 400。
只要发现总量与配置不同就初始化失败，不截断、不降级为较小池。
管理脚本继续只管理模块，不创建或启停 swap；一个活动 swap area 且页数不超过发现池容量。

## Patch 5 Client 与后续 Patch 的边界

- Patch 5 Client 只完成新版协议、拓扑保存和发现日志；原有 Server 间映射保持不变。
  原有 `rswap_resolve_remote_page()` 逐描述符寻址可以访问两段 mmap，
  但线性 Server-local slot 仍先遍历 DRAM Chunk，再遍历 PMem Chunk，**尚不构成 Tier Interleave**。
- Patch 6 基于保存的每 Server 两类容量实现两级静态映射。Tier-local Chunk 必须换算回
  Server 描述符下标：DRAM 为 `local_chunk`，PMem 为 `dram_chunks + local_chunk`。
  Tier-local slot 从 0 开始；原始 swap offset 不减 1，不能把 Server-local slot 直接当作 Tier-local slot。
- Patch 7 再扩展 Tier 级统计和逐页 Trace。当前 stats 的 `version=1` 与控制协议 `version=2`
  相互独立，不能只因控制协议升级就修改 debugfs 格式或 Python parser。
- 继续使用现有 `frontswap_register_managed_ops_v2` 内核接口，不需要为控制协议升级修改内核。
  该符号中的 v2、`MODULE_VERSION` 和 `RSWAP_PROTOCOL_VERSION` 是不同的版本标识。
- FREE_SIZE 表示整个预分配池容量，不是随换页变化的空闲 Slot 数；Server 不处理逐页分配请求。
  数据 I/O 仍为 Client 直接发起的一侧 RDMA Read／Write，PMem 不需要新的 RDMA opcode 或持久化协议。
  MR 地址和 RKey 只在当前 Server 进程／Session 生命周期内有效，PMem Tier 不承诺重启恢复 swap 数据。
- 读取后不释放／重映射位置，继续遵循现有 Frontswap entry 生命周期。
  正常顺序仍是 swapoff 成功 → 卸载 Client → 停止 Server；
  连接失败或卸载后重新建连前重启参与的 Server，不在存活 Session 中重新发现／替换描述符。
