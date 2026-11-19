# O5 换负载均衡器：测前预测

记录时间：2026-11-19T14:12:43+08:00。状态：**尚未执行 O5 正式压测或故障注入**。本文在结果产生前保存；测后保留原文，另写分析解释偏差。

基底提交：`b42e8641d4572e73a36b7e4b657a7362f40d1203`。HAProxy 版本：`HAProxy version 2.8.30-0ubuntu0.24.04.2 2026/10/27 - https://haproxy.org/`。配置：`deploy/haproxy-o5.cfg`，SHA-256 `ab712e585c91abbf949daf055a5290c6cbd77b834974cd533e7a96487c83071f`。完整版本和环境分别保存到 `deploy/o5-haproxy-version.txt`、`deploy/o5-environment.json`。

## 要回答的问题

用 HAProxy 接管业务流量并配置 HTTP 主动健康检查，重做 C1、C3：比较实际检出/恢复时间、客户端错误与尾延迟，以及 lbd 命令行参数与 HAProxy 配置文件的对应关系。O5 不重做 C2。

业务路径：`loadgen → HAProxy 127.0.0.1:18081 → enrolld 8101/8102/8103`。现有 `lbd :8080/:8090` 仅保留给 scaler 进行后端登记和就绪跟踪；O5 压测不请求 :8080。scaler 仍检查 systemd 进程状态、补建被杀实例。HAProxy 静态配置三个固定端口，自行检查它们，不接收 scaler 的删除/登记通知。该集成差异是比较条件，必须在结论中说明。

## 保持的条件与已知差异

三个实例初始均 ready，CPUQuota=25%、AllowedCPUs=2、MemoryMax=128M，预热 60s、模拟拉镜像 20s，应用 `-work 1000000`。控制、HAProxy、采集与压测绑定 CPU 0–1。客户端沿用 Poisson 到达、默认 seed、160 请求/秒、240 秒、2s 客户端超时；成功请求 200ms 为 SLO 阈值，`-stop-below 0`。

HAProxy 使用 roundrobin，GET `/readyz` 并明确要求 HTTP 200；`inter/fastinter/downinter=1s`、`fall=3`、`rise=2`、`timeout check=500ms`、`timeout connect=500ms`。HAProxy 2.8 默认初始 UP，脚本必须等三台均有真实的 L7OK 且稳定 UP 两秒后才启动业务压测；DOWN 后仍按 rise=2 恢复。显式关闭重试（`retries 0`、`retry-on none`），不配置按错误的被动摘除，也不配置按业务 p99 的离群摘除。

HAProxy 的 `timeout check` 是连接建立后的检查等待时间；连接阶段另受 `timeout connect` 和 inter 限制。其 server/client 2s 是相应阶段的无活动超时，不能宣称与 lbd/客户端的整体 2s 完全等价。检查调度、连接复用、日志开销也不同。lbd 仍发送少量控制就绪探测，HAProxy 另发一组健康探测。

## O5-C1 预测

计划：第 45 秒采故障前 CPU；第 60 秒 kill 8102；第 150 秒核对 scaler 和 HAProxy 三台都恢复；第 160 秒 pause 8103 二十秒；第 180 秒左右自动 SIGCONT；240 秒结束。

停止后，预计 scaler 仍较快发现进程退出并补建。HAProxy 没有收到 scaler 的删除通知，须依赖自身连续三次失败检查，预计经过若干秒（粗估约 2–5s）才 DOWN。它可能比正式 C1 的 `removed` 0.363s 慢，但后者包含 scaler 主动删除这条更快路径，不能据此比较两者主动健康检查的优劣。检测前可能有连接失败、502/503/504 或客户端超时，具体状态码由实际实现决定。

预计替补仍需约 20–21s 拉镜像、60s 预热及就绪确认，从创建到可用约 83–85s；HAProxy 与 scaler/lbd 各自的检查相位不同，恢复 UP 与 scaler ready 不一定同刻。

暂停时进程仍 active，预计 scaler 不因进程退出而补建；HAProxy 的 `/readyz` 探测超时并积累连续失败，可能在约 2–5s DOWN，和正式 C1 卡住检出 2.889s 处于同一数量级。粗略以 160/3×检出时长估计有约 107–267 个请求暴露于故障窗口，但在途请求、到达随机性和超时使实际错误数可能不同。SIGCONT 后预计需约 1–3s 才恢复 UP，以真实日志/采样为准。

## O5-C3 预测

正式标定仅引用 `runs/c1-stop/cpu-before.json` 中 8102 的 U=`0.1105564` 核，`U/0.85×100=13.006635…%`，最近整数为 **13%**。不使用任何手动练习样本。计划第 60 秒将 8102 从 25% 降到 13%，第 180 秒恢复 25%。

预计 CPU 限流会使 8102 的业务延迟和全站尾延迟增大，但轻量 `/readyz` 仍可能及时成功，因此 HAProxy 普通健康检查很可能不把它判 DOWN。不预设必须出现错误或超过 200ms；正式 C3 未摘除组已展示在 SLO 内变慢的情况。若实际 DOWN，则结合检查原因、CPU throttling、PID 和时序解释，不能为了符合预测改配额。

本配置不实现 lbd 的“10s 窗口业务 p99 相对其他实例的中位数超过 3 倍、且绝对超过 50ms”等离群规则，不能期待它自动复现 c3-outlier 的三次 ejected。正式对照为 `runs/c3-slow/` 和 `runs/c3-outlier/`；应分别讨论主动就绪检查与业务指标摘除的能力，不能把 HAProxy 的错误被动检查等同于延迟离群摘除。

## 证据和测量约定

自动目录为 `runs/o5-c1/`、`runs/o5-c3/`；练习使用 `runs/o5-manual-c1-01/`、`runs/o5-manual-c3-01/` 等独立名字。任何已有目录都拒绝覆盖。C 正式 88 个原始文件按原清单在运行前后复核。

客户端对比统一取同一 loadgen 的 `steps.csv`、`timeline.csv`。`marks.csv` 记录实际注入毫秒时间，不能用计划第 60 秒代替实际值。HAProxy Runtime API `show stat` 以目标 200ms 间隔采样，保留原始服务器行，状态转换写入 `haproxy-events.csv`；事件时刻是采样观测时刻，内部转换的保守区间为上次采样请求开始到本次采样响应结束，实际最大间隔记入 `observer-meta.json`。原生健康日志和业务日志导出并压缩保存，journal 时间为接收日志的时间，不冒充内部精确转换时刻。

`control-lbd-events.csv` 仅作 scaler 控制路径旁证，其 removed/down 不计作 HAProxy 检出。HAProxy `%Tr` 业务日志测的是服务端响应头等待时间，不等于 lbd 的端到端直方图或客户端 p99；保留每请求日志做按实例诊断，跨实现性能对比使用客户端指标。不用 stats 中的 rtime 平均值冒充 p99。

## 资料

- HAProxy 2.8 配置手册：https://docs.haproxy.org/2.8/configuration.html
- HTTP 健康检查：https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/health-checks/
- Runtime API show stat：https://www.haproxy.com/documentation/haproxy-runtime-api/reference/show-stat/

本文件只记录预测和实验设计，没有 O5 实测结论。
