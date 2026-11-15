#!/usr/bin/env bash
# 端到端演示：与浏览器操作等价的 curl 流程
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "== 1. 上传原料声明 =="
curl -s -F "file=@${HERE}/samples/declarations.csv;type=text/csv" "$BASE/api/declarations"; echo

echo "== 2. 上传配方关系 =="
curl -s -F "file=@${HERE}/samples/relations.csv;type=text/csv" "$BASE/api/relations"; echo

echo "== 3. 查看成品标注（FG02 应为待核） =="
curl -s "$BASE/api/state" | python3 -m json.tool

echo "== 4. 冲突声明应被拒收 =="
curl -s -w " [HTTP %{http_code}]\n" -F "file=@${HERE}/samples/declarations_conflict.csv;type=text/csv" "$BASE/api/declarations"

echo "== 5. 成环配方应被拒收 =="
curl -s -w " [HTTP %{http_code}]\n" -F "file=@${HERE}/samples/relations_cycle.csv;type=text/csv" "$BASE/api/relations"

echo "== 6. 上传更正声明，待核应清零 =="
curl -s -F "file=@${HERE}/samples/declarations_corrected.csv;type=text/csv" "$BASE/api/declarations"; echo
curl -s "$BASE/api/state" | python3 -m json.tool
