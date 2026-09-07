# v0.7.1 — grammars know when to stop

Focused qualification on 2026-09-07, using the two local RTX PRO 6000 96 GB
GPUs at **400 W each**. No performance, needle, vision, or full tool-eval reruns.
Existing benchmark numbers remain attributed to their original releases.

Runtime: K3.25 `0490d2f…`, DFlash2 `bf582e4…` K5, FP8 MLA, TP2/EP2/DCP2,
C16 scheduler, 1,048,576 max length, vision enabled, baseline rollback.
Image `ghcr.io/tpurtell/glm-5.3-flash-exl3-4bpw-2x-rtx:v0.7.1`:
`sha256:fc6615ac0386dd03f3ef6380fba01489f48f7619b064157d5b95431989fd9ddc`.
Embedded image source: `9c35641652670bd216a4ad29495c3edd41f0828c`.
All 81 v0.7.0 filesystem layers are unchanged; five patch/test layers are added.

## Results

| Check | Result |
|---|---:|
| Installed-method CPU regressions | 14/14 pass |
| Negative control: original v0.7.0 | 5 expected failures: 3 termination, 2 reasoning-window |
| Existing grammar/tool canary, C8 | 145/145 pass |
| Complete JSON suite | 29/29 pass |
| Additional exact upstream 500-token requests, final run | 3/3 complete valid JSON |
| FSM errors / tracebacks / server ERROR lines / HTTP errors | 0 / 0 / 0 / 0 |
| Post-ready kernel compilations | 0 |
| API health before/after | 200 / 200 |

The 145-case canary comprises 20 sequential strict calls, 100 concurrent
required/named strict calls at C8, five ignore-EOS transport/termination probes,
10 ordinary tool calls, and 10 plain-chat controls. Strict calls check complete
output and exact argument values. Its five ignore-EOS probes **do not claim
complete JSON output**.

The 29 complete-JSON checks comprise three upstream-prompt requests with a
2,048-token budget and default thinking, 16 concurrent requests alternating
thinking on/off at C16, five normal strict-schema responses, and five
ignore-EOS strict-schema responses with an explicit `}` stop and
`include_stop_str_in_output=true`. Every one requires parseable JSON, expected
schema/values, and `finish_reason=stop`.

DFlash was not bypassed: during the final JSON suite, counters increased by
1,598 draft steps, 7,868 draft tokens, and 4,803 accepted tokens. These are
activity checks, not a throughput benchmark or workload-general acceptance rate.

## Initial probe findings — retained, not erased

The [initial probe](initial-probe/json-reasoning.json) used an overly strict
test contract in two places. No runtime code changed between it and the final
qualification:

- The verbatim upstream reproduction allows only 500 output tokens, including
  thinking. Two of its first three responses exhausted that budget and returned
  partial JSON with `finish_reason=length`; the third completed. All three
  completed on the final repeat. The final script retains these verbatim runs
  as separate diagnostics and also tests the same prompt with sufficient budget.
  A truncated response is never counted as a complete-JSON pass.
- Five `ignore_eos=true` requests without an explicit stop produced valid JSON
  followed by additional text until the token limit. Requiring `stop` and a
  single complete JSON body from this forced-continuation request was incorrect.
  The termination backport prevents crashes, not continued generation requested
  by that flag. For complete responses, keep the normal EOS behavior or supply
  an explicit stop. The final suite checks both supported patterns.

The original probe script is preserved beside its results. Zero FSM/server
errors were observed over **all 201 post-ready requests**, including that first
probe. This does not claim every initial response was complete or valid JSON.

## Reproduce the focused checks

Start the v0.7.1 recipe normally and wait for its release-ready marker. Then:

```bash
python3 scripts/verify-structured-output-live.py --output /tmp/glm-json.json
python3 scripts/verify-issue136-xgrammar-live.py \
  --base-url http://127.0.0.1:8001/v1 \
  --model wrldsuksgo2mars/GLM-5.3-Flash-EXL3-K3.25-v1 \
  --concurrency 8 --output /tmp/glm-grammar.json
```

The image build itself runs both CPU suites. They can also be invoked inside
the image at `/opt/glm53-tests/test-xgrammar-termination.py` and
`/opt/glm53-tests/test-structured-output-reasoning.py`, without GPUs.

[Qualification audit](qualification.json), [JSON results](json-reasoning.json),
[canary results](grammar-canary.json), [JIT audit](jit-audit.json), and the
before/after environment receipts bind the results to the tested image,
unchanged model metadata, launch command, and 400 W power limits.
