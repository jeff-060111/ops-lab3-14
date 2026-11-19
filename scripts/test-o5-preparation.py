"""Run on the cloud host: fixtures/mocks only, never start HAProxy/loadgen or signal instances."""
import csv
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
sys.dont_write_bytecode = True
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('o5', ROOT / 'scripts/run-o5.py')
o5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o5)


def stats(status='UP'):
    s = io.StringIO()
    s.write('# pxname,svname,status,check_status,check_duration,stot,rtime\n')
    w = csv.writer(s)
    for name in o5.TARGETS:
        w.writerow([o5.BACKEND,name,status if name=='inst8102' else 'UP','L7OK',1,12,3])
    return s.getvalue()


class PreparationTests(unittest.TestCase):
    def test_port_precheck_accepts_time_wait_and_rejects_listener(self):
        # An isolated random loopback port, never an application or HAProxy port.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
            listener.listen(1)
            with self.assertRaisesRegex(RuntimeError, '实际 TCP 监听'):
                o5.ensure_no_listener(port)
            with socket.socket() as client:
                client.connect(('127.0.0.1', port))
                accepted, _ = listener.accept()
                accepted.close()
                self.assertEqual(client.recv(1), b'')
        # The old no-reuse bind probe fails here despite there being no listener.
        with socket.socket() as old_probe:
            with self.assertRaises(OSError):
                old_probe.bind(('127.0.0.1', port))
        o5.ensure_no_listener(port)

    def test_actual_formal_baseline_and_calibration(self):
        self.assertEqual(len(o5.protected_hashes()),88)
        self.assertEqual(o5.calibration()['slow_quota_percent'],13)
        self.assertEqual(o5.calibration()['usage_cores'],'0.1105564')

    def test_stats_rejects_missing_or_duplicate_backends(self):
        _, rows = o5.parse_stats(stats('DOWN 1/2'))
        self.assertEqual(o5.normalized(rows[1]['status']),'DOWN')
        self.assertEqual(o5.normalized('UP 2/3'),'UP')
        with self.assertRaises(RuntimeError):
            o5.parse_stats(stats().replace('enrolld_o5,inst8103','other,inst8103'))
        with self.assertRaises(RuntimeError):
            o5.parse_stats(stats()+'enrolld_o5,inst8102,UP,L7OK,1,12,3\n')

    def test_polling_transition_keeps_interval_and_transitional_semantics(self):
        with tempfile.TemporaryDirectory(prefix='o5-fixture-') as temp:
            out=Path(temp);observer=o5.Observer(out)
            sequence=iter(['UP','UP 2/3','DOWN','DOWN 1/2','UP'])
            counter=[0]
            def fake_runtime(query):
                value=stats(next(sequence));counter[0]+=1
                if counter[0]==5:observer.stop_event.set()
                return value
            with patch.object(o5,'runtime',side_effect=fake_runtime),patch.object(o5,'POLL',0):
                observer._run()
            self.assertIsNone(observer.error)
            transitions=o5.read_csv(out/'haproxy-events.csv')
            rows=[r for r in transitions if r['server']=='inst8102']
            self.assertEqual([r['event'] for r in rows],['initial','down','up'])
            self.assertTrue(all(int(r['previous_observed_unix_ms']) <= int(r['observed_unix_ms'])
                                for r in rows[1:]))
            self.assertEqual(len(o5.read_csv(out/'haproxy-stats.csv')),15)

    def test_no_overwrite_and_name_traversal(self):
        for value in ['../oops','/tmp/oops','..','x/y','x y','']:
            with self.assertRaises(RuntimeError):o5.valid_name(value)
        # Existing formal data are rejected before any environment commands.
        with patch.object(o5.common,'preflight',side_effect=AssertionError('must not reach services')):
            with self.assertRaisesRegex(RuntimeError,'已存在'):o5.preflight('c1-stop')

    def test_sampling_detection_does_not_claim_internal_exact_timestamp(self):
        with tempfile.TemporaryDirectory(prefix='o5-summary-fixture-') as temp:
            out=Path(temp)
            # Reuse real immutable client data as a fixture, write only to /tmp.
            for n in ['steps.csv','timeline.csv']:
                (out/n).write_bytes((ROOT/'runs/c1-stop'/n).read_bytes())
            start=int(o5.read_csv(out/'steps.csv')[0]['start_unix'])*1000
            with (out/'marks.csv').open('w',newline='') as stream:
                w=csv.DictWriter(stream,fieldnames=['unix_ms','time','action','target']);w.writeheader()
                w.writerow({'unix_ms':start+60000,'time':'fixture','action':'pause','target':o5.TARGETS['inst8103']})
            with (out/'haproxy-events.csv').open('w',newline='') as stream:
                w=csv.DictWriter(stream,fieldnames=['observed_unix_ms','previous_observed_unix_ms','previous_poll_start_unix_ms','addr','event']);w.writeheader()
                w.writerow({'observed_unix_ms':start+62900,'previous_observed_unix_ms':start+62701,'previous_poll_start_unix_ms':start+62700,
                            'addr':o5.TARGETS['inst8103'],'event':'down'})
                w.writerow({'observed_unix_ms':start+82500,'previous_observed_unix_ms':start+82301,'previous_poll_start_unix_ms':start+82300,
                            'addr':o5.TARGETS['inst8103'],'event':'up'})
            o5.summarize(out)
            result=json.loads((out/'haproxy-detection.json').read_text())['injections'][0]
            self.assertEqual(result['detection_observed_s'],2.9)
            self.assertEqual(result['detection_lower_bound_s'],2.7)
            self.assertEqual(result['recovery_observed_s_from_injection'],22.5)
            self.assertNotIn('lbd 判定',(out/'summary.md').read_text())


if __name__=='__main__':
    unittest.main(verbosity=2)
