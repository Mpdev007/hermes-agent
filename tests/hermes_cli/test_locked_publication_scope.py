"""Native install locks + real JSON-line worker; resolver leaf alone is inert.

All state is tiny private fixtures. No PM runtime bootstrap/download: the worker
launcher selects the already-qualified interpreter, then runs unmodified worker
main. Neither sync, recovery, activation, lock nor IPC is replaced.
"""
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def fingerprint(root):
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        s = p.lstat()
        result[str(p.relative_to(root))] = [s.st_mode, s.st_size, s.st_mtime_ns, s.st_uid, s.st_gid,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None]
    return result


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / 'home' / '.hermes'
    home.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home.parent))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: home.parent)
    core = tmp_path / 'core'
    core.mkdir()
    (core/'pyproject.toml').write_text('[project]\nname="fixture"\nversion="0"\nrequires-python=">=3.11"\ndependencies=[]\n[project.optional-dependencies]\nmcp=[]\n')
    (core/'uv.lock').write_text('version = 1\n')
    (home/'config.yaml').write_text('mcp_servers:\n  fixture:\n    command: never-execute\n')
    import pm.paths
    monkeypatch.setattr(pm.paths, 'repo_root', lambda: core)
    from pm.environments import install_state_dir
    directory = install_state_dir(core)
    directory.mkdir(parents=True)
    for name in ['alpha', 'beta']:
        p = home/'profiles'/name
        p.mkdir(parents=True)
        (p/'config.yaml').write_text('plugins:\n  disabled: [foreign]\n')
    return home, core, directory/'publication.json', tmp_path


def config_row(home, names=('profiles/alpha', 'profiles/beta')):
    rows = []
    for i, name in enumerate(names):
        config = home/name/'config.yaml'
        previous = b'plugins:\n  enabled: []\n# previous\n' if i == 0 else None
        rows.append({'config':str(config), 'previous':base64.b64encode(previous).decode() if previous else None,
                     'config_after':hashlib.sha256(config.read_bytes()).hexdigest()})
    return {'configs':rows, 'facts_before':None}


def plugin_row(home, *, own=False):
    directory = home/'plugins' if own else home/'profiles'/'alpha'/'plugins'
    target, backup = directory/'fixture', directory/'.previous-fixture'
    for p, text in [(target, 'new'), (backup, 'old')]:
        p.mkdir(parents=True)
        (p/'code.py').write_text(text)
    metadata = directory/'.install-metadata.json'
    metadata.write_text('{"fixture":{"revision":"new"}}')
    return {'kind':'plugin', 'target':str(target), 'backup':str(backup), 'metadata':str(metadata),
            'target_existed':True, 'facts_before':None,
            'metadata_before':base64.b64encode(b'{"fixture":{"revision":"old"}}').decode(),
            'metadata_after':base64.b64encode(metadata.read_bytes()).decode()}


WORKER = '''import atexit, json, os, pathlib, runpy, sys
from pathlib import Path
sys.path.insert(0, SOURCE)
from pm.packages import Venv
# Only the package acquisition/build leaf is inert.
Venv.apply = lambda *args, **kwargs: {}
trace = []
def audit(event, args):
    if event == 'open':
        _, mode, flags = args
        write = isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)
    else:
        write = event in {'os.remove','os.rename','os.mkdir','os.rmdir','os.chmod','os.utime'}
    if write and any(isinstance(a,(str,bytes,os.PathLike)) and FOREIGN in os.fsdecode(a) for a in args):
        trace.append({'write':event,'args':[str(a) for a in args]})
def profile(frame,event,arg):
    if event != 'call': return
    name=frame.f_code.co_name
    if name in {'sync_venv','_venv_install_lock','runtime_lock','recover_publication','finish_publication'}:
        from hermes_cli.maintenance_scope import current_scope
        item={'native':name,'file':frame.f_code.co_filename,'scope':current_scope().to_dict(),'pid':os.getpid()}
        if name == 'sync_venv' and frame.f_back.f_code.co_name == 'main':
            item['request_context']=frame.f_back.f_locals['request']['context']
        if name == 'recover_publication':
            # Another process must be unable to acquire the SAME native lock.
            from pm.environments import install_state_dir
            lock=install_state_dir(frame.f_locals['project'])/'.install.lock'
            import subprocess
            check='import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
            checked=subprocess.run([sys.executable,'-I','-c',check,str(lock)],capture_output=True)
            item['install_lock_held']=checked.returncode != 0 and b'BlockingIOError' in checked.stderr
        trace.append(item)
sys.addaudithook(audit)
sys.setprofile(profile)
atexit.register(lambda: Path(EVIDENCE).write_text(json.dumps(trace,indent=2)))
runpy.run_path(WORKER_PATH,run_name='__main__')
'''


def worker_launcher(state, monkeypatch):
    home, core, journal, tmp = state
    from pm import client
    wrapper = tmp/'worker-fixture.py'
    source = str(Path(client.__file__).resolve().parents[1])
    evidence = tmp/'worker-evidence.json'
    def command(spec, arguments, worker, environment):
        wrapper.write_text('SOURCE='+repr(source)+'\nFOREIGN='+repr(str(home/'profiles'))+'\nEVIDENCE='+repr(str(evidence))+'\nWORKER_PATH='+repr(str(worker))+'\n'+WORKER)
        return [sys.executable, '-I', '-B', str(wrapper)]
    monkeypatch.setattr(client, '_worker_command', command)
    return evidence


@pytest.mark.parametrize('route', ['helper', 'repair', 'pending-helper', 'pending-repair', 'early', 'pending-early'])
@pytest.mark.parametrize('kind', ['configs', 'mixed', 'plugin'])
def test_worker_late_journal(state, monkeypatch, route, kind):
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope
    from hermes_cli.runtime_state import runtime_lock
    from hermes_cli import main_install_repair as helper
    from pm.recovery import repair_dependencies
    evidence = worker_launcher(state, monkeypatch)
    row = plugin_row(home) if kind == 'plugin' else config_row(home, ('', 'profiles/alpha') if kind == 'mixed' else ('profiles/alpha','profiles/beta'))
    data = json.dumps(row).encode()
    before = fingerprint(home/'profiles')
    own_before = (home/'config.yaml').read_bytes()
    original_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name,*a,**kw: None if name == 'mcp' else original_find(name,*a,**kw))
    injected = []
    def profile(frame,event,arg):
        if event == 'call' and frame.f_code.co_name == '_request' and not injected:
            with runtime_lock(core, timeout=None) as held:
                assert held
                journal.write_bytes(data)
            injected.append('after_helper_resolved_before_worker_lock')
    from contextlib import nullcontext
    if route.startswith('pending-'):
        from hermes_cli.venv_sync import arm_completion
        arm_completion(core, maintenance_scope=MaintenanceScope.select('default'))
    error = None
    with (nullcontext() if route.startswith('pending-') else maintenance_scope(MaintenanceScope.select('default'))):
        sys.setprofile(profile)
        try:
            if route.endswith('helper'): helper._install_configured_features_missing_deps(core)
            elif route.endswith('early'):
                from hermes_cli._early_recovery import recover_if_needed
                (journal.parent/'.repair-incomplete').write_text('{}')
                recover_if_needed(core, argv=['chat'])
            else: repair_dependencies(core)
        except Exception as exc:
            error = {'type':type(exc).__name__, 'message':str(exc)}
        finally:
            sys.setprofile(None)
    traces = json.loads(evidence.read_text())
    after = fingerprint(home/'profiles')
    result={'injection':injected,'before':before,'after':after,'journal_before':data.hex(),
            'journal_after':journal.read_bytes().hex() if journal.exists() else None,'error':error,'trace':traces}
    (tmp/'result.json').write_text(json.dumps(result,indent=2))
    assert injected and any(t.get('native') == 'recover_publication' and t['install_lock_held'] for t in traces)
    assert before == after and (home/'config.yaml').read_bytes() == own_before, result
    assert not any('write' in t for t in traces), result
    assert journal.read_bytes() == data
    assert error and error['type'] == 'ScopeSafetyRefusal', result
    assert any(t.get('request_context',{}).get('maintenance_scope') == MaintenanceScope.select('default').to_dict() for t in traces)
    assert all(t['scope']['mode'] == 'default' for t in traces if t.get('native') == 'recover_publication')


def test_helper_new_pending_refusal_is_fatal(state, monkeypatch):
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal, MaintenanceScope
    from hermes_cli.runtime_state import runtime_lock
    from hermes_cli.venv_sync import arm_completion
    from hermes_cli.main_install_repair import _install_configured_features_missing_deps
    evidence = worker_launcher(state, monkeypatch)
    original_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name,*a,**kw: None if name == 'mcp' else original_find(name,*a,**kw))
    data = json.dumps(config_row(home)).encode()
    before = fingerprint(home/'profiles')
    injected = []
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == '_request' and not injected:
            with runtime_lock(core, timeout=None):
                arm_completion(core, maintenance_scope=MaintenanceScope.select('default'))
                journal.write_bytes(data)
            injected.append(True)
    sys.setprofile(profile)
    try:
        with pytest.raises(ScopeSafetyRefusal):
            _install_configured_features_missing_deps(core)
    finally: sys.setprofile(None)
    assert injected and fingerprint(home/'profiles') == before and journal.read_bytes() == data
    assert not any('write' in t for t in json.loads(evidence.read_text()))



@pytest.mark.parametrize('kind', ['config', 'plugin'])
@pytest.mark.parametrize('own', [False, True])
def test_worker_publish_and_finish(state, monkeypatch, kind, own):
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope, ScopeSafetyRefusal
    from pm import client
    from pm.plugin_inputs import Selection, StagedUpdate
    from pm.store import tree_digest
    worker_launcher(state, monkeypatch)
    target_home = home if own else home/'profiles'/'alpha'
    if kind == 'config':
        change = Selection({'home': str(target_home), 'enabled': [], 'disabled': ['old']})
    else:
        target = target_home/'plugins'/'fixture'
        target.mkdir(parents=True)
        (target/'plugin.yaml').write_text('name: fixture\n')
        (target/'code.py').write_text('old')
        (target.parent/'.install-metadata.json').write_text('{"fixture":{"revision":"old"}}')
        staged = tmp/'staged'
        staged.mkdir()
        (staged/'plugin.yaml').write_text('name: fixture\n')
        (staged/'code.py').write_text('new')
        change = StagedUpdate({'target':str(target),'staged':str(staged),'target_digest':tree_digest(target),
                              'old_metadata':{'fixture':{'revision':'old'}},'new_metadata':{'fixture':{'revision':'new'}}})
    before = fingerprint(home/'profiles')
    error = None
    with maintenance_scope(MaintenanceScope.select('default')):
        try: client.sync_venv(explicit=True, plugins=change)
        except Exception as exc: error = exc
    after = fingerprint(home/'profiles')
    assert before == after
    assert not journal.exists()
    if own:
        assert error is None, error
        if kind == 'config':
            assert 'old' in (home/'config.yaml').read_text()
        else:
            assert (target/'code.py').read_text() == 'new'
            assert not list(target.parent.glob('.previous-*'))
    else:
        assert isinstance(error, ScopeSafetyRefusal), error


@pytest.mark.parametrize('kind', ['config', 'plugin', 'mixed', 'unknown', 'corrupt'])
@pytest.mark.parametrize('finish', [False, True])
def test_locked_refusal_is_atomic(state, kind, finish):
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope, ScopeSafetyRefusal
    from hermes_cli.runtime_state import runtime_lock, recover_publication, finish_publication
    row = (plugin_row(home) if kind == 'plugin' else
           config_row(home, ('', 'profiles/alpha') if kind == 'mixed' else ('profiles/alpha','profiles/beta')))
    if kind == 'unknown': row['kind'] = 'unknown'
    data = b'{bad' if kind == 'corrupt' else json.dumps(row).encode()
    journal.write_bytes(data)
    before = fingerprint(home/'profiles')
    own_before = (home/'config.yaml').read_bytes()
    with maintenance_scope(MaintenanceScope.select('default')), runtime_lock(core, timeout=None):
        with pytest.raises((ScopeSafetyRefusal, json.JSONDecodeError)):
            (finish_publication if finish else recover_publication)(core)
    assert fingerprint(home/'profiles') == before
    assert (home/'config.yaml').read_bytes() == own_before
    assert journal.read_bytes() == data


@pytest.mark.parametrize('kind', ['config', 'plugin'])
@pytest.mark.parametrize('finish', [False, True])
@pytest.mark.parametrize('own', [False, True])
def test_locked_native_recovery_positive(state, kind, finish, own):
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope
    from hermes_cli.runtime_state import runtime_lock, recover_publication, finish_publication
    row = plugin_row(home, own=own) if kind == 'plugin' else config_row(home, ('',) if own else ('profiles/alpha','profiles/beta'))
    journal.write_text(json.dumps(row))
    with maintenance_scope(MaintenanceScope.select('default' if own else 'all')), runtime_lock(core, timeout=None):
        (finish_publication if finish else recover_publication)(core)
    assert not journal.exists()
    if kind == 'plugin':
        assert Path(row['target'],'code.py').read_text() == ('new' if finish else 'old')
        assert not Path(row['backup']).exists()
    elif not finish:
        assert Path(row['configs'][0]['config']).read_bytes() == base64.b64decode(row['configs'][0]['previous'])
        if not own: assert not (home/'profiles'/'beta'/'config.yaml').exists()


@pytest.mark.parametrize('bad', ['unknown', 'null', 'foreign-home', 'extra-field', 'pending-broaden'])
def test_worker_rejects_invalid_scope_on_wire(state, monkeypatch, bad):
    home, core, journal, tmp = state
    from pm import client
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion
    evidence = worker_launcher(state, monkeypatch)
    scope = MaintenanceScope.select('default').to_dict()
    if bad == 'unknown': scope['mode'] = 'unknown'
    elif bad == 'null': scope = None
    elif bad == 'foreign-home': scope['home'] = str(home/'profiles'/'alpha')
    elif bad == 'extra-field': scope['schema'] = 999
    else:
        arm_completion(core, maintenance_scope=MaintenanceScope.select('default'))
        scope = {'mode':'all', 'home':None}
    data = json.dumps(config_row(home)).encode()
    journal.write_bytes(data)
    before = fingerprint(home/'profiles')
    request = {'id':'scope-negative','operation':'sync_venv','arguments':{'explicit':True},
               'packages':[],'callbacks':[], 'context':{'repo':str(core), 'lockfile':str(core/'pm.lock'), 'maintenance_scope':scope}}
    command = client._worker_command(None, {}, Path(client.__file__).with_name('worker.py'), {})
    result = subprocess.run(command, input=json.dumps(request)+'\n', text=True, capture_output=True, timeout=20)
    (tmp/'wire-response.json').write_text(result.stdout)
    response = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert response['error']['type'] == 'ScopeSafetyRefusal', response
    assert fingerprint(home/'profiles') == before and journal.read_bytes() == data
    assert not any(t.get('native') == 'recover_publication' for t in json.loads(evidence.read_text()))


@pytest.mark.parametrize('payload', ['{bad', '{"schema":999,"maintenance_scope":{"mode":"all","home":null}}'])
def test_corrupt_pending_at_locked_activation_is_fatal_even_without_journal(state, payload):
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal
    from hermes_cli.venv_sync import completion_pending_path
    from pm.environments import activate_dependencies
    completion_pending_path(core).write_text(payload)
    with pytest.raises(ScopeSafetyRefusal):
        activate_dependencies(core)


@pytest.mark.parametrize('mode', ['explicit', 'flagless', 'all'])
@pytest.mark.parametrize('kind', ['config', 'mixed', 'plugin'])
def test_public_bootstrap_final_lock_gap(state, mode, kind):
    import inspect
    import shutil
    home, core, journal, tmp = state
    from hermes_cli.maintenance_scope import MaintenanceScope
    from hermes_cli.venv_sync import arm_completion
    import pm
    source = Path(pm.__file__).resolve().parents[1]
    for p in source.iterdir():
        target = core/p.name
        if target.exists() or p.name in {'.git','__pycache__','install-stamp.json'}: continue
        if p.name == 'hermes_bootstrap.py': shutil.copy2(p,target)
        else: target.symlink_to(p, target_is_directory=p.is_dir())
    if mode == 'flagless': arm_completion(core, maintenance_scope=MaintenanceScope.select('default'))
    row = plugin_row(home) if kind == 'plugin' else config_row(home, ('','profiles/alpha') if kind == 'mixed' else ('profiles/alpha','profiles/beta'))
    data = json.dumps(row).encode()
    before = fingerprint(home/'profiles')
    own_before = (home/'config.yaml').read_bytes()
    args = ['--help'] if mode == 'flagless' else ['update','--maintenance-scope', 'all' if mode == 'all' else 'default','--no-gateway-restart']
    script = tmp/'bootstrap-fixture.py'
    output = tmp/'bootstrap-evidence.json'
    code = "import sys,os,json,hashlib\nfrom pathlib import Path\n" + inspect.getsource(fingerprint)
    code += "CORE=Path("+repr(str(core))+"); JOURNAL=Path("+repr(str(journal))+"); DATA="+repr(data)+"; HOME=Path("+repr(str(home))+ "); OUTPUT=Path("+repr(str(output))+")\n"
    code += "sys.path.insert(0,str(CORE)); sys.argv=['hermes', *"+repr(args)+"]\n"
    code += """
trace=[]
writes=[]
def audit(event,args):
    if event == 'open':
        _,mode,flags=args
        write=isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)
    else: write=event in {'os.remove','os.rename','os.mkdir','os.rmdir','os.chmod','os.utime'}
    if write and any(isinstance(a,(str,bytes,os.PathLike)) and str(HOME/'profiles') in os.fsdecode(a) for a in args):
        writes.append({'event':event,'args':[str(a) for a in args]})
def profile(frame,event,arg):
    if event != 'call': return
    name=frame.f_code.co_name
    if name in {'resolve_scope','activate_dependencies','recover_publication','runtime_lock'}: trace.append(name)
    if name == 'activate_dependencies':
        from hermes_cli.runtime_state import runtime_lock
        with runtime_lock(CORE, timeout=None) as held:
            assert held
            JOURNAL.write_bytes(DATA)
        trace.append('writer_after_final_scope_check')
sys.addaudithook(audit)
sys.setprofile(profile)
try:
    import hermes_bootstrap
finally:
    sys.setprofile(None)
    OUTPUT.write_text(json.dumps({'trace':trace,'foreign_writes':writes,'after':fingerprint(HOME/'profiles'),
                                 'journal_after':JOURNAL.read_bytes().hex() if JOURNAL.exists() else None},indent=2))
"""
    script.write_text(code)
    result=subprocess.run([sys.executable,str(script)],capture_output=True,text=True,timeout=20)
    (tmp/'bootstrap-stderr.log').write_text(result.stderr)
    evidence=json.loads(output.read_text())
    assert 'writer_after_final_scope_check' in evidence['trace'] and 'recover_publication' in evidence['trace']
    if mode == 'all':
        assert result.returncode == 0, result.stderr
        assert evidence['after'] != before and evidence['foreign_writes']
        assert evidence['journal_after'] is None
    else:
        assert result.returncode != 0 and 'maintenance scope refused' in result.stderr
        assert evidence['after'] == before and not evidence['foreign_writes']
        assert evidence['journal_after'] == data.hex()
        assert (home/'config.yaml').read_bytes() == own_before



