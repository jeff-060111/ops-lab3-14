"""Capture cgroup evidence without depending on application survival."""
import csv
import pathlib
import sys
import time

root = pathlib.Path('/sys/fs/cgroup/system.slice/lab3-a.service')
with open(sys.argv[1], 'w', buffering=1) as out:
    writer = csv.writer(out)
    writer.writerow(['unix_time', 'memory_current_bytes', 'memory_peak_bytes', 'memory_max_bytes', 'oom', 'oom_kill'])
    while root.exists():
        try:
            events = dict(line.split() for line in (root / 'memory.events').read_text().splitlines())
            writer.writerow([time.time(), (root / 'memory.current').read_text().strip(),
                             (root / 'memory.peak').read_text().strip(),
                             (root / 'memory.max').read_text().strip(),
                             events.get('oom', 0), events.get('oom_kill', 0)])
        except FileNotFoundError:
            break
        time.sleep(0.05)
