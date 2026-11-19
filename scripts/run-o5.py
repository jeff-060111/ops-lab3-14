#!/usr/bin/env python3
"""O5 HAProxy: --check is read-only; default runs C1+C3; --manual collects without timed injection.

Business: loadgen -> HAProxy :18081 -> enrolld :8101..8103.
Control only: existing scaler -> existing lbd :8090. No lbd business proxying.
Uses only Python stdlib, installed HAProxy, systemd, original loadgen/inject.sh/lab3sum.
"""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / 'deploy/haproxy-o5.cfg'
PREDICTION = REPO / 'runs/o5-prediction-before.md'
ENVIRONMENT = REPO / 'deploy/o5-environment.json'
SOCKET = REPO / 'data/o5-haproxy/admin.sock'
UNIT = 'lab3-o5-haproxy.service'
BACKEND = 'enrolld_o5'
TARGETS = {f'inst{p}': f'127.0.0.1:{p}' for p in (8101, 8102, 8103)}
FORMAL = ('c1-stop', 'c2-default', 'c2-sensitive', 'c3-slow', 'c3-outlier')
POLL = 0.2
RATE, DURATION, NORMAL = 160, 240, 25
URL = 'http://127.0.0.1:18081/api/courses/1'
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location('c1_common', REPO / 'scripts/run-c1.py')
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)


def command(args, timeout=15, check=True):
    return subprocess.run([str(a) for a in args], cwd=REPO, capture_output=True,
                          text=True, timeout=timeout, check=check)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def valid_name(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
        raise RuntimeError('目录名只能包含字母、数字、点、下划线、连字符。')
    return name


def protected_hashes():
    """Check immutable C run bytes against committed C manifest; LAB3.md may grow O5 sections."""
    result = {}
    for line in (REPO / 'C_EVIDENCE_SHA256SUMS.txt').read_text().splitlines():
        expected, name = line.split('  ', 1)
        if any(name.startswith(f'runs/{r}/') for r in FORMAL):
            actual = digest(REPO / name)
            if actual != expected:
                raise RuntimeError(f'C 正式原始证据校验不符：{name}')
            result[name] = actual
    if len(result) != 88:
        raise RuntimeError(f'预期保护 C 正式原始文件 88 个，实际 {len(result)} 个。')
    return result


def calibration():
    source = REPO / 'runs/c1-stop/cpu-before.json'
    raw = json.loads(source.read_text())
    rows = [r for r in raw['data']['result'] if r['metric'].get('instance') == TARGETS['inst8102']]
    if raw.get('status') != 'success' or len(rows) != 1:
        raise RuntimeError('正式 C1 CPU 样本缺少唯一的 8102。')
    usage = Decimal(rows[0]['value'][1])
    quota = int((usage / Decimal('0.85') * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if not 0 < quota < NORMAL:
        raise RuntimeError('正式 C1 标定配额不在 (0,25)；需先复核标定，禁止静默改用手动数据。')
    return {'source': str(source.relative_to(REPO)), 'sha256': digest(source),
            'usage_cores': str(usage), 'slow_quota_percent': quota,
            'formula': 'U / 0.85 * 100, nearest integer percent', 'normal_quota_percent': NORMAL}


def unit_properties(port):
    r = command(['systemctl', 'show', f'lab3-inst-{port}.service',
                 '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID',
                 '-p', 'CPUQuotaPerSecUSec', '-p', 'AllowedCPUs', '-p', 'MemoryMax',
                 '-p', 'MemorySwapMax', '-p', 'ControlGroup'])
    result = dict(l.split('=', 1) for l in r.stdout.splitlines() if '=' in l)
    group = result.get('ControlGroup')
    if group:
        cg = Path('/sys/fs/cgroup') / group.lstrip('/')
        for n in ['cpu.max', 'cpu.stat', 'cpuset.cpus.effective']:
            if (cg / n).is_file():
                result[n] = (cg / n).read_text()
    pid = result.get('MainPID', '0')
    if pid != '0' and Path(f'/proc/{pid}/status').is_file():
        result['proc_state'] = next(l for l in Path(f'/proc/{pid}/status').read_text().splitlines()
                                    if l.startswith('State:'))
    return result


def preflight(name=None):
    if name:
        valid_name(name)
        if (REPO / 'runs' / name).exists():
            raise RuntimeError(f'runs/{name} 已存在；请更换名字，禁止覆盖。')
    inputs = common.preflight('o5-environment-probe-only')
    for p in [CONFIG, PREDICTION, ENVIRONMENT, REPO / 'deploy/o5-haproxy-version.txt']:
        if not p.is_file():
            raise RuntimeError(f'缺少准备文件：{p.relative_to(REPO)}')
    env = json.loads(ENVIRONMENT.read_text())
    if env.get('config_sha256') != digest(CONFIG):
        raise RuntimeError('HAProxy 配置与测前环境记录不一致；先更新预测与记录再测。')
    if env.get('haproxy_binary_sha256') != digest(shutil.which('haproxy') or '/missing-haproxy'):
        raise RuntimeError('HAProxy 二进制与测前版本记录不一致。')
    for name, expected in env.get('lab_binary_sha256', {}).items():
        if digest(REPO / name) != expected:
            raise RuntimeError(f'实验二进制在准备后发生变化：{name}')
    if '尚未执行' not in PREDICTION.read_text():
        raise RuntimeError('缺少明确的测前预测快照。')
    command(['haproxy', '-c', '-f', CONFIG])
    units = {}
    for port in (8101, 8102, 8103):
        u = unit_properties(port)
        cpu_max = u.get('cpu.max', '').split()
        if (u.get('ActiveState') != 'active' or u.get('SubState') != 'running'
                or u.get('MainPID', '0') == '0' or u.get('AllowedCPUs') != '2'
                or u.get('MemoryMax') != str(128 * 1024 * 1024)
                or len(cpu_max) != 2 or cpu_max[0] == 'max'
                or Decimal(cpu_max[0]) / Decimal(cpu_max[1]) != Decimal('0.25')
                or 'T (stopped)' in u.get('proc_state', '')):
            raise RuntimeError(f'810{port-8100} 实际 systemd/cgroup 配置不符合初始条件：{u}')
        units[str(port)] = u
    active = command(['systemctl', 'list-units', '--all', '--plain', '--no-legend',
                      '--no-pager', '--state=active', 'lab3-inst-*']).stdout
    actual = {line.split()[0] for line in active.splitlines() if line.strip()}
    expected = {f'lab3-inst-{p}.service' for p in (8101, 8102, 8103)}
    if actual != expected:
        raise RuntimeError(f'活动实验实例不是恰好三台：{sorted(actual)}')
    return {'control_plane': inputs, 'units': units, 'calibration': calibration(),
            'formal_c_sha256': protected_hashes(), 'haproxy_config_sha256': digest(CONFIG),
            'prediction_sha256': digest(PREDICTION),
            'script_sha256': digest(__file__), 'prepared_environment': env}


def runtime(query):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        client.connect(str(SOCKET))
        client.sendall((query + '\n').encode())
        chunks = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 2_000_000:
                raise RuntimeError('HAProxy Runtime API 响应过大。')
    return b''.join(chunks).decode()


def parse_stats(raw):
    lines = raw.strip().splitlines()
    if not lines or not lines[0].startswith('# '):
        raise RuntimeError('HAProxy show stat 缺少 CSV 表头。')
    rows = list(csv.DictReader(io.StringIO('\n'.join([lines[0][2:]] + lines[1:]))))
    servers = [r for r in rows if r.get('pxname') == BACKEND and r.get('svname') in TARGETS]
    if len(servers) != 3 or {r['svname'] for r in servers} != set(TARGETS):
        raise RuntimeError('HAProxy 配置不是预期的三个后端。')
    return rows, servers


def normalized(status):
    # UP 2/3 is still routing; DOWN 1/2 is still excluded.
    return status.split()[0]


def proxy_active():
    return command(['systemctl', 'is-active', UNIT], check=False).returncode == 0


def check_proxy_identity():
    props = command(['systemctl', 'show', UNIT, '-p', 'ExecStart', '-p', 'MainPID',
                     '-p', 'AllowedCPUs']).stdout
    if str(CONFIG) not in props or 'AllowedCPUs=0-1' not in props:
        raise RuntimeError('同名 HAProxy unit 的配置或绑核不符，拒绝接管。')
    pid = re.search(r'^MainPID=(\d+)$', props, re.M)
    info = runtime('show info')
    native_pid = re.search(r'^Pid: (\d+)$', info, re.M)
    if not pid or not native_pid or pid[1] != native_pid[1]:
        raise RuntimeError('HAProxy Runtime socket 与实验 unit 的 PID 不符。')
    return props


def wait_proxy_ready(timeout=30):
    end = time.monotonic() + timeout
    stable = None
    while time.monotonic() < end:
        if not proxy_active():
            raise RuntimeError('O5 HAProxy unit 未运行；查看 journalctl -u ' + UNIT)
        try:
            _, servers = parse_stats(runtime('show stat'))
            if all(r['status'] == 'UP' and r.get('check_status') == 'L7OK' for r in servers):
                stable = stable or time.monotonic()
                if time.monotonic() - stable >= 2:
                    check_proxy_identity()
                    return
            else:
                stable = None
        except (OSError, RuntimeError):
            stable = None
        time.sleep(0.2)
    raise RuntimeError('30 秒内三个 HAProxy 后端未稳定 UP。')


def ensure_no_listener(port=18081):
    # A bind() probe without SO_REUSEADDR can reject a free listening port while
    # accepted connections from the previous HAProxy remain in TIME_WAIT.
    # Inspect LISTEN only; HAProxy itself performs the authoritative bind.
    listeners = command(['ss', '-H', '-lnt', f'sport = :{port}']).stdout.strip()
    if listeners:
        raise RuntimeError(f'{port} 已有实际 TCP 监听；请先核对占用进程：{listeners}')


def start_proxy():
    if proxy_active():
        raise RuntimeError('O5 HAProxy 已在运行；自动实验需先 --stop-proxy，手动采集可直接继续。')
    ensure_no_listener()
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    command(['systemd-run', '--unit=lab3-o5-haproxy', '--collect',
             '--property=WorkingDirectory=' + str(REPO), '--property=AllowedCPUs=0-1',
             '--property=StandardOutput=journal', '--property=StandardError=journal',
             '--property=LogRateLimitIntervalSec=0', '--property=SyslogIdentifier=lab3-o5-haproxy',
             shutil.which('haproxy'), '-db', '-f', CONFIG])
    wait_proxy_ready()


def stop_proxy():
    if proxy_active():
        check_proxy_identity()
        command(['systemctl', 'stop', UNIT], timeout=20)


class Observer:
    """Preserve server rows verbatim from show stat; derive explicitly sampled state transitions."""
    def __init__(self, out):
        self.out, self.stop_event = out, threading.Event()
        self.error, self.max_gap_ms, self.last_ms, self.polls = None, 0, None, 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        previous = {}
        try:
            with (self.out / 'haproxy-stats.csv').open('w', newline='') as stream, \
                    (self.out / 'haproxy-events.csv').open('w', newline='') as events:
                writer = None
                ev = csv.DictWriter(events, fieldnames=['observed_unix_ms', 'poll_start_unix_ms',
                    'previous_observed_unix_ms', 'previous_poll_start_unix_ms', 'backend', 'server', 'addr', 'event',
                    'previous_status', 'status', 'check_status', 'check_duration_ms'])
                ev.writeheader()
                while not self.stop_event.is_set():
                    tick = time.monotonic()
                    begin = time.time_ns() // 1_000_000
                    _, servers = parse_stats(runtime('show stat'))
                    observed = time.time_ns() // 1_000_000
                    if self.last_ms is not None:
                        self.max_gap_ms = max(self.max_gap_ms, observed - self.last_ms)
                    self.last_ms, self.polls = observed, self.polls + 1
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=['poll_start_unix_ms',
                            'observed_unix_ms'] + list(servers[0]))
                        writer.writeheader()
                    for row in servers:
                        writer.writerow({'poll_start_unix_ms': begin, 'observed_unix_ms': observed, **row})
                        name, state = row['svname'], normalized(row['status'])
                        old = previous.get(name)
                        if old is None or state != old['state']:
                            ev.writerow({'observed_unix_ms': observed, 'poll_start_unix_ms': begin,
                                'previous_observed_unix_ms': '' if old is None else old['time'],
                                'previous_poll_start_unix_ms': '' if old is None else old['begin'],
                                'backend': BACKEND, 'server': name, 'addr': TARGETS[name],
                                'event': 'initial' if old is None else state.lower(),
                                'previous_status': '' if old is None else old['status'],
                                'status': row['status'], 'check_status': row.get('check_status', ''),
                                'check_duration_ms': row.get('check_duration', '')})
                        previous[name] = {'state': state, 'time': observed, 'begin': begin, 'status': row['status']}
                    stream.flush()
                    events.flush()
                    self.stop_event.wait(max(0, POLL - (time.monotonic() - tick)))
        except Exception as exc:
            self.error = str(exc)

    def check(self):
        if self.error:
            raise RuntimeError('HAProxy 证据采集失败：' + self.error)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('HAProxy observer 未及时停止。')
        self.check()
        dump(self.out / 'observer-meta.json', {'requested_interval_ms': POLL * 1000,
             'max_observed_gap_ms': self.max_gap_ms, 'polls': self.polls,
             'note': 'Transitions are observed by polling, not exact internal HAProxy event timestamps.'})


def read_csv(path):
    with Path(path).open(newline='') as stream:
        return list(csv.DictReader(stream))


def copy_events(source, target, from_ms):
    if not source.exists():
        raise RuntimeError(f'缺少原始事件文件：{source}')
    rows = read_csv(source)
    with target.open('w', newline='') as stream:
        fields = source.read_text().splitlines()[0].split(',')
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(r for r in rows if int(r['unix_ms']) >= from_ms)


def quantile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def capture_journal(out, start, end):
    def stamp(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f UTC')
    p = subprocess.Popen(['journalctl', '--no-pager', '--output=json', '-u', UNIT,
                          '--since', stamp(start), '--until', stamp(end)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    values, request_count, health_count = {}, 0, 0
    with gzip.open(out / 'haproxy-journal.jsonl.gz', 'wt', encoding='utf-8') as raw, \
            (out / 'haproxy-requests.csv').open('w', newline='') as parsed, \
            (out / 'haproxy-health-native.jsonl').open('w') as health:
        fields = ['journal_received_unix_us', 'backend', 'server', 'status', 'tr_ms',
                  'ta_ms', 'bytes', 'termination']
        writer = csv.DictWriter(parsed, fieldnames=fields)
        writer.writeheader()
        for line in p.stdout:
            raw.write(line)
            record = json.loads(line)
            message = record.get('MESSAGE', '')
            if not isinstance(message, str):
                continue
            match = re.search(r'O5REQ backend=(\S+) server=(\S+) status=(\d+) '
                              r'tr_ms=(-?\d+) ta_ms=(-?\d+) bytes=(\d+) termination=(\S+)', message)
            if match:
                data = dict(zip(fields[1:], match.groups()))
                data['journal_received_unix_us'] = record.get('__REALTIME_TIMESTAMP', '')
                writer.writerow(data)
                request_count += 1
                if data['server'] in TARGETS and 200 <= int(data['status']) < 300 and int(data['tr_ms']) >= 0:
                    values.setdefault(data['server'], []).append(int(data['tr_ms']))
            elif BACKEND + '/' in message or 'health check' in message.lower():
                health.write(line)
                health_count += 1
    err = p.stderr.read()
    if p.wait(timeout=10):
        raise RuntimeError('journalctl 导出失败：' + err)
    dump(out / 'haproxy-backend-response-header-latency.json', {
        'measurement': 'HAProxy %Tr, server response header time in ms; successful logged responses only',
        'not_same_as': 'lbd end-to-end histogram or loadgen client latency; not CPU utilization',
        'journal_request_count': request_count, 'native_health_log_count': health_count,
        'backends': {n: {'samples': len(v), 'p50_ms': quantile(v, .5),
                         'p99_ms': quantile(v, .99), 'max_ms': max(v)} for n, v in values.items()}})
    if request_count == 0:
        raise RuntimeError('没有采到 HAProxy 业务日志；本轮证据不完整。')
    return request_count


def summarize(out):
    timeline = read_csv(out / 'timeline.csv')
    steps = read_csv(out / 'steps.csv')
    total = next(r for r in steps if r['step'] == 'all')
    if len(timeline) != DURATION or int(total['dropped']) != 0:
        raise RuntimeError('逐秒记录不完整或发生 dropped，本轮不能作为计划内正式结果。')
    if sum(int(r['sent']) for r in timeline) != int(total['sent']):
        raise RuntimeError('steps/timeline 的 sent 不一致。')
    if sum(int(r['ok']) for r in timeline) != int(total['ok']):
        raise RuntimeError('steps/timeline 的 ok 不一致。')
    start = int(total['start_unix']) * 1000
    marks, events = read_csv(out / 'marks.csv'), read_csv(out / 'haproxy-events.csv')
    detection = []
    for mark in marks:
        if mark['action'] not in ('kill', 'pause', 'slow'):
            continue
        at = int(mark['unix_ms'])
        down = next((e for e in events if e['addr'] == mark['target'] and e['event'] == 'down'
                     and int(e['observed_unix_ms']) >= at), None)
        up = next((e for e in events if down and e['addr'] == mark['target'] and e['event'] == 'up'
                   and int(e['observed_unix_ms']) > int(down['observed_unix_ms'])), None)
        row = {'action': mark['action'], 'target': mark['target'], 'mark_unix_ms': at,
               'mark_relative_s': (at-start)/1000, 'observed_down': down, 'observed_up': up}
        if down:
            row['detection_observed_s'] = (int(down['observed_unix_ms'])-at)/1000
            row['detection_lower_bound_s'] = max(0, (int(down['previous_poll_start_unix_ms'])-at)/1000)
        if up:
            row['recovery_observed_s_from_injection'] = (int(up['observed_unix_ms'])-at)/1000
        detection.append(row)
    dump(out / 'haproxy-detection.json', {'measurement': 'Conservative polling interval: after the previous polling request began and no later than the current response completed.',
                                         'injections': detection})
    # Original lab3sum supplies the same client/instance calculation as task C.
    # Its lbd-specific injection table is deliberately replaced by HAProxy observations.
    base = command(['bin/lab3sum', '-dir', out, '-slo', '0.2']).stdout.split('\n## 注入')[0]
    base += '\n## 客户端阶段汇总\n\n'
    base += f"发出 {total['sent']}，成功 {total['ok']}，错误率 {float(total['error_rate'])*100:.2f}%，成功请求 p99 {total['p99_ms']} ms，SLO good {total['slo_good']}。\n"
    base += '\n## HAProxy 的采样观测\n\n| 注入 | 相对秒 | 首次 DOWN 观测用时 s | DOWN 时间下界 s | 首次恢复 UP 用时 s（从注入算） |\n|---|---:|---:|---:|---:|\n'
    for r in detection:
        base += f"| {r['action']} {r['target']} | {r['mark_relative_s']:.3f} | {r.get('detection_observed_s','未观测到')} | {r.get('detection_lower_bound_s','—')} | {r.get('recovery_observed_s_from_injection','—')} |\n"
    base += '\n状态按 200 ms 目标间隔采样，实际间隔见 observer-meta.json。精确配置和原始检查原因见 haproxy.cfg、haproxy-health-native.jsonl；journal 时间为日志接收时间。\n'
    base += '\ncontrol-lbd-events.csv 仅是 scaler 管理路径的旁证，其中 removed/down 不是 HAProxy 的判定。CPU Prometheus 数据来自实例；原 lb_* 指标仍属于控制用 lbd，不能作为 HAProxy 指标。\n'
    base += '\nHAProxy %Tr 是服务端响应头耗时，和 lbd 的直方图、客户端 p99 不是同一测量口径。跨负载均衡器比较统一使用 loadgen steps.csv/timeline.csv。\n'
    (out / 'summary.md').write_text(base)
    return marks, total


class Runner:
    def __init__(self, name, scenario, automatic, inputs):
        self.name, self.scenario, self.automatic, self.inputs = name, scenario, automatic, inputs
        self.out = REPO / 'runs' / name
        self.child = self.injection = self.observer = None
        self.anchor = self.start_wall = None
        self.start_pid = {p: inputs['units'][p]['MainPID'] for p in ('8102', '8103')}
        self.ended = self.created = False

    def event(self, action, planned=None, **extra):
        row = {'time': datetime.now().astimezone().isoformat(timespec='milliseconds'),
               'action': action, 'planned_s': planned, 'observed_s': None if self.anchor is None
               else round(time.monotonic()-self.anchor, 4), **extra}
        print('[O5] ' + json.dumps(row, ensure_ascii=False), flush=True)
        if self.out.is_dir():
            with (self.out / 'runner-events.jsonl').open('a') as stream:
                stream.write(json.dumps(row, ensure_ascii=False)+'\n')

    def check(self):
        self.observer.check()
        if self.child.poll() is not None:
            raise RuntimeError('loadgen 提前退出，取消后续注入。')

    def wait_at(self, second):
        while time.monotonic() < self.anchor + second:
            self.check()
            time.sleep(min(.1, max(0, self.anchor+second-time.monotonic())))
        self.check()
        if time.monotonic()-self.anchor-second > 2:
            raise RuntimeError(f'第 {second} 秒动作迟到超过 2 秒，取消后续注入。')

    def inject(self, args, second):
        self.check()
        self.event('inject_begin', second, args=args)
        self.injection = subprocess.Popen(['bash', 'scripts/inject.sh', *map(str,args)],
                                         cwd=REPO, start_new_session=True)
        while self.injection.poll() is None:
            self.check()
            time.sleep(.05)
        if self.injection.returncode:
            raise RuntimeError('inject.sh 失败。')
        self.event('inject_complete', second, args=args)

    def snapshot(self, label):
        dump(self.out / (label + '.json'), {'time_unix_ms': time.time_ns()//1_000_000,
             'instances': common.get_json(common.SCALER+'/instances'),
             'control_lbd_backends': common.get_json(common.LB+'/backends'),
             'units': {str(p): unit_properties(p) for p in (8101,8102,8103)},
             'haproxy_servers': parse_stats(runtime('show stat'))[1]})
        self.event('snapshot', label=label)

    def baseline_ready(self):
        ins = common.get_json(common.SCALER+'/instances')
        _, rows = parse_stats(runtime('show stat'))
        if ({i['addr'] for i in ins} != set(TARGETS.values())
                or not all(i['state']=='ready' for i in ins)
                or not all(r['status']=='UP' for r in rows)):
            raise RuntimeError('第 150 秒替补尚未在 scaler 与 HAProxy 同时就绪，取消第二次故障。')

    def restore_faults(self):
        # Only resume a stopped original main PID, never stop unrelated units.
        u = unit_properties(8103)
        if u.get('MainPID') == self.start_pid['8103'] and 'T (stopped)' in u.get('proc_state',''):
            r = command(['systemctl','kill','--kill-whom=main','--signal=SIGCONT',
                         'lab3-inst-8103.service'], check=False)
            self.event('emergency_resume', returncode=r.returncode)
            if r.returncode:
                raise RuntimeError('紧急恢复 8103 失败。')
        if self.scenario == 'c3':
            u = unit_properties(8102)
            if u.get('MainPID') != self.start_pid['8102']:
                raise RuntimeError('8102 PID 发生变化，需要人工核对；拒绝向新进程静默套用配额。')
            if u.get('cpu.max','').split() and Decimal(u['cpu.max'].split()[0])/Decimal(u['cpu.max'].split()[1]) != Decimal('.25'):
                command(['bash','scripts/inject.sh','restore','8102','25'])
                self.event('emergency_quota_restore', quota_percent=25)

    def cleanup(self):
        common.stop_child(self.injection)
        common.stop_child(self.child)
        self.restore_faults()
        if self.observer and self.observer.thread.is_alive():
            self.observer.stop()

    def run(self):
        self.out.mkdir()  # no exist_ok: never overwrite a completed or aborted run
        self.created = True
        shutil.copyfile(CONFIG, self.out/'haproxy.cfg')
        shutil.copyfile(PREDICTION, self.out/'prediction-before.md')
        shutil.copyfile(REPO/'deploy/o5-haproxy-version.txt', self.out/'haproxy-version.txt')
        plan = {'scenario':self.scenario,'automatic_injections':self.automatic,
                'url':URL,'rate':RATE,'duration_s':DURATION,'slo_s':.2,
                'control_plane_note':'Existing lbd carries no experiment business requests.',
                'inputs':self.inputs,'actions_s':{'kill_8102':60,'check_replacement':150,
                    'pause_8103_for_20s':160} if self.scenario=='c1' else
                    {'slow_8102':60,'restore_8102':180},
                'baseline_comparison': ['runs/c1-stop'] if self.scenario=='c1' else
                    ['runs/c3-slow','runs/c3-outlier']}
        dump(self.out/'runner-plan.json',plan)
        (self.out/'haproxy-stats-before.csv').write_text(runtime('show stat'))
        (self.out/'haproxy-info-before.txt').write_text(runtime('show info'))
        self.snapshot('before-load')
        self.observer = Observer(self.out)
        self.observer.start()
        env = os.environ.copy();env['GOMAXPROCS']='2'
        run_command=['taskset','-c','0-1','bin/loadgen','-url',URL,'-plan','160@240s',
                     '-slo','0.2s','-stop-below','0','-out',str(self.out/'steps.csv'),
                     '-timeline',str(self.out/'timeline.csv')]
        self.start_wall=time.time()
        (REPO/'data/run-start').write_text(str(int(self.start_wall))+'\n')
        self.anchor=time.monotonic()
        dump(self.out/'run-clock.json',{'launch_wall_unix':self.start_wall,
             'launch_monotonic':self.anchor,'note':'Injection schedule uses monotonic launch time; loadgen CSV start_unix has second resolution.'})
        with (self.out/'loadgen.log').open('w') as log:
            self.child=subprocess.Popen(run_command,cwd=REPO,env=env,stdout=log,
                                        stderr=subprocess.STDOUT,start_new_session=True)
            self.event('load_started',command=run_command)
            self.wait_at(45)
            q=urllib.parse.urlencode({'query':'rate(cgroup_cpu_usage_seconds_total{job="instances"}[30s])'})
            (self.out/'cpu-before.json').write_bytes(common.fetch(common.PROM+'/api/v1/query?'+q))
            self.wait_at(55);self.snapshot('before-fault')
            if self.automatic:
                self.wait_at(60)
                if self.scenario=='c1':
                    self.inject(['kill',8102],60)
                    self.wait_at(150);self.snapshot('replacement-before-pause');self.baseline_ready()
                    self.wait_at(160);self.inject(['pause',8103,20],160)
                    self.wait_at(185);self.snapshot('after-resume')
                else:
                    self.inject(['slow',8102,self.inputs['calibration']['slow_quota_percent']],60)
                    self.wait_at(120);self.snapshot('during-slow')
                    self.wait_at(180);self.inject(['restore',8102,25],180)
                    self.wait_at(185);self.snapshot('after-quota-restore')
            else:
                print('手动采集中：在另一 SSH 终端执行注入。当前终端不要另输命令。',flush=True)
                self.wait_at(120);self.snapshot('manual-midpoint')
                self.wait_at(210);self.snapshot('manual-late')
            deadline=self.anchor+DURATION+15
            while self.child.poll() is None:
                self.observer.check()
                if time.monotonic()>deadline:
                    raise RuntimeError('loadgen 未按期结束。')
                time.sleep(.1)
            if self.child.returncode:
                raise RuntimeError(f'loadgen 退出码 {self.child.returncode}')
        self.event('load_finished')
        self.restore_faults()
        time.sleep(2)  # allow final per-request logs to reach the journal
        self.observer.stop()
        self.snapshot('after-load')
        (self.out/'haproxy-stats-end.csv').write_text(runtime('show stat'))
        (self.out/'haproxy-info-end.txt').write_text(runtime('show info'))
        for name in ['marks.csv','decisions.csv','lbd-events.csv']:
            dest='control-lbd-events.csv' if name=='lbd-events.csv' else name
            copy_events(REPO/'data'/name,self.out/dest,int(self.start_wall*1000))
        shutil.copyfile(REPO/'data/instances.csv',self.out/'instances.csv')
        logged=capture_journal(self.out,self.start_wall,time.time())
        query='rate(cgroup_cpu_usage_seconds_total{job="instances"}[30s])'
        params=urllib.parse.urlencode({'query':query,'start':self.start_wall,
                                      'end':self.start_wall+DURATION,'step':'1s'})
        (self.out/'cpu-cores-range.json').write_bytes(common.fetch(common.PROM+'/api/v1/query_range?'+params))
        marks,total=summarize(self.out)
        wanted=[('kill',TARGETS['inst8102']),('pause',TARGETS['inst8103']),('resume',TARGETS['inst8103'])] \
            if self.scenario=='c1' else [('slow',TARGETS['inst8102']),('restore',TARGETS['inst8102'])]
        actual=[(m['action'],m['target']) for m in marks]
        if actual!=wanted:
            raise RuntimeError(f'注入记录不符合本轮场景：{actual}；预期 {wanted}')
        after=protected_hashes()
        if after!=self.inputs['formal_c_sha256']:
            raise RuntimeError('C 原始证据发生变化。')
        dump(self.out/'validation.json',{'status':'PASS','scenario':self.scenario,
             'formal_c_files_unchanged':len(after),'timeline_seconds':DURATION,
             'marks':actual,'client_sent':int(total['sent']),'client_ok':int(total['ok']),
             'journal_request_count':logged,'note':'HAProxy logs may include responses after a client timeout; counts need not equal client success count.'})
        manifest=[]
        for path in sorted(self.out.iterdir()):
            if path.is_file():manifest.append(digest(path)+'  '+path.name)
        (self.out/'SHA256SUMS.txt').write_text('\n'.join(manifest)+'\n')
        self.ended=True
        self.event('finished',directory=str(self.out))
        # runner-events is now final; refresh its checksum after the last event.
        manifest=[digest(p)+'  '+p.name for p in sorted(self.out.iterdir())
                  if p.is_file() and p.name!='SHA256SUMS.txt']
        (self.out/'SHA256SUMS.txt').write_text('\n'.join(manifest)+'\n')
        print((self.out/'summary.md').read_text(),flush=True)


def interrupted(signum, frame):
    raise KeyboardInterrupt(f'signal {signum}')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group()
    g.add_argument('--check',action='store_true',help='只读检查；不启动 HAProxy、压测或注入')
    g.add_argument('--plan',action='store_true',help='只显示预测与计划；不要求服务运行')
    g.add_argument('--start-proxy',action='store_true',help='只启动实验 HAProxy，供手动练习')
    g.add_argument('--stop-proxy',action='store_true',help='只停止本脚本专用 HAProxy unit')
    g.add_argument('--status',action='store_true',help='只读显示专用 HAProxy 后端状态')
    g.add_argument('--manual',metavar='NAME',help='采集 240 秒，不自动注入；需另一个终端手动注入')
    p.add_argument('--scenario',choices=['both','c1','c3'],default='both')
    p.add_argument('--prefix',default='o5',help='自动目录前缀，默认 o5-c1 / o5-c3')
    args=p.parse_args()
    runner=None;owned_proxy=False
    try:
        if args.plan:
            print(PREDICTION.read_text());print(json.dumps(calibration(),ensure_ascii=False,indent=2));return 0
        if args.status:
            if not proxy_active():print('O5 HAProxy 未运行。');return 0
            check_proxy_identity()
            for row in parse_stats(runtime('show stat'))[1]:
                print(json.dumps({k:row.get(k) for k in ['svname','status','check_status','check_duration','stot','scur','rtime','hrsp_2xx','hrsp_5xx']},ensure_ascii=False))
            return 0
        if args.manual and args.scenario=='both':
            p.error('--manual 必须指定 --scenario c1 或 c3。')
        if args.stop_proxy:
            with (REPO/'data/o5-runner.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);stop_proxy()
            print('专用 HAProxy 已停止；现有 lbd/scaler 保持运行。');return 0
        names=[args.manual] if args.manual else [valid_name(args.prefix)+'-'+s
               for s in (['c1','c3'] if args.scenario=='both' else [args.scenario])]
        inputs=preflight()
        if not args.start_proxy:
            for name in names:
                valid_name(name)
                if (REPO/'runs'/name).exists():raise RuntimeError(f'runs/{name} 已存在；换 --prefix 或手动目录名。')
        print('检查通过：HAProxy 配置有效；三台 ready；实际配额25%、CPU2、128M；88个C正式文件一致。',flush=True)
        print('正式输出：'+', '.join('runs/'+n for n in names),flush=True)
        print('C3 标定：'+json.dumps(inputs['calibration'],ensure_ascii=False),flush=True)
        if args.check:
            print('只读检查结束，未启动 HAProxy/压测/故障注入。');return 0
        os.sched_setaffinity(0,{0,1})
        with (REPO/'data/o5-runner.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGHUP,interrupted)
            if args.start_proxy:
                preflight()
                if proxy_active():
                    wait_proxy_ready()
                    print('专用 HAProxy 已在运行且三个后端 UP；未重启。');return 0
                owned_proxy=True;start_proxy();owned_proxy=False
                print('HAProxy 已就绪：127.0.0.1:18081；没有启动业务压测。');return 0
            for name,scenario in zip(names,[args.scenario] if args.manual else
                                      (['c1','c3'] if args.scenario=='both' else [args.scenario])):
                inputs=preflight(name)
                if args.manual:
                    wait_proxy_ready()
                else:
                    if proxy_active():raise RuntimeError('已有手动 HAProxy；先 --stop-proxy，再运行自动实验。')
                    owned_proxy=True;start_proxy()
                runner=Runner(name,scenario,not bool(args.manual),inputs)
                try:runner.run()
                finally:runner.cleanup()
                runner=None
                if owned_proxy:stop_proxy();owned_proxy=False
        print('O5 采集完成；正式 C 文件保留。');return 0
    except (Exception,KeyboardInterrupt) as exc:
        print('[O5] 已停止：'+str(exc),file=sys.stderr,flush=True)
        if runner and runner.created:
            runner.event('aborted',reason=str(exc))
            dump(runner.out/'ABORTED.json',{'reason':str(exc),'formal_result':False})
        return 1
    finally:
        if owned_proxy:
            try:stop_proxy()
            except Exception as exc:print('停止专用 HAProxy 失败：'+str(exc),file=sys.stderr)


if __name__=='__main__':
    raise SystemExit(main())
