#!/usr/bin/env bash
# O3 写入压测：运行共同负载曲线，并保存磁盘时序与实例末次指标。
# 用法（在仓库根目录、lbd/Prometheus/scaler 就绪后）：scripts/run_o3.sh <新运行名>
# 可选环境变量：GEN_CPUS、SLO、O3_BODY、O3_PLAN、O3_DISK。
set -euo pipefail

name=${1:-}
if [ "$#" -ne 1 ] || [[ ! $name =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo '用法：scripts/run_o3.sh <新运行名（字母、数字、点、下划线或连字符）>' >&2
  exit 2
fi

[ -x scripts/run3.sh ] && [ -x scripts/plan.sh ] || {
  echo '请在仓库根目录运行，并确认 scripts/run3.sh 与 scripts/plan.sh 可执行' >&2
  exit 1
}
command -v iostat >/dev/null || { echo '缺少 iostat（sysstat）' >&2; exit 1; }
command -v curl >/dev/null || { echo '缺少 curl' >&2; exit 1; }
[ ! -e "runs/$name" ] || { echo "runs/$name 已存在，请换一个名字" >&2; exit 1; }

export GEN_CPUS="${GEN_CPUS:-0}"
export SLO="${SLO:-0.2}"
O3_BODY=${O3_BODY:-'{"student":"s{{seq}}","course":"c1"}'}
O3_PLAN=${O3_PLAN:-$(scripts/plan.sh 150 1500)}
O3_DISK=${O3_DISK:-vda}
echo "O3：$name；负载曲线 $O3_PLAN；磁盘 $O3_DISK"

io_file=$(mktemp /tmp/o3-iostat.XXXXXX)
iostat -d -x -t "$O3_DISK" 1 >"$io_file" &
io_pid=$!

cleanup() {
  local rc=$? p
  trap - EXIT
  set +e
  kill "$io_pid" 2>/dev/null
  wait "$io_pid" 2>/dev/null
  if [ -d "runs/$name" ]; then
    cp "$io_file" "runs/$name/iostat.log" || rc=1
    for p in 8101 8102 8103 8104; do
      curl -fsS -m 3 "http://127.0.0.1:$p/metrics" \
        >"runs/$name/inst-$p-metrics-end.txt" || \
        rm -f "runs/$name/inst-$p-metrics-end.txt"
    done
  fi
  rm -f "$io_file"
  exit "$rc"
}
trap cleanup EXIT

scripts/run3.sh "$name" \
  -url http://127.0.0.1:8080/api/enroll \
  -method POST -body "$O3_BODY" -plan "$O3_PLAN"
