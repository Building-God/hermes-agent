"""Bounded live interface drill as Codex, never as Harry; no recurring runner."""
import asyncio, hashlib, hmac, json, sys, time, uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

ROOT = Path('C:/Users/User/DiscordBots/Jarvis')
HOME = Path('C:/Users/User/AppData/Local/hermes')
CONFIG = json.loads(sys.stdin.read())
OUTPUT = None
import importlib.util
client_path=Path(CONFIG['client_path'])
spec=importlib.util.spec_from_file_location('primary_interface_client',client_path)
hermes_router=importlib.util.module_from_spec(spec)
spec.loader.exec_module(hermes_router)


async def run():
    started = json.loads((HOME / 'gateway_state.json').read_text())
    assert hashlib.sha256(Path(hermes_router.__file__).read_bytes()).hexdigest() == CONFIG['client_sha256'], 'Primary client source changed'
    import yaml
    cfg = yaml.safe_load((HOME / 'config.yaml').read_text())
    peer = 'operator-verification'
    assert cfg['a2a']['peer_identities'][peer]['user'] == peer, 'Verifier must never impersonate Harry'
    expires = cfg['a2a'].get('interactive_peer_expires_at', {}).get(peer, float('inf'))
    values = dotenv_values(HOME / '.env')
    peers = dict(item.split(':', 1) for item in values['A2A_PEER_TOKENS'].split(',') if ':' in item)
    ui_token = peers['jarvis-interactive']
    probe_token = peers[peer]
    assert not hmac.compare_digest(ui_token, probe_token)
    assert time.time() < expires
    endpoint = dotenv_values(ROOT / '.env')['HERMES_A2A_URL']
    assert urlparse(endpoint).hostname in ('localhost', '127.0.0.1', '::1'), 'Probe only the declared local interface'
    context = 'operator-owned-verification-' + uuid.uuid4().hex
    code = 'indigo-' + uuid.uuid4().hex[:8]
    records = []
    receipt = {'started_at': time.time(), 'revision': started['code_sha'], 'gateway_pid': started['pid'],
               'context_id': context, 'actor': peer, 'genuine_harry_request': False,
               'human_receipt': 'not inferred', 'scope': 'Six controlled reasoning/memory turns through the actual declared primary HTTP consumer and real serving default agent; no requested tools, files or task mutations.',
               'router_sha256': hashlib.sha256(Path(hermes_router.__file__).read_bytes()).hexdigest(),
               'requests': records, 'passed': False}
    class ProbeClient(httpx.AsyncClient):
        def probe_headers(self, headers):
            headers = dict(headers or {})
            assert hmac.compare_digest(headers.get('Authorization', ''), 'Bearer ' + ui_token), 'Primary interface client did not select the distinct UI credential'
            headers['Authorization'] = 'Bearer ' + probe_token
            return headers
        async def post(self, url, **kwargs):
            assert str(url).rstrip('/') == endpoint.rstrip('/'), 'Probe must reach the configured primary endpoint'
            kwargs['headers'] = self.probe_headers(kwargs.get('headers'))
            return await super().post(url, **kwargs)
        def stream(self, method, url, **kwargs):
            assert str(url).rstrip('/') == endpoint.rstrip('/'), 'Probe must stream from the configured primary endpoint'
            kwargs['headers'] = self.probe_headers(kwargs.get('headers'))
            return super().stream(method, url, **kwargs)
    prompts = [
        'Controlled native operator verification, not a Harry request. Do not use tools, create tasks, save memory outside this conversation, modify anything or contact anyone. Our temporary context reference is ' + code + '. Reply briefly with that reference.',
        'Controlled hypothetical: a queue has two cards for the same Instagram login problem, with different task IDs. Does unique task ID prove the queue problem is fixed? Explain in one short sentence; no actions.',
        'What was the temporary reference in my first message? Answer only the reference; no tools or actions.',
        'Controlled hypothetical: a code fix is committed, but no live serving check has been made. Is functional success proved? Explain one missing check in one sentence; no actions.',
        'Controlled hypothetical: an agent sent a result, but Harry has not replied to it. Has a Harry result receipt been observed? Explain briefly; no actions.',
        'This is the sixth controlled turn in this same context. State the temporary reference from turn one, then one unresolved verification issue from our hypothetical examples. No tools, tasks or actions.',
    ]
    try:
        for index, prompt in enumerate(prompts):
            assert time.time() < expires, 'Controlled capability expired'
            before = time.time()
            if CONFIG.get('primary_http_url'):
                url=CONFIG['primary_http_url']
                assert urlparse(url).hostname in ('localhost','127.0.0.1','::1')
                async with httpx.AsyncClient(timeout=75) as client:
                    actual=await client.post(url,json={'text':prompt,'surface':'dash','context_id':context},
                        headers={'X-Jarvis-Verification-Token':probe_token})
                    actual.raise_for_status()
                    body=actual.json()
                    assert body.get('backend')=='hermes' and body.get('actor')==peer, 'Actual HTTP consumer lost backend or verification provenance'
                    response=body['reply']
            else:
                response = await hermes_router.send_to_hermes(prompt, context_id=context, env_path=client_path.parent / '.env',
                    timeout_s=75, client_factory=lambda **kwargs: ProbeClient(**kwargs), stream=index == 5)
            assert response and 'anti-loop protection' not in response.lower()
            records.append({'turn': index + 1, 'transport': 'primary-http' if CONFIG.get('primary_http_url') else ('stream' if index == 5 else 'http'),
                            'request': prompt, 'response': response[:3000], 'elapsed_seconds': time.time() - before})

        assert all(code in records[index]['response'] for index in (0, 2, 5)), 'Context/reference was lost'
        assert any(word in records[1]['response'].lower() for word in ('no', 'not', 'duplicate', 'same problem')), 'Queue reasoning did not identify missing semantic proof'
        assert any(word in records[3]['response'].lower() for word in ('live', 'serving', 'deploy', 'reproduc', 'not')), 'Committed-code reasoning missed live outcome verification'
        assert any(word in records[4]['response'].lower() for word in ('no', 'not', 'unobserved', 'receipt')), 'Human receipt boundary not expressed'
        ended = json.loads((HOME / 'gateway_state.json').read_text())
        assert ended['pid'] == started['pid'] and ended['code_sha'] == started['code_sha']
        receipt.update(passed=True, ended_at=time.time(), primary_client_selected_distinct_ui_credential=True,
                       same_serving_process=True, verification_actor='operator-verification; not Harry')
    except Exception as error:
        receipt.update(ended_at=time.time(), error_type=type(error).__name__, error=str(error)[:400])
    print(json.dumps(receipt))


if __name__ == '__main__':
    asyncio.run(run())
