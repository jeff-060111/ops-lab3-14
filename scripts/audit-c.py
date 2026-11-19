#!/usr/bin/env python3
"""Read-only audit of the five formal task-C runs; --write saves derived indexes."""
import argparse
import csv
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parent.parent
FORMAL = ['c1-stop', 'c2-default', 'c2-sensitive', 'c3-slow', 'c3-outlier']
STANDARD = ['meta.txt', 'steps.csv', 'timeline.csv', 'lbd-events.csv', 'decisions.csv',
            'instances.csv', 'marks.csv', 'lbd-metrics-end.txt', 'loadgen.log', 'summary.md']


def read_csv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config(meta, label):
    return json.JSONDecoder().raw_decode(meta.split(label + ':\n', 1)[1])[0]


def summary_errors(run):
    text = (run / 'summary.md').read_text()
    rows = []
    if '没有缺口。' not in text:
        part = text.split('## 缺口', 1)[1].split('## 实例', 1)[0]
        for line in part.splitlines():
            fields = [x.strip() for x in line.strip('|').split('|')]
            if len(fields) == 6 and all(x.isdigit() for x in fields):
                rows.append([int(x) for x in fields])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    report = (REPO / 'LAB3.md').read_text()
    section = report.split('## 4. 任务 C：故障注入', 1)[1].split('## 5.', 1)[0]
    question = report.split('**问题 3：健康检查。**', 1)[1].split('**问题 4：', 1)[0]
    assert '<填写>' not in section, 'Task C still has placeholders'
    assert '<填写>' not in question, 'Health-check answer missing'
    # C3 prediction is a historical, unmodified pre-measurement snapshot.
    assert (REPO / 'runs/c3-prediction-before.md').read_text() in report
    c2_before=(REPO/'runs/c2-prediction-before.md').read_text()
    c2_display=report.split('### C2 测前预测',1)[1].split('排版说明：',1)[0]
    assert re.findall(r'\d+(?:\.\d+)?',c2_before)==re.findall(r'\d+(?:\.\d+)?','### C2 测前预测'+c2_display)
    assert not re.search(r'runs/c[123]-manual', section + question), 'Practice data referenced in formal analysis'
    results = {}
    hashes_before = {}
    for name in FORMAL:
        run = REPO / 'runs' / name
        assert all((run / f).is_file() for f in STANDARD), f'{name}: missing standard output'
        files = sorted(p for p in run.rglob('*') if p.is_file())
        hashes_before.update({str(p.relative_to(REPO)): digest(p) for p in files})
        rows, timeline = read_csv(run/'steps.csv'), read_csv(run/'timeline.csv')
        allrow = next(r for r in rows if r['step'] == 'all')
        stages = [r for r in rows if r['step'] != 'all']
        t0, t1 = int(allrow['start_unix']), int(allrow['end_unix'])
        sent, ok = int(allrow['sent']), int(allrow['ok'])
        errors = sum(int(allrow[k]) for k in ['http_err', 'timeout', 'net_err'])
        assert sent - ok == errors, f'{name}: sent/ok/error mismatch'
        assert all(int(r['target_rps']) == 160 for r in stages), f'{name}: wrong load'
        assert sum(int(r['sent']) for r in timeline) == sent
        assert sum(int(r['ok']) for r in timeline) == ok
        assert sum(int(r['errors']) for r in timeline) == errors
        assert int(allrow['dropped']) == 0
        assert not allrow['warn']
        assert len(timeline) == t1 - t0 == (120 if name.startswith('c2-') else 240)
        bad = [r for r in timeline if int(r['errors']) or (r['p99_ms'] and float(r['p99_ms']) > 200)]
        gaps = summary_errors(run)
        assert sum(r[2] for r in gaps) == len(bad), f'{name}: gap duration mismatch'
        assert sum(r[4] for r in gaps) == sum(int(r['errors']) for r in bad)
        summary = subprocess.run(['bin/lab3sum', '-dir', str(run), '-slo', '0.2'], cwd=REPO,
                                 capture_output=True, text=True, check=True).stdout
        assert summary == (run/'summary.md').read_text(), f'{name}: non-reproducible summary'
        runner_plan=json.loads((run/'runner-plan.json').read_text())
        runner_name='run-c1.py' if name.startswith('c1-') else ('run-c2.py' if name.startswith('c2-') else 'run-c3.py')
        assert runner_plan['script_sha256']==digest(REPO/'scripts'/runner_name), f'{name}: wrapper source differs from recorded version'
        meta = (run/'meta.txt').read_text()
        lb, scaler = config(meta, 'lbd /config'), config(meta, 'scaler /config')
        assert scaler['mode'] == 'fixed' and scaler['replicas'] == 3
        assert scaler['quota_percent'] == 25 and scaler['cpus'] == '2' and scaler['memory'] == '128M'
        assert scaler['args'] == '-warmup 60s -work 1000000' and not scaler['wait_node']
        assert not scaler['restart']
        assert lb['policy'] == 'rr' and lb['check_path'] == '/readyz' and lb['timeout'] == '2s'
        settings = ('500ms', '200ms', 1, 2) if name == 'c2-sensitive' else ('1s', '500ms', 3, 2)
        assert tuple(lb[k] for k in ['check_interval','check_timeout','fall','rise']) == settings
        assert lb['outlier'] == (name == 'c3-outlier')
        marks = [r for r in read_csv(run/'marks.csv') if t0*1000 <= int(r['unix_ms']) < t1*1000]
        events = [r for r in read_csv(run/'lbd-events.csv') if t0*1000 <= int(r['unix_ms']) < t1*1000]
        lives = [r for r in read_csv(run/'instances.csv') if t0*1000 <= int(r['unix_ms']) < t1*1000]
        incidents = []
        for m in marks:
            if m['action'] not in ['kill','pause','slow']:
                continue
            matching = [e for e in events if int(e['unix_ms']) >= int(m['unix_ms'])
                        and (m['target'] == '*' or e['backend'] == m['target'])]
            detections = [e for e in matching if e['event'] in ['removed','down','ejected']]
            first = detections[0] if detections else None
            delta = (int(first['unix_ms'])-int(m['unix_ms']))/1000 if first else None
            incidents.append({'action':m['action'],'target':m['target'],'unix_ms':int(m['unix_ms']),
                              'detection':first,'detection_seconds':delta})
        if name == 'c1-stop':
            assert [m['action'] for m in marks] == ['kill','pause','resume']
            assert [i['detection']['event'] for i in incidents] == ['removed','down']
            assert len([r for r in lives if r['event']=='died']) == 1
            assert len([r for r in lives if r['event']=='created']) == 1
            assert json.loads((run/'runner-plan.json').read_text())['inputs']['prediction'] in report
        elif name.startswith('c2-'):
            assert [m['action'] for m in marks] == ['pause','resume']
            assert all(m['target']=='*' for m in marks)
            assert 1.9 <= (int(marks[1]['unix_ms'])-int(marks[0]['unix_ms']))/1000 <= 2.2
            assert {e['backend'] for e in events if e['event']=='down'} == set(scaler_addr for scaler_addr in [
                '127.0.0.1:8101','127.0.0.1:8102','127.0.0.1:8103'])
            assert not any(r['event'] in ['created','died','restarted'] for r in lives)
        else:
            assert [m['action'] for m in marks] == ['slow','restore']
            assert all(m['target']=='127.0.0.1:8102' for m in marks)
            assert 119 <= (int(marks[1]['unix_ms'])-int(marks[0]['unix_ms']))/1000 <= 121
            assert not lives and not any(e['event']=='down' for e in events)
            before = json.loads((run/'before-slow.json').read_text())['resources']
            during = json.loads((run/'during-slow.json').read_text())['resources']
            after = json.loads((run/'after-quota-restore.json').read_text())['resources']
            for port in ['8101','8102','8103']:
                assert before[port]['pid']==during[port]['pid']==after[port]['pid']
                assert before[port]['quota_percent']==after[port]['quota_percent']==25
                assert during[port]['quota_percent']==(13 if port=='8102' else 25)
            assert len([e for e in events if e['event']=='ejected']) == (3 if name.endswith('outlier') else 0)
        results[name] = {'sent':sent,'ok':ok,'errors':errors,'http_err':int(allrow['http_err']),
                         'timeouts':int(allrow['timeout']),'codes':allrow['codes'],
                         'p99_ms':float(allrow['p99_ms']),'max_ms':float(allrow['max_ms']),
                         'slow_successes':sum(int(r['slow']) for r in timeline),
                         'gap_seconds':len(bad),'gap_intervals':gaps,'incidents':incidents,
                         'events':events,'time_window_unix':[t0,t1],'lbd_config':lb,'scaler_config':scaler}
    # Check that each comparison changes only its intended lbd options (apart from start time).
    for a,b,allowed in [('c2-default','c2-sensitive',{'started','check_interval','check_timeout','fall'}),
                        ('c3-slow','c3-outlier',{'started','outlier'})]:
        x,y=results[a]['lbd_config'],results[b]['lbd_config']
        assert {k for k in x.keys()|y.keys() if x.get(k)!=y.get(k)} <= allowed
    sample=json.loads((REPO/'runs/c1-stop/cpu-before.json').read_text())
    value=next(r['value'][1] for r in sample['data']['result'] if r['metric']['instance']=='127.0.0.1:8102')
    assert int((Decimal(value)/Decimal('0.85')*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))==13
    for name,value in hashes_before.items():
        assert digest(REPO/name)==value, f'Raw file changed during audit: {name}'
    # The course prohibits the host's public IPv4 address in reports/raw outputs.
    for label,text in [('LAB3.md',report)]+[(name,(REPO/name).read_text()) for name in hashes_before]:
        for value in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])',text):
            try:
                address=ipaddress.IPv4Address(value)
            except ipaddress.AddressValueError:
                continue
            assert not address.is_global, f'{label}: public IPv4 address present'
    audit={'audited_at':datetime.now().astimezone().isoformat(timespec='seconds'),
           'scope':'only five formal task-C runs; practice directories excluded',
           'base_commit':subprocess.run(['git','rev-parse','HEAD'],cwd=REPO,capture_output=True,text=True,check=True).stdout.strip(),
           'checks':['standard outputs present','client totals reconcile','timeline totals reconcile',
                     'gap statistics reconcile','summaries rebuild identically','resource and load configs match',
                     'comparison variables isolated','wrapper versions match recorded hashes','fault actions match scenarios','predictions retained',
                     'task-C and question-3 placeholders cleared','raw bytes unchanged during audit'],
           'formal_runs':results,'raw_files_sha256':hashes_before}
    if args.write:
        (REPO/'C_EVIDENCE_AUDIT.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2))
    print(json.dumps({'status':'PASS','formal_runs':len(results),'raw_files_checked':len(hashes_before),
                      'audit_file':'C_EVIDENCE_AUDIT.json' if args.write else None},ensure_ascii=False))


if __name__=='__main__':
    main()
