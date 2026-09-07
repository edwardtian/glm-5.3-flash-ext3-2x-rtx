#!/usr/bin/env python3
"""Bind focused grammar results to one live image and audit server failures."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    root = args.results
    before = json.loads((root / 'environment-before.json').read_text())
    after = json.loads((root / 'environment-after.json').read_text())
    reports = {name: json.loads((root / name).read_text())
               for name in ('json-reasoning.json', 'grammar-canary.json')}
    inspect = json.loads(command('docker', 'inspect', args.container))[0]
    lines = command('docker', 'logs', '--timestamps', args.container).splitlines()
    markers = [i for i, line in enumerate(lines)
               if 'GLM release startup warmup complete; container is ready.' in line]
    assert len(markers) == 1, 'expected exactly one ready marker'
    post_ready = lines[markers[0] + 1:]
    counts = {
        'fsm_failures': sum('Failed to advance FSM' in line for line in lines),
        'tracebacks': sum('Traceback (most recent call last)' in line for line in lines),
        'error_lines': sum(bool(re.search(r'\bERROR\b', line)) for line in lines),
        'post_ready_http_errors': sum(bool(re.search(r'HTTP/1\.[01]" [45]\d\d', line)) for line in post_ready),
        'post_ready_jit': sum('JIT compilation during inference:' in line for line in post_ready),
    }
    checks = {
        'same_container': before['container']['id'] == after['container']['id'] == inspect['Id'],
        'same_image': before['container']['image_id'] == after['container']['image_id'] == inspect['Image'],
        'same_model_artifact': before['model_artifact'] == after['model_artifact'],
        'same_command': before['container']['command'] == after['container']['command'] == inspect['Config']['Cmd'],
        'same_environment': before['container']['environment'] == after['container']['environment'],
        'healthy_before_after': before['api_health_status'] == after['api_health_status'] == 200,
        'still_running': inspect['State']['Running'],
        '400w_per_gpu': all(gpu['power.limit'] == 400 for env in (before, after) for gpu in env['gpus']),
        'json_suite': reports['json-reasoning.json']['status'] == 'PASS'
            and reports['json-reasoning.json']['completed_cases'] == 29,
        'canary': reports['grammar-canary.json']['status'] == 'PASS'
            and reports['grammar-canary.json']['completed_cases'] == 145,
        'speculation_used': reports['json-reasoning.json']['speculation_active'],
        'no_server_failures': all(counts[name] == 0 for name in
            ('fsm_failures', 'tracebacks', 'error_lines', 'post_ready_http_errors')),
    }
    report = {
        'schema_version': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
        'status': 'PASS' if all(checks.values()) else 'FAIL', 'checks': checks,
        'container_id': inspect['Id'], 'image_id': inspect['Image'],
        'image_reference': inspect['Config']['Image'],
        'source_revision': inspect['Config']['Labels']['org.opencontainers.image.revision'],
        'server_log_sha256': hashlib.sha256(('\n'.join(lines) + '\n').encode()).hexdigest(),
        'counts': counts,
        'receipt_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ('environment-before.json', 'environment-after.json', *reports)},
        'scope': 'Focused structured-output qualification only; historical benchmark numbers are not refreshed',
    }
    (root / 'qualification.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
