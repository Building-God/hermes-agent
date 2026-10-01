"""Bounded native checks for explicitly declared original request outcomes.

These checks execute current effects. Worker prose and card state cannot supply
or replay the result. They are invoked by a different profile's owned review.
"""
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def _local_json(url):
    if urlparse(url).hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Outcome check must use the declared local service')
    with urllib.request.urlopen(url, timeout=12) as response:
        if response.status != 200:
            raise ValueError('Outcome service unavailable')
        return json.loads(response.read(1_000_000))


def check_queue(config):
    questions = _local_json(config['url'])['questions']
    if not isinstance(questions, list):
        raise ValueError('Question service returned an invalid queue')
    sessions = []
    agent_asks = []
    for question in questions:
        text = str(question.get('text') or '').lower()
        options = ' '.join(str(x) for x in question.get('options') or []).lower()
        first_sentence = text.split('.', 1)[0]
        if 'instagram' in first_sentence and any(x in first_sentence for x in ('cookie', 'login', 'session')):
            sessions.append(question['id'])
        if (question.get('id') in config.get('agent_owned_question_ids', [])
            or 'repair verification question' in text
            or ('queue' in text and 'verify' in text and 'please open' in text)
            or ('cycle' in text and ('how proceed' in text or 'harry tests' in options))):
            agent_asks.append(question['id'])
    failures = []
    if len(sessions) > 1:
        failures.append('Multiple questions still compete over the same Instagram session problem')
    if agent_asks:
        failures.append('Agent deployment, cycle or verification work is still assigned to Harry')
    return {'passed': not failures, 'failures': failures,
            'instagram_session_questions': sessions, 'agent_owned_questions': agent_asks,
            'observed_at': time.time(), 'url': config['url'],
            'scope': 'Actual queue intent and agent-owned demands; no count-based acceptance or Harry receipt'}


def check_conversation(config, *, dashboard=False):
    script = Path(__file__).with_name('kanban_outcome_probe.py')
    result = subprocess.run([config['python'], '-I', str(script)],
                            input=json.dumps(config), text=True, capture_output=True,
                            timeout=150, check=False)
    if result.returncode:
        raise ValueError('Native conversation probe failed to execute; agent repair required')
    report = json.loads(result.stdout.strip())
    if report.get('passed') is not True or report.get('actor') != 'operator-verification':
        return {**report, 'passed': False,
                'failures': ['Current primary client conversation/context probe failed']}
    if dashboard:
        health = _local_json(config['health_url'])
        runtime = health.get('hermes_client_runtime') or {}
        report['dashboard_runtime'] = runtime
        if (not runtime.get('pid') or runtime.get('source_sha256') != config['client_sha256']
            or runtime.get('credential_sha256') != config['ui_credential_sha256']):
            report.update(passed=False, failures=[
                'Actual dashboard consumer has not proved the current source and credential; '
                'use its safe idle restart and recheck, never ask Harry to set database flags'])
    return report


def verify_outcome(conn, task_id, run_id, config):
    from hermes_cli.kanban_operator import repair_contract
    original = task_id
    contract, _ = repair_contract(conn, task_id)
    if contract:
        original = contract['original_task_id']
    declared = (config.get('outcome_checks') or {}).get(original)
    if original not in config.get('cohort_task_ids', []) or not declared:
        return None
    started = time.time()
    try:
        if declared['kind'] == 'queue':
            report = check_queue(declared)
        elif declared['kind'] in ('conversation', 'dashboard'):
            report = check_conversation(declared, dashboard=declared['kind'] == 'dashboard')
        else:
            raise ValueError('Unknown declared outcome check; refuse prose-only acceptance')
    except Exception as error:
        report = {'passed': False, 'failures': [str(error)[:500]], 'error_type': type(error).__name__}
    report.update(original_task_id=original, review_run_id=run_id,
                  started_at=started, ended_at=time.time(), kind=declared['kind'],
                  human_receipt='unobserved; not inferred')
    return report
