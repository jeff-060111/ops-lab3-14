#!/usr/bin/env python3
"""C2 正式对照：调用课程采集/注入脚本，切换 lbd 检查参数。"""
import argparse
from datetime import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lab3_c1_timing", REPO / "scripts/run-c1.py")
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)
UNIT = "lab3-c2-lbd.service"
LB, SCALER = timing.LB, timing.SCALER
PORTS = (8101, 8102, 8103)
TARGETS = {f"127.0.0.1:{p}" for p in PORTS}
SERVICES = {f"lab3-inst-{p}.service" for p in PORTS}
MODES = {
    "default": {"check_interval": "1s", "check_timeout": "500ms", "fall": 3, "rise": 2},
    "sensitive": {"check_interval": "500ms", "check_timeout": "200ms", "fall": 1, "rise": 2},
}


def command(args, check=True):
    result = subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=20)
    if check and result.returncode:
        raise RuntimeError(f"命令失败 {args}: {result.stderr.strip()}")
    return result


def active_services():
    output = command(["systemctl", "list-units", "--plain", "--no-legend", "--state=active",
                      "lab3-inst-*"]).stdout
    return {line.split()[0] for line in output.splitlines() if line.strip()}


def ready_snapshot():
    instances = timing.get_json(SCALER + "/instances")
    backends = timing.get_json(LB + "/backends")
    ok = (len(instances) == len(backends) == 3
          and {i["addr"] for i in instances} == TARGETS
          and {b["addr"] for b in backends} == TARGETS
          and all(i["state"] == "ready" for i in instances)
          and all(b["up"] and b["eligible"] and not b["ejected"] and not b["draining"] for b in backends))
    return ok, {"instances": instances, "backends": backends}


def validate_scaler():
    cfg = timing.get_json(SCALER + "/config")
    expected = {"mode": "fixed", "replicas": 3, "quota_percent": 25, "cpus": "2",
                "memory": "128M", "pull": "20s", "wait_node": False,
                "restart": "", "args": "-warmup 60s -work 1000000"}
    if any(cfg.get(k) != v for k, v in expected.items()):
        raise RuntimeError("scaler 必须保持正式 C1 的固定三实例和资源配置。")
    if Path(cfg["data"]).resolve() != (REPO / "data").resolve():
        raise RuntimeError("scaler 工作目录不是本仓库 data。")
    if active_services() != SERVICES:
        raise RuntimeError("活动 lab3-inst 服务不是恰好 8101、8102、8103；拒绝 pause all。")
    return cfg


def preflight(prefix, modes):
    if os.geteuid() != 0:
        raise RuntimeError("请在云主机使用 root 运行。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", prefix):
        raise RuntimeError("实验前缀只能包含字母、数字、点、下划线和连字符。")
    if command(["pgrep", "-x", "loadgen"], check=False).returncode != 1:
        raise RuntimeError("已有压测运行，或无法检查进程。")
    for mode in modes:
        if (REPO / "runs" / f"{prefix}-{mode}").exists():
            raise RuntimeError(f"runs/{prefix}-{mode} 已存在，请更换 --prefix。")
    if "### C2 测前预测" not in (REPO / "LAB3.md").read_text():
        raise RuntimeError("C2 测前预测尚未写入报告。")
    dependency = REPO / "runs/c1-stop"
    if not (dependency / "summary.md").exists() or not (dependency / "steps.csv").exists():
        raise RuntimeError("缺少正式 C1 runs/c1-stop，不能改用练习数据。")
    meta = (dependency / "meta.txt").read_text()
    if "160@240s" not in meta or '"quota_percent": 25' not in meta or '"cpus": "2"' not in meta:
        raise RuntimeError("正式 C1 配置与预定基线不一致。")
    if (REPO / "scripts/inject.sh").read_text().count("--kill-whom=main") != 3:
        raise RuntimeError("请先确认 inject.sh 的主进程信号兼容修复。")
    cfg = validate_scaler()
    ok, snapshot = ready_snapshot()
    if not ok:
        raise RuntimeError("开始前需要三个实例全部 ready、eligible。")
    timing.fetch(timing.PROM + "/-/ready")
    return {"scaler": cfg, "ready": snapshot, "dependency": "runs/c1-stop",
            "dependency_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in [dependency / "meta.txt", dependency / "steps.csv"]}}


class LoadBalancer:
    """只管理本仓库的 lbd；正式实验结束后以同一 unit 恢复默认配置。"""
    def __init__(self):
        self.changed = False

    def owned_pids(self):
        result = command(["pgrep", "-x", "lbd"], check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("无法检查 lbd 进程。")
        found = []
        for token in result.stdout.split():
            try:
                if (Path("/proc") / token / "exe").resolve(strict=True) == (REPO / "bin/lbd").resolve():
                    found.append(int(token))
            except FileNotFoundError:
                pass
        return found

    def stop(self):
        pids = self.owned_pids()
        if len(pids) > 1:
            raise RuntimeError("本仓库有多个 lbd 进程，停止自动切换以免误操作。")
        if not pids:
            return
        state = command(["systemctl", "show", UNIT, "-p", "ActiveState", "-p", "MainPID"], check=False).stdout
        if "ActiveState=active" in state and f"MainPID={pids[0]}\n" in state:
            command(["systemctl", "stop", UNIT])
        else:
            # 仅向已验证可执行路径的原前台 lbd 发退出信号。
            if (Path("/proc") / str(pids[0]) / "exe").resolve(strict=True) != (REPO / "bin/lbd").resolve():
                raise RuntimeError("lbd PID 已变化，停止操作。")
            os.kill(pids[0], signal.SIGINT)
        deadline = time.monotonic() + 15
        while self.owned_pids():
            if time.monotonic() > deadline:
                raise RuntimeError("lbd 没有正常退出。")
            time.sleep(0.1)

    def configure(self, mode):
        self.stop()
        self.changed = True
        settings = MODES[mode]
        args = ["systemd-run", "--quiet", "--collect", "--unit=" + UNIT,
                "-p", "AllowedCPUs=0-1", "-p", "WorkingDirectory=" + str(REPO),
                "-p", "Restart=no", str(REPO / "bin/lbd"),
                "-check-interval", settings["check_interval"],
                "-check-timeout", settings["check_timeout"],
                "-fall", str(settings["fall"]), "-rise", str(settings["rise"])]
        command(args)
        deadline, stable = time.monotonic() + 60, None
        while time.monotonic() < deadline:
            try:
                cfg = timing.get_json(LB + "/config")
                expected = {**settings, "policy": "rr", "check_path": "/readyz", "timeout": "2s", "outlier": False}
                if any(cfg.get(k) != v for k, v in expected.items()):
                    raise RuntimeError("lbd 启动后的配置与本轮计划不同。")
                ok, _ = ready_snapshot()
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
        raise RuntimeError("lbd 重启后，60 秒内三个实例未恢复就绪。")


class Run:
    def __init__(self, name, mode, inputs):
        self.name, self.mode, self.inputs = name, mode, inputs
        self.out = REPO / "runs" / name
        self.child = self.injection = None
        self.pause_maybe_active = False
        self.anchor = None
        self.env = os.environ.copy()
        self.env.update(GEN_CPUS="0-1", SLO="0.2", DATA="data", LB=LB, SCALER=SCALER)

    def event(self, action, planned=None, **extra):
        record = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  "mode": self.mode, "action": action, "planned_s": planned,
                  "observed_s": None if self.anchor is None else round(time.monotonic() - self.anchor, 4), **extra}
        print("[C2] " + json.dumps(record, ensure_ascii=False), flush=True)
        if self.out.is_dir():
            with (self.out / "runner-events.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def cleanup(self):
        timing.stop_child(self.injection)
        if self.pause_maybe_active:
            result = command(["systemctl", "kill", "--kill-whom=main", "--signal=SIGCONT", *sorted(SERVICES)], check=False)
            self.event("emergency_resume_all", returncode=result.returncode, stderr=result.stderr)
        timing.stop_child(self.child)

    def run(self):
        validate_scaler()
        ok, before = ready_snapshot()
        if not ok:
            raise RuntimeError("本轮开始前实例未全部就绪。")
        start_path = REPO / "data/run-start"
        previous = timing.start_stamp(start_path)
        launched = time.time_ns()
        args = ["bash", "scripts/run3.sh", self.name, "-url", "http://127.0.0.1:8080/api/courses/1",
                "-plan", "160@120s"]
        self.child = subprocess.Popen(args, cwd=REPO, env=self.env, start_new_session=True)
        wall, self.anchor, stamp = timing.wait_start(start_path, previous, self.child, launched)
        plan = {"mode": self.mode, "command": args, "dependency": self.inputs,
                "lbd_config": timing.get_json(LB + "/config"), "initial_state": before,
                "anchor_wall": wall, "run_start_file": stamp,
                "planned_pause_s": 40, "pause_duration_s": 2,
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        with (self.out / "runner-plan.json").open("x") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        self.event("load_started")
        timing.wait_at(self.anchor, 40, self.child)
        if active_services() != SERVICES:
            raise RuntimeError("注入前活动实例集合变化，取消 pause all。")
        self.pause_maybe_active = True
        self.event("inject_begin", 40, args=["pause", "all", "2"])
        self.injection = subprocess.Popen(["bash", "scripts/inject.sh", "pause", "all", "2"],
                                         cwd=REPO, env=self.env, start_new_session=True)
        while self.injection.poll() is None:
            if self.child.poll() is not None:
                raise RuntimeError("压测提前结束，恢复暂停的实例。")
            time.sleep(0.03)
        if self.injection.returncode:
            raise RuntimeError(f"inject.sh 退出码 {self.injection.returncode}")
        self.pause_maybe_active = False
        self.event("resumed", 42)
        timing.wait_at(self.anchor, 46, self.child)
        _, snapshot = ready_snapshot()
        with (self.out / "after-resume.json").open("x") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        result = self.child.wait()
        if result:
            raise RuntimeError(f"run3.sh 退出码 {result}")
        self.event("finished", summary=str(self.out / "summary.md"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="c2")
    parser.add_argument("--mode", choices=("both", "default", "sensitive"), default="both")
    parser.add_argument("--check", action="store_true", help="只检查，不切换 lbd、不加压、不注入")
    args = parser.parse_args()
    modes = list(MODES) if args.mode == "both" else [args.mode]
    balancer = LoadBalancer()
    current = None
    success = False
    try:
        inputs = preflight(args.prefix, modes)
        print("C2 检查通过：仅使用正式 runs/c1-stop；固定三实例、160请求/秒。", flush=True)
        print("计划：每组120秒，第40秒暂停全部实例2秒；先默认，再灵敏；最后恢复默认lbd。", flush=True)
        if args.check:
            print("只读检查完成，未改变服务。")
            return 0
        os.sched_setaffinity(0, {0, 1})
        with (REPO / "data/c1-runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            preflight(args.prefix, modes)
            signal.signal(signal.SIGTERM, timing.interrupted)
            signal.signal(signal.SIGHUP, timing.interrupted)
            try:
                for mode in modes:
                    print(f"[C2] 切换 lbd：{mode}；等待三个实例恢复就绪…", flush=True)
                    balancer.configure(mode)
                    current = Run(f"{args.prefix}-{mode}", mode, inputs)
                    try:
                        current.run()
                    finally:
                        current.cleanup()
                success = True
            finally:
                if balancer.changed:
                    print("[C2] 恢复默认 lbd 配置并等待实例就绪…", flush=True)
                    balancer.configure("default")
        print("[C2] 完成；lbd 默认配置已恢复，scaler 和三个实例继续运行。", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[C2] 已停止：{exc}", file=sys.stderr, flush=True)
        if current:
            current.event("aborted", reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
