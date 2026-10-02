"""Legacy foreground entrypoints must honor the verified release selection."""
import hashlib
import json
import os
from pathlib import Path
import subprocess


def redirect_selected_gateway(home, current_root):
    home=Path(home);pointer=home/'runtime/active-release.json'
    if not pointer.is_file():return None
    selected=json.loads(pointer.read_text(encoding='utf8'))
    target=Path(selected['root']).resolve()
    if target==Path(current_root).resolve():return None
    launcher=Path(selected['launcher'])
    runtime=target/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if (not target.is_absolute() or not launcher.is_absolute() or not runtime.is_file()
        or hashlib.sha256(launcher.read_bytes()).hexdigest()!=selected['launcher_sha256']):
        raise ValueError('Selected gateway release redirect failed verification; editable source will not serve')
    # The sealed controller performs full source/runtime preflight before launch.
    # No shell, replacement of source, process kill, or selector mutation here.
    env=dict(os.environ);env['HERMES_HOME']=str(home)
    result=subprocess.run([str(runtime),str(launcher)],cwd=target,env=env,shell=False,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    return result.returncode
