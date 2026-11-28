# 第三阶段：双节点核心与 BFS 实机验收

**当前继续测试使用新目录 `...-r3`，保留现有 Client/Server，只继续容量专项和 BFS：**

```bash
sudo /usr/bin/python3 /mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/stage3_host.py \
  /tmp/hermit-stage3-core-bfs-20261128-r3 \
  --resume-live /tmp/hermit-stage3-core-bfs-20261128-r2
```

新目录已无 sudo 准备。入口核验旧 Client 实例和唯一旧控制器/工作负载，
以 pidfd 只停止这些旧进程，保留 RDMA，复制最初的环境恢复基线，并以全新输出继续完整测试。
如果前一轮是已识别的容量 trace 过滤器设置失败，且 core/edges 结果完整通过，
则记录两项通过证据的来源并跳过这两项，只继续 capacity/BFS；旧测试 cgroup 的最终清理也一并接续。
`run_tests` 及采样器启动前都检查输出目录未被使用。旧目录内的失败记录不覆盖。
首次 slot-cache 恢复已成功清空并重新启用 swap，但随后复用旧的 `controlled.heat`，
触发采样器 EEXIST；BFS 仍未开始。下文旧恢复命令作为历史记录，当前不再使用。

启动前再次复查：暂停到用户决策期间持续持有 BFS/采样器的 pidfd，允许目标在 TERM 后已经退出，
不会因后续 CONT 的 ESRCH 误报失败；身份检查期间退出也统一处理。
每组容量仅选择采集起点不早于该组 AUDIT 的观测，跳过跨提交时刻的采集；
已捕获的 post/WC 错误立即失败。全量本地回归 **200 项通过**，其中第三阶段专项 44 项。
包含真实自有空闲子进程的 stop/term/exit 测试；没有启动实机负载或修改保留现场。

r2 的 core/edges 已实际通过：4096 次 Warmup Store，4096 hot＋4096 cold Adaptive Store，
post/WC 全部成功，内容、相等/全零、固定策略、非目标和 stop 检查均通过，unknown=0。
随后在大负载初始化后、首次 pageout 前设置 trace 过滤器失败：
`alloc_ret` 是打印日志的标签，tracefs 的真实字段是 `ret`。
过滤器现改为 `slot < 64 && ret == 0`，先读取内核事件 `format` 核验声明字段，
并在停止旧进程或启动新负载前让内核实际接受该过滤器，再清零过滤器。
预检证据保存在 `trace-schema.json`，避免用旧轮的数据分配再次发现接口错误。
新增专项覆盖了字段声明与打印标签差异、格式不符拒绝，以及只恢复后两阶段的完整衔接。

控制器：[stage3_host.py](stage3_host.py)，由操作者使用 sudo 在 Optane 启动。
沿用已有 96 GiB 双节点池和已有 64 GiB swap 文件，不重新格式化 swap、不安装内核、
不自动管理 Server。Client 1.19 编译产物由控制器直接加载。

两台 Server 保持各 16 GiB DRAM + 32 GiB PMem、端口 9400、48 个 Client cores：

```bash
# node04
cd /home/yxr/projects/hermit/remoteswap/server
sudo ./rswap-server --ip 10.0.0.13 --port 9400 --client-cores 48 \
  --dram-node 0 --dram-gib 16 --pmem-node auto --pmem-gib 32

# server02
cd /home/yxr/projects/hermit/remoteswap/server
sudo ./rswap-server --ip 10.0.0.20 --port 9400 --client-cores 48 \
  --dram-node 0 --dram-gib 16 --pmem-node auto --pmem-gib 32
```

如果 Server 已按上述配置等待连接，无需重复启动。卸载 Client 后再次连接前必须重新启动 Server。

```bash
# Optane：先由 Codex 无 sudo 完成准备和本地检查；OUT 必须是新目录。
/usr/bin/python3 remoteswap/tests/stage3_host.py /tmp/hermit-stage3-core-bfs-20261128 --prepare

# 两台 Server 都就绪后，由用户启动本轮验收。
sudo /usr/bin/python3 /mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/stage3_host.py \
  /tmp/hermit-stage3-core-bfs-20261128
```

顺序为：真实 5s/4 轮采样的 Warmup 与第五组切换、冷热 Store 与内容检查；
受控快照的相等/全零、非目标 Interleave、固定策略优先及 stop；
40 GiB 换出填满合计 32 GiB DRAM，验证跨层回退，再释放指定 Server 的一个 Slot 验证同层回退，
停止策略后逐字校验全部数据；最后运行用户的原始 BFS 脚本。
受控快照不冒充实际 Idle 采样，池满后的磁盘回退仅有本地专项验证，本轮不强行填满整个 96 GiB 池。
core、边界、容量三个阶段结束后，在所有自有负载退出、控制 fd 关闭且身份解绑后，
先保存观测，再对本轮测试 swap 执行 swapoff/swapon，保持两条 RDMA Session。
严格空池检查放在 swapoff 之后，避免把内核 per-CPU swap slot 缓存的延迟归还误判为泄漏。
同层回退的小步检查把负载临时固定到一个允许的 CPU：释放指定 DRAM entry 后，
再释放 64 个 PMem entry 触发该内核 SWAP_BATCH=64 的归还，核验仅另一 Server 有一个 DRAM 空位；
之后重新触页恢复所有被丢弃页面的原始内容，再做全部内容校验。

BFS 保持当前脚本的 6 GiB cgroup、16 线程、64G 图、三轮计时。
采样器使用用户最终指定的 **`a_bit_window=10、ewma_window=2`**，绑定 exec 后实际 BFS PID，
并运行在 BFS cgroup 外。检查实际提交与决策、两个 Server 的读写、unknown 计数、正常退出及内核日志。
只采集每池前 64 个 Slot 的数值决策，校验精确比较并与成功审计的快照均值对应；
该样本不能作为完整容量分配回放。受控小负载另采集完整 Store 决策与 post/WC 关联。

基线 0 不计数；成功提交第 5 组后进入 Adaptive，再完成 10 组，即总第 **15** 组时，
如果 BFS 尚未结束，控制器暂停经核验的 BFS 和采样器，输出 `BFS PAUSED FOR REVIEW`，
保存 `bfs.review.json`。**暂停期间不再产生新的采样组，Codex 应向用户汇报整体情况，等待 stop/continue。**
名义时间为 Warmup 100 秒、后续十组 Adaptive 200 秒，实际还包括初始基线、扫描、上传开销。
正常退出仍要求三轮计时；用户明确结束则记录 `stopped_by_user=true`、实际已完成轮数与覆盖情况，
不会声称三轮正常完成。

```bash
# 只有用户决定提前结束后才执行；只信号终止核验过身份的本轮 BFS。
printf 'stop\n' > /tmp/hermit-stage3-core-bfs-20261128/bfs.command
```

用户决定继续时可写 `continue`，继续后每完成额外十组再暂停一次。
新组数条件取代原来的 Adaptive 三分钟通知及固定的 BFS/整轮时长预算。
如果连续 15 分钟没有新 EWMA 组完成，则作为进度异常暂停并汇报。
任一检查失败则暂停本轮自有进程、保留控制 fd/swap/RDMA/日志，无自动重试。
成功或用户明确提前结束后，正常恢复本轮之前的 swap、Hermit/THP/readahead 配置和 cgroup 设置。
不要把人为结束的 BFS 视为算法正确性或完整运行的证据。

### 每组观测

控制器另外输出 `bfs.ewma.tsv` 和 `bfs.ewma.jsonl`，生产采样器依旧仅写 `summary.tsv`：

- 从基线起记录每组实际完成时间、未舍入 mean、相邻组 mean 变化、pages、零热页数、
  H≥mean 热页/H<mean 冷页数量与比例，以及五档热度分布。
- 第 5 组是 Adaptive 入口观测，第 6～15 组是十个完整后续组。
- 每组对齐同一个 binding/window 的最近一次提交后内存池观测，记录时间偏差。
  DRAM/PMem 使用量以 occupied Slots（live+retired）计，单位 GiB；附 `1:r` 比例、精确页数比例及占用份额。
  例如 DRAM 20 GiB、PMem 40 GiB，输出 `1:2`，份额约 33.3%/66.7%。这是容量对比，不是带宽或累计写入量。
- 记录两 Server 各层占用、相邻观测间 hot/cold Store 增量和跨层回退增量。
  容量与计数均为 Client 池整体观测，只有热度分布严格属于目标 mm。
- 如目标恰在提交后退出，来不及观测该组的活动池状态，则标注容量缺失，不拿进程退出后的 0 占用顶替；
  最后一份活跃负载观测另存在 `last_live_occupancy`，避免把清理后的空池当成运行末尾的容量分布。
- BFS 自然提前结束时按实际组数报告；若未覆盖 Adaptive，则标注 PARTIAL，不强制延长或自动重跑。

## 本轮 core 失败与恢复

2026-11-28 首轮在第 5 组后停止：不是分类不符合比较规则，而是受控负载没产生新的 hot Store。
Warmup 的 4096 个热区页已写入 PMem，换入后负载只读，后续换出可复用原有干净 swap 副本；
本轮 Adaptive 新 Store 只有冷区的 4096 页，均以 H≈0.0256、mean≈0.33553 正确选择 PMem。
热区采样 H≈0.64672，但未触发重新写入，因此热页进 DRAM 的验收尚未完成。
已捕获的 8192 次 Store post/WC 全部成功，查询审计 unknown=0。

修复仅涉及测试：真实采样专项在第五组后逐页写回原值，显式弄脏页面；
边界、固定策略和停止用例的 helper 每次 pageout 前也写回原值。
volatile 防止编译器删除同值写入，不改变被校验的数据。
本地回归通过只读 PTE 写保护捕获每页真实写入，模拟 PAGEOUT 而不访问当前 RDMA/swap。

原始日志与失败时源码另存
`tests/artifacts/stage3-core-clean-reuse-20261128/`；恢复时还会在本轮目录中保存 `failed-core-clean-reuse/`。
以下入口只适用于这次已识别的失败，需用户在另一个 Optane 终端执行：

```bash
sudo /usr/bin/python3 /mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/stage3_host.py \
  /tmp/hermit-stage3-core-bfs-20261128 --recover-core-store
```

恢复核验原 Client 实例、swap、源文件/二进制哈希、旧控制器 PID/start time/命令行、
两个直接子进程及其命令行，以 pidfd 只终止这些进程，替换已退出的测试 helper 和审计采样器。
两条 RDMA Session 和 swap 保留，不需重启 Server；重新进行 core、边界、容量和 BFS，
原失败记录不改写为成功。核验不符则停止并保留现场，不自动扩大恢复范围。

第一次恢复在换出测试负载退出后等待空池失败：错误行显示 Server 0 PMem 残留 32 个 live Slot；
`SwapCached=0`，swap 已用 448 KiB。这与内核 per-CPU swap entry 归还缓存一致：
`free_swap_slot()` 将条目暂存，达到 64 项才批量调用 `swapcache_free_entries()`，
后者经 `swap_range_free()` 才触发 Frontswap invalidate；单纯等待不会保证清空。
本次不修改内核，补充上述阶段间 swap drain 及缓存批量归还检查。

首次空池检查失败曾使用下面的第二次、单独恢复入口（历史记录）：

```bash
sudo /usr/bin/python3 /mnt/sdb3/data/yxr/projects/hermit/remoteswap/tests/stage3_host.py \
  /tmp/hermit-stage3-core-bfs-20261128 --recover-slot-cache
```

该入口只接受已识别的空池检查失败，核验同一 Client 实例、swap、当前源文件/二进制哈希，
定位唯一 `--recover-core-store` 旧控制器并确认其无活跃子进程，以 pidfd 仅停止它。
保存第二次失败证据后，先 drain 测试 swap 并验证空池，再 swapon，保留两条 RDMA 连接，
安装准备好的测试 helper 并继续原测试流程。若 swapoff 后仍非空，则停止，不绕过检查。
