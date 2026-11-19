#!/usr/bin/env python3
"""C3 准备/执行：正式 C1 标定；第60秒降额、第180秒恢复；两组仅改变离群开关。"""
import argparse
import csv
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.parse

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lab3_c2_helpers", REPO / "scripts/run-c2.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
timing = common.timing
LB, SCALER, PROM = common.LB, common.SCALER, timing.PROM
UNIT = "lab3-c3-lbd.service"
MANAGED_UNITS = (UNIT, common.UNIT)
TARGET = "127.0.0.1:8102"
SERVICE = "lab3-inst-8102.service"
MODES = {"slow": False, "outlier": True}
QUERIES = {
    "backend-p99-ms": '1000 * histogram_quantile(0.99, sum by (backend, le) (rate(lb_request_duration_seconds_bucket[30s])))',
    "site-p99-ms": '1000 * histogram_quantile(0.99, sum by (le) (rate(lb_request_duration_seconds_bucket[30s])))',
    "cpu-cores": 'rate(cgroup_cpu_usage_seconds_total{job="instances"}[30s])',
    "cpu-limit-cores": 'cgroup_cpu_limit_cores{job="instances"}',
    "backend-up": 'lb_backend_up',
    "backend-eligible": 'lb_backend_eligible',
    "backend-inflight": 'lb_inflight_requests',
}


def baseline():
    """只读取正式 C1，验证采样位于该轮负载内且在第一次故障前。"""
    run = REPO / "runs/c1-stop"
    files = {name: run / name for name in ("steps.csv", "marks.csv", "cpu-before.json", "meta.txt")}
    if not all(p.is_file() for p in files.values()):
        raise RuntimeError("缺少正式 runs/c1-stop 的标定文件，不能使用练习目录代替。")
    with files["steps.csv"].open() as stream:
        steps = list(csv.DictReader(stream))
    stage = next(r for r in steps if r["step"] != "all")
    if Decimal(stage["target_rps"]) != 160:
        raise RuntimeError("正式 C1 负载不是 160 请求/秒。")
    start, end = int(stage["start_unix"]), int(stage["end_unix"])
    with files["marks.csv"].open() as stream:
        marks = list(csv.DictReader(stream))
    injections = [int(m["unix_ms"])/1000 for m in marks
                  if start <= int(m["unix_ms"])/1000 < end and m["action"] in ("kill", "pause", "slow")]
    if not injections:
        raise RuntimeError("正式 C1 中没有可验证的故障时刻。")
    data = json.loads(files["cpu-before.json"].read_text())
    if data.get("status") != "success" or data.get("data", {}).get("resultType") != "vector":
        raise RuntimeError("正式 C1 的 CPU 查询无效。")
    values = [r for r in data["data"]["result"]
              if r.get("metric", {}).get("job") == "instances" and r["metric"].get("instance") == TARGET]
    if len(values) != 1:
        raise RuntimeError("无法唯一找到正式 C1 的 8102 CPU 用量。")
    sampled, value = values[0]["value"]
    if not start <= float(sampled) < min(injections):
        raise RuntimeError("CPU 标定不是正式 C1 的故障前采样。")
    usage = Decimal(value)
    if not usage.is_finite() or usage <= 0:
        raise RuntimeError("CPU 用量必须为有限正数。")
    unrounded = usage / Decimal("0.85") * 100
    quota = int(unrounded.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not 1 <= quota < 25:
        raise RuntimeError("标定值不能产生有效的低于25%的降额。")
    return {"source": "runs/c1-stop/cpu-before.json", "target": TARGET, "sample_unix": sampled,
            "usage_cores": str(usage), "formula": "U / 0.85 * 100; nearest integer percent",
            "unrounded_quota_percent": str(unrounded), "slow_quota_percent": quota,
            "normal_quota_percent": 25, "rate": 160,
            "sha256": {name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p in files.items()}}


def resources():
    result = {}
    for port in common.PORTS:
        unit = f"lab3-inst-{port}.service"
        props = common.command(["systemctl", "show", unit, "-p", "ControlGroup", "-p", "MainPID", "-p", "ActiveState"]).stdout
        values = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        group = values.get("ControlGroup", "")
        if values.get("ActiveState") != "active" or not group or int(values.get("MainPID", "0")) <= 0:
            raise RuntimeError(f"{unit} 未运行。")
        cg = Path("/sys/fs/cgroup") / group.lstrip("/")
        if cg.resolve().name != unit:
            raise RuntimeError("服务 cgroup 路径不符合预期。")
        maximum, period = (cg / "cpu.max").read_text().split()
        if maximum == "max":
            raise RuntimeError(f"{unit} 没有 CPU 配额。")
        result[str(port)] = {"unit": unit, "pid": int(values["MainPID"]), "cgroup": group,
                             "quota_percent": float(Decimal(maximum) / Decimal(period) * 100),
                             "cpu.max": (cg / "cpu.max").read_text(),
                             "cpu.stat": (cg / "cpu.stat").read_text()}
    return result


def require_normal_resources():
    snapshot = resources()
    if any(not math.isclose(r["quota_percent"], 25, abs_tol=0.001) for r in snapshot.values()):
        raise RuntimeError("开始前必须三个实例实际 CPU 配额均为25%；本检查不会自动修改配额。")
    return snapshot


def preflight(prefix, modes, calibration):
    if os.geteuid() != 0:
        raise RuntimeError("请在云主机以 root 运行。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", prefix):
        raise RuntimeError("实验前缀不合法。")
    for mode in modes:
        if (REPO / "runs" / f"{prefix}-{mode}").exists():
            raise RuntimeError(f"runs/{prefix}-{mode} 已存在，请更换 --prefix。")
    if common.command(["pgrep", "-x", "loadgen"], check=False).returncode != 1:
        raise RuntimeError("已有 loadgen 运行或无法检查进程。")
    if "### C3 测前预测" not in (REPO / "LAB3.md").read_text():
        raise RuntimeError("请先保存 C3 测前预测。")
    try:
        cfg = common.validate_scaler()
    except OSError as exc:
        raise RuntimeError("scaler 的8091接口不可用；请在C终端启动scaler，等三个实例就绪后再检查。") from exc
    except RuntimeError as exc:
        if "拒绝 pause all" in str(exc):
            raise RuntimeError("C3 需要8101、8102、8103三个活动实例；它们可能仍在拉镜像/启动，请等待全部ready后再检查。C3不执行pause all。") from exc
        raise
    ok, state = common.ready_snapshot()
    if not ok:
        raise RuntimeError("三个实例尚未全部 ready、eligible。")
    lb = timing.get_json(LB + "/config")
    expected = {"policy": "rr", "check_path": "/readyz", "check_interval": "1s",
                "check_timeout": "500ms", "fall": 3, "rise": 2, "timeout": "2s"}
    if any(lb.get(k) != v for k, v in expected.items()):
        raise RuntimeError("请先把 lbd 健康检查恢复默认配置，避免混入 C2 灵敏参数。")
    timing.fetch(PROM + "/-/ready")
    return {"calibration": calibration, "scaler": cfg, "initial_state": state,
            "resources": require_normal_resources()}


class LoadBalancer(common.LoadBalancer):
    def stop(self):
        pids = self.owned_pids()
        if len(pids) > 1:
            raise RuntimeError("本仓库存在多个 lbd，拒绝自动切换。")
        if not pids:
            return
        managed = False
        for unit in MANAGED_UNITS:
            state = common.command(["systemctl", "show", unit, "-p", "ActiveState", "-p", "MainPID"], check=False).stdout
            if "ActiveState=active" in state and f"MainPID={pids[0]}\n" in state:
                common.command(["systemctl", "stop", unit])
                managed = True
                break
        if not managed:
            if (Path("/proc") / str(pids[0]) / "exe").resolve(strict=True) != (REPO / "bin/lbd").resolve():
                raise RuntimeError("lbd PID 已变化。")
            os.kill(pids[0], signal.SIGINT)
        deadline = time.monotonic() + 15
        while self.owned_pids():
            if time.monotonic() > deadline:
                raise RuntimeError("lbd 没有正常退出。")
            time.sleep(0.1)

    def configure(self, mode):
        self.stop()
        self.changed = True
        args = ["systemd-run", "--quiet", "--collect", "--unit=" + UNIT,
                "-p", "AllowedCPUs=0-1", "-p", "WorkingDirectory=" + str(REPO), "-p", "Restart=no",
                str(REPO / "bin/lbd"), "-check-interval", "1s", "-check-timeout", "500ms",
                "-fall", "3", "-rise", "2", "-timeout", "2s"]
        if MODES[mode]:
            args.append("-outlier")
        common.command(args)
        expected = {"policy": "rr", "check_path": "/readyz", "check_interval": "1s",
                    "check_timeout": "500ms", "fall": 3, "rise": 2, "timeout": "2s",
                    "outlier": MODES[mode], "outlier_window": "10s", "outlier_factor": 3,
                    "outlier_min_ms": 50, "outlier_min_requests": 50, "outlier_error_rate": 0.2,
                    "outlier_max_frac": 0.34, "outlier_eject": "30s"}
        deadline, stable = time.monotonic() + 60, None
        while time.monotonic() < deadline:
            try:
                cfg = timing.get_json(LB + "/config")
                if any(cfg.get(k) != v for k, v in expected.items()):
                    raise RuntimeError("lbd 的实际配置与 C3 计划不同。")
                ok, _ = common.ready_snapshot()
                if ok:
                    if stable is None:
                        stable = time.monotonic()
                    if time.monotonic() - stable >= 3:
                        return cfg
                else:
                    stable = None
            except (OSError, ValueError):
                stable = None
            time.sleep(0.3)
        raise RuntimeError("lbd 切换后未能恢复三个就绪实例。")


def collect_prometheus(out, event):
    with (out / "steps.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    stage = next(r for r in rows if r["step"] != "all")
    index = {}
    for name, query in QUERIES.items():
        params = urllib.parse.urlencode({"query": query, "start": stage["start_unix"],
                                         "end": stage["end_unix"], "step": 5})
        raw = timing.fetch(PROM + "/api/v1/query_range?" + params)
        # 先保存原始响应，再检查查询是否成功。
        with (out / (name + "-range.json")).open("xb") as stream:
            stream.write(raw)
        parsed = json.loads(raw)
        if parsed.get("status") != "success" or not parsed.get("data", {}).get("result"):
            raise RuntimeError(f"Prometheus 查询没有有效结果：{name}")
        index[name] = {"query": query, "start": stage["start_unix"], "end": stage["end_unix"], "step": 5}
        event("query_saved", query=name)
    with (out / "prometheus-query-index.json").open("x") as stream:
        json.dump(index, stream, ensure_ascii=False, indent=2)


class Run:
    def __init__(self, name, mode, inputs):
        self.name, self.mode, self.inputs = name, mode, inputs
        self.out = REPO / "runs" / name
        self.child = self.injection = None
        self.anchor = None
        self.quota_maybe_changed = False
        self.env = os.environ.copy()
        self.env.update(GEN_CPUS="0-1", SLO="0.2", DATA="data", LB=LB, SCALER=SCALER)

    def event(self, action, planned=None, **extra):
        record = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  "mode": self.mode, "action": action, "planned_s": planned,
                  "observed_s": None if self.anchor is None else round(time.monotonic() - self.anchor, 4), **extra}
        print("[C3] " + json.dumps(record, ensure_ascii=False), flush=True)
        if self.out.is_dir():
            with (self.out / "runner-events.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def snapshot(self, label):
        ok, state = common.ready_snapshot()
        result = {"ready": ok, "state": state, "resources": resources()}
        with (self.out / (label + ".json")).open("x") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        self.event("snapshot", label=label)
        return result

    def inject(self, action, quota, planned):
        if self.child.poll() is not None:
            raise RuntimeError("压测已结束，取消配额变更。")
        self.event("inject_begin", planned, action_name=action, quota_percent=quota)
        self.injection = subprocess.Popen(["bash", "scripts/inject.sh", action, "8102", str(quota)],
                                         cwd=REPO, env=self.env, start_new_session=True)
        while self.injection.poll() is None:
            if self.child.poll() is not None:
                raise RuntimeError("配额变更期间压测提前结束。")
            time.sleep(0.03)
        if self.injection.returncode:
            raise RuntimeError(f"inject.sh 退出码 {self.injection.returncode}")
        actual = resources()["8102"]["quota_percent"]
        if not math.isclose(actual, quota, abs_tol=0.001):
            raise RuntimeError(f"配额实测 {actual}%，不等于计划 {quota}%。")
        self.event("inject_complete", planned, action_name=action, quota_percent=actual)

    def cleanup(self):
        timing.stop_child(self.injection)
        if self.quota_maybe_changed:
            result = common.command(["systemctl", "set-property", "--runtime", SERVICE, "CPUQuota=25%"], check=False)
            self.event("emergency_restore_quota", returncode=result.returncode, stderr=result.stderr)
        timing.stop_child(self.child)

    def run(self):
        initial = require_normal_resources()
        ok, before = common.ready_snapshot()
        if not ok:
            raise RuntimeError("本轮开始前实例未全部就绪。")
        start_path = REPO / "data/run-start"
        previous, launched = timing.start_stamp(start_path), time.time_ns()
        args = ["bash", "scripts/run3.sh", self.name, "-url", "http://127.0.0.1:8080/api/courses/1",
                "-plan", "160@240s"]
        self.child = subprocess.Popen(args, cwd=REPO, env=self.env, start_new_session=True)
        wall, self.anchor, stamp = timing.wait_start(start_path, previous, self.child, launched)
        plan = {"mode": self.mode, "inputs": self.inputs, "command": args, "resources": initial,
                "state": before, "lbd_config": timing.get_json(LB + "/config"), "anchor_wall": wall,
                "run_start_file": stamp, "planned_s": {"slow": 60, "restore": 180, "end": 240},
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        with (self.out / "runner-plan.json").open("x") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        self.event("load_started")
        timing.wait_at(self.anchor, 55, self.child)
        self.snapshot("before-slow")
        timing.wait_at(self.anchor, 60, self.child)
        self.quota_maybe_changed = True
        self.inject("slow", self.inputs["calibration"]["slow_quota_percent"], 60)
        timing.wait_at(self.anchor, 120, self.child)
        self.snapshot("during-slow")
        timing.wait_at(self.anchor, 180, self.child)
        self.inject("restore", 25, 180)
        self.quota_maybe_changed = False
        timing.wait_at(self.anchor, 185, self.child)
        self.snapshot("after-quota-restore")
        result = self.child.wait()
        if result:
            raise RuntimeError(f"run3.sh 退出码 {result}，已生成文件保留。")
        self.event("load_finished")
        collect_prometheus(self.out, self.event)
        self.event("finished", summary=str(self.out / "summary.md"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="c3")
    parser.add_argument("--mode", choices=("both", "slow", "outlier"), default="both")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="仅读取正式C1并打印计划，不要求服务运行")
    group.add_argument("--check", action="store_true", help="只读检查环境，不启动压测或改变服务")
    args = parser.parse_args()
    current, balancer = None, None
    try:
        calibration = baseline()
        modes = list(MODES) if args.mode == "both" else [args.mode]
        print("C3 标定与计划：" + json.dumps(calibration, ensure_ascii=False), flush=True)
        print("每组240秒；第60秒将8102降至计划配额；第180秒恢复25%；结束恢复默认lbd。", flush=True)
        if args.plan:
            print("仅打印计划完成，未检查或改变运行服务，未创建实验结果目录。")
            return 0
        inputs = preflight(args.prefix, modes, calibration)
        if args.check:
            print("只读检查通过；未启动压测、切换lbd或改变配额。")
            return 0
        os.sched_setaffinity(0, {0, 1})
        balancer = LoadBalancer()
        with (REPO / "data/c1-runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            inputs = preflight(args.prefix, modes, calibration)
            signal.signal(signal.SIGTERM, timing.interrupted)
            signal.signal(signal.SIGHUP, timing.interrupted)
            try:
                for mode in modes:
                    print(f"[C3] 切换 lbd：{mode}；等待就绪…", flush=True)
                    require_normal_resources()
                    balancer.configure(mode)
                    current = Run(f"{args.prefix}-{mode}", mode, inputs)
                    try:
                        current.run()
                    finally:
                        current.cleanup()
            finally:
                if balancer.changed:
                    print("[C3] 恢复不启用离群摘除的默认 lbd…", flush=True)
                    balancer.configure("slow")
        print("[C3] 两组完成；配额已恢复25%，lbd恢复默认配置。", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[C3] 已停止：{exc}", file=sys.stderr, flush=True)
        if current:
            current.event("aborted", reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
