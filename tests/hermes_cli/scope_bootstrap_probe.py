"""Fresh public bootstrap and actual native publication recovery.
No bootstrap/prepare/activation/recovery stubs. Only all-fault's external PM
query fails inertly. Fixtures have no credentials. --readonly remounts both
foreign profiles read-only; audit records even failed write attempts.
"""
import base64, hashlib, json, os, pathlib, shutil, subprocess, sys


def fingerprint(root):
    result = {}
    for p in [root, *sorted(root.rglob('*'))]:
        s = p.lstat()
        result[str(p.relative_to(root))] = {'mode': s.st_mode, 'size': s.st_size,
            'mtime_ns': s.st_mtime_ns, 'uid': s.st_uid, 'gid': s.st_gid,
            'sha256': hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None}
    return result


def setup(case, work, readonly):
    root = pathlib.Path(__file__).resolve().parents[2]
    work.mkdir(parents=True)
    home = work / 'home' / '.hermes'
    home.mkdir(parents=True)
    os.environ.update(HOME=str(home.parent), HERMES_HOME=str(home))
    core = work / 'core'
    core.mkdir()
    # Full source is visible. Bootstrap is a byte-identical regular file so its
    # __file__ selects a separate native install key; no product code is replaced.
    for path in root.iterdir():
        if path.name == 'hermes_bootstrap.py':
            shutil.copy2(path, core / path.name)
        elif path.name not in {'.git', 'install-stamp.json', '__pycache__'}:
            (core / path.name).symlink_to(path, target_is_directory=path.is_dir())
    sys.path.insert(0, str(core))
    from pm.environments import install_state_dir
    from hermes_cli.venv_sync import completion_pending_path
    state = install_state_dir(core)
    state.mkdir(parents=True)
    rows = []
    for name in ('alpha', 'beta'):
        profile = home / 'profiles' / name
        profile.mkdir(parents=True)
        p = profile / 'config.yaml'
        p.write_bytes(b'plugins:\n  disabled: [foreign]\nmemory:\n  provider: ""\n')
        previous = b'plugins:\n  enabled: [foreign]\nmemory:\n  provider: foreign\n' if name == 'alpha' else None
        rows.append({'config': str(p), 'previous': base64.b64encode(previous).decode() if previous else None,
                     'config_after': hashlib.sha256(p.read_bytes()).hexdigest()})
    journal = state / 'publication.json'
    publication = json.dumps({'configs': rows, 'facts_before': None})
    journal.write_text(publication)
    pending = completion_pending_path(core)
    scoped = {'schema': 1, 'maintenance_scope': {'mode': 'default', 'home': str(home)}}
    if case in {'flagless', 'corrupt', 'stale', 'broaden', 'unreadable', 'mismatch', 'flagless-pm', 'legacy-dispatch'}:
        pending.write_text('{broken' if case == 'corrupt' else json.dumps(scoped))
    if case == 'stale':
        os.utime(pending, (1, 1))
        (core / 'install-stamp.json').write_text(json.dumps({'updateMechanism': 'self', 'maintenance_scope': {'mode': 'all', 'home': None}}))
    if case == 'unreadable':
        pending.chmod(0)
    if case == 'mismatch':
        scoped['maintenance_scope']['home'] = str(home / 'profiles' / 'alpha')
        pending.write_text(json.dumps(scoped))
    if case in {'all-fault', 'fallback-publication'}:
        os.environ.pop('HERMES_DISABLE_LAZY_INSTALLS', None)
        (core / '.git').mkdir()
        (core / 'install-stamp.json').write_text(json.dumps({'updateMechanism': 'self'}))
    argv = ['update', '--maintenance-scope', 'default', '--no-gateway-restart']
    if case in {'flagless', 'corrupt', 'stale', 'unreadable', 'mismatch'}:
        argv = ['--help']
    elif case == 'flagless-pm':
        argv = ['pm', 'repair']
    elif case == 'explicit-pm':
        argv = ['pm', 'install', '--maintenance-scope', 'default']
    elif case == 'legacy-dispatch':
        argv = ['update', '--post-swap', str(work / 'old-handoff')]
    elif case == 'broaden':
        argv = ['update', '--maintenance-scope', 'all']
    elif case == 'invalid':
        argv = ['update', '--maintenance-scope', 'wrong']
    elif case == 'no-permission':
        journal.unlink()
        argv = ['update', '--maintenance-scope', 'default']
    elif case in {'all', 'all-fault'}:
        argv = ['update', '--maintenance-scope', 'all']
    if case in {'late-publication', 'fallback-publication'}:
        journal.unlink()  # a publisher appears between preflight and real prepare_launch
    context = {'case': case, 'home': str(home), 'core': str(core), 'argv': argv,
        'journal': str(journal), 'publication': publication, 'readonly': readonly,
        'before': fingerprint(home / 'profiles'),
        'journal_before': publication.encode().hex() if case in {'late-publication', 'fallback-publication'} else (journal.read_bytes().hex() if journal.exists() else None)}
    (work / 'context.json').write_text(json.dumps(context))
    command = [sys.executable, str(pathlib.Path(__file__).resolve()), '--run', str(work)]
    if readonly:
        command = ['bwrap', '--die-with-parent', '--unshare-all', '--new-session', '--bind', '/', '/',
                   '--proc', '/proc', '--dev', '/dev', '--ro-bind', str(home / 'profiles'), str(home / 'profiles'), *command]
    return subprocess.call(command)


def run(work):
    c = json.loads((work / 'context.json').read_text())
    home, core, journal = (pathlib.Path(c[k]) for k in ('home', 'core', 'journal'))
    sys.path.insert(0, str(core))
    os.environ.update(HOME=str(home.parent), HERMES_HOME=str(home))
    if c['case'] in {'all-fault', 'fallback-publication'}:
        import pm
        def offline_query(**kwargs):
            if c['case'] == 'fallback-publication':
                journal.write_text(c['publication'])
            raise RuntimeError('fixture external PM query unavailable')
        pm.venv_is_current = offline_query
    writes, trace = [], []
    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            write = isinstance(mode, str) and any(x in mode for x in 'wax+') or isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        else:
            write = event in {'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.chmod', 'os.utime', 'os.link', 'os.symlink'}
        if write and any(isinstance(a, (str, bytes, os.PathLike)) and str(home / 'profiles') in os.fsdecode(a) for a in args):
            writes.append({'event': event, 'args': [str(a) for a in args]})
    def profile(frame, event, arg):
        if event == 'call' and frame.f_code.co_name in {'resolve_scope', 'prepare_launch', '_settle_interrupted_update', 'restore_interrupted_pull', 'recover_if_needed', 'activate_dependencies', 'recover_publication', '_continue_legacy_post_swap'}:
            trace.append(frame.f_code.co_name)
            if c['case'] == 'late-publication' and frame.f_code.co_name == 'prepare_launch':
                journal.write_text(c['publication'])
    sys.addaudithook(audit)
    sys.setprofile(profile)
    sys.argv = ['hermes', *c['argv']]
    code = 0
    try:
        import hermes_bootstrap
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise  # retain the native failure code, never label refusal as success
    finally:
        sys.setprofile(None)
        result = {'case': c['case'], 'code': code, 'trace': trace, 'foreign_writes': writes,
                  'before': c['before'], 'after': fingerprint(home / 'profiles'),
                  'readonly': c['readonly'], 'journal_before': c['journal_before'],
                  'journal_after': journal.read_bytes().hex() if journal.exists() else None,
                  'bootstrap_sha256': hashlib.sha256((core / 'hermes_bootstrap.py').read_bytes()).hexdigest()}
        (work / 'probe.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    if sys.argv[1] == '--run':
        run(pathlib.Path(sys.argv[2]))
    else:
        raise SystemExit(setup(sys.argv[1], pathlib.Path(sys.argv[2]), '--readonly' in sys.argv[3:]))
