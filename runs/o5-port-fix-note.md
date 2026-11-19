# O5 组间端口检查修复记录

记录时间：2026-11-19T14:33:14+08:00。

O5-C1 在 2026-11-19 14:24:45 开始，240秒负载结束后生成完整结果，validation 为 PASS，34项 SHA256SUMS 校验通过。目录共35个文件（含清单），修复前后全部保持逐字节一致。O5-C3 尚未启动，目录不存在。

切换 C3 前原脚本用不带 SO_REUSEADDR 的临时 socket.bind 检查 18081 空闲。HAProxy 停止后，旧已接受连接的 TIME_WAIT 可以让这个探测报 EADDRINUSE，尽管没有进程处于 LISTEN。这一行为已在独立随机本机端口复现：旧 bind 探测失败，允许复用的 bind 成功，ss 显示无监听。

修复仅把启动前检查改为 ss 的 TCP LISTEN 查询，真正的端口绑定交给 HAProxy；已有实际监听仍拒绝启动。没有改变 HAProxy 配置、压测参数、故障时间、注入命令、数据采集或汇总方式。

C1 原脚本 SHA-256：24b8f6da59ead0bc1a0f3cdbe7c55c805252be6b916c4f2293c645f078cf5eca。
修复后脚本 SHA-256：962333df2827fa6ecdf5cff820cac697c5c60a8540718d2b01b9ea00c3018401。
原脚本保存在 runs/o5-c1-runner-before-port-fix.py，对应 C1 runner-plan.json 的 inputs.script_sha256。

6项夹具测试通过（含真实随机端口的 LISTEN/TIME_WAIT 回归测试）；C3只读预检通过。运行 python3 scripts/run-o5.py --scenario c3 续跑，输出仍为 runs/o5-c3/。不重新执行C1，不覆盖成功目录。
