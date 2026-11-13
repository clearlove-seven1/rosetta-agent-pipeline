#!/usr/bin/env bash
# 恢复所有 interrupted 状态的 workbench run（4 路并发）
# 用法：bash scripts/resume_interrupted.sh [目录过滤关键词，如 2hbb]
set -u
cd "$(dirname "$0")/.."

PY=~/.conda/envs/rosetta-agent/bin/python
FILTER="${1:-}"
EXCLUDE="${EXCLUDE:-2jie}"   # 默认排除 2JIE（relax >30min 必超时，恢复无意义）
LIST=$(mktemp)

for d in runs/*/; do
    [ -f "$d/manifest.json" ] || continue
    if [ -n "$FILTER" ] && [[ "$d" != *"$FILTER"* ]]; then continue; fi
    if [[ "${d,,}" == *"2jie"* && "$EXCLUDE" == *2jie* ]]; then continue; fi
    st=$("$PY" -c "import json;print(json.load(open('$d/manifest.json'))['status'])" 2>/dev/null || echo "?")
    [ "$st" == "interrupted" ] && echo "${d%/}"
done > "$LIST"

echo "待恢复: $(wc -l < "$LIST") 个"
if [ "$(wc -l < "$LIST")" -eq 0 ]; then rm -f "$LIST"; exit 0; fi

export PY
xargs -P 4 -L 1 bash -c '"$PY" -m workbench.cli --resume "$1" >/dev/null 2>&1 || echo "RESUME_FAIL $1"' _ < "$LIST"

echo "恢复完成；仍失败/中断的:"
"$PY" - <<EOF
import json, glob
for f in glob.glob("runs/*/manifest.json"):
    m = json.load(open(f))
    if m.get("status") in ("interrupted", "failed", "running"):
        print(" ", m["run_id"], "->", m["status"])
EOF
rm -f "$LIST"
