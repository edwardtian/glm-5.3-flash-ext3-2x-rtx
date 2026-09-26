#!/usr/bin/env bash
# Persist the complete pinned 88-case tool-eval-bench suite (including all 19
# Hard Mode cases) into an existing qualification result directory.
#
# Mirrors the v0.7.0 GLM tool-eval invocation (benchmarks/v0.7.0-k325/
# tool-eval-20260906/RUN.md): C8 (--parallel 8), temperature 0, 900 s timeout,
# 8 turns, no injected errors, fixed reference date, and the chat template's
# default thinking (on, max effort; no backend kwargs).
#
# Evaluator: either TOOL_EVAL_DIR (git checkout at the pinned commit with a
# .venv) or TOOL_EVAL_BENCH (default: tool-eval-bench on PATH, e.g. a uv tool
# install from the pinned commit; its --version must carry the pinned hash).
# Env: RESULT_DIR (existing), MODEL (default: the single id in /v1/models),
# BASE_URL, REFERENCE_DATE (2026-09-04), PARALLEL (8),
# TOOL_EVAL_BACKEND_KWARGS (optional JSON, e.g. to force a thinking mode).
set -euo pipefail
EXPECTED=cf54b4bfe705f12f71e8866f10730572497c8105
if [[ -n "${TOOL_EVAL_DIR:-}" ]]; then
  [[ $(git -C "$TOOL_EVAL_DIR" rev-parse HEAD) == "$EXPECTED" ]] || { echo "Expected tool-eval-bench $EXPECTED in $TOOL_EVAL_DIR" >&2; exit 1; }
  TOOL_EVAL_BENCH="$(realpath -e -- "$TOOL_EVAL_DIR/.venv/bin/tool-eval-bench")"
else
  TOOL_EVAL_BENCH="${TOOL_EVAL_BENCH:-tool-eval-bench}"
  version="$("$TOOL_EVAL_BENCH" --version 2>&1)"
  [[ "$version" == *"+g${EXPECTED:0:9}"* ]] || { echo "Expected tool-eval-bench built from ${EXPECTED:0:9}; got: $version" >&2; exit 1; }
fi
RESULT_DIR="$(realpath -e -- "${RESULT_DIR:?Set existing qualification result directory}")"
[[ ! -e "$RESULT_DIR/tools.json" ]] || { echo "Tools result already exists" >&2; exit 1; }
BASE_URL="${BASE_URL:-http://127.0.0.1:8001}"
BASE_URL="${BASE_URL%/}"
BASE_URL="${BASE_URL%/v1}"
if [[ -z "${MODEL:-}" ]]; then
  MODEL="$(python3 - "$BASE_URL" <<'PY'
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1] + "/v1/models", timeout=30) as response:
    ids = [item["id"] for item in json.load(response)["data"]]
if len(ids) != 1:
    sys.exit(f"Set MODEL; server lists {ids}")
print(ids[0])
PY
)"
fi
EXTRA=()
[[ -n "${TOOL_EVAL_BACKEND_KWARGS:-}" ]] && EXTRA+=(--backend-kwargs "$TOOL_EVAL_BACKEND_KWARGS")
"$TOOL_EVAL_BENCH" --version >"$RESULT_DIR/tools-version.txt" 2>&1
"$TOOL_EVAL_BENCH" run --hardmode \
  --model "$MODEL" \
  --backend vllm --base-url "$BASE_URL/v1/" \
  --temperature 0 --timeout 900 --max-turns 8 --parallel "${PARALLEL:-8}" \
  --error-rate 0 --reference-date "${REFERENCE_DATE:-2026-09-04}" \
  "${EXTRA[@]}" \
  --json-file "$RESULT_DIR/tools.json" --output-dir "$RESULT_DIR/tools-runs" \
  --no-live --redact-url
python3 - "$RESULT_DIR" <<'PY'
import json, shutil, sys
from pathlib import Path
root = Path(sys.argv[1])
result = json.loads((root / "tools.json").read_text())
assert result["status"] == "completed" and result["total_scenarios"] == 88, (result["status"], result["total_scenarios"])
shutil.copyfile(result["report_path"], root / "tools.md")
scores = result["scores"]
hard = next(c for c in scores["category_scores"] if c["label"] == "Hard Mode")
print(json.dumps({"points": f"{scores['total_points']}/{scores['max_points']}",
                  "hard_mode": f"{hard['earned']}/{hard['max']}",
                  "final_score": result["final_score"]}))
PY
