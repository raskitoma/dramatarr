#!/usr/bin/env python3
"""Read existing containers over SSH; write private config and inventory, never deploy.

Usage: python3 scripts/prepare-unification.py --host root@NAS --output /private/directory
Copy the generated .env and runtime/ into the combined source directory only at cutover.
No secrets are printed. Existing containers, databases and mounts are not modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess


def env(container):
    return dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)


def mount(container, destination):
    matches = [m for m in container['Mounts'] if m['Destination'] == destination]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one mount for {container["Name"]}:{destination}')
    return matches[0]


def write_env(path, values):
    # Compose single quotes preserve $, #, backslashes and spaces; forbid unsupported inputs.
    for value in values.values():
        if any(c in str(value) for c in "\r\n'"):
            raise ValueError('Environment value needs manual quoting; nothing was deployed')
    path.write_text(''.join(f"{k}='{v}'\n" for k, v in sorted(values.items())))
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (out / '.env').exists():
        raise SystemExit('Output already contains configuration; use a fresh private directory')
    raw = subprocess.check_output(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        args.host, 'docker inspect dramatarr-api-1 dramatarr-web-1 dramadex dramadown arr-postgres'])
    api, web, catalogue, downloader, postgres = json.loads(raw)
    a, c, d = env(api), env(catalogue), env(downloader)
    library = mount(api, a['DRAMATARR_LIBRARY_ROOT'])['Source']
    download = mount(downloader, d['DOWNLOAD_DIR'])['Source']
    profile = mount(downloader, d['CHROME_USER_DATA_DIR'])
    if profile['Type'] != 'volume':
        raise SystemExit('Chrome uses a bind mount; adapt the volume declaration before proceeding')
    networks = set(catalogue['NetworkSettings']['Networks']) & set(postgres['NetworkSettings']['Networks'])
    if len(networks) != 1:
        raise SystemExit('Cannot identify the catalogue Postgres network unambiguously')
    if not c['DRAMADEX_DB_URL'].startswith('postgresql'):
        raise SystemExit('Expected the production Postgres database; refusing SQLite fallback')
    user = api['Config']['User'].split(':')
    if len(user) != 2 or not all(x.isdigit() for x in user):
        raise SystemExit('Expected numeric API uid:gid')
    values = {k:v for k,v in a.items() if k.startswith('DRAMATARR_')}
    portmap = web['HostConfig']['PortBindings']['80/tcp']
    if len({item['HostPort'] for item in portmap}) != 1:
        raise SystemExit('Ambiguous web port')
    values.update(DRAMATARR_LIBRARY_PATH=library,
        DRAMATARR_DATA_PATH=mount(api, a['DRAMATARR_DATA_DIR'])['Source'],
        DRAMATARR_CATALOGUE_DATA_PATH=mount(catalogue, c['DRAMADEX_DATA_DIR'])['Source'],
        DRAMATARR_CHROME_VOLUME=profile['Name'], DRAMATARR_POSTGRES_NETWORK=networks.pop(),
        DRAMADEX_DB_URL=c['DRAMADEX_DB_URL'], DRAMADEX_API_KEY=c['DRAMADEX_API_KEY'],
        DRAMATARR_UID=user[0], DRAMATARR_GID=user[1], DRAMATARR_PORT=portmap[0]['HostPort'],
        DRAMATARR_WORKER_KEY=secrets.token_urlsafe(48),
        DRAMATARR_INCOMING_PATH=str(Path(library).parent / '.dramatarr-incoming'))
    # Remove old container paths that the combined Compose explicitly owns.
    for key in ['DRAMATARR_LIBRARY_ROOT','DRAMATARR_DATA_DIR','DRAMATARR_DATABASE_URL']:
        values.pop(key, None)
    runtime = out / 'runtime'
    runtime.mkdir(mode=0o700)
    write_env(runtime / 'catalogue.env', {k:v for k,v in c.items() if k.startswith('DRAMADEX_') or k=='TZ'})
    write_env(runtime / 'downloader.env', {k:v for k,v in d.items() if k not in {'PATH','HOME','HOSTNAME','NODE_VERSION','YARN_VERSION'}})
    write_env(out / '.env', values)
    inventory = [{
        'name': item['Name'], 'image': item['Image'], 'user': item['Config']['User'],
        'mounts': item['Mounts'], 'networks': list(item['NetworkSettings']['Networks']),
        'environment_sha256': hashlib.sha256(json.dumps(env(item), sort_keys=True).encode()).hexdigest(),
    } for item in [api, web, catalogue, downloader, postgres]]
    (out / 'inventory.json').write_text(json.dumps({
        'containers': inventory, 'downloads_share_library': library == download,
        'legacy_download_path': download, 'staging_path': values['DRAMATARR_INCOMING_PATH'],
    }, indent=2)+'\n')
    print('Private configuration and mount inventory written. No services changed.')

if __name__ == '__main__':
    os.umask(0o077)
    main()
