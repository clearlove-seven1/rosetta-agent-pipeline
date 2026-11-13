#!/usr/bin/env bash
# v0.4 排查·扩样本批量：对 select_expansion.py 选出的突变批量跑 workbench CLI
# 用法：bash scripts/batch_expand.sh runs/expansion_selection.tsv [并发数]
set -u
cd "$(dirname "$0")/.."

SELECTION="${1:-runs/expansion_selection.tsv}"
PAR="${2:-4}"
PY=~/.conda/envs/rosetta-agent/bin/python
FAILS=$(mktemp)

run_one() {
    local pdb chain pos wt mut exp
    read -r pdb chain pos wt mut exp <<< "$1"
    "$PY" -m workbench.cli \
        --protein "examples/${pdb,,}/${pdb,,}.pdb" \
        --chain "$chain" --resnum "$pos" --to "$mut" --wt "$wt" \
        --name "s669_${pdb}_${chain}${pos}${mut}" \
        --note "S669 扩样本(实验ddG=${exp})" >/dev/null 2>&1 \
        || echo "FAIL ${pdb}_${chain}${pos}${wt}->${mut}" >> "$FAILS"
}
export -f run_one
export PY FAILS

# 结构文件就位（选择脚本已缓存到 /tmp/s669_pdbs，复制入 examples/）
tail -n +2 "$SELECTION" | cut -f1 | sort -u | while read -r pdb; do
    low="${pdb,,}"
    mkdir -p "examples/$low"
    if [ ! -f "examples/$low/$low.pdb" ] && [ -f "/tmp/s669_pdbs/$pdb.pdb" ]; then
        cp "/tmp/s669_pdbs/$pdb.pdb" "examples/$low/$low.pdb"
    fi
done

echo "待跑: $(tail -n +2 "$SELECTION" | wc -l) 条"
# -d '\n'：整行作为一个参数传给 run_one（默认会按空白拆参导致 read 只拿到第一列）
tail -n +2 "$SELECTION" | xargs -d '\n' -P "$PAR" -L 1 bash -c 'run_one "$@"' _

echo "BATCH_DONE 失败数: $(wc -l < "$FAILS")"
rm -f "$FAILS"
