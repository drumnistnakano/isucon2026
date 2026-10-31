"""PostgreSQL の log_min_duration_statement=0 のログをクエリごとに集計する。

usage: python -I pgagg.py <logfile> <since "YYYY-MM-DD HH:MM:SS"> [top]
"""
import re
import sys
from collections import defaultdict

path, since = sys.argv[1], sys.argv[2]
top = int(sys.argv[3]) if len(sys.argv) > 3 else 30

head_re = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC \[\d+\]: .*? (LOG|DETAIL|ERROR|FATAL|STATEMENT|HINT|CONTEXT|WARNING):  (.*)$")
dur_re = re.compile(r"^duration: ([\d.]+) ms  (statement|execute [^:]*|parse [^:]*|bind [^:]*): (.*)$", re.S)


def normalize(sql):
    s = re.sub(r"'(?:[^']|'')*'", "?", sql)
    s = re.sub(r"(?<![$\w])\d+(?:\.\d+)?\b", "?", s)
    s = re.sub(r"\(\s*(?:\?|\$\d+)(?:\s*,\s*(?:\?|\$\d+))+\s*\)", "(...)", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


stats = defaultdict(lambda: [0, 0.0, 0.0])  # kind -> {query: [count, total, max]}
by_kind = defaultdict(lambda: [0, 0.0])
errors = defaultdict(int)
first = last = None

cur = None  # (ts, level, text)


def flush(entry):
    global first, last
    if entry is None:
        return
    ts, level, text = entry
    if ts < since:
        return
    if level in ("ERROR", "FATAL"):
        errors[f"{level}: {normalize(text)[:200]}"] += 1
        return
    if level != "LOG":
        return
    m = dur_re.match(text)
    if not m:
        return
    first = first or ts
    last = ts
    ms = float(m.group(1))
    kind = m.group(2).split(" ")[0]
    by_kind[kind][0] += 1
    by_kind[kind][1] += ms
    if kind in ("execute", "statement"):
        st = stats[normalize(m.group(3))]
        st[0] += 1
        st[1] += ms
        st[2] = max(st[2], ms)


with open(path, encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.rstrip("\n")
        h = head_re.match(line)
        if h:
            flush(cur)
            cur = [h.group(1), h.group(2), h.group(3)]
        elif cur is not None and line.startswith("\t"):
            # DETAIL: parameters などの長い値は捨てて、SQL の続きだけ繋ぐ
            if cur[1] == "LOG" and len(cur[2]) < 20000:
                cur[2] += "\n" + line
flush(cur)

grand = sum(v[1] for v in stats.values())
print(f"# PostgreSQL クエリ集計 ({first} 〜 {last} UTC)")
print()
print("## 種類別 (extended protocol では parse / bind / execute が別々に記録される)")
print()
print(f"{'kind':<10} {'count':>9} {'total_ms':>12}")
for k, (c, t) in sorted(by_kind.items(), key=lambda x: -x[1][1]):
    print(f"{k:<10} {c:>9} {t:>12.1f}")
print()
print(f"## execute / statement の合計時間 上位 {top} (全 {len(stats)} 種類, 合計 {grand:.1f} ms)")
print()
print(f"{'total_ms':>10} {'%':>6} {'count':>8} {'mean_ms':>9} {'max_ms':>9}  query")
for q, (c, t, mx) in sorted(stats.items(), key=lambda x: -x[1][1])[:top]:
    print(f"{t:>10.1f} {t / grand * 100:>5.1f}% {c:>8} {t / c:>9.3f} {mx:>9.2f}  {q[:300]}")
print()
print(f"## 呼び出し回数 上位 {top}")
print()
print(f"{'count':>8} {'total_ms':>10} {'mean_ms':>9}  query")
for q, (c, t, mx) in sorted(stats.items(), key=lambda x: -x[1][0])[:top]:
    print(f"{c:>8} {t:>10.1f} {t / c:>9.3f}  {q[:300]}")
if errors:
    print()
    print("## ERROR / FATAL")
    print()
    for e, c in sorted(errors.items(), key=lambda x: -x[1]):
        print(f"{c:>6}  {e}")
