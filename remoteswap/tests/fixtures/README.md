# 历史协议兼容测试夹具

`rswap_hotness_v1.c/.h` 保存 Client 1.17 的 PFN 采样及发送端，
供 Patch 8/10、旧显示格式、capacity-mean 的回归测试使用。
它们验证仍保留的旧上传 ABI、放置实现及历史日志读取能力；
**不表示生产采样器仍使用旧算法，也不作为第二阶段验收证据。**

当前生产采样器为 `../../tools/rswap_hotness.c`；第二阶段直接测试它的
`test_stage2_hotness.py`、`test_stage2_loop.py` 和 `test_stage2_publish.py`
分别覆盖采样/历史、完整主循环，以及真实生产发送端到 Client 接收端的协议。
