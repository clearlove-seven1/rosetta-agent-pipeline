#!/usr/bin/env bash
# S669 基准批量：对 2HBB 全部 46 个已收录突变跑 workbench 确定性 CLI（4 路并发）
# 用法：bash scripts/batch_s669_2hbb.sh   （在仓库根目录）
set -u
cd "$(dirname "$0")/.."

PY=~/.conda/envs/rosetta-agent/bin/python
MUTS=$(mktemp)
FAILS=$(mktemp)

grep "^2HBB," rosetta_manuals/stability_benchmark_s669.csv \
  | awk -F, '{print $3, $4, $5}' | sort -u > "$MUTS"
echo "待跑突变数: $(wc -l < "$MUTS")"

run_one() {
    local pos="$1" wt="$2" mut="$3"
    "$PY" -m workbench.cli \
        --protein examples/2hbb/2hbb.pdb --chain A \
        --resnum "$pos" --to "$mut" --wt "$wt" \
        --name "s669_2hbb_A${pos}${mut}" \
        --note "S669 基准批量" >/dev/null 2>&1 \
        || echo "FAIL A:${pos}${wt}->${mut}" >> "$FAILS"
}
export -f run_one
export PY FAILS

xargs -P 4 -L 1 bash -c 'run_one "$@"' _ < "$MUTS"

echo "BATCH_DONE 失败数: $(wc -l < "$FAILS")"
echo "成功 run 数: $(ls runs/ | grep -c s669_2hbb)"
rm -f "$MUTS" "$FAILS"
