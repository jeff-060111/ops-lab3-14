"""Derive GC/CPU deltas from saved samples; never replace raw output.

Usage: python3 scripts/summarize-o1.py runs/o1-default > runs/o1-default/gc-summary.csv
Each stage uses its first and last successful sample, both inside the stage.
Counters stop at the last successful scrape, not at process death.
"""
import csv
import json
from pathlib import Path
import sys


def parse_metrics(raw):
    result = {}
    for line in raw.splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            result[key] = float(value)
    return result


directory = Path(sys.argv[1])
samples = []
for line in (directory / "metrics-watch.jsonl").read_text().splitlines():
    row = json.loads(line)
    if "metrics" in row:
        samples.append((row["unix_time"], parse_metrics(row["metrics"])))
steps = list(csv.DictReader((directory / "steps.csv").open()))
steps.append({"step": "all_observed", "target_rps": "", "start_unix": "-inf", "end_unix": "inf"})
writer = csv.writer(sys.stdout)
writer.writerow([
    "step", "target_rps", "sample_start_unix", "sample_end_unix", "observed_s",
    "gc_cycles_delta", "gc_cpu_estimate_s", "process_cpu_s", "gc_per_s",
    "gc_cpu_estimate_ms_per_s", "process_cpu_ms_per_s", "rss_sample_peak_mib",
    "report_inflight_sample_peak", "last_gc_cycles_total",
])
for step in steps:
    if step["step"] == "all":
        continue
    start, end = float(step["start_unix"]), float(step["end_unix"])
    selected = [(t, m) for t, m in samples if start <= t < end]
    if len(selected) < 2:
        writer.writerow([step["step"], step["target_rps"]] + [""] * 12)
        continue
    first_t, first = selected[0]
    last_t, last = selected[-1]
    duration = last_t - first_t
    gc = last["go_gc_cycles_total"] - first["go_gc_cycles_total"]
    gc_cpu = last["go_gc_cpu_seconds_total"] - first["go_gc_cpu_seconds_total"]
    cpu = last["process_cpu_seconds_total"] - first["process_cpu_seconds_total"]
    writer.writerow([
        step["step"], step["target_rps"], f"{first_t:.6f}", f"{last_t:.6f}", f"{duration:.6f}",
        int(gc), f"{gc_cpu:.6f}", f"{cpu:.6f}", f"{gc / duration:.6f}",
        f"{gc_cpu / duration * 1000:.6f}", f"{cpu / duration * 1000:.6f}",
        f'{max(m["process_resident_memory_bytes"] for _, m in selected) / 1048576:.6f}',
        int(max(m['http_inflight_requests{route="/api/report"}'] for _, m in selected)),
        int(last["go_gc_cycles_total"]),
    ])
