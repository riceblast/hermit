# Mapping 容量显示：核心功能及实际 BFS 验收

2026-11-25 验收 **PASS**。默认 `mapping` 输出 `server tier capacity used free`，
used 按内部 live+retired 计算，不再单列 live／retired。
容量以一位小数的 KB／MB／GB 显示，按 1024 进制、超过 1024 才升级，最大使用 GB。
全池容量及 Client 主体元数据也使用可读单位，序号保持整数。

`mapping --raw`／debugfs `mapping_raw` 保留原 v1 页数和字节数；pool 工具读取原始接口，
历史日志解析不受显示舍入影响。默认 mapping 显示格式为 v2，Client 模块版本仍为 1.13。

## 配置与范围

- Client：optane，配套 #8 内核，48 个在线 CPU（0–47）。
- Server 0：node04／10.0.0.13:9400，DRAM NUMA 0、PMem NUMA 1。
- Server 1：server02／10.0.0.20:9400，DRAM NUMA 0、PMem NUMA 2。
- 每 Server DRAM 16 GiB＋PMem 32 GiB，总池 96 GiB；每台注册 6 个 MR、建立 144 个 QP。
- 本轮 Client 模块 SHA256：`55cfb663c758f453505b38a93f11601f18d84fadf2892b6d4c0617821fc9207a`。

编译、脚本语法、diff 检查和 27 项相关既有回归通过。先完成一次无 swap 的双节点
静态检查：默认／原始接口各自与脚本输出一致，20 次重复读取稳定，四池容量正确，
pool-status 可读，正常卸载且接口移除。随后按操作者要求增加下述实际应用验证，
没有重复静态轮次或扩大参数矩阵。

## 实际应用：一次 BFS

原始 Ligra BFS，64G 文本图、40 线程、24 GiB cgroup-v1 本地内存限制，
既有 64 GiB swap，1 次计时轮次并保留程序自带预热。memsw 不设有限配额，
batch_account 关闭，使用既有 4 KiB 基线设置；未改 BFS、Server 或换页算法。
本轮仅验证真实换页期间的容量显示，没有开启 Adaptive、热度采样、Trace、A/B 或故障注入。

操作者执行 sudo 控制器，负责 Client 模块／swap／cgroup／系统参数操作；
助手启动无 sudo 的 Server、读取日志、完成离线核对并归档。

- BFS 退出码为 0，有一条 `Running time : 0.18` 输出；应用端到端约 **240.32 秒**。
  0.18 秒仅为算法计时，不代表加载、初始化及整个换页运行时间。
- 每秒采样，共 **243** 个快照，其中 **212** 个快照远端占用非零。
- 采样到的总占用峰值为 **44.5180 GiB**，四池均有实际占用；单池采样峰值约 11.1 GiB。
  这是周期采样峰值，不承诺捕获瞬时最高值；各池独立峰值不直接相加作为同刻峰值。
- 成功 RDMA Write **25759771** 页，Read **18754422** 页，四池均有成功读写。
  WC／提交／未归属错误均为 0；本轮异步 Read 为 0，不宣称异步路径硬件覆盖。
- 每个快照的格式、容量范围与舍入容差均通过检查；原始计数满足精确容量等式。
  人类可读和原始文件分开读取，序号相同时逐字段精确核对，不将不同序号的并发快照强行对齐。
- swapoff 后四池 used=0、free=capacity，正式 pool 报告 settled 检查通过。
  Client 已卸载、接口移除，两台 Server 释放资源并退出；cgroup 删除、原系统参数和配置恢复。
  新增 dmesg 无相关错误／WARNING／BUG。

总占用峰值附近实际输出如下（GB 标签按本接口的 1024 进制约定）：

```text
version=2 capacity=96.0GB metadata=288.0MB sequence=53090581
server tier capacity used free
0 dram 16.0GB 11.1GB 4.9GB
0 pmem 32.0GB 11.1GB 20.9GB
1 dram 16.0GB 11.1GB 4.9GB
1 pmem 32.0GB 11.1GB 20.9GB
```

运行结束、swapoff 后四池均回到 `0.0KB` 已用容量。上述证据验证容量显示与实际应用
换页兼容，不作为 BFS 算法 ground truth、性能提升、全容量或故障恢复的证明。
本轮未进行 TB 级实机分配，也未声称硬件覆盖全部单位边界。

## 证据与复核

完整证据位于 [artifacts/mapping-core-20261125/](artifacts/mapping-core-20261125/)，
沿用 artifacts 的 Git 忽略规则：

- `static/`：静态读取、raw 对照、pool-status 及卸载记录。
- `bfs/results.json`、`bfs/verification.json`：控制器及正式离线校验结果。
- `bfs/mapping.timeline.txt`、`bfs/mapping.samples.jsonl`、`bfs/mapping.peak.txt`：实际显示及对应精确计数。
- `bfs/samples.jsonl`、`bfs/pool-report.json`：正式池采样与 settled 报告。
- `bfs/bfs.console.log`、`bfs/bfs.exit`、`bfs/workload.json`：应用结果与参数。
- `bfs/server-*.log`、`bfs/server-*.status.json`：远端注册、资源清理及进程退出证据。
- `bfs/settings.before.json`、`bfs/restored.json`、`bfs/dmesg.new`：原设置、恢复与新增内核日志。
- `bfs/host.py`、`bfs/check.py`：本轮操作者控制器及离线复核脚本。

无 sudo 复核保存的 BFS 日志：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 remoteswap/tests/artifacts/mapping-core-20261125/bfs/check.py
```

原始运行目录：`/tmp/hermit-mapping-core-20261125-wIeDXG/`、
`/tmp/hermit-mapping-bfs-20261125-0lfySK/`。
