#!/usr/bin/env bash
# Run from /root/lab3 with Prometheus/node_exporter already running.
# Refuses to replace an existing service or result directory.
set -euo pipefail
name=${1:?usage: bash scripts/run-o1.sh NAME default_or_170MiB}
limit=${2:?}
[[ $name =~ ^o1-[A-Za-z0-9._-]+$ ]]
[[ $limit == default || $limit == 170MiB ]]
test ! -e "runs/$name"
if systemctl is-active --quiet lab3-a; then
  echo 'lab3-a is already active; stop the previous experiment first' >&2
  exit 1
fi
curl -fsS http://127.0.0.1:9090/-/ready >/dev/null
systemctl reset-failed lab3-a 2>/dev/null || true
scratch=$(mktemp -d "$PWD/data/o1.XXXXXX")
watchers=()
cleanup() {
  for pid in "${watchers[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  systemctl stop lab3-a 2>/dev/null || true
  systemctl reset-failed lab3-a 2>/dev/null || true
}
trap cleanup EXIT
started=$(date --iso-8601=seconds)
env_args=(--setenv=GOGC=100)
if [[ $limit == 170MiB ]]; then
  env_args+=(--setenv=GOMEMLIMIT=170MiB)
else
  # Explicitly use Go's default unlimited setting, independent of manager env.
  env_args+=(--setenv=GOMEMLIMIT=off)
fi
systemd-run --unit=lab3-a -p MemoryMax=200M -p MemorySwapMax=0 \
  -p Restart=no "${env_args[@]}" \
  "$PWD/bin/enrolld" -addr 127.0.0.1:8081 -data "$PWD/data/a" -report-max 0
sleep 3
curl -fsS http://127.0.0.1:8081/metrics > "$scratch/idle-metrics.txt"
systemctl show lab3-a -p MainPID,CPUQuotaPerSecUSec,MemoryMax,MemorySwapMax,Environment,Restart,AllowedCPUs \
  > "$scratch/unit-properties.txt"
{
  date --iso-8601=seconds
  git rev-parse HEAD
  sha256sum bin/enrolld bin/loadgen bin/promsteps
  free -m
  swapon --show
} > "$scratch/environment.txt"
python3 taskA_steps/watch-memory.py "$scratch/memory-watch.csv" & watchers+=($!)
python3 scripts/watch-o1.py "$scratch/metrics-watch.jsonl" & watchers+=($!)
GEN_CPUS=0-1 DATA=data/a bash scripts/run.sh "$name" \
  -url 'http://127.0.0.1:8081/api/report?mb=8&ms=500' \
  -rates 10,20,30 -step 20s -seed 1 -stop-below 0 \
  > "$scratch/console.log" 2>&1
for pid in "${watchers[@]}"; do kill "$pid" 2>/dev/null || true; done
wait 2>/dev/null || true
watchers=()
mv "$scratch/"* "runs/$name/"
rmdir "$scratch"
{
  date --iso-8601=seconds
  systemctl show lab3-a -p ActiveState,Result,ExecMainStatus,CPUUsageNSec,MemoryPeak,NRestarts
  journalctl -u lab3-a --since "$started" --no-pager
  journalctl -k --since "$started" --no-pager | grep -E 'oom-kill.*lab3-a|Memory cgroup out of memory: Killed process' || true
} > "runs/$name/exit-evidence.txt"
cleanup
trap - EXIT
systemctl list-units --all lab3-a.service --no-pager > "runs/$name/cleanup.txt"
echo "Completed runs/$name"
