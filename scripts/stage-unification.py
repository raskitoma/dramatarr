#!/usr/bin/env python3
"""Stage tracked source and private configuration in a new NAS directory, then build only.

Never rsyncs into a component's existing source or data directory, never restarts services,
and never creates missing legacy mounts. The new staging mount is created explicitly.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',required=True)
    p.add_argument('--remote-source',required=True)
    p.add_argument('--private-config',required=True,type=Path)
    args=p.parse_args()
    root=Path(__file__).resolve().parent.parent
    changes=subprocess.check_output(['git','status','--porcelain'],cwd=root)
    if changes.strip(): raise SystemExit('Commit the reviewed source before staging')
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    inventory=json.loads((args.private_config/'inventory.json').read_text())
    remote=Path(args.remote_source)
    if not remote.is_absolute() or '..' in remote.parts:
        raise SystemExit('Use an absolute new source directory')
    for container in inventory['containers']:
        for mount in container['mounts']:
            source=Path(mount['Source'])
            if remote == source or remote.is_relative_to(source) or source.is_relative_to(remote):
                raise SystemExit('Source directory overlaps an existing data mount')
    def ssh(command, **kw):
        return subprocess.run(['ssh','-o','BatchMode=yes',args.host,command],check=True,**kw)
    q=shlex.quote
    # Refuse to overwrite any existing source directory; old deployments remain untouched.
    ssh(f'test ! -e {q(str(remote))} && mkdir -m 700 {q(str(remote))}')
    files=subprocess.check_output(['git','ls-files','-z'],cwd=root)
    subprocess.run(['rsync','-az','--from0','--files-from=-',str(root)+'/',
                    args.host+':'+str(remote)+'/'],input=files,check=True)
    subprocess.run(['rsync','-az',str(args.private_config/'.env'),
                    args.host+':'+str(remote)+'/.env'],check=True)
    subprocess.run(['rsync','-az',str(args.private_config/'runtime'),
                    args.host+':'+str(remote)+'/'],check=True)
    # Only the new staging directory can be created. Existing paths must be present.
    incoming=inventory['staging_path']
    api=next(c for c in inventory['containers'] if c['name']=='/dramatarr-api-1')
    uid,gid=api['user'].split(':')
    legacy=[m['Source'] for c in inventory['containers'] for m in c['mounts'] if m['Type']=='bind']
    ssh(' && '.join('test -d '+q(s) for s in legacy))
    ssh(f'test ! -e {q(incoming)} && mkdir -m 750 {q(incoming)} && chown {uid}:{gid} {q(incoming)}')
    prefix=f'cd {q(str(remote))} && DRAMATARR_REVISION={q(revision)} '
    ssh(prefix+'docker compose config -q')
    ssh(prefix+'docker compose build')
    (args.private_config/'staged-build.json').write_text(json.dumps({
        'revision':revision,'remote_source':str(remote),'staging_path':incoming,
        'production_changed':False},indent=2)+'\n')
    print('Combined images built. Production containers and databases were not changed.')

if __name__=='__main__':
    os.umask(0o077)
    main()
