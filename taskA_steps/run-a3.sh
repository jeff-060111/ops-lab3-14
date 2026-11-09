#!/usr/bin/env bash
# Run on the lab host from /root/lab3. Each name must be unused.
set -euo pipefail
name=${1:?usage: bash taskA_steps/run-a3.sh NAME MEMORY_MAX REPORT_MAX}
limit=${2:?}
report_max=${3:?}
[[ $name =~ ^a3-[A-Za-z0-9._-]+$ ]]
test ! -e "runs/$name"
mkdir -p runs/a1
systemctl stop lab3-a 2>/dev/null || true
systemctl reset-failed lab3-a 2>/dev/null || true
started=$(date --iso-8601=seconds)
systemd-run --unit=lab3-a -p "MemoryMax=$limit" -p MemorySwapMax=0 \
  "$PWD/bin/enrolld" -addr 127.0.0.1:8081 -data "$PWD/data/a" -report-max "$report_max"
sleep 3
curl -fsS http://127.0.0.1:8081/metrics > "runs/a1/$name-idle-metrics.txt"
systemctl show lab3-a -p MainPID,CPUQuotaPerSecUSec,MemoryMax,MemorySwapMax,Environment \
  > "runs/a1/$name-properties.txt"
python3 taskA_steps/watch-memory.py "runs/a1/$name-memory.csv" &
watcher=$!
trap 'kill "$watcher" 2>/dev/null || true' EXIT
GEN_CPUS=0-1 DATA=data/a scripts/run.sh "$name" \
  -url 'http://127.0.0.1:8081/api/report?mb=8&ms=500' \
  -rates 10,20,30 -step 20s -stop-below 0 \
  > "runs/a1/$name-console.log" 2>&1
kill "$watcher" 2>/dev/null || true
wait "$watcher" 2>/dev/null || true
trap - EXIT
mv "runs/a1/$name-memory.csv" "runs/$name/memory-watch.csv"
mv "runs/a1/$name-idle-metrics.txt" "runs/$name/idle-metrics.txt"
mv "runs/a1/$name-properties.txt" "runs/$name/unit-properties.txt"
{
  date --iso-8601=seconds
  systemctl show -p ActiveState,Result,ExecMainStatus lab3-a
  journalctl -u lab3-a --since "$started" --no-pager
  journalctl -k --since "$started" --no-pager | grep -Ei 'oom|killed process' || true
  dmesg --ctime | grep -E 'oom-kill.*lab3-a|Memory cgroup out of memory: Killed process' | tail -n 4 || true
} > "runs/$name/exit-evidence.txt"
scripts/table.sh "runs/$name" ok_rps error_rate p99_ms inflight_avg \
  report_inflight_max rss_max_mb heap_max_mb mem_current_max_mb mem_limit_mb
cat "runs/$name/exit-evidence.txt"
