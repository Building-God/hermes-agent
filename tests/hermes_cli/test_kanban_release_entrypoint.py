import hashlib,json
from pathlib import Path
import pytest
from hermes_cli.kanban_release_entrypoint import reconcile_entrypoint


@pytest.fixture
def sealed_entry(tmp_path):
    home=tmp_path/'hermes';runtime=home/'runtime';launchers=runtime/'launchers';launchers.mkdir(parents=True)
    entry=tmp_path/'Jarvis/scripts/hermes_gateway_launch.py';entry.parent.mkdir(parents=True)
    baseline=b'from contextlib import contextmanager\n@contextmanager\ndef _selector_lock(home):\n    yield\ndef _preflight(home,selected):\n    if selected.get("fail"):\n        raise ValueError("failed preflight")\n'
    sha=hashlib.sha256(baseline).hexdigest();launcher=launchers/(sha+'.py');launcher.write_bytes(baseline)
    selected={'launcher':str(launcher),'launcher_sha256':sha}
    (runtime/'active-release.json').write_text(json.dumps(selected))
    entry.write_bytes(b'older service entry')
    return {'home':str(home),'path':str(entry)},selected,baseline


def test_selected_controller_restores_displaced_entry_and_preserves_it(sealed_entry):
    declaration,selected,baseline=sealed_entry
    action=reconcile_entrypoint(declaration,now=100)
    assert action['release_entry_repaired']
    assert Path(declaration['path']).read_bytes()==baseline
    state=json.loads(Path(action['receipt']).read_text())
    assert Path(state['backup']).read_bytes()==b'older service entry'
    assert state['phase']=='restored'
    assert state['due_at']==220
    assert reconcile_entrypoint(declaration,now=103) is None


def test_unsealed_controller_never_overwrites_entry(sealed_entry):
    declaration,selected,baseline=sealed_entry
    Path(selected['launcher']).write_bytes(b'changed controller')
    with pytest.raises(ValueError,match='seal changed'):
        reconcile_entrypoint(declaration,now=100)
    assert Path(declaration['path']).read_bytes()==b'older service entry'


def test_failed_preflight_has_fixed_retry_deadline_and_keeps_entry(sealed_entry):
    declaration,selected,baseline=sealed_entry
    selected['fail']=True
    home=Path(declaration['home']);(home/'runtime/active-release.json').write_text(json.dumps(selected))
    for now in [100,110,120]:
        action=reconcile_entrypoint(declaration,now=now)
        assert action['exception']=='release_entry_repair_failed'
    state=json.loads(Path(action['receipt']).read_text())
    assert state['phase']=='exhausted'
    assert state['due_at']==220
    assert state['attempts']==3
    assert Path(declaration['path']).read_bytes()==b'older service entry'
    assert reconcile_entrypoint(declaration,now=130)['exception']=='release_entry_repair_pending'


def test_repeated_entry_clobbers_cannot_create_an_unbounded_file_fight(sealed_entry):
    declaration,selected,baseline=sealed_entry
    entry=Path(declaration['path'])
    for now in [100,110,120]:
        entry.write_bytes(('clobber'+str(now)).encode())
        assert reconcile_entrypoint(declaration,now=now)['release_entry_repaired']
    entry.write_bytes(b'fourth clobber')
    assert reconcile_entrypoint(declaration,now=130)['exception']=='release_entry_repair_exhausted'
    assert entry.read_bytes()==b'fourth clobber'


def test_release_entry_fault_cannot_abort_native_task_reconciliation(tmp_path,monkeypatch):
    from hermes_cli import kanban_operator as operator
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    conn=kb.connect()
    try:
        actions=operator.reconcile(conn,settings={'enabled':True,'release_entrypoint':{'home':str(tmp_path),'path':str(tmp_path/'wrong.py')}})
        assert actions[0]['exception']=='release_entry_reconciliation_failed'
        assert all(a.get('exception')!='reconciliation_failed' for a in actions)
    finally:
        conn.close()


import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_release_redirect as redirect


@pytest.mark.parametrize('mode',['selected','same','corrupt','absent'])
def test_editable_startup_cannot_escape_selected_release(tmp_path,monkeypatch,mode):
    home=tmp_path/'home';(home/'runtime').mkdir(parents=True)
    target=tmp_path/'release';runtime=target/'venv'/('Scripts/python.exe' if redirect.os.name=='nt' else 'bin/python')
    runtime.parent.mkdir(parents=True);runtime.write_bytes(b'python')
    launcher=home/'controller.py';launcher.write_bytes(b'controller')
    if mode!='absent':
        (home/'runtime/active-release.json').write_text(json.dumps({'root':str(target),'launcher':str(launcher),'launcher_sha256':hashlib.sha256(b'controller').hexdigest()}))
    if mode=='corrupt':launcher.write_bytes(b'changed')
    calls=[]
    def run(argv,**kwargs):calls.append((argv,kwargs));return SimpleNamespace(returncode=17)
    monkeypatch.setattr(redirect.subprocess,'run',run)
    current=target if mode=='same' else tmp_path/'unfinished-source'
    if mode=='corrupt':
        with pytest.raises(ValueError,match='verification'):redirect.redirect_selected_gateway(home,current)
    else:
        result=redirect.redirect_selected_gateway(home,current)
        assert result==(17 if mode=='selected' else None)
    assert len(calls)==(1 if mode=='selected' else 0)
    if calls:assert calls[0][0]==[str(runtime),str(launcher)] and calls[0][1]['cwd']==target
