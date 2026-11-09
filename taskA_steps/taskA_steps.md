# 任务 A 实验操作记录（2026-11-09）

主机 `root@124.71.144.6`，工作目录 `/root/lab3`。运行前确认没有 `lab3-*` 实例；现有 Prometheus 位于 `127.0.0.1:9090`。发现 node_exporter 未运行，按任务 0 的参数恢复，只监听 `127.0.0.1:9100`，绑核 `0-1`。保留主机上的其他服务，未改动其配置。

预测先写入 `LAB3.md`，A1/A3 初始预测另存 `runs/a1/predictions-before.md`；A2 降低配额前的预测另存 `runs/a1/a2-prediction.md`。以下命令均在云主机仓库根目录执行。重新实验请更换 run 名称，避免覆盖原始数据。

## A1

```bash
for n in 1 2; do
  systemd-run --quiet --scope -p CPUQuota=50% bin/enrolld -burn "$n" -for 10s \
    > "runs/a1/burn-$n.txt" 2>&1
done
```

## A2

已在 `deploy/queries-red.txt` 填写两个 cgroup 周期计数器的 rate 之比，限定 `job="enrolld"`。实例不绑核，loadgen 绑核 `0-1`，因此共用 CPU；这一点与 B 的分核方式不同。

```bash
systemd-run --unit=lab3-a -p CPUQuota=100% -p MemoryMax=256M \
  "$PWD/bin/enrolld" -addr 127.0.0.1:8081 -data "$PWD/data/a" -work 2000000
export GEN_CPUS=0-1 DATA=data/a
scripts/run.sh a2-base -url http://127.0.0.1:8081/api/courses/1 -rates 30,60 -step 20s
# 基线 CPU/request 约 4.24～4.35 ms，选择 R=94，并补测同到达率的对照。
scripts/run.sh a2-q100 -url http://127.0.0.1:8081/api/courses/1 -rates 94,94 -step 20s
systemctl set-property --runtime lab3-a CPUQuota=50%
scripts/run.sh a2-q50 -url http://127.0.0.1:8081/api/courses/1 -rates 94,94 -step 20s
# q100 两阶段平均 CPU 的较大值 0.411707 / 0.7 = 0.588153 核，向上取 60%。
systemctl set-property --runtime lab3-a CPUQuota=60%
scripts/run.sh a2-q60 -url http://127.0.0.1:8081/api/courses/1 -rates 94,94 -step 20s
systemctl stop lab3-a
systemd-run --unit=lab3-a --setenv=GOMAXPROCS=1 -p CPUQuota=50% -p MemoryMax=256M \
  "$PWD/bin/enrolld" -addr 127.0.0.1:8081 -data "$PWD/data/a" -work 2000000
scripts/run.sh a2-q50-p1 -url http://127.0.0.1:8081/api/courses/1 -rates 94,94 -step 20s
```

所有 run 沿用默认 Poisson 到达和 seed=1；各轮到达序列可对照，但调度、GC 和背景负载仍会变化。`a2-design.txt` 保存原 PID 和热修改配额后的 PID，验证没有为 50% 对照重启服务。

## A3

`run-a3.sh` 停止上轮实例、清除失败状态，然后以指定 `MemoryMax`、`MemorySwapMax=0`、`-report-max` 重建实例。CPU 不设配额，也不继承 A2 的 GOMAXPROCS 环境变量。每轮均执行 `scripts/run.sh`，到达率 `10,20,30`、每阶段 20 秒；增加 `-stop-below 0` 以保留 OOM 后的完整阶段，死亡后的连接错误不代表独立 OOM。

`watch-memory.py` 额外每 50 ms 读取 cgroup 的 current、peak、max 和 OOM 事件。Prometheus 仍按原配置每秒抓取 enrolld，`red.csv`/`use.csv` 均由原始脚本产生。`idle-metrics.txt`、`unit-properties.txt`、`exit-evidence.txt` 分别保存空载基线、实际限制和 systemd/内核证据。

```bash
bash taskA_steps/run-a3.sh a3-oom 200M 0
# OOM 早于 20 请求/秒阶段的完整查询窗口，补一轮容量测量。
bash taskA_steps/run-a3.sh a3-sizing 512M 0
# max(14*8*2+8.6328125, 298.875, 294.63671875)*1.1 = 328.7625 MiB，取 336M。
bash taskA_steps/run-a3.sh a3-limit336 336M 0
# 8*8*2+8.6328125 = 136.6328125 MiB < 200 MiB，保守留余量。
bash taskA_steps/run-a3.sh a3-bounded8 200M 8
systemctl stop lab3-a
systemctl reset-failed lab3-a 2>/dev/null || true
```

## 结束与核对

完成各组实验后 `systemctl stop lab3-a` 并清除该服务失败状态。保留监控和数据供后续任务使用。将 `runs/a1`、所有 `runs/a2-*`、`runs/a3-*`、任务 A 文档和查询文件同步到本地；原始 CSV/日志不手工修改。报告中的表格用 `scripts/table.sh` 生成。
