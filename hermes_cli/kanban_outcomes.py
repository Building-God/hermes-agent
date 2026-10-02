"""Bounded native checks for explicitly declared original request outcomes.

These checks execute current effects. Worker prose and card state cannot supply
or replay the result. They are invoked by a different profile's owned review.
"""
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def declared_check(config, original):
    if original not in config.get('cohort_task_ids', []):
        return None
    explicit = (config.get('outcome_checks') or {}).get(original)
    manifest = Path(__file__).with_name('operator_outcomes.json')
    if not manifest.is_file():
        return explicit
    sealed = json.loads(manifest.read_text(encoding='utf8'))['checks'].get(original)
    if explicit:
        return {**explicit, **({'required_transport':sealed['required_transport']} if sealed and sealed.get('required_transport') else {})}
    return sealed


def outcome_signature(declared):
    return hashlib.sha256(json.dumps(declared,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def receipt_matches_declaration(report, declared):
    """Old successes cannot certify a changed interface or outcome contract."""
    if not report.get('passed') or report.get('kind')!=declared['kind']:return False
    required=declared.get('required_transport')
    if required and (not report.get('requests') or any(r.get('transport')!=required for r in report['requests'])):
        return False
    signature=report.get('outcome_check_sha256')
    if signature:return signature==outcome_signature(declared)
    # Compatibility is limited to an actual R17 primary HTTP drill against
    # precisely the current client; earlier direct-client probes are stale.
    if declared['kind'] not in ('conversation','dashboard') or not declared.get('primary_http_url'):return False
    requests=report.get('requests') or []
    return bool(report.get('actor')=='operator-verification'
                and report.get('router_sha256')==declared.get('client_sha256')
                and len(requests)>=6 and all(r.get('transport')=='primary-http' for r in requests))


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
    missing_choices = [task for task in config.get('preserve_pending_choice_tasks', [])
                       if not any(q.get('source_task') == task and q.get('options') for q in questions)]
    failures = []
    if missing_choices:
        failures.append('A known unresolved Harry-only billing or physical-device choice disappeared; preserve it or independently reconcile its genuine resolution, never treat an agent dismissal as Harry answering')
    if len(sessions) > 1:
        failures.append('Multiple questions still compete over the same Instagram session problem')
    if agent_asks:
        failures.append('Agent deployment, cycle or verification work is still assigned to Harry')
    return {'passed': not failures, 'failures': failures,
            'instagram_session_questions': sessions, 'agent_owned_questions': agent_asks,
            'missing_protected_choice_tasks': missing_choices,
            'observed_at': time.time(), 'url': config['url'],
            'scope': 'Actual queue intent and agent-owned demands; no count-based acceptance or Harry receipt'}


def check_conversation(config, *, dashboard=False):
    if config.get('required_transport')=='discord-front-door' and not config.get('discord_probe_url'):
        return {'passed':False,'actor':'operator-verification','requests':[],
                'failures':['The original request concerns Discord chat. A dashboard HTTP probe cannot certify that route; native Discord front-door verification remains agent-owned.']}
    script = Path(__file__).with_name('kanban_outcome_probe.py')
    runtime = Path(__file__).resolve().parents[1] / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not runtime.is_file():
        raise ValueError('Prepared native verifier runtime is missing; agent-owned safe-release repair required')
    child_env = dict(os.environ)
    child_env.pop('HERMES_A2A_URL', None)
    child_env.pop('HERMES_A2A_TOKEN', None)
    result = subprocess.run([str(runtime), '-I', str(script)],
                            input=json.dumps(config), text=True, capture_output=True,
                            timeout=150, check=False, env=child_env)
    if result.returncode:
        detail = next((line for line in reversed(result.stderr.splitlines()) if 'Error:' in line), 'child process failed')[:240]
        raise ValueError('Native conversation probe failed to execute in the prepared runtime: ' + detail)
    report = json.loads(result.stdout.strip())
    required=config.get('required_transport')
    if required and (not report.get('requests') or any(r.get('transport')!=required for r in report['requests'])):
        return {**report,'passed':False,'failures':['Native probe did not reproduce the declared original transport: '+required]}
    if report.get('passed') is not True or report.get('actor') != 'operator-verification':
        return {**report, 'passed': False,
                'failures': ['Current primary client conversation/context probe failed']}
    if dashboard:
        health = _local_json(config['health_url'])
        runtime = health.get('hermes_client_runtime') or {}
        report['dashboard_runtime'] = runtime
        expected_credential = config.get('ui_credential_sha256')
        if not expected_credential:
            from dotenv import dotenv_values
            token = dotenv_values(Path(config['client_path']).parent / '.env').get('HERMES_A2A_TOKEN')
            if not token:
                raise ValueError('Configured primary credential missing; agent repair required')
            expected_credential = hashlib.sha256(token.encode()).hexdigest()
        if (not runtime.get('pid') or runtime.get('source_sha256') != config['client_sha256']
            or runtime.get('credential_sha256') != expected_credential):
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
    declared = declared_check(config, original)
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
                  outcome_check_sha256=outcome_signature(declared),
                  started_at=started, ended_at=time.time(), kind=declared['kind'],
                  human_receipt='unobserved; not inferred')
    return report
