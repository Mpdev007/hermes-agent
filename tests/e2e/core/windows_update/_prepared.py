"""Stdlib driver for the installed prepared entry; never imported by the product.

The fixture supplies a topology bootstrap (native checkout join, direct child spawn), not a PM
sync. Tracing parks the native entry before activation; the driver parks it again after the
installed main() has returned. The error control supplies an invalid fixture plan; custody and
error finalization stay real. The exhausted cell's audit hook is armed before the first product
import, and its request carries the parent's bootstrap-only job record.
"""

from __future__ import annotations

DRIVER = r'''
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

root, private, mode, cell = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
AUDITED = mode == 'prepared' and cell == 'exhausted'
LOCK = []  # the resolved native lock path, once the product module that names it is imported

def emit(event, **data):
    with (private / (mode + '.jsonl')).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(event=event, pid=os.getpid(), **data)) + '\n')

def audit(event, args):
    # An exhaustion refusal must not attempt activation, process creation or a shared write.
    # Opening the existing native lock read/write is necessary to try a lease, and changes no bytes;
    # before the lock path is known (product imports) even that open counts as a shared write.
    # A native DLL write is outside this Python audit; no external child may be started either.
    if event in ('subprocess.Popen', 'os.system', 'os.startfile'):
        emit('forbidden-process', audit=event)
        raise AssertionError('refused prepared child attempted a subprocess')
    targets = ()
    if event == 'open':
        path, access, flags = args
        if (isinstance(access, str) and any(c in access for c in 'wax+')) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            targets = (path,)
    elif event in ('os.rename', 'os.link', 'os.symlink'):
        targets = args[:2]
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.truncate', 'os.utime'):
        targets = args[:1]
    shared = Path(os.environ['HERMES_HOME']).resolve()
    for path in targets:
        if isinstance(path, (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(path)).resolve()
            if event == 'open' and LOCK and target == LOCK[0] and not flags & (os.O_TRUNC | os.O_APPEND):
                emit('lock-open', flags=flags)
                continue
            if target == shared or shared in target.parents:
                emit('forbidden-write', audit=event, target=str(target))
                raise AssertionError('refused prepared child attempted a shared write')

if AUDITED:
    # Before the first product import, so an import-time process or shared write is observed too.
    # Interpreter startup (-I -S) and this driver's own stdlib imports above precede it.
    sys.addaudithook(audit)
    emit('audit-armed', product_loaded=sorted(
        name for name in sys.modules if name.split('.')[0] in ('hermes_cli', 'hermes_agent', 'pm')))
sys.path.insert(0, str(root))
from hermes_cli import update_completion as completion, update_lock as lock
if AUDITED:
    LOCK.append(lock.checkout_lock_path(root).resolve())

def gate(name, timeout=180):
    deadline = time.monotonic() + timeout
    while not (private / name).exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('fixture gate timed out: ' + name)
        threading.Event().wait(0.05)

def held():
    record = lock._HELD
    return dict(path=None if record is None else record['path'],
                fd=None if record is None else record['fd'],
                lease=None if record is None else record.get('lease'),
                depth=None if record is None else record['depth'],
                join=completion._checkout_join(root))

def member(proc, job):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    api.IsProcessInJob.restype = wintypes.BOOL
    answer = wintypes.BOOL()
    if not api.IsProcessInJob(int(proc._handle), job, ctypes.byref(answer)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(answer.value)

def spawn(child_mode):
    log = (private / (child_mode + '.log')).open('wb')
    try:
        proc = subprocess.Popen(
            [sys.executable, '-I', '-S', '-B', '-u', '-X', 'utf8', sys.argv[0],
             str(root), str(private), child_mode, cell],
            cwd=root, env=dict(os.environ), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        log.close()
    return proc

def owner():
    assert lock._acquire_checkout(root) is None
    from hermes_cli.update_receipt import UpdateReceipt
    receipt = UpdateReceipt().data
    request = dict(schema=1, source=str(root), home=os.environ['HERMES_HOME'],
                   receipt=receipt, maintenance_scope=dict(mode='all', home=None),
                   gateway_mode=False, windows_resume=None, desktop=False, assume_yes=True,
                   no_gateway_restart=True, branch='main', snapshot_id=None,
                   pre_update_version=None, sibling_snapshots={}, plan=None,
                   bytecode_cache=str(private / 'bytecode'))
    if cell == 'tail-error':
        request['plan'] = {'fixture_prepared_tail_error': True}
    if cell == 'exhausted':
        # The record _bind_and_resume writes for a bound bootstrap, in the request the prepared child reads too.
        # It attests only the bootstrap: the prepared child must still refuse its ancestor-only join.
        request[completion._UPDATE_JOB] = completion._UPDATE_JOB_BOUND
    (private / 'request.json').write_text(json.dumps(request), encoding='utf-8')
    job = lock.update_tree_job()
    proc = spawn('bootstrap')
    emit('spawn', child=proc.pid, child_in_owner_job=member(proc, job), update_id=receipt['update_id'])
    gate('owner-exit', 1800)

def bootstrap():
    # This process deliberately receives no job binding; the owner proves that with IsProcessInJob.
    request = json.loads((private / 'request.json').read_text(encoding='utf-8'))
    completion._join_for_preparation(request)
    extra = []
    try:
        if cell == 'exhausted':
            fd = os.open(lock.checkout_lock_path(root), os.O_RDWR | os.O_BINARY)
            while (offset := lock._take_lease(fd)) is not None:
                extra.append(offset)
            emit('exhausted', count=len(extra) + 1, slots=lock._LEASE_SLOTS, held=held())
        emit('ready', held=held())
        gate('spawn-prepared')
        job = lock.update_tree_job()
        proc = spawn('prepared')
        emit('spawn', child=proc.pid, child_in_bootstrap_job=member(proc, job))
        proc.wait(timeout=1500)
        emit('child-exit', code=proc.returncode)
        gate('bootstrap-exit')
    finally:
        if cell == 'exhausted':
            for offset in extra:
                lock._unlock(fd, offset)
            os.close(fd)

def prepared():
    # Run the installed main and --prepared branch, with no replacements for lock/PM/receipt code.
    request, result = private / 'request.json', private / 'result.json'
    paths = ['hermes_cli/update_completion.py', 'hermes_cli/update_lock.py',
             'hermes_cli/maintenance_scope.py', 'hermes_cli/_startup_fast.py']
    emit('target', root=str(root), executable=sys.executable,
         modules=dict(completion=completion.__file__, lock=lock.__file__),
         blobs={p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths})
    activation_file = str(root / 'pm/environments.py')

    def trace(frame, event, value):
        name, filename = frame.f_code.co_name, frame.f_code.co_filename
        if event == 'call' and name == 'activate_dependencies' and filename == activation_file:
            emit('activation', held=held())
            gate('activate')
        if event == 'return' and frame.f_code is completion._write_json.__code__:
            if frame.f_locals.get('path') == result and result.exists():
                emit('result', held=held())
        return trace

    sys.settrace(trace)
    sys.argv = [str(root / 'hermes_cli/update_completion.py'), str(request), str(result), '--prepared']
    code = completion.main()
    # Parked after the installed main() returned: a lease released on its way out shows here.
    emit('exit', code=code, held=held())
    gate('prepared-exit')
    return code

def contender():
    refusal = lock._acquire_checkout(root)
    # holder_held: the refusal's native classification (recorded owner gone, the lock still held).
    emit('contend', acquired=refusal is None, held=held(),
         reason=None if refusal is None else refusal.reason, holder=None if refusal is None else refusal.pid,
         holder_held=None if refusal is None else refusal.held)
    return 0 if refusal is None else 2

try:
    if mode == 'owner':
        owner()
    elif mode == 'bootstrap':
        bootstrap()
    elif mode == 'prepared':
        raise SystemExit(prepared())
    elif mode == 'contender':
        raise SystemExit(contender())
    else:
        raise ValueError(mode)
finally:
    if mode != 'prepared':
        lock._release_checkout()
'''
