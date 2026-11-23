# Adaptive 未知页首选 DRAM：Client 1.13

## 目标与规则

将没有可用热度的 Adaptive Store 默认放到 DRAM，并在单/双 Server 的 DRAM 池中轮转。
沿用既有 4 KiB、1/2 Server、一个活动 swap area 和静态拓扑约束。

| 条件 | 首选位置 | reason | RR |
| --- | --- | --- | --- |
| Inactive / Warmup | 原 offset 两级 Interleave | 0 / 1 | 不推进 |
| Adaptive，无快照 | DRAM | 2 | DRAM RR |
| Adaptive，无可用阈值 | DRAM | 3 | DRAM RR |
| Adaptive，PFN miss | DRAM | 4 | DRAM RR |
| Adaptive，已知正热度 >= T | DRAM | 5 | DRAM RR |
| Adaptive，已知正热度 < T | PMem | 6 | PMem RR |
| Adaptive，已知热度 0 | 原 offset 两级 Interleave | 7 | 不推进 |

这里的未知默认适用于整个 Client，包括监控地址空间以外的 PFN。
未知保持 heat_ppm=-1，不改记为 hot，不参与均值或阈值样本；已知零热度仍参与均值。
当前合法上传中，无可用阈值时没有已知 PFN 记录。
global-mean/capacity-mean 的阈值计算、继承与更新规则不变。

未知和已知热页共用一个 DRAM 原子 RR 序列；单 Server 始终选择 0，双 Server 按 ticket%2 选节点。
RR 按选择尝试推进，分配失败不回滚，换快照不清零，新 Start 才清零。
零热度只沿用 Interleave 的首选 Server/Tier，实际 Slot 继续从动态空闲链表分配。

首选池满时继续按“另一 Server 同 Tier → 首选 Server 另一 Tier → 剩余池”尝试，
单节点跳过不存在的候选。未知页也允许进入 PMem，全部不可用仍返回原 Store 错误路径。
本次不增加 DRAM 配额、热页容量预留或主动迁移；未知页可能与已知热页竞争 DRAM。
已有映射、Load、Invalidate、在途引用、失败回滚、Completion/poll/drain 均不变。

## 修改范围与接口

- `client/rswap_placement.c`：三种未知原因汇入 DRAM RR，已知正热度共用 Tier RR 出口，零热度直接保留 Interleave。
- `client/rswap_placement.h`、`rswap_trace.h`：定义并记录数字 `placement_rule=1`；`placement` 状态也显示此值。
- `client/rswap_client.c`：模块版本升级 1.13。
- `tools/rswap_trace.py`：根据 placement_rule 验证未知首选和共享 ticket，兼容旧日志；`rswap_analysis.py` 的逐条 TSV 保留标识。
- 更新生产 C 策略/上传/I/O 回归、Trace 回归，以及当前使用文档。

placement_rule 缺省或 0 表示历史未知 Interleave；1 表示本轮未知 DRAM RR。
标识独立于 threshold policy（global-mean/capacity-mean），不通过热度或目标位置猜测日志版本。
旧 Trace 无阈值字段时仍按旧的严格 heat>mean 规则验证；历史 1.12 保留正热度 heat>=T 和零热度 Interleave。
新规则下未知/热页 ticket 在同 run、同 Tier 内不得重复，包含分配失败的尝试。
Trace 继续全数字格式，保留 reason 0..7 与原 alloc_seq/Store post/WC 关联。
`policy_stats` v2 的计数格式和覆盖率含义不变；未知进入 DRAM 不增加已知热度覆盖率。

不修改 Server、RDMA 协议 v2、上传 ABI v2、I/O stats v2 或 #8 内核。
1.12 ABI v2 采样器可沿用，但正式分析应使用更新后的工具。

## 本地检查

```bash
make -C remoteswap/client KDIR="$PWD/linux-5.14-rc5" -j12
make -C remoteswap/tools
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s remoteswap/tests -p 'test_*.py'
bash -n remoteswap/client/manage_rswap_client.sh
git diff --check
modinfo -F version remoteswap/client/rswap-client.ko
```

专项覆盖单/双节点、三种未知原因、未知/热页共享 RR、零热度保留原 offset 映射且不消耗 ticket、
独立 PMem RR、新 Start 重置及快照替换不重置。实际策略接实际 Store 分配/MR/QP/Load 函数，
用每 Server 一页 DRAM 的小池验证 DRAM 满后走 PMem、Stop 后原位置读取和最终全部回收。
Trace 覆盖新旧语义、共享 ticket 冲突（含失败分配）、容量 Fallback/回收，以及实际 publication 热度关联。
这些是生产函数的用户态 stub 检查，不替代 HCA/DMA、实页内容或内核并发验收。

2026-11-23 本地执行结果：

- Client 完成增量编译，修改涉及的 placement、RDMA/Trace 等对象实际重新构建，无编译告警；modinfo 确认 **1.13**。
- `make -C remoteswap/tools` 成功，采样器实现本轮无需修改。
- 专项 **13 项**及整组 **84 项**无特权回归通过；包含实际 sampler→内核上传桥接及新规则逐代热度关联。
- `bash -n` 与 `git diff --check` 通过。
- 未加载模块、启停 swap、连接/重启 Server 或运行实机/BFS；本地 stub 的成功不表示实机内容验收完成。

日志位于 `/tmp/hermit-unknown-dram-client-build.log`、`/tmp/hermit-unknown-dram-tools-build.log`、
`/tmp/hermit-unknown-dram-targeted.log` 和 `/tmp/hermit-unknown-dram-regression.log`。

## 实机测试方案与执行边界

随后按操作者要求跳过独立功能测试，只执行一次双节点 BFS。
Interleave DRAM/PMem 总带宽 386.42/386.39 MiB/s，Adaptive 524.25/198.81 MiB/s，
后者 DRAM 约为 PMem 的 2.64 倍；无新增 I/O 错误或容量回退，正常收尾。
详见 [本轮结果](unknown-dram-bfs-result.md)。以下独立功能项仍未执行，不追加实验。
随后另按要求完成一次单节点 16/32 GiB BFS：Adaptive DRAM/PMem 217.20/332.04 MiB/s，
DRAM 满池和大量容量回退使 PMem 更高；结果见 [单节点报告](unknown-dram-single-bfs-result.md)。
再按要求以单节点 32/32 GiB、64 GiB swap 运行一轮：Interleave 349.19/349.14 MiB/s，
Adaptive 536.08/224.57 MiB/s，无容量回退；短 Trace 丢事件，完整决策验证未认证。
详见 [32/32 GiB 报告](unknown-dram-single32-bfs-result.md)。

1. **单节点小工作集**：覆盖 Warmup、已知零/正热度、PFN miss；额外在首个快照前进入 Adaptive，
   并发布空热度快照，分别覆盖无快照/无阈值。未知首选 DRAM、零热度按原 offset 分布，
   正热度分类保持不变。逐页/逐 word 读回验证内容。
2. **双节点小工作集**：混合未知、热、冷、零页，采集 mapping/decision/store_result 和逐页快照。
   检查未知和热页合用 DRAM RR，PMem RR 独立，RR 看首选顺序而非实际完成顺序；
   用 alloc_seq 关联事件，检查所有 CPU buffer loss 为零。
3. **容量回退**：用可控分配耗尽首选 DRAM 池，验证先选其他 Server DRAM；所有 DRAM 不可用后
   才跨 Tier，reason 仍保留未知。实机是否缩小现有 Server 池或加大工作集，在执行前确定；
   本轮不宣称已覆盖整池耗尽或磁盘写回退。
4. **生命周期**：重复 Store、策略 Stop/新 Start、快照更新及活进程 swapoff；Load 读取原映射，
   内容正确，最终 live/retired 为零、free=capacity，无新增 WC/提交错误，正常卸载。
5. **BFS 集成**：同条件观察完整 Interleave/Adaptive 稳定阶段的 Read、Write、总带宽，
   同时报告零热度、未知、正热度比例和跨 Tier Fallback。DRAM 带宽上升不代替内容/机制验收，
   也不保证算法运行时间改善；需要比较时与 1.12 使用相同配置。

实现阶段只完成代码与本地验证；随后分别按要求完成上述双节点/单节点各一次 BFS 观察。
独立内容/容量专项及对照重复实验仍未执行。
