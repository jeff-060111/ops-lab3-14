# O5 复现与材料入口

**状态：C1、C3正式实验、报告和证据审计均已完成。** 结果见 [LAB3.md 第12节](LAB3.md#12-选做-o5换负载均衡器)，完整证据导航见 [TASK_O5_EVIDENCE.md](TASK_O5_EVIDENCE.md)，当前数据核验见 [O5_EVIDENCE_AUDIT.json](O5_EVIDENCE_AUDIT.json)。

准备时的版本、环境和检查结果保存在 deploy/o5-haproxy-version.txt、deploy/o5-environment.json、deploy/o5-preparation-checks.json；这些文件的状态文字对应当时检查时刻。测前预测原文、C1原脚本和端口修复记录均保留。C3保存业务日志38624条，比完整客户端及HAProxy计数38626少2条，报告已说明该限制。

只读核验已保存的结果，无需scaler运行：

```bash
python3 scripts/audit-o5.py
python3 scripts/audit-c.py
python3 scripts/test-o5-preparation.py
```

重新实验前确认原lbd/scaler运行、三台ready、配额25%；使用新的前缀，已有目录拒绝覆盖：

```bash
python3 scripts/run-o5.py --check --prefix o5-repeat-01
python3 scripts/run-o5.py --prefix o5-repeat-01
```

输出为 runs/o5-repeat-01-c1/ 与 runs/o5-repeat-01-c3/。人工练习按 [手册](docs/O5_手动实验操作手册.md)，采用独立目录名；练习采集不会定时注入，由操作者在另一SSH终端执行故障指令。HAProxy业务入口为127.0.0.1:18081，原lbd仅保留scaler控制用途。
