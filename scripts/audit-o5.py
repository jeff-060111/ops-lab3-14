#!/usr/bin/env python3
"""Read-only O5 evidence audit. --write saves the derived root JSON; never runs a workload."""
import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import gzip
import hashlib
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('o5_runner', REPO/'scripts/run-o5.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def csv(path):
    return runner.read_csv(path)


def check_text(label, text):
    assert not re.search(r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----', text), label
    for value in re.findall(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', text):
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            continue
        assert not address.is_global, f'{label}: public IPv4 address'


def native_stats(path):
    return runner.parse_stats(Path(path).read_text())[1]


def stat_values(unit):
    return {l.split()[0]: int(l.split()[1]) for l in unit['cpu.stat'].splitlines()}


def audit(require_report=True):
    protected = runner.protected_hashes()
    paths = [p for name in ['o5-c1','o5-c3'] for p in sorted((REPO/'runs'/name).iterdir()) if p.is_file()]
    hashes = {str(p.relative_to(REPO)):sha(p) for p in paths}
    assert len(hashes)==70
    old = ast.parse((REPO/'runs/o5-c1-runner-before-port-fix.py').read_text())
    new = ast.parse((REPO/'scripts/run-o5.py').read_text())
    old_start = next(n for n in old.body if isinstance(n,ast.FunctionDef) and n.name=='start_proxy')
    normalized=[]
    for node in new.body:
        if isinstance(node,ast.FunctionDef) and node.name=='ensure_no_listener':
            continue
        normalized.append(old_start if isinstance(node,ast.FunctionDef) and node.name=='start_proxy' else node)
    new.body=normalized
    assert ast.dump(old)==ast.dump(new),'Runner change extends beyond pre-start port check'
    raw_sample=read(REPO/'runs/c1-stop/cpu-before.json')
    value=next(r['value'][1] for r in raw_sample['data']['result'] if r['metric']['instance']=='127.0.0.1:8102')
    quota=int((Decimal(value)/Decimal('.85')*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    assert quota==13
    results={}
    pattern=re.compile(r'O5REQ backend=(\S+) server=(\S+) status=(\d+) tr_ms=(-?\d+) ta_ms=(-?\d+) bytes=(\d+) termination=(\S+)')
    for name in ['o5-c1','o5-c3']:
        directory=REPO/'runs'/name
        assert not (directory/'ABORTED.json').exists()
        listed={}
        for line in (directory/'SHA256SUMS.txt').read_text().splitlines():
            expected,filename=line.split('  ',1)
            assert '/' not in filename and '\\' not in filename
            assert sha(directory/filename)==expected,(name,filename)
            listed[filename]=expected
        assert set(listed)=={p.name for p in directory.iterdir() if p.is_file()}-{'SHA256SUMS.txt'}
        assert len(listed)==34
        plan=read(directory/'runner-plan.json');inputs=plan['inputs']
        expected_script=REPO/('runs/o5-c1-runner-before-port-fix.py' if name=='o5-c1' else 'scripts/run-o5.py')
        assert inputs['script_sha256']==sha(expected_script)
        assert inputs['haproxy_config_sha256']==sha(directory/'haproxy.cfg')==sha(REPO/'deploy/haproxy-o5.cfg')
        assert inputs['prediction_sha256']==sha(directory/'prediction-before.md')==sha(REPO/'runs/o5-prediction-before.md')
        assert inputs['formal_c_sha256']==protected
        assert inputs['calibration']['usage_cores']==value and inputs['calibration']['slow_quota_percent']==quota
        assert plan['automatic_injections'] and plan['rate']==160 and plan['duration_s']==240 and plan['slo_s']==.2
        assert plan['url']=='http://127.0.0.1:18081/api/courses/1'
        prediction=(directory/'prediction-before.md').read_text()
        prepared=inputs['prepared_environment']['prepared_at']
        total=next(r for r in csv(directory/'steps.csv') if r['step']=='all')
        t0,t1=int(total['start_unix']),int(total['end_unix'])
        assert datetime.fromisoformat(prepared).timestamp()<t0
        assert prepared in prediction and '尚未执行' in prediction
        timeline=csv(directory/'timeline.csv')
        assert len(timeline)==240 and [int(r['sec']) for r in timeline]==list(range(240))
        sent,ok=int(total['sent']),int(total['ok'])
        errors=sum(int(total[k]) for k in ['http_err','timeout','net_err','dropped'])
        assert sent==sum(int(r['sent']) for r in timeline)==38626
        assert ok==sum(int(r['ok']) for r in timeline)
        assert errors==sent-ok==sum(int(r['errors']) for r in timeline)
        assert int(total['dropped'])==0 and not total['warn']
        bad=[int(r['sec']) for r in timeline if int(r['errors']) or float(r['p99_ms'] or 0)>200]
        gaps=[]
        for sec in bad:
            if gaps and gaps[-1][1]==sec:gaps[-1][1]=sec+1
            else:gaps.append([sec,sec+1])
        slow=sum(int(r['slow']) for r in timeline)
        marks=csv(directory/'marks.csv')
        expected_actions=['kill','pause','resume'] if name=='o5-c1' else ['slow','restore']
        assert [m['action'] for m in marks]==expected_actions
        wanted=[('kill','127.0.0.1:8102',60),('pause','127.0.0.1:8103',160),('resume','127.0.0.1:8103',180)] if name=='o5-c1' else [('slow','127.0.0.1:8102',60),('restore','127.0.0.1:8102',180)]
        launch=read(directory/'run-clock.json')['launch_wall_unix']
        for m,(action,target,planned) in zip(marks,wanted):
            assert m['target']==target and abs(int(m['unix_ms'])/1000-launch-planned)<1
        stats=csv(directory/'haproxy-stats.csv')
        observer=read(directory/'observer-meta.json')
        assert len(stats)==observer['polls']*3 and observer['polls']>=1200
        assert observer['requested_interval_ms']==200 and observer['max_observed_gap_ms']<1000
        states=defaultdict(Counter)
        for row in stats:states[row['svname']][row['status']]+=1
        assert set(states)==set(runner.TARGETS)
        events=csv(directory/'haproxy-events.csv')
        for row in events:
            if row['event']!='initial':
                assert int(row['previous_poll_start_unix_ms'])<=int(row['previous_observed_unix_ms'])<=int(row['observed_unix_ms'])
        detection=read(directory/'haproxy-detection.json')
        for m,d in zip([m for m in marks if m['action'] in ['kill','pause','slow']],detection['injections']):
            assert int(m['unix_ms'])==d['mark_unix_ms'] and m['target']==d['target']
            if d['observed_down']:
                e=d['observed_down'];at=int(m['unix_ms'])
                assert d['detection_observed_s']==(int(e['observed_unix_ms'])-at)/1000
                assert d['detection_lower_bound_s']==max(0,(int(e['previous_poll_start_unix_ms'])-at)/1000)
        before_stats=native_stats(directory/'haproxy-stats-before.csv')
        end_stats=native_stats(directory/'haproxy-stats-end.csv')
        assert sum(int(r['stot']) for r in end_stats)==sent
        native_success=sum(int(r['hrsp_2xx']) for r in end_stats)
        requests=csv(directory/'haproxy-requests.csv')
        native_logs=[];backend_values=defaultdict(list);journal_codes=Counter()
        with gzip.open(directory/'haproxy-journal.jsonl.gz','rt') as stream:
            for line in stream:
                check_text(name+'/journal',line)
                record=json.loads(line);message=record.get('MESSAGE','')
                if not isinstance(message,str):continue
                for match in pattern.finditer(message):
                    fields=match.groups()
                    native_logs.append((record['__REALTIME_TIMESTAMP'],*fields))
                    journal_codes[fields[2]]+=1
                    if fields[1] in runner.TARGETS and 200<=int(fields[2])<300 and int(fields[3])>=0:
                        backend_values[fields[1]].append(int(fields[3]))
        assert len(native_logs)==len(requests)
        for raw,parsed in zip(native_logs,requests):
            assert tuple(parsed[k] for k in ['journal_received_unix_us','backend','server','status','tr_ms','ta_ms','bytes','termination'])==raw
        latency=read(directory/'haproxy-backend-response-header-latency.json')
        for server,values in backend_values.items():
            assert latency['backends'][server]=={'samples':len(values),'p50_ms':runner.quantile(values,.5),'p99_ms':runner.quantile(values,.99),'max_ms':max(values)}
        assert latency['journal_request_count']==len(native_logs)
        log_gap=sent-len(native_logs)
        assert log_gap==(0 if name=='o5-c1' else 2)
        validation=read(directory/'validation.json')
        assert validation['status']=='PASS' and validation['formal_c_files_unchanged']==88
        assert validation['client_sent']==sent and validation['client_ok']==ok and validation['journal_request_count']==len(native_logs)
        assert all(r['status']=='UP' for r in end_stats)
        last=read(directory/'after-load.json')
        assert len(last['instances'])==3 and all(i['state']=='ready' for i in last['instances'])
        cpu_evidence=None
        if name=='o5-c1':
            assert [e['event'] for e in events if e['event']!='initial']==['down','up','down','up']
            assert [d['observed_down']['check_status'] for d in detection['injections']]==['L4CON','L7TOUT']
            lives=[r for r in csv(directory/'instances.csv') if t0*1000<=int(r['unix_ms'])<t1*1000]
            assert len([r for r in lives if r['event']=='created'])==len([r for r in lives if r['event']=='died'])==1
        else:
            assert all(row['status']=='UP' for row in stats)
            assert all(int(r['chkfail'])==int(r['chkdown'])==0 for r in before_stats+end_stats)
            assert all(e['event']=='initial' for e in events)
            snapshots=[read(directory/f)['units']['8102'] for f in ['before-fault.json','during-slow.json','after-quota-restore.json','after-load.json']]
            assert len({u['MainPID'] for u in snapshots})==1
            assert [u['cpu.max'].split()[0] for u in snapshots]==['25000','13000','25000','25000']
            a,b=map(stat_values,snapshots[:2]);assert b['nr_throttled']>a['nr_throttled']
            cpu_evidence={'main_pid':snapshots[0]['MainPID'],'cpu_max_sequence':[u['cpu.max'].strip() for u in snapshots],
                          'nr_throttled_before55':a['nr_throttled'],'nr_throttled_during120':b['nr_throttled'],
                          'nr_periods_delta_55_to_120':b['nr_periods']-a['nr_periods'],
                          'note':'65-second interval includes 5 seconds before quota reduction; throttled periods are not CPU utilization.'}
            assert native_success==sent
        rebuilt=runner.command(['bin/lab3sum','-dir',directory,'-slo','0.2']).stdout.split('\n## 注入')[0]
        assert (directory/'summary.md').read_text().startswith(rebuilt)
        for path in directory.iterdir():
            if path.is_file() and path.suffix!='.gz':check_text(str(path.relative_to(REPO)),path.read_text())
        results[name]={'time_window_unix':[t0,t1],'duration_seconds':240,'sent':sent,'ok':ok,'errors':errors,
            'error_rate_exact':errors/sent,'http_err':int(total['http_err']),'timeouts':int(total['timeout']),
            'client_codes':total['codes'],'client_p99_ms':float(total['p99_ms']),'client_max_ms':float(total['max_ms']),
            'client_mean_ms':float(total['mean_ms']),'slow_successes':slow,'gap_seconds':len(bad),'gap_intervals':gaps,
            'marks':marks,'detection':detection['injections'],'observer':observer,
            'states':{k:dict(v) for k,v in states.items()},'native_check_counters_end':[{k:r[k] for k in ['svname','chkfail','chkdown','stot','hrsp_2xx']} for r in end_stats],
            'journal_request_count':len(native_logs),'journal_missing_vs_client':log_gap,
            'journal_status_counts':dict(journal_codes),'backend_response_header_latency':latency['backends'],
            'cpu_evidence':cpu_evidence,'script_sha256':inputs['script_sha256'],'prediction_sha256':inputs['prediction_sha256']}
    assert protected==runner.protected_hashes()
    assert hashes=={str(p.relative_to(REPO)):sha(p) for p in paths}
    report=(REPO/'LAB3.md').read_text()
    if require_report:
        section=report.split('## 12. 选做 O5：换负载均衡器',1)[1]
        assert '<填写>' not in section and '38624' in section and '38626' in section
        assert 'runs/o5-c1/' in section and 'runs/o5-c3/' in section and 'TIME_WAIT' in section
        assert '2.625' in section and '3.677' in section and '26.63' in section
        assert '13%' in section and '0.363' in section and '0.1105564' in section
        assert not re.search(r'runs/[^\s`|]*manual',section)
        line=next(l for l in report.splitlines() if '| O5 换负载均衡器' in l)
        assert '已完成' in line and '<未做' not in line
        check_text('LAB3.md',report)
    return {'audited_at':datetime.now().astimezone().isoformat(timespec='seconds'),
            'status':'PASS','scope':'two formal O5 runs only; 70 immutable files including per-run manifests',
            'base_commit':runner.command(['git','rev-parse','HEAD']).stdout.strip(),
            'checks':['all 68 per-run checksums match; no unlisted raw files','70 O5 raw files unchanged during audit',
                      '88 task-C formal raw files match original manifest','client/timeline/native response totals reconcile',
                      'predictions precede runs and snapshots match','same HAProxy config, resource conditions and formal C1 calibration',
                      'runner AST differs only in pre-start port check','fault targets and observed injection times match scenarios',
                      'polling-derived detection intervals trace to raw events','native journal and parsed request records reconcile',
                      '2 missing C3 request logs explicitly recorded','C3 actual quota change, stable PID and throttling verified',
                      'client/instance summary rebuilds match','no public IPv4 or private key blocks in new raw evidence'],
            'formal_runs':results,'formal_raw_sha256':hashes,'protected_c_raw_sha256':protected,
            'report_checked':require_report,'limitations':['One run per configuration; task-C baselines were measured earlier the same day.',
                'HAProxy business traffic replaces lbd; lbd remains in scaler control path.',
                'C1 lbd removed after kill is a scaler notification path, unlike HAProxy active checks.',
                'HAProxy transitions are polling observations with ~200ms intervals, not exact internal timestamps.',
                'C3 journal has 38624 request logs vs 38626 complete client responses; response-header p99 uses logged samples.',
                'HAProxy %Tr is not the same measurement as client p99 or lbd duration histograms.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write',action='store_true')
    args=parser.parse_args()
    result=audit()
    if args.write:(REPO/'O5_EVIDENCE_AUDIT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':result['status'],'formal_runs':2,'o5_files_checked':len(result['formal_raw_sha256']),
                      'c_files_protected':len(result['protected_c_raw_sha256']),'c3_missing_request_logs':2},ensure_ascii=False))


if __name__=='__main__':
    main()
