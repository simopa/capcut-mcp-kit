# Added in capcut-mcp-kit (2026): fifth-review regressions, isolated synthetic projects only.
# See NOTICE at the repository root.
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import draft_store as ds
import existing_project as ep
import save_draft_impl as sd
from test_commit_safety import call, client, env, png
from test_existing_project import COPIES, HOST
from test_crash_recovery import crash_when, existing_with_media, mixed_copies


def new(client):
    d=call(client,'/create_draft',width=1080,height=1920)['output']['draft_id']
    assert call(client,'/add_text',draft_id=d,text='Old',start=0,end=1)['success']
    return d


def test_backup_destination_is_not_proof_of_completed_backup(env,client):
    d=new(client)
    assert call(client,'/save_draft',draft_id=d,project_name='Backup')['success']
    # Only the previous project holds this user's file.
    root=env.projects/'Backup'
    (root/'irreplaceable.txt').write_text('original only')
    assert call(client,'/add_text',draft_id=d,text='New',start=1,end=2)['success']
    code=f"""
import os
import save_draft_impl as sd
sd.find_capcut_projects_dir=lambda: {str(env.projects)!r}
sd.capcut_is_running=lambda: False
real=sd.journal_update
def die_after_backup_reservation(*a,**kw):
    result=real(*a,**kw)
    details=kw.get('details') or {{}}
    if details.get('phase')=='ready' and details.get('pairs',[{{}}])[0].get('backup'):
        os._exit(73)
    return result
sd.journal_update=die_after_backup_reservation
import capcut_server
capcut_server.app.test_client().post('/save_draft',json={{'draft_id':{d!r},'project_name':'Backup'}},headers={HOST!r})
"""
    result=subprocess.run([sys.executable,'-c',code],cwd=os.path.dirname(sd.__file__),env=os.environ.copy(),capture_output=True)
    assert result.returncode==73,result.stderr
    details=ds.journal_pending(d)[0][3]
    p=details['pairs'][0]
    assert Path(p['aside'],'irreplaceable.txt').exists()
    destination=Path(p['backup'])
    assert not destination.exists()
    destination.mkdir()
    (destination/'unrelated.txt').write_text('unrelated folder appeared while backend stopped')
    sd.recover_saves()
    survivors=list(env.tmp.rglob('irreplaceable.txt'))
    assert survivors,'ONLY ORIGINAL DELETED: existence of unrelated backup directory was accepted as completed copy'


def test_old_target_content_is_checked_before_rolling_back_other_copy(env,client):
    d=new(client)
    export=env.tmp/'export';export.mkdir()
    opts=dict(project_name='OldChanged',draft_folder=str(export))
    assert call(client,'/save_draft',draft_id=d,**opts)['success']
    assert call(client,'/add_image',draft_id=d,image_url=png(env.tmp/'red.png',(255,0,0)),start=0,end=1)['success']
    details=crash_when(env,d,lambda x:x['phase']=='ready' and [sd._pair_state(p) for p in x['pairs']]==['new','old'],**opts)
    first,second=details['pairs']
    # User takes the exported new timeline into the still-old CapCut project; its directory inode stays the same.
    new_bytes=Path(first['target'],'draft_info.json').read_bytes()
    target=Path(second['target'])
    for rel in ('draft_info.json','template-2.tmp'):
        (target/rel).write_bytes(new_bytes)
    photos=[v for v in json.loads(new_bytes)['materials']['videos'] if v.get('type')=='photo']
    assert len(photos)==1
    media=Path(photos[0]['path']);assert media.exists()
    assert sd._pair_state(second) != 'old'
    sd.recover_saves()
    assert media.exists(),'NEW EXPORT MEDIA DELETED: edited old-inode target still references it'


def test_large_reference_file_is_not_evidence_of_no_references(env,client):
    d,root=existing_with_media(env,client,'Large')
    details=crash_when(env,d,mixed_copies(root))
    media=root/details['added'][0]['rel'];assert media.exists()
    other=root/'Timelines'/'OTHER';other.mkdir()
    ref=other/'draft_info.json'
    # Valid JSON followed by legal whitespace; the reference is at the start, not beyond the cutoff.
    raw=json.dumps({'materials':{'videos':[{'path':str(media)}]}}).encode()
    with ref.open('wb') as f:
        f.write(raw)
        block=b' '*(1024*1024)
        for _ in range(257): f.write(block)
    assert ref.stat().st_size>256*1024*1024
    sd.recover_saves()
    assert media.exists(),'REFERENCED MEDIA DELETED because valid timeline file exceeds 256 MiB'


def test_escaped_json_reference_is_preserved(env,client):
    d,root=existing_with_media(env,client,'Escaped')
    details=crash_when(env,d,mixed_copies(root))
    media=root/details['added'][0]['rel'];assert media.exists()
    other=root/'Timelines'/'OTHER';other.mkdir()
    data=json.dumps({'materials':{'videos':[{'path':str(media)}]}})
    data=data.replace(media.name, '\\u0069'+media.name[1:])
    assert json.loads(data)['materials']['videos'][0]['path']==str(media)
    (other/'draft_info.json').write_text(data)
    sd.recover_saves()
    assert media.exists(),'REFERENCED MEDIA DELETED because JSON escaping hides literal basename'


@pytest.mark.parametrize('change', ['changed', 'deleted'])
def test_changed_added_media_blocks_forward_reconciliation(env,client,change):
    d,root=existing_with_media(env,client,'MediaChanged')
    details=crash_when(env,d,lambda x:x['phase']=='ready' and all(ep._sha((root/rel).read_bytes())==h['new'] for rel,h in x['copies'].items()) and ep._sha((root/'draft_meta_info.json').read_bytes())==x['meta']['old'])
    media=root/details['added'][0]['rel']
    if change=='changed':
        png(media,(0,0,255))
    else:
        media.unlink()
    before=(root/'draft_meta_info.json').read_bytes()
    notes=sd.recover_saves()
    assert (root/'draft_meta_info.json').read_bytes()==before,'METADATA COMPLETED despite added media differing from journalled hash'
    assert ds.journal_unsettled(d), 'CONFLICT not exposed'


def test_symlink_in_planned_path_is_abandoned_not_permanently_pending(env,client):
    d,root=existing_with_media(env,client,'ChildLink')
    details=crash_when(env,d,mixed_copies(root))
    selected=root/'Timelines'/'TL-1'/'draft_info.json'
    outside=env.tmp/'timeline-outside.json';selected.rename(outside);selected.symlink_to(outside)
    for _ in range(3):
        notes=sd.recover_saves()
    assert ds.journal_pending(d)==[],'CONFLICT inspection detected link but temporary cleanup throws and leaves pending forever'
    assert ds.journal_unsettled(d)[0]['state']=='abandoned'


def backup_plan(env):
    """A real journal entry with a complete old version awaiting retirement."""
    source = env.projects / '.capcut-mcp-old-test'
    source.mkdir()
    (source / 'draft_info.json').write_text('{"old":true}')
    (source / 'user.txt').write_text('keep this original')
    dest = env.backups / 'Previous'
    env.backups.mkdir(exist_ok=True)
    pair = {'target': str(env.projects / 'Current'), 'aside': str(source),
            'old': {'exists': True, 'id': sd._identity(source), 'inventory': sd._inventory(source)},
            'backup': str(dest)}
    details = {'phase': 'ready', 'pairs': [pair]}
    op_id = ds.journal_begin('backup-test', 'replace', details)
    return source, dest, pair, details, op_id


def test_a_corrupted_completed_backup_never_authorizes_deleting_the_original(env):
    import shutil
    source, dest, pair, details, op_id = backup_plan(env)
    shutil.copytree(source, dest)
    pair['backup_id'] = sd._identity(dest)
    ds.journal_update(op_id, details=details)
    (dest / 'user.txt').write_text('changed after the copy completed')

    where, warning = sd._retire(op_id, details, pair)

    assert warning and where == str(source)
    assert (source / 'user.txt').read_text() == 'keep this original'
    assert (dest / 'user.txt').read_text() == 'changed after the copy completed'


def test_a_verified_backup_finishes_an_interrupted_removal_of_the_original(env):
    import shutil
    source, dest, pair, details, op_id = backup_plan(env)
    shutil.copytree(source, dest)
    pair['backup_id'] = sd._identity(dest)
    ds.journal_update(op_id, details=details)
    (source / 'draft_info.json').unlink()  # The process stopped inside rmtree.

    where, warning = sd._retire(op_id, details, pair)

    assert warning is None and where == str(dest)
    assert not source.exists()
    assert (dest / 'user.txt').read_text() == 'keep this original'
    assert (dest / 'draft_info.json').read_text() == '{"old":true}'


def test_an_empty_unjournalled_partial_does_not_block_the_backup(env):
    # Stopped right after creating the .partial folder, before journalling it
    source, dest, pair, details, op_id = backup_plan(env)
    partial = Path(str(dest) + '.partial')
    partial.mkdir()

    where, warning = sd._retire(op_id, details, pair)

    assert warning is None and where == str(dest) and not partial.exists() and not source.exists()
    assert (dest / 'user.txt').read_text() == 'keep this original'


def test_a_file_in_a_renamed_folder_is_not_read_again(tmp_path, monkeypatch):
    import builtins
    import util
    folder = tmp_path / '.capcut-mcp-saving-x'
    (folder / 'assets').mkdir(parents=True)
    (folder / 'assets' / 'media.bin').write_bytes(os.urandom(1 << 20))
    first = sd._inventory(folder)
    reads, real_open = [], builtins.open

    def counting(path, mode='r', *a, **k):
        if str(path).endswith('media.bin'):
            reads.append(path)
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr(builtins, 'open', counting)
    folder.rename(tmp_path / 'Project')
    assert sd._inventory(tmp_path / 'Project') == first and reads == []
    (tmp_path / 'Project' / 'assets' / 'media.bin').write_bytes(b'edited')  # a write changes the key
    assert sd._inventory(tmp_path / 'Project') != first and len(reads) == 1


@pytest.mark.parametrize('owned', [False, True], ids=['foreign-partial', 'edited-owned-partial'])
def test_partial_backup_with_unproven_data_is_preserved(env, owned):
    source, dest, pair, details, op_id = backup_plan(env)
    partial = Path(str(dest) + '.partial')
    (partial / 'project').mkdir(parents=True)
    if owned:
        pair['backup_partial_id'] = sd._identity(partial)
        ds.journal_update(op_id, details=details)
    (partial / 'project' / 'user-new.txt').write_text('new work after interruption')

    where, warning = sd._retire(op_id, details, pair)

    assert warning and where == str(source)
    assert (source / 'user.txt').read_text() == 'keep this original'
    assert (partial / 'project' / 'user-new.txt').read_text() == 'new work after interruption'


def test_edits_outside_the_old_timeline_are_a_conflict(env, client):
    d = new(client)
    assert call(client, '/save_draft', draft_id=d, project_name='Sidecar')['success']
    root = env.projects / 'Sidecar'
    details = crash_when(env, d, lambda x: x['phase'] == 'ready', project_name='Sidecar')
    (root / 'key_value.json').write_text('{"user":"changed after crash"}')
    assert sd._pair_state(details['pairs'][0]) == 'foreign'
    sd.recover_saves()
    assert (root / 'key_value.json').read_text() == '{"user":"changed after crash"}'


def test_a_metadata_symlink_also_closes_as_a_conflict(env, client):
    d, root = existing_with_media(env, client, 'MetaLink')
    crash_when(env, d, mixed_copies(root))
    meta = root / 'draft_meta_info.json'
    outside = env.tmp / 'outside-meta.json'
    meta.rename(outside)
    meta.symlink_to(outside)
    before = outside.read_bytes()

    sd.recover_saves()

    assert outside.read_bytes() == before
    assert ds.journal_pending(d) == []
    assert ds.journal_unsettled(d)[0]['state'] == 'abandoned'


def test_mixed_destinations_do_not_remove_a_target_whose_old_version_was_retired(env):
    source, backup, first, details, op_id = backup_plan(env)
    sd.rename_noreplace(source, backup)
    target = Path(first['target'])
    target.mkdir()
    (target / 'draft_info.json').write_text('{"new":true}')
    first.update(stage=str(env.projects / '.stage-a'), failed=str(env.projects / '.failed-a'),
                 in_capcut=True, new={'id': sd._identity(target), 'inventory': sd._inventory(target), 'sha': 'unused'})
    old = env.projects / 'Other'
    old.mkdir()
    (old / 'draft_info.json').write_text('{"old":true}')
    stage = env.projects / '.stage-b'
    stage.mkdir()
    (stage / 'draft_info.json').write_text('{"new":true}')
    second = {'target': str(old), 'stage': str(stage), 'aside': str(env.projects / '.aside-b'),
              'failed': str(env.projects / '.failed-b'), 'in_capcut': True,
              'old': {'exists': True, 'id': sd._identity(old), 'inventory': sd._inventory(old)},
              'new': {'id': sd._identity(stage), 'inventory': sd._inventory(stage), 'sha': 'unused'}}
    details['pairs'].append(second)
    ds.journal_update(op_id, details=details)

    notes = sd._settle_replace(op_id, details, None)

    assert notes and ds.journal_get(op_id)[0] == 'abandoned'
    assert (target / 'draft_info.json').read_text() == '{"new":true}'
    assert (backup / 'user.txt').read_text() == 'keep this original'
    assert (old / 'draft_info.json').read_text() == '{"old":true}'
