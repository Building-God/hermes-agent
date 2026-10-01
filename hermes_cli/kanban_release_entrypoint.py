"""Repair the one declared service entry from its selected immutable controller.

Called by the existing dispatcher. No process restart, scheduler or source edit.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _save(path, value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def reconcile_entrypoint(declaration,now=None):
    if not declaration:
        return None
    now=time.time() if now is None else now
    home=Path(declaration['home']).resolve();entry=Path(declaration['path']).resolve()
    if entry.name!='hermes_gateway_launch.py' or entry.parent.name!='scripts':
        raise ValueError('release entry declaration is not the owned controller')
    pointer=home/'runtime/active-release.json'
    selected=json.loads(pointer.read_text(encoding='utf-8'))
    launcher=Path(selected['launcher']).resolve()
    if launcher.parent!=home/'runtime/launchers':
        raise ValueError('selected controller escapes immutable launchers')
    expected=selected['launcher_sha256']
    baseline=launcher.read_bytes()
    if _sha(baseline)!=expected:
        raise ValueError('immutable controller seal changed; no entry repair permitted')
    current=entry.read_bytes() if entry.exists() else b''
    if _sha(current)==expected:
        return None
    state_path=home/'runtime/entrypoint-reconciliation.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get('phase')!='restored' and state.get('observed_sha256')==_sha(current) and state.get('expected_sha256')==expected:
        if state.get('phase')=='exhausted' or now<state.get('retry_at',0):
            return {'exception':'release_entry_repair_pending','owner':'agent','receipt':str(state_path)}
    else:
        recent=[t for t in state.get('repair_times',[]) if now-t<3600]
        state={'observed_sha256':_sha(current),'expected_sha256':expected,'due_at':now+120,'attempts':0,'repair_times':recent}
    spec=importlib.util.spec_from_file_location('sealed_operator_controller',launcher)
    controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    try:
        with controller._selector_lock(home):
            if json.loads(pointer.read_text(encoding='utf-8'))!=selected or (entry.read_bytes() if entry.exists() else b'')!=current:
                return {'exception':'release_entry_changed_during_reconciliation','owner':'agent'}
            if state['attempts']>=3 or len(state['repair_times'])>=3 or now>=state['due_at']:
                state['phase']='exhausted';_save(state_path,state)
                return {'exception':'release_entry_repair_exhausted','owner':'agent','receipt':str(state_path)}
            state['attempts']+=1;state['phase']='preflight';_save(state_path,state)
            controller._preflight(home,selected)
            backup_dir=home/'runtime/entrypoint-repairs';backup_dir.mkdir(exist_ok=True)
            backup=backup_dir/(str(time.time_ns())+'-'+_sha(current)+'.py')
            with backup.open('wb') as stream:
                stream.write(current);stream.flush();os.fsync(stream.fileno())
            state.update(backup=str(backup),phase='restoring');_save(state_path,state)
            temporary=entry.with_name(entry.name+'.operator.tmp')
            with temporary.open('wb') as stream:
                stream.write(baseline);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,entry)
            if _sha(entry.read_bytes())!=expected:
                raise ValueError('entry readback differs from selected controller')
            state.update(phase='restored',finished_at=now,repair_times=state['repair_times']+[now],selection=selected)
            _save(state_path,state)
            return {'release_entry_repaired':True,'owner':'agent','receipt':str(state_path),'expected_sha256':expected}
    except Exception as error:
        state.update(phase='exhausted' if state['attempts']>=3 or now>=state['due_at'] else 'retry',retry_at=now+10,error=type(error).__name__)
        _save(state_path,state)
        return {'exception':'release_entry_repair_failed','owner':'agent','receipt':str(state_path)}
