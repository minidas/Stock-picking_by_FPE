"""Retrieve public SEC 2012 EPS data, preserving provenance and filing vintages."""
from pathlib import Path
import argparse
from contextlib import ExitStack
import json
import hashlib
import math
import time
import uuid
import shutil
import subprocess
from urllib.parse import urlsplit, parse_qs
from datetime import datetime, timezone
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor'))
SOURCE_URL = 'https://hf-mirror.com/datasets/erlenbusch/sec-edgar/resolve/75008a0c4943e108944d0bc3982a12ab699ad70a/sec_edgar.duckdb'
_FILE_DIGESTS = {}


def file_record(path):
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns)
    cached = _FILE_DIGESTS.get(str(path))
    if not cached or cached[0] != signature:
        cached = (signature, hashlib.sha256(path.read_bytes()).hexdigest())
        _FILE_DIGESTS[str(path)] = cached
    return {'path': path.name, 'bytes': stat.st_size, 'sha256': cached[1]}


def resolve_public_url(raw, source_url=SOURCE_URL):
    """Resolve a public, pinned dataset URL; never print temporary signatures."""
    saved = raw / 'duckdb_resolved_url.txt'
    def valid(candidate):
        parsed = urlsplit(candidate)
        expiry = parse_qs(parsed.query).get('Expires', ['0'])[0]
        if parsed.hostname != 'cas-bridge.xethub.hf.co' or float(expiry) <= time.time() + 300:
            return False
        identity_path = raw / 'range_blocks/source_identity.json'
        if identity_path.exists():
            identity = json.loads(identity_path.read_text(encoding='utf-8'))
            if identity['source_identity'] != f'https://{parsed.hostname}{parsed.path}':
                raise ValueError('Resolved dataset differs from the retained cache source')
        return True
    if saved.exists():
        candidate = saved.read_text(encoding='utf-8-sig').strip()
        if valid(candidate):
            return candidate
    curl = shutil.which('curl.exe') or shutil.which('curl')
    if not curl:
        raise RuntimeError('curl is required to resolve the public dataset download')
    probe = raw / ('resolve_probe_' + uuid.uuid4().hex + '.bin')
    response = subprocess.run([curl, '--location', '--connect-timeout', '15', '--max-time', '60',
                               '--fail', '--silent', '--show-error', '--range', '0-4095',
                               '--max-filesize', '1000000', '--output', str(probe),
                               '--write-out', '%{url_effective}', source_url],
                              capture_output=True, text=True, timeout=75)
    candidate = response.stdout.strip()
    if response.returncode != 0 or not valid(candidate):
        raise RuntimeError(f'Public dataset URL resolution failed (curl status {response.returncode}); retained data are unchanged')
    stage = saved.with_name(saved.name + '.' + uuid.uuid4().hex + '.tmp')
    stage.write_text(candidate, encoding='utf-8')
    stage.replace(saved)
    return candidate

def main():
    parser = argparse.ArgumentParser(description='Retrieve 2012 constituent annual EPS from a public SEC-data mirror; preserve raw data and partial checkpoints.')
    parser.add_argument('--offline-cache', action='store_true', help='Read only already-downloaded database blocks; preserve partial rows if a block is missing.')
    parser.add_argument('--normalize-only', action='store_true', help='Rebuild the canonical EPS file from saved full JSON or partial JSONL without network or DuckDB.')
    parser.add_argument('--direct', action='store_true', help='Use direct remote DuckDB access instead of the default resumable localhost byte cache (slower on this network).')
    parser.add_argument('--threads', type=int, choices=(1, 2), default=2, help='DuckDB worker threads; memory is capped at 512 MiB.')
    parser.add_argument('--cached-http', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--resolved', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    raw = ROOT / 'data/raw/sec_2012_bulk'
    raw.mkdir(parents=True, exist_ok=True)
    url = SOURCE_URL
    if (raw / 'annual_eps_and_splits_2012.json').exists():
        build_dataset(json.loads((raw / 'annual_eps_and_splits_2012.json').read_text(encoding='utf-8')), raw, url)
        return
    if args.normalize_only:
        stream = raw / 'annual_eps_and_splits_2012.json.stream.jsonl'
        if not stream.exists():
            parser.error('No saved full response or partial stream exists yet')
        rows, seen = [], set()
        for line in stream.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # Abrupt interruption can leave an incomplete last line.
            key = tuple(row.get(k) for k in ('cik','adsh','tag','version','ddate','qtrs','uom','segments','coreg','value'))
            if key not in seen:
                seen.add(key)
                rows.append(row)
        build_dataset(rows, raw, url, retrieval_complete=False)
        return
    with ExitStack() as resources:
        return retrieve(args, raw, url, resources)


def retrieve(args, raw, url, resources):
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError('Install the tested optional dependency: python -m pip install --target vendor duckdb==2.0.0') from exc
    con = duckdb.connect()
    resources.callback(con.close)
    ext = (ROOT / 'vendor/duckdb_extensions').as_posix()
    con.execute(f"SET extension_directory='{ext}'")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    con.execute("SET http_timeout=120")
    con.execute("SET http_retries=2")
    con.execute(f'SET threads={args.threads}')
    con.execute("SET memory_limit='512MB'")
    spill = ROOT / 'outputs/sec_tmp'
    spill.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{spill.as_posix()}'")
    print('Attaching public SEC mirror', flush=True)
    # Curl can resolve HF's public redirect once, avoiding repeated slow redirect
    # requests for each database block. Never print the temporary signed URL.
    use_cache = not args.direct or args.offline_cache
    access_url = url
    if use_cache:
        from sec_range_cache import start_cache
        offline = args.offline_cache
        if offline:
            access_url = json.loads((raw / 'range_blocks/source_identity.json').read_text(encoding='utf-8'))['source_identity']
            con.execute('SET http_retries=0')
        else:
            access_url = resolve_public_url(raw, url)
        cache_server, access_url = start_cache(raw, access_url, allow_network=not offline)
        resources.callback(cache_server.server_close)
        resources.callback(cache_server.shutdown)
    con.execute(f"ATTACH '{access_url}' AS sec (READ_ONLY)")
    print('Mirror attached', flush=True)
    def query_to_json(sql, name):
        dest = raw / name
        if dest.exists():
            return json.loads(dest.read_text(encoding='utf-8'))
        (raw / (name + '.sql')).write_text(sql, encoding='utf-8')
        print('Querying ' + name, flush=True)
        rows = []
        for attempt in range(3):
            try:
                cur = con.execute(sql)
                keys = [item[0] for item in cur.description]
                rows = []
                stream_path = raw / (name + '.stream.jsonl')
                with stream_path.open('a', encoding='utf-8') as stream:
                    while True:
                        batch = cur.fetchmany(100)
                        if not batch:
                            break
                        normalized = json.loads(json.dumps([dict(zip(keys, row)) for row in batch], default=str))
                        for row in normalized:
                            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                        stream.flush()
                        rows.extend(normalized)
                        if name == 'annual_eps_and_splits_2012.json':
                            print(f'EPS rows persisted: {len(rows)}', flush=True)
                            build_dataset(rows, raw, url, retrieval_complete=False)
                break
            except duckdb.Error as exc:
                if attempt == 2:
                    raise
                print(f'Retrying {name} after {type(exc).__name__}; already downloaded blocks stay cached', flush=True)
                time.sleep(2)
        stage = dest.with_name(dest.name + '.' + uuid.uuid4().hex + '.tmp')
        stage.write_text(json.dumps(rows, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
        stage.replace(dest)
        print(f'Saved {name}: {len(rows)} rows', flush=True)
        return json.loads(dest.read_text(encoding='utf-8'))
    submissions = query_to_json("""
        SELECT * FROM sec.submissions
        WHERE filed >= DATE '2012-01-01' AND filed <= DATE '2012-12-31'
        AND period <= DATE '2012-12-31'
    """, 'submissions_2012.json')
    # Materializing a small local key table permits DuckDB to push a join filter.
    local_sub = (raw / 'submissions_2012.json').as_posix()
    con.execute(f"CREATE TEMP TABLE vintage AS SELECT * FROM read_json_auto('{local_sub}')")
    universe = ROOT / 'data/universe_2012_members.json'
    if universe.exists():
        member_ciks = {str(m['cik']).zfill(10) for m in json.loads(universe.read_text(encoding='utf-8'))['members'] if m.get('cik')}
        con.execute('CREATE TEMP TABLE member_ciks (cik VARCHAR)')
        con.executemany('INSERT INTO member_ciks VALUES (?)', [(cik,) for cik in sorted(member_ciks)])
        con.execute('DELETE FROM vintage WHERE cik NOT IN (SELECT cik FROM member_ciks)')
        print(f'Limiting EPS query to {len(member_ciks)} historical constituent CIKs', flush=True)
    facts = query_to_json("""
        SELECT n.adsh, n.tag, n.version, n.ddate, n.qtrs, n.uom,
               n.segments, n.coreg, n.value, s.cik, s.name, s.form, s.period, s.filed, s.accepted,
               s.fy, s.fp, s.instance
        FROM sec.numbers n JOIN vintage s USING (adsh)
        WHERE n.ddate <= DATE '2012-12-31' AND ((n.qtrs = 4 AND n.tag IN ('EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted'))
           OR n.tag ILIKE '%StockSplit%'
           OR n.tag ILIKE '%StockSplits%')
    """, 'annual_eps_and_splits_2012.json')
    build_dataset(facts, raw, url)

def extract_companies(facts, as_of='2012-12-31'):
    grouped = {}
    for row in facts:
        if str(row['filed']) > as_of or str(row['period']) > as_of or str(row['ddate']) > as_of:
            continue
        grouped.setdefault(str(row['cik']).zfill(10), []).append(row)
    companies = []
    for cik, rows in sorted(grouped.items()):
        eps = [r for r in rows if r['tag'] in ('EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted')
               and str(r['version']).startswith('us-gaap/')
               and r['qtrs'] == 4 and r['uom'] in ('USD/shares', 'USD')
               and not r.get('coreg') and not r.get('segments') and r.get('value') is not None
               and math.isfinite(float(r['value']))]
        annual = [r for r in eps if r['form'] in ('10-K', '10-K/A', '20-F', '20-F/A', '40-F', '40-F/A')]
        filers = {}
        for row in annual:
            filers.setdefault(row['adsh'], []).append(row)
        valid = []
        for adsh, filing_rows in filers.items():
            base = filing_rows[0]
            # Require a current-report annual anchor; amendments without the full
            # income statement never displace the complete prior annual filing.
            if not any(r['ddate'] == base['period'] for r in filing_rows):
                continue
            valid.append((base['period'], base['filed'], base['accepted'], adsh, filing_rows))
        if not valid:
            continue
        _, _, _, adsh, chosen = max(valid, key=lambda x: x[:4])
        base = chosen[0]
        by_date = {}
        for row in chosen:
            by_date.setdefault(row['ddate'], []).append(row)
        history = []
        conflicts = []
        for period, values in sorted(by_date.items()):
            preferred = [v for v in values if v['tag'] == 'EarningsPerShareDiluted'] or values
            distinct = {v['value'] for v in preferred}
            if len(distinct) != 1:
                conflicts.append({'period_end': period, 'values': sorted(distinct)})
                continue
            history.append({'period_end': period, 'available_at': base['filed'], 'eps': preferred[0]['value'],
                            'period_date_precision': 'nearest_month_end_in_SEC_dataset',
                            'tag': preferred[0]['tag'], 'accession': adsh, 'unit': preferred[0]['uom']})
        companies.append({'cik': cik, 'name': base['name'], 'form': base['form'],
                          'filing_date': base['filed'], 'accepted': base['accepted'],
                          'period_end': base['period'], 'accession': adsh,
                          'period_date_precision': 'nearest_month_end_in_SEC_dataset',
                          'instance': base['instance'], 'history': history,
                          'conflicts': conflicts, 'share_basis_verified': False,
                          'split_facts': [r for r in rows if 'StockSplit' in r['tag']],
                          'source_url': f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/",
                          'source_kind': 'third_party_mirror_of_SEC_financial_statement_data_sets'})
    return companies

def build_dataset(facts, raw, url, retrieval_complete=True):
    canonical = ROOT / 'data/research_2012_sec_eps.json'
    # A completed full raw response is authoritative even if a bounded probe
    # finishes later; the full process (or an offline rerun) will normalize it.
    if not retrieval_complete and (raw / 'annual_eps_and_splits_2012.json').exists():
        print('Keeping full response; bounded probe retained separately', flush=True)
        return
    companies = extract_companies(facts)
    payload = {'as_of': '2012-12-31', 'source_url': url,
               'retrieval_complete': retrieval_complete,
               'limited_query': not retrieval_complete,
               'retrieved_at': datetime.now(timezone.utc).isoformat(),
               'limitations': ['2012 filing vintage only; no institutional consensus',
                               'Third-party transformed mirror; not byte-identical original SEC ZIPs',
                               'History is a coherent single annual filing, not merged across split bases',
                               'Post-filing splits and share-class comparability require review before model use',
                               'Raw subset includes dimensional EPS; company histories use only non-dimensional standard diluted EPS tags',
                               'Legacy uom=USD is retained for standard per-share EPS tags; original unit is preserved for audit',
                               'SEC rounds fiscal dates to nearest month end; exact fiscal dates need original filing verification'],
               'summary': {'raw_fact_count': len(facts), 'company_count': len(companies),
                           'retrieval_complete': retrieval_complete},
               'companies': companies}
    if not retrieval_complete and canonical.exists():
        current = json.loads(canonical.read_text(encoding='utf-8'))
        if current.get('retrieval_complete') or len(current.get('companies', [])) > len(companies):
            print('Keeping broader canonical response; bounded probe retained separately', flush=True)
            return
    temp = canonical.with_name(canonical.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    if not retrieval_complete and (raw / 'annual_eps_and_splits_2012.json').exists():
        # Leave the staged JSON as a research artifact rather than replace a
        # full response that completed while this probe was normalizing.
        print('Full response arrived; bounded canonical update skipped', flush=True)
        return
    temp.replace(canonical)
    manifest = {'retrieved_at': payload['retrieved_at'], 'source_url': url,
                'original_sec_endpoint': 'https://www.sec.gov/files/dera/data/financial-statement-data-sets/2012q1.zip',
                'original_endpoint_status': 'HTTP 403 on two curl requests; data.sec.gov also HTTP 403; dcm.sec.gov DNS failure',
                'raw_scope': 'JSON serialization of selected mirrored rows; not complete source database or SEC ZIP',
                'upstream_dataset': 'https://huggingface.co/datasets/erlenbusch/sec-edgar',
                'database_expected_sha256_per_public_lfs_metadata': 'fdfd9c5807dac4725a1fb0b84b42a547be75f9a70d3568280a45930d1776a044',
                'entire_database_hash_verified': False,
                'retrieval_complete': retrieval_complete,
                'byte_range_cache': {'identity_file': 'range_blocks/source_identity.json',
                                     'block_hashes_and_offsets': 'range_blocks.jsonl',
                                     'scope': 'partial source database byte ranges, not a complete database download'},
                'files': [file_record(p)
                          for p in raw.iterdir() if p.is_file() and p.name not in ('manifest.json', 'last_range_request.json')
                          and not p.name.startswith('duckdb_resolved_url')]}
    (raw / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(payload['summary']), flush=True)

if __name__ == '__main__':
    main()
