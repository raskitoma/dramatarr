#!/usr/bin/env python3
"""Take consistent database snapshots over SSH without restarting production services.

Uses SQLite's online backup API in the API container, and pg_dump's consistent transaction.
The destination is a private local directory. This never copies an open WAL database file.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SQLITE = '''import sqlite3,tempfile,os,sys
fd,path=tempfile.mkstemp(suffix=".db");os.close(fd)
try:
 source=sqlite3.connect("file:/data/dramatarr.db?mode=ro",uri=True)
 target=sqlite3.connect(path);source.backup(target)
 assert target.execute("pragma integrity_check").fetchone()[0]=="ok"
 assert not target.execute("pragma foreign_key_check").fetchall()
 target.close();source.close()
 with open(path,"rb") as f:
  while chunk:=f.read(1048576):sys.stdout.buffer.write(chunk)
finally:os.unlink(path)
'''


def capture(host, command, path):
    if path.exists():
        raise RuntimeError(f'Refusing to overwrite {path.name}')
    tmp = path.with_suffix(path.suffix + '.partial')
    with tmp.open('xb') as handle:
        subprocess.run(['ssh','-o','BatchMode=yes',host,command], stdout=handle, check=True)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.rename(path)
    with path.open('rb') as handle:
        return {'bytes':path.stat().st_size,'sha256':hashlib.file_digest(handle,'sha256').hexdigest()}


def main():
    import shlex
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    args.output.mkdir(parents=True,mode=0o700,exist_ok=True)
    results={}
    results['library.db']=capture(args.host,
        'docker exec dramatarr-api-1 python -c '+shlex.quote(SQLITE),args.output/'library.db')
    # The existing Postgres container supplies its own administrator identity. No password
    # is copied to shell arguments, logs, or the backup manifest.
    results['catalogue.dump']=capture(args.host,
        'docker exec arr-postgres sh -c '+shlex.quote('pg_dump -U "$POSTGRES_USER" -d dramadex -Fc'),
        args.output/'catalogue.dump')
    (args.output/'checksums.json').write_text(json.dumps(results,indent=2)+'\n')
    print('Consistent snapshots captured and hashed. Restore validation is still required.')

if __name__=='__main__':
    os.umask(0o077)
    try: main()
    except Exception as exc:
        print(f'Snapshot failed: {exc}',file=sys.stderr)
        sys.exit(1)
