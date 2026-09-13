"""Fail-closed Supabase object store for the complete payment runtime."""
import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
import time

import requests


class StoreError(RuntimeError):
    pass

_CACHE={}


def sqlite_from_bytes(content):
    """Load SQLite bytes into memory without retaining an operational local database."""
    with tempfile.TemporaryDirectory() as folder:
        path=os.path.join(folder,'state.sqlite3')
        with open(path,'wb') as temporary: temporary.write(content)
        source=sqlite3.connect(path)
        target=sqlite3.connect(':memory:',timeout=30)
        source.backup(target); source.close()
    return target


def sqlite_to_bytes(connection):
    with tempfile.TemporaryDirectory() as folder:
        path=os.path.join(folder,'state.sqlite3')
        target=sqlite3.connect(path)
        connection.backup(target); target.close()
        with open(path,'rb') as temporary: return temporary.read()


def enabled():
    return os.environ.get('PAYMENT_BACKEND', '').strip().casefold() == 'supabase'


def key(name):
    namespace = os.environ.get('PAYMENT_NAMESPACE', 'production').strip().strip('/') or 'production'
    return namespace + '/' + name.lstrip('/')


def require():
    if not enabled() and os.environ.get('PYTEST_CURRENT_TEST'):
        return
    if not enabled():
        raise StoreError('PAYMENT_BACKEND=supabase fehlt; lokaler operativer Fallback ist gesperrt.')
    for name in ('SUPABASE_ACCESS_TOKEN', 'SUPABASE_PROJECT_REF'):
        if not os.environ.get(name, '').strip():
            raise StoreError(f'{name} fehlt; Supabase-Zugriff ist nicht möglich.')


def _request(sql, readonly=False):
    require()
    ref = os.environ['SUPABASE_PROJECT_REF'].strip()
    token = os.environ['SUPABASE_ACCESS_TOKEN'].strip()
    suffix = '/query/read-only' if readonly else '/query'
    try:
        response = requests.post(
            f'https://api.supabase.com/v1/projects/{ref}/database{suffix}',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
            json={'query': sql, 'parameters': [], 'read_only': bool(readonly)}, timeout=90,
        )
    except requests.exceptions.RequestException as exc:
        raise StoreError(f'Supabase nicht erreichbar: {type(exc).__name__}') from None
    if response.status_code not in (200, 201):
        detail=response.text[:500].replace(token,'[redacted]')
        raise StoreError(f'Supabase-Abfrage fehlgeschlagen (HTTP {response.status_code}): {detail}')
    value = response.json()
    return value if isinstance(value, list) else value.get('result', [])


def read_only_sql(sql):
    """Expose the app's existing read-only Postgres channel for diagnostics.

    Same credentials and endpoint as get()/put() (SUPABASE_ACCESS_TOKEN,
    SUPABASE_PROJECT_REF) — no separate connection or token, read-only.
    """
    return _request(sql, readonly=True)


def preflight():
    """Verify Supabase is reachable and the configured credentials actually authenticate.

    require() only checks that the environment variables are non-empty; it does not prove
    they are valid. This performs one lightweight authenticated call so a revoked/expired
    token or wrong project ref fails loudly here, before any payment data is touched.
    """
    if not enabled() and os.environ.get('PYTEST_CURRENT_TEST'):
        return
    require()
    _request('select 1', readonly=True)


def ensure_schema():
    require()
    sql="""
    create table if not exists public.payment_runtime_objects (
      key text primary key,
      version bigint not null default 1 check(version > 0),
      sha256 text not null,
      content bytea not null,
      updated_at timestamptz not null default now()
    );
    alter table public.payment_runtime_objects enable row level security;
    revoke all on public.payment_runtime_objects from anon, authenticated;
    create table if not exists public.payment_runtime_chunks (
      sha256 text not null, part integer not null, content bytea not null,
      primary key(sha256,part)
    );
    alter table public.payment_runtime_chunks enable row level security;
    revoke all on public.payment_runtime_chunks from anon, authenticated;
    """
    ref=os.environ['SUPABASE_PROJECT_REF'].strip();token=os.environ['SUPABASE_ACCESS_TOKEN'].strip()
    response=requests.post(f'https://api.supabase.com/v1/projects/{ref}/database/migrations',
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},
        json={'query':sql,'name':'payment runtime objects 20260911090016'},timeout=180)
    if response.status_code not in (200,201):
        detail=response.text[:500].replace(token,'[redacted]')
        raise StoreError(f'Supabase-Migration fehlgeschlagen (HTTP {response.status_code}): {detail}')


def _write(sql, label='payment runtime update'):
    """Use the only write-capable channel provisioned for this project."""
    require();ref=os.environ['SUPABASE_PROJECT_REF'].strip();token=os.environ['SUPABASE_ACCESS_TOKEN'].strip()
    time.sleep(1.05)
    response=requests.post(f'https://api.supabase.com/v1/projects/{ref}/database/migrations',
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},
        json={'query':sql,'name':label+' '+uuid.uuid4().hex},timeout=180)
    if response.status_code not in (200,201):
        detail=response.text[:500].replace(token,'[redacted]')
        raise StoreError(f'Supabase-Schreibvorgang fehlgeschlagen (HTTP {response.status_code}): {detail}')


def get(key, required=True):
    key = globals()['key'](key)
    cached=_CACHE.get(key)
    if cached and time.monotonic()-cached[0] < 15:
        return cached[1],cached[2]
    safe = key.replace("'", "''")
    rows = _request(
        f"select version,sha256,encode(content,'base64') as content from public.payment_runtime_objects where key='{safe}'",
        readonly=True,
    )
    if not rows:
        if required:
            raise StoreError(f'Supabase-Objekt fehlt: {key}')
        return None, 0
    raw = base64.b64decode(rows[0]['content'])
    if not raw:
        parts=_request(f"select encode(content,'base64') content from public.payment_runtime_chunks where sha256='{rows[0]['sha256']}' order by part",readonly=True)
        raw=b''.join(base64.b64decode(part['content']) for part in parts)
    if hashlib.sha256(raw).hexdigest() != rows[0]['sha256']:
        raise StoreError(f'Supabase-Objekt beschädigt: {key}')
    version=int(rows[0]['version']);_CACHE[key]=(time.monotonic(),raw,version)
    return raw,version


def put(key, content, expected_version=None):
    if not isinstance(content, bytes):
        raise TypeError('Supabase-Inhalt muss bytes sein.')
    key = globals()['key'](key)
    safe = key.replace("'", "''")
    digest = hashlib.sha256(content).hexdigest()
    if expected_version == 0:
        existing = get(key.split('/', 1)[1], required=False)[1]
        if existing:
            raise StoreError(f'Veralteter Supabase-Stand für {key}; Vorgang abgebrochen.')
    condition = 'true' if expected_version in (None, 0) else f'payment_runtime_objects.version={int(expected_version)}'
    expected = 'null' if expected_version is None else str(int(expected_version))
    chunk_size=96*1024
    chunk_count=max(1,(len(content)+chunk_size-1)//chunk_size)
    existing_chunks=_request(f"select count(*)::int count from public.payment_runtime_chunks where sha256='{digest}'",readonly=True)[0]['count']
    if int(existing_chunks)!=chunk_count:
        for index,start in enumerate(range(0,len(content),chunk_size)):
            encoded=base64.b64encode(content[start:start+chunk_size]).decode()
            _write(f"insert into public.payment_runtime_chunks(sha256,part,content) values('{digest}',{index},decode('{encoded}','base64')) on conflict do nothing;",'payment chunk '+digest[:12])
    _write(f"""
      do $$ begin
        if {expected} is not null and coalesce((select version from public.payment_runtime_objects where key='{safe}'),0) <> {expected}
        then raise exception 'stale payment runtime object'; end if;
      insert into public.payment_runtime_objects(key,version,sha256,content)
      values('{safe}',1,'{digest}',''::bytea)
      on conflict(key) do update set version=payment_runtime_objects.version+1,
        sha256=excluded.sha256,content=excluded.content,updated_at=now()
      where {condition}
      ; end $$;
    """,'payment object '+digest[:12])
    rows=_request(f"select version,sha256 from public.payment_runtime_objects where key='{safe}'",readonly=True)
    if len(rows) != 1 or rows[0]['sha256'] != digest:
        raise StoreError(f'Veralteter Supabase-Stand für {key}; Vorgang abgebrochen.')
    version=int(rows[0]['version']);_CACHE[key]=(time.monotonic(),content,version)
    return version


_MISSING=object()
def get_json(key, default=_MISSING):
    raw, version = get(key, required=default is _MISSING)
    return (json.loads(raw.decode('utf-8')) if raw is not None else default), version


def put_json(key, value, expected_version=None):
    return put(key, json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), expected_version)
