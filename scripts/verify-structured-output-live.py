#!/usr/bin/env python3
"""Focused recipe #2 / vLLM #53777 JSON and reasoning-boundary qualification.

Stores synthetic test requests and final responses, not credentials or private
prompts. Tests the exact upstream trigger plus concurrent thinking on/off and
complete strict JSON with ignore_eos. Run with DFlash2 enabled on the server.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import time
import urllib.request

PROMPT = ('Output a JSON config object for a CI job with keys "name" (string), '
          '"region" (string, one of us-east-1 or eu-west-1), "retries" (integer 1-3), '
          'and "env" (object with a KEY=VALUE pair). No prose, JSON only.')


def check_ci(value):
    assert isinstance(value, dict) and set(value) == {'name', 'region', 'retries', 'env'}, 'CI keys'
    assert isinstance(value['name'], str) and value['name'], 'CI name'
    assert value['region'] in ('us-east-1', 'eu-west-1'), 'CI region'
    assert type(value['retries']) is int and 1 <= value['retries'] <= 3, 'CI retries'
    assert isinstance(value['env'], dict) and value['env'], 'CI env'
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in value['env'].items()), 'CI env types'


def make_case(model, name, thinking=None, ignore_eos=False):
    payload = {'model': model, 'messages': [{'role': 'user', 'content': PROMPT}],
               'response_format': {'type': 'json_object'}, 'max_tokens': 500,
               'temperature': 0, 'stream': False}
    if thinking is not None:
        payload['chat_template_kwargs'] = {'enable_thinking': thinking}
        payload['max_tokens'] = 2048
    if ignore_eos:
        payload['messages'][0]['content'] = 'Return exactly this JSON object: {"ok":true,"count":7}'
        payload['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'termination_probe', 'strict': True, 'schema': {
                'type': 'object', 'properties': {'ok': {'type': 'boolean'}, 'count': {'type': 'integer'}},
                'required': ['ok', 'count'], 'additionalProperties': False}}}
        payload['ignore_eos'] = True
        payload['max_tokens'] = 256
    return name, payload, ignore_eos


def run_case(base_url, case):
    name, payload, ignore_eos = case
    started = time.monotonic()
    record = {'name': name, 'request': payload, 'ok': False}
    try:
        request = urllib.request.Request(base_url.rstrip('/') + '/chat/completions',
                    data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=180) as response:
            record['http_status'] = response.status
            result = json.load(response)
        choices = result.get('choices')
        assert isinstance(choices, list) and len(choices) == 1, 'choice cardinality'
        choice = choices[0]
        message = choice['message']
        record['finish_reason'] = choice.get('finish_reason')
        record['content'] = message.get('content')
        record['usage'] = result.get('usage')
        record['reasoning_characters'] = len(message.get('reasoning') or message.get('reasoning_content') or '')
        assert choice.get('finish_reason') == 'stop', 'incomplete finish state'
        assert not message.get('tool_calls'), 'unexpected tool call'
        value = json.loads(message['content'])
        if ignore_eos:
            assert value == {'ok': True, 'count': 7}, 'strict JSON values'
            assert type(value['ok']) is bool and type(value['count']) is int, 'strict JSON types'
        else:
            check_ci(value)
        record['ok'] = True
    except Exception as error:
        record['error'] = f'{type(error).__name__}: {error}'
    record['elapsed_seconds'] = round(time.monotonic() - started, 3)
    print(f'{name}: {"PASS" if record["ok"] else "FAIL"}', flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8001/v1')
    parser.add_argument('--model', default='wrldsuksgo2mars/GLM-5.3-Flash-EXL3-K3.25-v1')
    parser.add_argument('--concurrency', type=int, default=16)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error('concurrency must be positive')
    started = datetime.now(timezone.utc).isoformat()
    results = [run_case(args.base_url, make_case(args.model, f'exact-upstream-{i}')) for i in range(3)]
    cases = [make_case(args.model, f'concurrent-thinking-{i}', thinking=bool(i % 2)) for i in range(16)]
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results.extend(pool.map(lambda case: run_case(args.base_url, case), cases))
    results.extend(run_case(args.base_url, make_case(args.model, f'ignore-eos-complete-{i}',
                   thinking=False, ignore_eos=True)) for i in range(5))
    report = {'schema_version': 1, 'started_at': started,
              'finished_at': datetime.now(timezone.utc).isoformat(),
              'model': args.model, 'concurrency': args.concurrency,
              'expected_cases': 24, 'completed_cases': len(results),
              'failed_cases': sum(not item['ok'] for item in results), 'results': results,
              'scope': 'JSON parsing, schema/value checks, and stop finish state; server log audit is separate'}
    report['status'] = 'PASS' if report['failed_cases'] == 0 and len(results) == 24 else 'FAIL'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
