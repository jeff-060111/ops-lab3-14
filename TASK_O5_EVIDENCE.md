# 任务 O5：结果与证据索引

**已完成两轮正式采集、测前预测、配置对照和报告分析。** 主报告见 [LAB3.md 第12节](LAB3.md#12-选做-o5换负载均衡器)，数据审计见 [O5_EVIDENCE_AUDIT.json](O5_EVIDENCE_AUDIT.json)。本次材料建立在 C 提交 `b42e8641d4572e73a36b7e4b657a7362f40d1203` 上；最终包含O5的提交号以仓库历史和提交回执为准。

| 要求 | 证据 |
|---|---|
| 先预测再测量 | [测前预测原文](runs/o5-prediction-before.md)，两轮各保存同字节快照及hash，准备14:12:43，负载开始14:24:45/14:35:50 |
| 替换业务负载均衡器 | [HAProxy配置](deploy/haproxy-o5.cfg)、[构建版本](deploy/o5-haproxy-version.txt)、[环境](deploy/o5-environment.json)，业务入口18081直达三台实例 |
| 配置主动健康检查 | GET /readyz、明确HTTP200，inter=1s、fall=3、rise=2；检查/连接超时分别设置 |
| 重做C1并记录注入 | [marks](runs/o5-c1/marks.csv)、[客户端逐秒](runs/o5-c1/timeline.csv)、[生命周期](runs/o5-c1/instances.csv) |
| C1独立检出与恢复证据 | [HAProxy状态](runs/o5-c1/haproxy-stats.csv)、[观测事件](runs/o5-c1/haproxy-events.csv)、[检测区间](runs/o5-c1/haproxy-detection.json)、[原生健康日志](runs/o5-c1/haproxy-health-native.jsonl) |
| 重做C3、沿用正式C1标定 | [计划与来源hash](runs/o5-c3/runner-plan.json)、[降配快照](runs/o5-c3/during-slow.json)、[恢复快照](runs/o5-c3/after-quota-restore.json)、[注入](runs/o5-c3/marks.csv) |
| C3业务慢与就绪正常分离 | [完整客户端结果](runs/o5-c3/steps.csv)、[全部状态采样](runs/o5-c3/haproxy-stats.csv)、[原生计数](runs/o5-c3/haproxy-stats-end.csv)、[响应头耗时样本统计](runs/o5-c3/haproxy-backend-response-header-latency.json) |
| 可重复与异常说明 | [当前脚本](scripts/run-o5.py)、[C1原脚本](runs/o5-c1-runner-before-port-fix.py)、[端口修复记录](runs/o5-port-fix-note.md)、[手动手册](docs/O5_手动实验操作手册.md) |
| 校验与保护C | [只读审计脚本](scripts/audit-o5.py)、[审计JSON](O5_EVIDENCE_AUDIT.json)、[提交材料清单](O5_EVIDENCE_SHA256SUMS.txt)；70个O5原文件、88个C原文件逐字节核验 |

## 结果摘要

| 运行 | 发出 | 成功 | 错误 | 成功请求p99 ms | 缺口 |
|---|---:|---:|---:|---:|---:|
| O5-C1 | 38626 | 38298 | 328（0.85%） | 13.05 | 7s |
| O5-C3 | 38626 | 38626 | 0 | 26.63 | 0s |

C1 kill DOWN观测延迟区间2.425–2.625s，pause为3.476–3.677s；首次UP从注入算83.867s、22.486s。lbd正式C1 kill的0.363s removed来自scaler通知，不能当作同一主动检查算法的快慢排名。

C3实际25%→13%→25%，同一PID，限流计数增加；每台1210条状态采样均UP、前后chkfail/chkdown均0。正式lbd普通组、离群组的客户端p99分别24.70ms、13.77ms，后者三次ejected；本次HAProxy普通检查没有按业务p99的离群规则。这说明检测信号有不同盲区，三组均未突破200ms阈值。

## 需要保留的限制

- **C3日志少2条。** 客户端与HAProxy成功计数38626；journal与解析CSV38624。按后端日志分位数仅描述现有样本；主客户端记录完整，没有补造数据。详见审计JSON。
- **不同测量位置。** 客户端p99用于跨实现对比；HAProxy `%Tr`为响应头等待，不等于lbd直方图；stats rtime为平均值，不能冒充p99。
- **采样精度。** DOWN/UP来自约200ms轮询，实际最大间隔C1=201ms、C3=202ms；区间包括Runtime API请求耗时。journal时间为接收时刻。
- **不同控制集成。** lbd保留在scaler控制路径，O5业务不经过它；control-lbd-events.csv仅为旁证，不能当HAProxy检出。
- **单次/不同时间。** C与O5基线在同日不同时间运行，不能据此给出普遍性能排序。
- **端口修复发生在两轮之间。** AST验证只改启动前监听检查，C1原文件全部不变；C3并未在失败切换中启动。

## 核验与复现

```bash
python3 scripts/audit-o5.py
python3 scripts/audit-c.py
python3 scripts/test-o5-preparation.py
```

前两条仅检查保存数据；第三条使用临时夹具和独立随机本机端口，不启动实验HAProxy/loadgen、不向应用实例发信号。历史准备检查保存在 deploy/o5-preparation-checks.json，记录的是C3正式运行前的状态；当前完整结果以 O5_EVIDENCE_AUDIT.json 为准。

每轮原始文件清单自包含：`(cd runs/o5-c1 && sha256sum -c SHA256SUMS.txt)`，C3同理。根目录O5清单涵盖本次提交的报告、脚本和正式材料；C清单仅更新LAB3.md因新增第12节产生的报告hash，C的88个原始文件与原审计保持不变。

重新运行使用 `python3 scripts/run-o5.py --prefix o5-repeat-01`；先恢复lbd/scaler、三台ready和25%配额，再执行对应`--check --prefix o5-repeat-01`。已存在的正式目录不会覆盖。手动练习材料已保存本地，不作为本次正式结果。
