#!/usr/bin/env bash
# GLM-5.3 Flash serving qualification suite. Run on an otherwise idle endpoint:
# spec-decode counters and timings assume exclusive use of the server.
# Tool-eval-bench is the separate pinned suite in tool-quality.sh.
#
# Required:  RESULT_DIR (new directory), SPEC_TOKENS (tested draft length; 0 = none)
# Optional:  BASE_URL (http://127.0.0.1:8001), MODEL (default: the single id in
#            /v1/models), PART=all|core|reasoning|prefill|context|retrieval|coding|vision,
#            SPEC_METHOD (dflash2|mtp|none, informational), KV_CACHE_PROFILE (fp8|nvfp4),
#            VISION=0 (record a skipped vision receipt), CLIENTS ("1 2 4 8 16"),
#            REASONING_CONCURRENCY ("1 2 4"), REASONING_MAX_TOKENS (2048),
#            PREFILL_TOKENS, CONTEXT_DEPTHS, MAX_MODEL_LEN (1048576), BOUNDARY_SLACK (32),
#            RETRIEVAL_FILLER, RETRIEVAL_POSITIONS, MULTI_NEEDLE=0|1, MULTI_NEEDLE_TOKENS,
#            CODING_DEPTHS, CONTAINER (capture runtime.json read-only via docker inspect/logs),
#            TELEMETRY=0|1 (nvidia-smi boundary snapshots per step; default 1 when available).
# Lists larger than MAX_MODEL_LEN allows are skipped and recorded in skipped.txt.
# Every step runs even if an earlier one fails; the exit status is 1 if any failed.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RECIPE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

RESULT_DIR="${RESULT_DIR:?Set a new result directory}"
if [[ -e "$RESULT_DIR" ]]; then
  echo "Refusing to overwrite existing results: $RESULT_DIR" >&2
  exit 1
fi
SPEC_TOKENS="${SPEC_TOKENS:?Set the tested speculative draft length (0 when speculation is off)}"
SPEC_METHOD="${SPEC_METHOD:-dflash2}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8001}"
BASE_URL="${BASE_URL%/}"
BASE_URL="${BASE_URL%/v1}"
PART="${PART:-all}"
case "$PART" in all|core|reasoning|prefill|context|retrieval|coding|vision) ;; *) echo "Invalid PART=$PART" >&2; exit 2 ;; esac
KV_CACHE_PROFILE="${KV_CACHE_PROFILE:-fp8}"
case "$KV_CACHE_PROFILE" in fp8) KV_CACHE_DTYPE=fp8_ds_mla ;; nvfp4) KV_CACHE_DTYPE=nvfp4_ds_mla ;; *) echo "Invalid KV_CACHE_PROFILE=$KV_CACHE_PROFILE" >&2; exit 2 ;; esac
VISION="${VISION:-1}"
CLIENTS="${CLIENTS:-1 2 4 8 16}"
REASONING_CONCURRENCY="${REASONING_CONCURRENCY:-1 2 4}"
REASONING_MAX_TOKENS="${REASONING_MAX_TOKENS:-2048}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-1048576}"
BOUNDARY_SLACK="${BOUNDARY_SLACK:-32}"
PREFILL_TOKENS="${PREFILL_TOKENS:-2048 8192 32768 65536 131072 262144 524288}"
CONTEXT_DEPTHS="${CONTEXT_DEPTHS:-2048 8192 32768 131072 262144 524288}"
RETRIEVAL_FILLER="${RETRIEVAL_FILLER:-8192 240000 1000000}"
RETRIEVAL_POSITIONS="${RETRIEVAL_POSITIONS:-0.05 0.5 0.95}"
MULTI_NEEDLE="${MULTI_NEEDLE:-1}"
MULTI_NEEDLE_TOKENS="${MULTI_NEEDLE_TOKENS:-$(( MAX_MODEL_LEN - 1024 < 1000000 ? MAX_MODEL_LEN - 1024 : 1000000 ))}"
CODING_DEPTHS="${CODING_DEPTHS:-0 8192 32768 65536 128000}"
CONTAINER="${CONTAINER:-}"
if [[ -z "${TELEMETRY:-}" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1; then TELEMETRY=1; else TELEMETRY=0; fi
fi

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

mkdir -p "$RESULT_DIR/logs"
RESULT_DIR="$(realpath -e -- "$RESULT_DIR")"
[[ "$TELEMETRY" == 1 ]] && mkdir -p "$RESULT_DIR/telemetry"
COMMON=(--base-url "$BASE_URL" --model "$MODEL")
SPEC_ARGS=()
REASONING_SPEC_ARGS=()
if [[ "$SPEC_TOKENS" != 0 ]]; then
  SPEC_ARGS+=(--collect-spec-metrics)
else
  REASONING_SPEC_ARGS+=(--no-spec-metrics)
fi
FAILED=()

export RECIPE_DIR BASE_URL MODEL PART SPEC_METHOD SPEC_TOKENS KV_CACHE_PROFILE VISION MAX_MODEL_LEN \
  BOUNDARY_SLACK CLIENTS REASONING_CONCURRENCY REASONING_MAX_TOKENS PREFILL_TOKENS CONTEXT_DEPTHS \
  RETRIEVAL_FILLER RETRIEVAL_POSITIONS MULTI_NEEDLE MULTI_NEEDLE_TOKENS CODING_DEPTHS CONTAINER TELEMETRY
python3 - "$RESULT_DIR/suite.json" <<'PY'
import datetime, json, os, subprocess, sys
e = os.environ
git = lambda *a: subprocess.run(["git", "-C", e["RECIPE_DIR"], *a], capture_output=True, text=True).stdout.strip()
config = {
    "schema": "glm53-benchmark-suite-v1",
    "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "recipe_revision": git("rev-parse", "HEAD"), "recipe_branch": git("branch", "--show-current"),
    "recipe_dirty": git("status", "--porcelain") != "",
    "base_url": e["BASE_URL"], "model": e["MODEL"], "part": e["PART"],
    "spec_method": e["SPEC_METHOD"], "spec_tokens": int(e["SPEC_TOKENS"]),
    "kv_cache_profile": e["KV_CACHE_PROFILE"], "vision": e["VISION"] == "1",
    "max_model_len": int(e["MAX_MODEL_LEN"]), "boundary_slack": int(e["BOUNDARY_SLACK"]),
    "clients": [int(v) for v in e["CLIENTS"].split()],
    "reasoning_concurrency": [int(v) for v in e["REASONING_CONCURRENCY"].split()],
    "reasoning_max_tokens": int(e["REASONING_MAX_TOKENS"]),
    "prefill_tokens": [int(v) for v in e["PREFILL_TOKENS"].split()],
    "context_depths": [int(v) for v in e["CONTEXT_DEPTHS"].split()],
    "retrieval_filler": [int(v) for v in e["RETRIEVAL_FILLER"].split()],
    "retrieval_positions": [float(v) for v in e["RETRIEVAL_POSITIONS"].split()],
    "multi_needle": e["MULTI_NEEDLE"] == "1", "multi_needle_tokens": int(e["MULTI_NEEDLE_TOKENS"]),
    "coding_depths": [int(v) for v in e["CODING_DEPTHS"].split()],
    "container": e["CONTAINER"] or None, "telemetry": e["TELEMETRY"] == "1",
}
with open(sys.argv[1], "x") as output:
    json.dump(config, output, indent=2)
    output.write("\n")
PY

# step NAME COMMAND... : log to logs/NAME.log, record status in steps.jsonl, never abort.
step() {
  local name="$1"; shift
  local started ended status
  local wrapper=()
  [[ "$TELEMETRY" == 1 ]] && wrapper=(python3 "$SCRIPT_DIR/run-with-gpu-telemetry.py" --output "$RESULT_DIR/telemetry/$name.json" --)
  echo "== $name" >&2
  started="$(date -u +%FT%TZ)"
  set +e
  "${wrapper[@]}" "$@" 2>&1 | tee "$RESULT_DIR/logs/$name.log"
  status=${PIPESTATUS[0]}
  set -e
  ended="$(date -u +%FT%TZ)"
  python3 -c 'import json, sys; print(json.dumps({"name": sys.argv[1], "status": int(sys.argv[2]), "started": sys.argv[3], "ended": sys.argv[4], "command": sys.argv[5:]}))' \
    "$name" "$status" "$started" "$ended" "$@" >>"$RESULT_DIR/steps.jsonl"
  if [[ "$status" != 0 ]]; then
    echo "!! $name failed with status $status; continuing" >&2
    FAILED+=("$name")
  fi
}

# fit RESERVE VALUES... : print values whose value + RESERVE fits MAX_MODEL_LEN.
fit() {
  local reserve="$1"; shift
  local kept=() value
  for value in "$@"; do
    if (( ${value%.*} + reserve <= MAX_MODEL_LEN )); then
      kept+=("$value")
    else
      echo "skipped $value (+$reserve reserved) > MAX_MODEL_LEN=$MAX_MODEL_LEN" | tee -a "$RESULT_DIR/skipped.txt" >&2
    fi
  done
  echo "${kept[*]}"
}

wants() { [[ "$PART" == all || "$PART" == "$1" ]]; }

if [[ -n "$CONTAINER" ]]; then
  step runtime python3 "$SCRIPT_DIR/capture-runtime.py" --container "$CONTAINER" --base-url "$BASE_URL" --output "$RESULT_DIR/runtime.json"
else
  echo "CONTAINER unset: runtime.json not captured (run capture-runtime.py separately)" >&2
fi

if wants core; then
  step api-tools python3 "$SCRIPT_DIR/test-api-tool-constraints.py" "${COMMON[@]}" --output "$RESULT_DIR/api-tools.jsonl"
fi
if wants core || [[ "$PART" == vision ]]; then
  if [[ "$VISION" == 1 ]]; then
    step vision python3 "$SCRIPT_DIR/test-vision-vllm.py" "${COMMON[@]}" --output "$RESULT_DIR/vision.json"
  else
    printf '{"schema": "glm53-vllm-vision-limit.v1", "skipped": "vision disabled", "passed": null}\n' >"$RESULT_DIR/vision.json"
    echo "skipped: vision disabled" >&2
  fi
fi
if wants core; then
  step seven python3 "$SCRIPT_DIR/benchmark-workloads.py" "${COMMON[@]}" --suite seven --runs 3 --warmups 1 "${SPEC_ARGS[@]}" --output "$RESULT_DIR/seven.jsonl"
  step orchid python3 "$SCRIPT_DIR/benchmark-workloads.py" "${COMMON[@]}" --suite orchid --runs 5 --warmups 1 "${SPEC_ARGS[@]}" --output "$RESULT_DIR/orchid.jsonl"
  # shellcheck disable=SC2086
  step clients python3 "$SCRIPT_DIR/benchmark-decode.py" --base-url "$BASE_URL/v1" --model "$MODEL" \
    --profile "$KV_CACHE_PROFILE" --mtp-tokens "$SPEC_TOKENS" --mtp-policy "${MTP_POLICY:-static}" \
    --request-mode clients --thinking off --concurrency $CLIENTS --output-tokens 256 \
    --warmup-runs 2 --runs 3 "${SPEC_ARGS[@]}" --output "$RESULT_DIR/clients.json"
fi
if wants reasoning; then
  # shellcheck disable=SC2086
  step reasoning-coding python3 "$SCRIPT_DIR/benchmark-reasoning-coding.py" "${COMMON[@]}" \
    --concurrency $REASONING_CONCURRENCY --warmup-batches 1 --batches 3 \
    --max-tokens "$REASONING_MAX_TOKENS" "${REASONING_SPEC_ARGS[@]}" --output "$RESULT_DIR/reasoning-coding.json"
fi
if wants prefill; then
  read -r -a prefill_tokens <<<"$(fit 1 $PREFILL_TOKENS)"
  if (( ${#prefill_tokens[@]} )); then
    step prefill python3 "$SCRIPT_DIR/benchmark-prefill.py" --base-url "$BASE_URL/v1" --model "$MODEL" \
      --profile "$KV_CACHE_PROFILE" --prompt-tokens "${prefill_tokens[@]}" --runs 3 --output "$RESULT_DIR/prefill.json"
  fi
fi
if wants context; then
  read -r -a context_depths <<<"$(fit 256 $CONTEXT_DEPTHS)"
  if (( ${#context_depths[@]} )); then
    step context python3 "$SCRIPT_DIR/benchmark-context.py" "${COMMON[@]}" --depths "${context_depths[@]}" \
      --output-tokens 256 --runs 3 --warmups 1 "${SPEC_ARGS[@]}" --output "$RESULT_DIR/context.jsonl"
  fi
  step context-boundary python3 "$SCRIPT_DIR/benchmark-context.py" "${COMMON[@]}" \
    --depths $(( MAX_MODEL_LEN - 256 - BOUNDARY_SLACK )) --output-tokens 256 --runs 1 --warmups 0 \
    --output "$RESULT_DIR/context-boundary.jsonl"
fi
if wants retrieval; then
  read -r -a retrieval_filler <<<"$(fit 512 $RETRIEVAL_FILLER)"
  if (( ${#retrieval_filler[@]} )); then
    # shellcheck disable=SC2086
    step retrieval python3 "$SCRIPT_DIR/test-context-retrieval.py" "${COMMON[@]}" \
      --filler-tokens "${retrieval_filler[@]}" --positions $RETRIEVAL_POSITIONS --output "$RESULT_DIR/retrieval.jsonl"
  fi
  if [[ "$MULTI_NEEDLE" == 1 ]]; then
    step multi-needle python3 "$SCRIPT_DIR/test-multi-needle-vllm.py" "${COMMON[@]}" \
      --tokens "$MULTI_NEEDLE_TOKENS" --nonce "suite-$(date -u +%Y%m%dT%H%M%SZ)" --output "$RESULT_DIR/multi-needle.json"
  fi
fi
if wants coding; then
  read -r -a coding_depths <<<"$(fit 256 $CODING_DEPTHS)"
  if (( ${#coding_depths[@]} )); then
    step code-agent python3 "$SCRIPT_DIR/benchmark-code-agent-depth.py" "${COMMON[@]}" \
      --depths "${coding_depths[@]}" --dflash-tokens "$SPEC_TOKENS" --kv-cache "$KV_CACHE_DTYPE" \
      --output "$RESULT_DIR/code-agent.json"
  fi
fi

if (( ${#FAILED[@]} )); then
  echo "Failed steps: ${FAILED[*]}" >&2
  exit 1
fi
echo "Suite complete: $RESULT_DIR" >&2
