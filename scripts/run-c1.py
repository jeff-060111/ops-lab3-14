#!/usr/bin/env python3
"""C1 定时包装：保留课程 run3.sh / inject.sh 生成的原始结果。"""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parent.parent
LB = "http://127.0.0.1:8090"
SCALER = "http://127.0.0.1:8091"
PROM = "http://127.0.0.1:9090"
TARGETS = {f"127.0.0.1:{p}" for p in (8101, 8102, 8103)}


def fetch(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return response.read()


def get_json(url):
    return json.loads(fetch(url))


def preflight(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise RuntimeError("实验名称只能包含字母、数字、点、下划线和连字符。")
    if os.geteuid() != 0:
        raise RuntimeError("需要在云主机上以 root 运行。")
    if (REPO / "runs" / name).exists():
        raise RuntimeError(f"runs/{name} 已存在，请换新名称，原结果保留。")
    for path in ("scripts/run3.sh", "scripts/inject.sh", "bin/loadgen", "bin/lab3sum"):
        if not (REPO / path).is_file():
            raise RuntimeError(f"缺少 {path}")
    report = (REPO / "LAB3.md").read_text()
    predictions = [line for line in report.splitlines() if line.startswith("预测（C1 之前写）")]
    if len(predictions) != 1 or "<填写>" in predictions[0]:
        raise RuntimeError("请先填写 LAB3.md 中的 C1 测前预测。")
    if subprocess.run(["pgrep", "-x", "loadgen"], capture_output=True).returncode != 1:
        raise RuntimeError("已有 loadgen 在运行，或无法检查进程；本轮未启动。")
    cfg = get_json(SCALER + "/config")
    expected = {"mode": "fixed", "replicas": 3, "quota_percent": 25, "cpus": "2",
                "memory": "128M", "pull": "20s", "wait_node": False,
                "restart": "", "args": "-warmup 60s -work 1000000"}
    if any(cfg.get(key) != value for key, value in expected.items()):
        raise RuntimeError("scaler 参数与本轮 C1 计划不一致，请检查 /config。")
    if Path(cfg["data"]).resolve() != (REPO / "data").resolve():
        raise RuntimeError("scaler 数据目录不是本仓库的 data。")
    lb = get_json(LB + "/config")
    expected_lb = {"policy": "rr", "check_path": "/readyz", "check_interval": "1s",
                   "check_timeout": "500ms", "fall": 3, "rise": 2,
                   "timeout": "2s", "outlier": False}
    if any(lb.get(key) != value for key, value in expected_lb.items()):
        raise RuntimeError("lbd 参数不是本轮计划的默认健康检查配置。")
    if (REPO / lb["events"]).resolve() != (REPO / "data/lbd-events.csv").resolve():
        raise RuntimeError("lbd 事件日志不是本仓库的 data/lbd-events.csv。")
    backends, instances = get_json(LB + "/backends"), get_json(SCALER + "/instances")
    if (len(backends) != 3 or {b["addr"] for b in backends} != TARGETS
            or not all(b["up"] and b["eligible"] and not b["draining"] and not b["ejected"] for b in backends)):
        raise RuntimeError("需要 8101、8102、8103 三个可用后端；重复实验前需重启 scaler 并等待就绪。")
    if (len(instances) != 3 or {i["addr"] for i in instances} != TARGETS
            or not all(i["state"] == "ready" for i in instances)):
        raise RuntimeError("三个初始实例尚未全部 ready。")
    fetch(PROM + "/-/ready")
    return {"scaler": cfg, "lbd": lb, "backends": backends, "instances": instances,
            "prediction": predictions[0]}


def start_stamp(path):
    try:
        st = path.stat()
        content = path.read_text().strip()
        if not content.isdigit():
            return None
        return st.st_ino, st.st_mtime_ns, st.st_ctime_ns, content
    except FileNotFoundError:
        return None


def wait_start(path, previous, child, launched_ns, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("run3.sh 在开始压测前退出，未进行注入。")
        stamp = start_stamp(path)
        if stamp and stamp != previous and stamp[1] >= launched_ns:
            # run-start 的内容只精确到秒；文件写入时间保留亚秒精度。
            wall = stamp[1] / 1e9
            anchor = time.monotonic() - (time.time() - wall)
            return wall, anchor, stamp
        time.sleep(0.02)
    raise RuntimeError("30 秒内未看到本轮新的 run-start，取消注入。")


def wait_at(anchor, seconds, child):
    deadline = anchor + seconds
    while True:
        if child.poll() is not None:
            raise RuntimeError("run3.sh 提前结束，取消后续注入。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if -remaining > 2:
                raise RuntimeError(f"第 {seconds} 秒动作迟到超过 2 秒，取消后续注入。")
            return
        time.sleep(min(remaining, 0.1))


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    except ProcessLookupError:
        pass


class Runner:
    def __init__(self, name, inputs):
        self.name, self.inputs = name, inputs
        self.out = REPO / "runs" / name
        self.child = self.injection = None
        self.pause_maybe_active = False
        self.anchor = None
        self.env = os.environ.copy()
        self.env.update(GEN_CPUS="0-1", SLO="0.2", DATA="data", LB=LB, SCALER=SCALER)

    def event(self, action, planned=None, **extra):
        record = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                  "action": action, "planned_s": planned,
                  "observed_s": None if self.anchor is None else round(time.monotonic() - self.anchor, 4), **extra}
        print("[C1] " + json.dumps(record, ensure_ascii=False), flush=True)
        if self.out.is_dir():
            with (self.out / "runner-events.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def inject(self, args, planned):
        if self.child.poll() is not None:
            raise RuntimeError("压测已结束，取消注入。")
        self.event("inject_begin", planned, args=args)
        self.injection = subprocess.Popen(["bash", "scripts/inject.sh", *args], cwd=REPO,
                                         env=self.env, start_new_session=True)
        while self.injection.poll() is None:
            if self.child.poll() is not None:
                raise RuntimeError("注入期间压测提前结束。")
            time.sleep(0.05)
        if self.injection.returncode:
            raise RuntimeError(f"inject.sh 退出码 {self.injection.returncode}")
        self.event("inject_complete", planned, args=args)

    def cleanup(self):
        stop_child(self.injection)
        if self.pause_maybe_active:
            try:
                result = subprocess.run(["systemctl", "kill", "--kill-whom=main", "--signal=SIGCONT", "lab3-inst-8103.service"],
                                        capture_output=True, text=True, timeout=5)
                self.event("emergency_resume", returncode=result.returncode, stderr=result.stderr)
            except Exception as exc:
                print(f"恢复暂停失败：{exc}；请执行 systemctl kill --kill-whom=main --signal=SIGCONT lab3-inst-8103", file=sys.stderr)
        stop_child(self.child)

    def run(self):
        start_path = REPO / "data/run-start"
        previous = start_stamp(start_path)
        launched_ns = time.time_ns()
        command = ["bash", "scripts/run3.sh", self.name, "-url",
                   "http://127.0.0.1:8080/api/courses/1", "-plan", "160@240s"]
        self.child = subprocess.Popen(command, cwd=REPO, env=self.env, start_new_session=True)
        wall, self.anchor, stamp = wait_start(start_path, previous, self.child, launched_ns)
        plan = {"command": command, "inputs": self.inputs, "run_start_file": stamp,
                "anchor_wall": wall, "anchor_note": "run-start file mtime; close to, not identical to, first request",
                "planned_actions_s": {"cpu_sample": 45, "kill_8102": 60, "check_replacement": 150,
                                      "pause_8103": 160, "resume_8103_approximately": 180},
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        with (self.out / "runner-plan.json").open("x") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        self.event("load_started")
        wait_at(self.anchor, 45, self.child)
        query = urllib.parse.urlencode({"query": 'rate(cgroup_cpu_usage_seconds_total{job="instances"}[30s])'})
        raw = fetch(PROM + "/api/v1/query?" + query)
        with (self.out / "cpu-before.json").open("xb") as stream:
            stream.write(raw)
        response = json.loads(raw)
        results = response.get("data", {}).get("result", [])
        if response.get("status") != "success" or len(results) != 3:
            raise RuntimeError("故障前 CPU 查询未返回三个实例，取消注入。")
        self.event("cpu_sample_saved", 45)
        wait_at(self.anchor, 60, self.child)
        self.inject(["kill", "8102"], 60)
        wait_at(self.anchor, 150, self.child)
        ready, backends = get_json(SCALER + "/instances"), get_json(LB + "/backends")
        snapshot = {"instances": ready, "backends": backends}
        with (self.out / "replacement-before-pause.json").open("x") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        if (len(ready) != 3 or len(backends) != 3 or not all(i["state"] == "ready" for i in ready)
                or not all(b["eligible"] for b in backends)
                or "127.0.0.1:8103" not in {i["addr"] for i in ready}):
            raise RuntimeError("第 150 秒三个实例未全部就绪，取消暂停，保留本轮数据。")
        self.event("replacement_ready", 150)
        wait_at(self.anchor, 160, self.child)
        self.pause_maybe_active = True
        self.inject(["pause", "8103", "20"], 160)
        self.pause_maybe_active = False
        self.event("resumed")
        result = self.child.wait()
        if result:
            raise RuntimeError(f"run3.sh 退出码 {result}，保留已生成文件。")
        self.event("finished", summary=str(self.out / "summary.md"))


def interrupted(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", default="c1-stop")
    parser.add_argument("--check", action="store_true", help="仅检查配置，不启动压测或注入")
    args = parser.parse_args()
    runner = None
    try:
        inputs = preflight(args.name)
        print("检查通过：默认 lbd、固定三个 ready 实例、25% CPU、绑核 2、128M 内存。", flush=True)
        print("计划：45s CPU采样；60s kill 8102；150s 检查补建；160s pause 8103 20秒；240s 结束。", flush=True)
        if args.check:
            print("仅检查完成；未启动压测或注入。")
            return 0
        os.sched_setaffinity(0, {0, 1})
        with (REPO / "data/c1-runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 获得锁后复查，避免两个包装脚本同时通过初始检查。
            inputs = preflight(args.name)
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGHUP, interrupted)
            runner = Runner(args.name, inputs)
            try:
                runner.run()
            finally:
                runner.cleanup()
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"[C1] 已停止：{exc}", file=sys.stderr, flush=True)
        if runner:
            runner.event("aborted", reason=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
