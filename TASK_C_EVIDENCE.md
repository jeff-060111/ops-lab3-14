# 任务C：正式结果、证据索引与问题回答

整理时间：2026-11-19T12:55:12+08:00。范围为任务C及其对应的健康检查分析题；O5仍待完成。

## 1. 正式材料范围

只使用五个正式目录：`runs/c1-stop/`、`runs/c2-default/`、`runs/c2-sensitive/`、`runs/c3-slow/`、`runs/c3-outlier/`。手动练习目录不进入本索引、汇总、报告结论或归档。

主报告为 [LAB3.md](LAB3.md)：第4节记录C1～C3的预测、实测和解释；第6节问题3回答健康检查。完整LAB3还包含小组其他任务的背景内容，本归档不声明任务D或O5已经完成。

基底提交：`09a46ca29f989397a1b4bdd66c5ed275c6373c09`。本次材料在此基底上整理；包含全部任务C材料的提交号请以仓库提交历史及提交回执为准，基底号不代表最终评分提交。

归档是C材料快照，不包含bin、tools、data、Git数据库和练习数据。独立复现仍需完整小组仓库、已准备的Linux主机及课程工具。

## 2. 正式结果总表

| 运行 | 发出 | 成功 | 错误 | 慢成功请求 | 全程成功p99(ms) | 缺口(s) | 直接出处 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| C1 停止与卡住 | 38626 | 38456 | 170 | 0 | 13.13 | 4 | [steps.csv](runs/c1-stop/steps.csv) 的all行；[summary.md](runs/c1-stop/summary.md) |
| C2 默认 | 19326 | 19019 | 307 | 455 | 1201.16 | 5 | [steps.csv](runs/c2-default/steps.csv) 的all行；[summary.md](runs/c2-default/summary.md) |
| C2 灵敏 | 19326 | 18944 | 382 | 45 | 13.68 | 3 | [steps.csv](runs/c2-sensitive/steps.csv) 的all行；[summary.md](runs/c2-sensitive/summary.md) |
| C3 不启用离群 | 38626 | 38626 | 0 | 0 | 24.70 | 0 | [steps.csv](runs/c3-slow/steps.csv) 的all行；[summary.md](runs/c3-slow/summary.md) |
| C3 启用离群 | 38626 | 38626 | 0 | 0 | 13.77 | 0 | [steps.csv](runs/c3-outlier/steps.csv) 的all行；[summary.md](runs/c3-outlier/summary.md) |

慢成功请求指成功但超过200ms；p99只统计成功请求。不能把五轮混成一个成功率来比较不同故障，也不能平均分位数。

## 3. 关键结论到原始证据的映射

行号按CSV实际文件行计，含表头。事件筛选只取该轮steps的[start_unix,end_unix)；累计日志中的历史行不作为本轮结论依据，也不手工删改。

| 结论或待核对数值 | 原始文件和定位 |
| --- | --- |
| C1停止：0.363秒后removed；17个相关逐秒错误 | [marks.csv](runs/c1-stop/marks.csv) 第3行；[lbd-events.csv](runs/c1-stop/lbd-events.csv) 第5行；[timeline.csv](runs/c1-stop/timeline.csv) 第62行 |
| C1卡住：2.889秒后down；153个相关逐秒错误 | [marks.csv](runs/c1-stop/marks.csv) 第4行；[lbd-events.csv](runs/c1-stop/lbd-events.csv) 第8行；[timeline.csv](runs/c1-stop/timeline.csv) 的sec=160、161、162 |
| C2 默认：首次down距pause 3.298秒 | [marks.csv](runs/c2-default/marks.csv) 第2行；[lbd-events.csv](runs/c2-default/lbd-events.csv) 第8行 |
| C2 灵敏：首次down距pause 0.341秒 | [marks.csv](runs/c2-sensitive/marks.csv) 第4行；[lbd-events.csv](runs/c2-sensitive/lbd-events.csv) 第20行 |
| C3标定：正式C1中8102用量0.1105564核，计算降至13% | [cpu-before.json](runs/c1-stop/cpu-before.json) 的instance=127.0.0.1:8102；[runner-plan.json](runs/c3-slow/runner-plan.json) 的inputs.calibration |
| C3实际配额25%→13%→25%，PID保持不变 | [before-slow.json](runs/c3-slow/before-slow.json)、[during-slow.json](runs/c3-slow/during-slow.json)、[after-quota-restore.json](runs/c3-slow/after-quota-restore.json) 的resources["8102"] |
| C3变慢但未主动down：8102节流、业务尾延迟升高 | [during-slow.json](runs/c3-slow/during-slow.json) 的cpu.stat；[backend-p99-ms-range.json](runs/c3-slow/backend-p99-ms-range.json) 的backend=127.0.0.1:8102；[backend-up-range.json](runs/c3-slow/backend-up-range.json)；[lbd-events.csv](runs/c3-slow/lbd-events.csv) |
| C3首次延迟摘除等待25秒，错误率为0，之后共三次 | [marks.csv](runs/c3-outlier/marks.csv) 第4行；[lbd-events.csv](runs/c3-outlier/lbd-events.csv) 第20行 及后续ejected/restored行 |
| C3配额恢复后仍可处于ejected；最后恢复比分配额晚6.001秒 | [after-quota-restore.json](runs/c3-outlier/after-quota-restore.json) 的state.backends与resources；[marks.csv](runs/c3-outlier/marks.csv) 的本轮restore；[lbd-events.csv](runs/c3-outlier/lbd-events.csv) 的最后restored |

容量线C≈79.6来自正式B0： [补测steps](runs/b0-cap-110-150/steps.csv) 首次出错前最大成功吞吐113.7，乘0.7得到79.59；[B0元数据](runs/b0-one/meta.txt)记录单实例25%配额配置。由此选择约2C，即160请求/秒。

## 4. 问题回答完成清单

| 项目 | 已记录内容 | 位置 |
| --- | --- | --- |
| C1预测与实测 | 退出和卡住分别由谁发现、检出/补建/恢复、错误、预测偏差 | LAB3第4节C1 |
| C2配置取舍 | 两组参数、错误与慢请求、恢复后积压、与预测不同之处 | LAB3第4节C2 |
| C3配额算式 | 正式C1的U、13%算式、25%恢复及实际cgroup证据 | LAB3第4节C3 |
| C3健康检查盲点 | 哪个实例变慢、主动状态up、离群触发及反复摘除 | LAB3第4节C3 |
| 分析题3 | 四类故障的检出机制、速度/扰动成本、离群收益与风险、限制比例及成立条件 | LAB3第6节问题3 |

**问题 3：健康检查。** 停止、卡住、暂停、变慢四次注入中，各是谁、用多久发现的，健康检查看到了什么？检出快与误判少之间怎样取舍？离群摘除解决了什么问题，又带来什么风险？为什么要限制同时摘除的比例？

所用数据：第4节及正式C1、C2、C3目录中的marks、lbd-events、instances、steps、summary和C3资源/指标快照；不使用练习数据。

C1停止进程后，scaler约0.363秒发现退出并通知lbd移除，产生17个错误；暂停20秒时进程仍存在，由主动健康检查在2.889秒判down，产生153个错误，说明进程存活不等于能够响应。C2共同暂停两秒，默认和灵敏检查首次判down分别为3.298、0.341秒。灵敏组错误更多（382对307），却有更少慢请求（45对455）、更短缺口（3对5秒）；默认组恢复执行后也继续探针超时，说明短扰动的影响包含积压和恢复确认，不能只按间隔乘阈值判断，也不能单凭检出快选择配置。

C3降至13%后，8102发生节流和业务尾延迟升高，但主动健康状态保持up；离群规则25秒后按真实业务延迟摘除，共触发三次。全程成功请求p99从24.70降至13.77ms，两组均无错误或200ms违约，支持补充探针盲点，不能夸大为修复了SLO故障。摘除会把流量推给其他实例，也有样本噪声、重复摘除和共同变慢时难以识别的风险，因此限制比例以保留容量。本结论限于本组三实例、160请求/秒及剩余两台能承载的条件；实际选参需同时衡量错误、延迟、恢复成本和容错容量。

## 5. 测量口径与容易误读的地方

1. 延迟按Unix毫秒字段相减。marks的unix_ms和显示time分两次取时，可能有约1ms差；不混用显示时间来核对毫秒差。
2. 包装脚本用亚秒开始标记，课程CSV用整数秒起点，注入显示可相差约一秒；这不等于定时动作晚了一秒。
3. “注入到判定之间的错误”按整秒桶近似。C2灵敏组276包含两个整秒桶，不能解读为0.341秒内精确失败276个请求。
4. p99只统计成功请求；低p99不能掩盖错误。Prometheus按backend直方图p99与客户端全程p99口径不同。
5. C3的恢复55秒仅是第一次restored，后面仍可再ejected；最终恢复看完整事件序列。资源恢复、分流恢复和统计窗口回落分别记录。
6. “无down且up采样为1”支持健康状态未被判不健康，但没有逐次探针日志，不能证明每次探针都成功。
7. 实例创建时间为负表示负载前已存在；同端口多行可能是进程更替。从创建计实例·分钟，包含拉取和预热。
8. C2三项检查参数一起改变，不能单独归因于某个参数；每种方案仅一轮正式测量，结论限于本机、当前负载和剩余容量条件。

## 6. 配置、版本与复现

共同条件：固定三实例，正常每实例25%CPU、绑核2、128M内存；读请求work=1000000、warmup=60s；loadgen运行在CPU0-1，200ms SLO。C1和C3各240秒；C2每组120秒。C2改变检查间隔/超时/fall；C3只改变outlier开关，降额与恢复保持一致。

脚本源码与运行时runner-plan记录的SHA-256匹配： [C1](scripts/run-c1.py)、[C2](scripts/run-c2.py)、[C3](scripts/run-c3.py)。底层采集与注入为 [run3.sh](scripts/run3.sh)、[inject.sh](scripts/inject.sh)；本机信号发送明确选主进程，避免辅助进程信号兼容错误。[Prometheus配置](deploy/prometheus.yml)与[lab3sum源码](cmd/lab3sum/main.go)随包保留。

以下复现命令在完整仓库的云主机中运行，需要先满足各脚本只读检查条件，并使用未存在的新名字：

```bash
python3 scripts/run-c1.py c1-repeat-01
python3 scripts/run-c2.py --prefix c2-repeat-01
python3 scripts/run-c3.py --prefix c3-repeat-01
```

三条分别独立运行，上一轮结束后才运行下一轮。手动操作另见本地C1/C2/C3手册；不要向已有正式目录写入新负载。

测前证据：C1在 [runner-plan](runs/c1-stop/runner-plan.json) 的inputs.prediction保留启动时预测；C2保留 [原始预测](runs/c2-prediction-before.md) 及 [路径排版更正说明](runs/c2-prediction-format-note.md)；C3保留 [原始预测](runs/c3-prediction-before.md)。排版更正不改变预测数值或假设，原始文件保留。

## 7. 可重复运行的核对与完整性

```bash
python3 scripts/audit-c.py
```

这是只读检查，不执行压测、注入或服务切换。`--write`仅更新根目录派生审计JSON。 [C_EVIDENCE_AUDIT.json](C_EVIDENCE_AUDIT.json)记录基底提交、五轮数值和88个原始文件SHA-256。

已核对：标准原始文件齐全；客户端发送/成功/错误相符；逐秒累计与全程相符；缺口与timeline相符；五个summary均可由lab3sum逐字重建；负载、实例和参数符合计划；两组对照只改变预定配置；运行包装脚本版本与快照相同；预测保留；本节和分析题无占位符；核对过程中原始文件字节未变。

随包 [C_EVIDENCE_SHA256SUMS.txt](C_EVIDENCE_SHA256SUMS.txt)覆盖本次导出的报告、脚本、正式数据及索引。在归档根目录可执行：

```bash
sha256sum -c C_EVIDENCE_SHA256SUMS.txt
```

原始文件与派生说明分开；不能为了让结论更好看而修改CSV、事件或原始查询响应。
