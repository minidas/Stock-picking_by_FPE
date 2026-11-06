"""Save a bounded partial EPS result through an already-running local cache."""
from pathlib import Path
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor'))
import duckdb

port = int(sys.argv[1])
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 100
if not 1 <= limit <= 20000:
    raise ValueError('Limit must be 1 to 20000')
raw = ROOT / 'data/raw/sec_2012_bulk'
con = duckdb.connect()
con.execute(f"SET extension_directory='{(ROOT / 'vendor/duckdb_extensions').as_posix()}'")
con.execute('LOAD httpfs')
con.execute('SET http_timeout=180')
con.execute('SET threads=2')
con.execute(f"ATTACH 'http://127.0.0.1:{port}/sec.duckdb' AS sec (READ_ONLY)")
print('Partial query attached to existing byte cache', flush=True)
subfile = (raw / 'submissions_2012.json').as_posix()
con.execute(f"CREATE TEMP TABLE vintage AS SELECT * FROM read_json_auto('{subfile}')")
members = json.loads((ROOT / 'data/universe_2012_members.json').read_text(encoding='utf-8'))['members']
con.execute('CREATE TEMP TABLE members (cik VARCHAR)')
con.executemany('INSERT INTO members VALUES (?)', [(str(m['cik']).zfill(10),) for m in members if m.get('cik')])
con.execute('DELETE FROM vintage WHERE cik NOT IN (SELECT cik FROM members)')
sql = """
SELECT n.adsh,n.tag,n.version,n.ddate,n.qtrs,n.uom,n.segments,n.coreg,n.value,
       s.cik,s.name,s.form,s.period,s.filed,s.accepted,s.fy,s.fp,s.instance
FROM sec.numbers n JOIN vintage s USING(adsh)
WHERE n.qtrs=4 AND n.tag IN ('EarningsPerShareDiluted','EarningsPerShareBasicAndDiluted')
  AND n.ddate <= DATE '2012-12-31'
"""
sql += f' LIMIT {limit}'
name = f'partial_eps_2012_{limit}.json'
(raw / (name + '.sql')).write_text(sql, encoding='utf-8')
cursor = con.execute(sql)
keys = [d[0] for d in cursor.description]
rows = [dict(zip(keys, row)) for row in cursor.fetchall()]
(raw / name).write_text(json.dumps(rows, default=str, indent=2), encoding='utf-8')
print(f'Saved bounded partial query: {len(rows)} rows', flush=True)
if not (raw / 'annual_eps_and_splits_2012.json').exists():
    from download_2012_sec_bulk import build_dataset
    normalized = json.loads((raw / name).read_text(encoding='utf-8'))
    build_dataset(normalized, raw,
                  'https://hf-mirror.com/datasets/erlenbusch/sec-edgar/resolve/main/sec_edgar.duckdb',
                  retrieval_complete=False)
