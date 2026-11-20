"""Data-only maintenance scope; build/PM selection remains shared."""
import argparse
import json
from pathlib import Path
from unittest.mock import Mock
import pytest

@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    foreign = root / 'profiles' / 'foreign'
    foreign.mkdir(parents=True)
    for h in (root, foreign):
        (h / 'config.yaml').write_text('config_version: 1\nmodel:\n  default: fixture-model\n')
        (h / '.env').write_text('FIXTURE=only\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(root))
    return root, foreign

def test_public_update_parser():
    from hermes_cli.subcommands.update import build_update_parser
    p = argparse.ArgumentParser()
    build_update_parser(p.add_subparsers(), cmd_update=lambda args: None)
    assert p.parse_args(['update','--maintenance-scope','default']).maintenance_scope == 'default'
    assert p.parse_args(['update']).maintenance_scope is None
    with pytest.raises(SystemExit):
        p.parse_args(['update','--maintenance-scope','foreign'])

def test_default_scope_bound_to_actual_home(homes, monkeypatch):
    from hermes_cli.maintenance_scope import MaintenanceScope
    root, foreign = homes
    scope = MaintenanceScope.select('default')
    assert scope.to_dict() == {'mode': 'default', 'home': str(root.resolve())}
    assert scope.homes([root, foreign]) == [root]
    monkeypatch.setenv('HERMES_HOME',str(foreign))
    with pytest.raises(ValueError):
        MaintenanceScope.select('default')
    with pytest.raises(ValueError):
        scope.validate()

def test_all_compatibility(homes):
    from hermes_cli.maintenance_scope import MaintenanceScope
    assert MaintenanceScope.select('all').homes(list(homes)) == list(homes)

def test_pending_rearm_cannot_broaden(homes, tmp_path):
    from hermes_cli.maintenance_scope import MaintenanceScope, resolve_scope
    from hermes_cli.venv_sync import arm_completion
    checkout = tmp_path/'source';checkout.mkdir()
    scope = MaintenanceScope.select('default')
    marker = arm_completion(checkout, maintenance_scope=scope)
    assert json.loads(marker.read_text())['maintenance_scope'] == scope.to_dict()
    arm_completion(checkout)
    assert resolve_scope(checkout).to_dict() == scope.to_dict()
    with pytest.raises(ValueError):
        resolve_scope(checkout, 'all')

def test_manifest_home_mismatch(homes,tmp_path):
    from hermes_cli.maintenance_scope import MaintenanceScope, resolve_scope
    from hermes_cli.venv_sync import completion_pending_path
    marker=completion_pending_path(tmp_path/'source');marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text(json.dumps({'schema':1,'maintenance_scope':{'mode':'default','home':str(homes[1])}}))
    with pytest.raises(ValueError):resolve_scope(tmp_path/'source')

def test_product_migrations_filtered_not_dependency_union(homes, monkeypatch):
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope
    from hermes_cli import memory_provider_migration as memory, left_core_migration as left
    from pm.plugins_state import dependency_homes
    seen=[]
    monkeypatch.setattr(memory,'_pending_provider',lambda h,**kw: seen.append(('memory',h)))
    monkeypatch.setattr(left,'_pending',lambda h,**kw: seen.append(('left',h)) or [])
    with maintenance_scope(MaintenanceScope.select('default')):
        memory.migrate_all_homes(); left.migrate_all_homes()
        assert dependency_homes()==list(homes)
    assert seen==[('memory',homes[0]),('left',homes[0])]

def test_profile_sync_does_not_backfill_foreign(homes):
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope
    from hermes_cli.update_cmd_maint import _sync_profiles_after_update
    env=homes[1]/'.env';env.unlink()
    before=(homes[1]/'config.yaml').read_bytes()
    with maintenance_scope(MaintenanceScope.select('default')):
        _sync_profiles_after_update()
    assert not env.exists()
    assert (homes[1]/'config.yaml').read_bytes()==before

def test_state_guard_and_catalog_skip_foreign(homes,monkeypatch):
    from hermes_cli.maintenance_scope import MaintenanceScope, maintenance_scope
    from hermes_cli import update_cmd_maint as maint, plugin_catalog
    seen=[]
    monkeypatch.setattr(maint,'_verify_and_restore_one_state_db',lambda h,**kw:seen.append(('db',h)))
    monkeypatch.setattr(plugin_catalog,'invalidate_live_cache_for_home',lambda h:seen.append(('catalog',h)))
    with maintenance_scope(MaintenanceScope.select('default')):
        maint._verify_and_restore_state_dbs_post_update();maint._invalidate_live_plugin_catalog_caches()
    assert seen==[('db',homes[0]),('catalog',homes[0])]

@pytest.mark.parametrize('body',['', '{"schema":1', '{"schema":2}', 'not a scope'])
def test_corrupt_pending_never_widens(homes,tmp_path,body):
    from hermes_cli.venv_sync import completion_pending_path, arm_completion
    marker=completion_pending_path(tmp_path/'src');marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text(body)
    with pytest.raises(ValueError):arm_completion(tmp_path/'src')
    assert marker.read_text()==body

def test_native_bootstrap_flag_survives_reexec(homes,tmp_path,monkeypatch):
    from hermes_cli import source_completion as sc
    from pm import environments
    root=tmp_path/'src';(root/'hermes_cli').mkdir(parents=True)
    (root/'hermes_cli/source_completion.py').write_text('# fixture identity\n')
    calls=[]
    monkeypatch.setattr(environments,'project_python',lambda root:Path('/fixture/python'))
    monkeypatch.setattr(environments,'activation_environment',lambda root:{})
    monkeypatch.setattr(sc.subprocess,'call',lambda cmd,**kw:calls.append(cmd) or 0)
    assert sc.main(['--source',str(root),'--maintenance-scope','default','--finish-update'])==0
    assert calls[0][-1]=='--prepared'
    assert calls[0][calls[0].index('--maintenance-scope')+1]=='default'
    assert '--finish-update' in calls[0]

def test_pending_resume_ignores_newer_completion_stamp(homes,tmp_path,monkeypatch):
    from hermes_cli import venv_sync as vs, update_custody
    from hermes_cli.maintenance_scope import MaintenanceScope
    from pm import environments
    from types import SimpleNamespace
    root=tmp_path/'source';root.mkdir()
    marker=vs.arm_completion(root, maintenance_scope=MaintenanceScope.select('default'))
    monkeypatch.setattr(vs,'_tree_matches_completed_stamp',lambda root:True)
    monkeypatch.setattr(environments,'activation_environment',lambda root:{})
    calls=[]
    monkeypatch.setattr(update_custody,'run',lambda cmd,**kw:calls.append((cmd,kw)) or SimpleNamespace(returncode=0))
    vs._finish_source_update(root,current=True,pending=marker)
    assert calls[0][0][calls[0][0].index('--maintenance-scope')+1]=='default'
    assert calls[0][1]['inherit_lock'] is True
    assert not marker.exists()

def test_update_dispatch_binds_scope_and_rejects_named_before_entry(homes,tmp_path,monkeypatch):
    from hermes_cli import main
    from hermes_cli.maintenance_scope import current_scope
    from types import SimpleNamespace
    monkeypatch.setattr(main,'PROJECT_ROOT',tmp_path/'source')
    seen=[]
    monkeypatch.setattr(main,'_cmd_update_scoped',lambda args:seen.append(current_scope().to_dict()))
    args=SimpleNamespace(maintenance_scope='default',no_gateway_restart=True,gateway=False)
    main.cmd_update(args)
    assert seen==[{'mode':'default','home':str(homes[0])}]
    monkeypatch.setenv('HERMES_HOME',str(homes[1]))
    with pytest.raises(ValueError):main.cmd_update(args)
    assert len(seen)==1

def test_launch_preparser_scopes_before_any_repair(homes,tmp_path,monkeypatch):
    from hermes_cli import venv_sync as vs
    from hermes_cli.maintenance_scope import current_scope
    seen=[]
    monkeypatch.setattr(vs,'_prepare_launch_scoped',lambda root,argv:seen.append(current_scope().mode))
    vs.prepare_launch(tmp_path/'src',['update','--maintenance-scope=default','--no-gateway-restart'])
    assert seen==['default']
    with pytest.raises(ValueError):vs.prepare_launch(tmp_path/'src',['update','--maintenance-scope','default'])

def test_borrowed_checkout_and_publication_recovery_refused(homes,tmp_path,monkeypatch):
    from hermes_cli.maintenance_scope import resolve_scope
    from pm import environments
    root=tmp_path/'src'
    monkeypatch.setattr(environments,'owning_home_root',lambda root:homes[1])
    with pytest.raises(ValueError,match='borrowed'):resolve_scope(root,'default')
    monkeypatch.setattr(environments,'owning_home_root',lambda root:None)
    journal=environments.install_state_dir(root)/'publication.json';journal.parent.mkdir(parents=True)
    journal.write_text('{}')
    with pytest.raises(ValueError,match='publication'):resolve_scope(root,'default')
    assert journal.read_text()=='{}'

def test_scoped_plugin_failure_remains_owed(homes,tmp_path,monkeypatch):
    from hermes_cli.maintenance_scope import maintenance_scope,MaintenanceScope
    from hermes_cli import source_build as build, memory_provider_migration as memory
    from hermes_cli import left_core_migration as left, main_install_repair
    monkeypatch.setattr(main_install_repair,'_install_configured_features_missing_deps',lambda root:None)
    monkeypatch.setattr(build,'source_frontends',lambda root:())
    monkeypatch.setattr(memory,'_pending_provider',lambda home,**kw:'hindsight')
    monkeypatch.setattr(memory,'_home_consent',lambda home:False)
    monkeypatch.setattr(memory,'_install_into',lambda home:lambda name:{'ok':False,'error':'fixture failure'})
    monkeypatch.setattr(left,'migrate_all_homes',lambda:[])
    with maintenance_scope(MaintenanceScope.select('default')):
        with pytest.raises(build.ProductBuildError):build.build_update_products(tmp_path,desktop=False)

def test_shared_union_keeps_foreign_enabled_selections(homes):
    from hermes_cli.maintenance_scope import maintenance_scope,MaintenanceScope
    from pm.plugins_state import enabled_plugins_ordered
    for n,h in enumerate(homes):
        (h/'config.yaml').write_text(f'plugins:\n  enabled: [fixture-{n}]\n')
    with maintenance_scope(MaintenanceScope.select('default')):
        assert enabled_plugins_ordered()=={homes[0]/'plugins':['fixture-0'],homes[1]/'plugins':['fixture-1']}

def test_source_tail_lock_is_real_in_default_scope(homes,tmp_path,monkeypatch):
    import os
    from hermes_cli import source_completion,source_build,venv_sync,update_cmd_maint,source_stamp
    from hermes_cli.update_lock import read_live_update
    seen=[]
    def build(root,**kw):
        seen.append(read_live_update(install_root=root).pid)
    monkeypatch.setattr(source_build,'build_update_products',build)
    monkeypatch.setattr(venv_sync,'publish_launchers',lambda root:None)
    monkeypatch.setattr(update_cmd_maint,'_run_post_update_maintenance',lambda **kw:True)
    monkeypatch.setattr(source_stamp,'write_source_stamp',lambda root:None)
    root=tmp_path/'src';root.mkdir()
    assert source_completion.complete_source_checkout(root,desktop=False,assume_yes=True,maintenance_scope='default')
    assert seen==[os.getpid()]
    assert not venv_sync.completion_pending_path(root).exists()

@pytest.mark.parametrize('payload',[None,b'',b'MAINTENANCE_SCOPE_SCHEMA = 0',b'bad syntax !'])
def test_target_without_scope_protocol_is_refused(homes,payload):
    from hermes_cli.maintenance_scope import MaintenanceScope,maintenance_scope,require_scope_target
    with maintenance_scope(MaintenanceScope.select('default')):
        with pytest.raises(ValueError,match='target'):require_scope_target(payload)

def test_compatible_target_capability_is_read_not_executed(homes):
    from hermes_cli.maintenance_scope import MaintenanceScope,maintenance_scope,require_scope_target
    with maintenance_scope(MaintenanceScope.select('default')):
        require_scope_target(b'MAINTENANCE_SCOPE_SCHEMA = 1\nraise RuntimeError("untrusted target must not execute")\n')

def test_git_commit_point_refuses_old_target_before_pause_or_obligations(homes,tmp_path,monkeypatch):
    import subprocess
    from hermes_cli import update_cmd_commit as commit,update_cmd_posix_pause as pause,venv_sync
    from hermes_cli.maintenance_scope import MaintenanceScope,maintenance_scope
    root=tmp_path/'target';root.mkdir();(root/'old.txt').write_text('old tree')
    for args in [('init','-q'),('add','.'),('-c','user.name=Scope Test','-c','user.email=scope@example.invalid','-c','commit.gpgsign=false','commit','-qm','old')]:
        subprocess.run(['git',*args],cwd=root,check=True,capture_output=True)
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    def forbidden():raise AssertionError('pause must not run before capability refusal')
    monkeypatch.setattr(pause,'pause_at_commit_point',forbidden)
    with maintenance_scope(MaintenanceScope.select('default')):
        refusal=commit.arm_commit_point(['git'],root,sha,pre=sha,target=sha,stash=None)
    assert 'target does not support' in refusal
    assert not venv_sync.completion_pending_path(root).exists()
    assert (root/'old.txt').read_text()=='old tree'

@pytest.mark.parametrize('mode,evict',[('all',True),('default',False)])
def test_native_dependency_prepare_retains_union_without_foreign_eviction(homes,tmp_path,monkeypatch,mode,evict):
    import pm
    from pm import client,receipt
    from hermes_cli import update_completion,gitlock,venv_sync
    from hermes_cli.maintenance_scope import MaintenanceScope
    root=tmp_path/'src';root.mkdir()
    scope=MaintenanceScope.select(mode)
    request={'source':str(root),'receipt':{'update_id':'a'*32},'maintenance_scope':scope.to_dict()}
    monkeypatch.setattr(client,'ensure_tools_for_sync',lambda:None)
    monkeypatch.setattr(gitlock,'convert_treeless_checkout_first',lambda root:None)
    monkeypatch.setattr(receipt,'last_for_update',lambda uid:None)
    calls=[]
    class StopAtSync(Exception):pass
    def sync(extras, **kwargs):
        calls.append({"extras": extras, **kwargs})
        raise StopAtSync
    monkeypatch.setattr(pm,'sync_venv',sync)
    with pytest.raises(StopAtSync):update_completion._prepare(request,tmp_path/'request.json',tmp_path/'result.json')
    from pm.extras import first_generation_selection
    assert calls==[{'extras': first_generation_selection(root), 'explicit':True,
                   'project_root':root,'evict_incompatible_plugins':evict}]
    assert json.loads((tmp_path/'request.json').read_text())['maintenance_scope']==scope.to_dict()
    if mode=='default':assert json.loads(venv_sync.completion_pending_path(root).read_text())['maintenance_scope']==scope.to_dict()

def test_native_update_request_freezes_scope(homes,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import main,update_cmd,update_receipt
    from hermes_cli.maintenance_scope import MaintenanceScope,maintenance_scope
    monkeypatch.setattr(main,'PROJECT_ROOT',tmp_path/'src')
    opts=SimpleNamespace(assume_yes=True,pre_update_version='fixture',no_gateway_restart=True)
    scope=MaintenanceScope.select('default')
    with maintenance_scope(scope):
        request=update_cmd._source_completion_request(opts,None,None,None,False,False)
    assert request['maintenance_scope']==scope.to_dict()
    assert request['home']==str(homes[0])
    update_receipt._current.set(None)

def test_completion_rechecks_pending_scope_after_lock_acquisition(homes,tmp_path,monkeypatch):
    from hermes_cli import source_completion,venv_sync
    from hermes_cli.maintenance_scope import MaintenanceScope
    root=tmp_path/'src';root.mkdir()
    # The first resolver observed no marker. Another updater armed default scope
    # before this caller obtained custody: its cached all scope cannot overwrite it.
    cached=MaintenanceScope.select('all')
    venv_sync.arm_completion(root,maintenance_scope=MaintenanceScope.select('default'))
    writes=[]
    monkeypatch.setattr(source_completion,'_complete_scoped',lambda root,**kw:writes.append(root) or True)
    with pytest.raises(ValueError,match='broaden'):
        source_completion._complete_locked(root,maintenance_scope=cached)
    assert writes==[]


@pytest.mark.parametrize("mode", ["all", "default"])
@pytest.mark.parametrize("frontends", [(), ("ui-tui",)])
def test_scoped_product_build_keeps_upstream_dependency_callback(homes, tmp_path, monkeypatch, mode, frontends):
    from hermes_cli import source_build as build, main_install_repair, memory_provider_migration, left_core_migration
    from hermes_cli.maintenance_scope import MaintenanceScope, current_scope
    root = tmp_path / "source"
    root.mkdir()
    scope = MaintenanceScope.select(mode)
    events = []
    def record(name):
        assert current_scope() == scope
        events.append(name)
    monkeypatch.setattr(main_install_repair, "_install_configured_features_missing_deps", lambda root: record("python"))
    monkeypatch.setattr(build, "source_frontends", lambda root: frontends)
    monkeypatch.setattr(build, "source_build_env", lambda **kw: {})
    monkeypatch.setattr(build, "prepare_source_dependencies", lambda *a, **kw: record("node"))
    monkeypatch.setattr(build, "source_product_current", lambda *a: False)
    monkeypatch.setattr(build, "build_source_tui", lambda *a, **kw: record("tui"))
    monkeypatch.setattr(memory_provider_migration, "migrate_all_homes", lambda: record("memory"))
    monkeypatch.setattr(left_core_migration, "migrate_all_homes", lambda: record("left-core"))
    build.build_update_products(root, desktop=False, maintenance_scope=scope,
                                after_dependencies=lambda: record("after-dependencies"))
    assert events == (["python", "node", "after-dependencies", "tui", "memory", "left-core"]
                      if frontends else ["python", "after-dependencies", "memory", "left-core"])
    assert current_scope().mode == "all"


@pytest.mark.parametrize("refusing_stage", ["python", "memory", "left-core"])
def test_product_build_preserves_typed_fatal_scope_refusal(homes, tmp_path, monkeypatch, refusing_stage):
    from hermes_cli import source_build as build, main_install_repair, memory_provider_migration, left_core_migration
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal
    root = tmp_path / "source"
    root.mkdir()
    events = []
    refusal = ScopeSafetyRefusal("fixture late publication authority refused")
    def step(name):
        events.append(name)
        if name == refusing_stage:
            raise refusal
    monkeypatch.setattr(main_install_repair, "_install_configured_features_missing_deps", lambda root: step("python"))
    monkeypatch.setattr(build, "source_frontends", lambda root: ())
    monkeypatch.setattr(memory_provider_migration, "migrate_all_homes", lambda: step("memory"))
    monkeypatch.setattr(left_core_migration, "migrate_all_homes", lambda: step("left-core"))
    with pytest.raises(ScopeSafetyRefusal) as caught:
        build.build_update_products(root, desktop=False, maintenance_scope="default",
                                    after_dependencies=lambda: step("after-dependencies"))
    assert caught.value is refusal
    order = ["python", "after-dependencies", "memory", "left-core"]
    assert events == order[:order.index(refusing_stage) + 1]


@pytest.mark.parametrize("refusing_stage", ["launchers", "build", "maintenance"])
def test_source_tail_stops_on_scope_refusal_and_retains_pending(homes, tmp_path, monkeypatch, refusing_stage):
    from hermes_cli import source_build, source_completion, source_stamp, update_cmd_maint, venv_sync
    from hermes_cli.maintenance_scope import ScopeSafetyRefusal, MaintenanceScope
    root = tmp_path / "source"
    root.mkdir()
    events = []
    refusal = ScopeSafetyRefusal("fixture native scope authority refused")
    def step(name):
        events.append(name)
        if name == refusing_stage:
            raise refusal
        return True
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda root: step("launchers"))
    monkeypatch.setattr(source_build, "build_update_products", lambda *a, **kw: step("build"))
    monkeypatch.setattr(update_cmd_maint, "_run_post_update_maintenance", lambda **kw: step("maintenance"))
    monkeypatch.setattr(source_stamp, "write_source_stamp", lambda root: step("stamp"))
    with pytest.raises(ScopeSafetyRefusal) as caught:
        source_completion.complete_source_checkout(root, desktop=False, assume_yes=True,
                                                   maintenance_scope="default")
    assert caught.value is refusal
    order = ["launchers", "build", "maintenance", "stamp"]
    assert events == order[:order.index(refusing_stage) + 1]
    assert json.loads(venv_sync.completion_pending_path(root).read_text())["maintenance_scope"] == MaintenanceScope.select("default").to_dict()
