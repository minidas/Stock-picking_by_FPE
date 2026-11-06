import re
from pathlib import Path
t = Path('data/raw/prices_2012/ivv_extracted.txt').read_text(encoding='utf8')
number = r'\d[\d,]*(?: \d[\d,]*)*'
p = re.compile(rf'^(.*?)\s+({number})\s{{2,}}\$?\s*({number})$')
old = re.compile(r'^(.*?\D)\s+([\d, ]+?)\s{2,}\$?\s*([\d, ]+)$')
for line in t.splitlines():
    line = line.strip()
    if re.search(r'[A-Za-z].*\d,\d{2}.*\d,\d{2}', line) and not p.match(line):
        print(ascii(line))
    m, n = old.match(line), p.match(line)
    if m and (not n or m.groups() != n.groups()):
        print('DIFF',ascii(line),m.groups(),n.groups() if n else None)
