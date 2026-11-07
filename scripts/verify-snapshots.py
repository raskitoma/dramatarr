#!/usr/bin/env python3
"""Restore a catalogue dump in an isolated, temporary Postgres container on the NAS.

No production database is attached. Uses an ephemeral tmpfs with no network. Also checks
SQLite snapshots locally, recording every table count and foreign-key/integrity verdict.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import time
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',required=True)
    parser.add_argument('--snapshots',type=Path,required=True)
    args=parser.parse_args()
    checks=json.loads((args.snapshots/'checksums.json').read_text())
    for name,expected in checks.items():
        with (args.snapshots/name).open('rb') as handle:
            assert hashlib.file_digest(handle,'sha256').hexdigest()==expected['sha256'], name
    db=sqlite3.connect(f'file:{args.snapshots / "library.db"}?mode=ro',uri=True)
    assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
    assert not db.execute('pragma foreign_key_check').fetchall()
    tables=[r[0] for r in db.execute("select name from sqlite_master where type='table'")]
    library={t:db.execute('select count(*) from "'+t.replace('"','""')+'"').fetchone()[0] for t in tables}
    db.close()
    name='dramatarr-restore-'+uuid.uuid4().hex[:10]
    def remote(command, **kw):
        return subprocess.run(['ssh','-o','BatchMode=yes',args.host,command],check=True,**kw)
    try:
        remote(f'docker run -d --name {name} --network none --tmpfs /var/lib/postgresql/data:rw,size=6g '
               '-e POSTGRES_HOST_AUTH_METHOD=trust postgres:17',stdout=subprocess.DEVNULL)
        for _ in range(60):
            p=subprocess.run(['ssh',args.host,f'docker exec {name} pg_isready -U postgres'],capture_output=True)
            if p.returncode==0: break
            time.sleep(1)
        else: raise RuntimeError('Isolated Postgres did not start')
        remote(f'docker exec {name} createdb -U postgres catalogue_restore')
        with (args.snapshots/'catalogue.dump').open('rb') as dump:
            remote(f'docker exec -i {name} pg_restore --no-owner --no-acl --exit-on-error '
                   '-U postgres -d catalogue_restore',stdin=dump)
        sql="""CREATE TEMP TABLE census(name text, rows bigint);
DO $$ DECLARE r record; n bigint; BEGIN
FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
EXECUTE format('SELECT count(*) FROM %I',r.tablename) INTO n;
INSERT INTO census VALUES(r.tablename,n); END LOOP; END $$;
SELECT json_object_agg(name,rows) FROM census;"""
        p=remote(f'docker exec -i {name} psql -XqAt -U postgres -d catalogue_restore',
                 input=sql.encode(),stdout=subprocess.PIPE)
        catalogue=json.loads(p.stdout.decode())
        result={'sqlite_integrity':'ok','sqlite_foreign_keys':'ok','library_counts':library,
                'catalogue_restore':'ok','catalogue_counts':catalogue}
        (args.snapshots/'restore-verification.json').write_text(json.dumps(result,indent=2)+'\n')
        print('SQLite integrity and isolated Postgres restore passed; table counts recorded.')
    finally:
        subprocess.run(['ssh',args.host,f'docker rm -f {shlex.quote(name)}'],capture_output=True)

if __name__=='__main__':
    os.umask(0o077)
    main()
