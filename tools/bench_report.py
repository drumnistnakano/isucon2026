"""bak.yml で取ってきたログを集計し、meta.json と logs/INDEX.md を作る。

usage: python -I bench_report.py <logs/<日時>[_job<ID>]> [--job-id N] [--score N] [--commit host=sha ...]
       python -I bench_report.py logs --index-only

入力 (bak.yml が置く生ログ):
  <dir>/<アプリ>/access.log.<日時>             nginx (LTSV)
  <dir>/<DB>/postgresql-16-main.log.<日時>     PostgreSQL

出力:
  <dir>/alp.txt                 最後のベンチ区間の alp 集計 (alp が PATH に無ければ skip)
  <dir>/slow-queries.txt        最後のベンチ区間の PostgreSQL クエリ集計 (tools/pgagg.py)
  <dir>/<host>-access.log.gz    nginx ログ全量
  <dir>/<host>-postgresql.log.gz  PostgreSQL ログから DETAIL: parameters 行を除いたもの
  <dir>/meta.json               ジョブ ID・スコア・コミット・ベンチ区間
  logs/INDEX.md                 logs/*/meta.json から作り直す一覧
"""
import argparse
import gzip
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ALP_MATCHING = ",".join([
    f"^/api/images/{UUID}$",
    f"^/api/posts/{UUID}$",
    f"^/api/posts/{UUID}/comments$",
    f"^/api/posts/{UUID}/realmojis$",
    f"^/api/users/{UUID}$",
    f"^/api/admin/users/{UUID}/certification$",
    f"^/api/friend-requests/{UUID}/[a-z]+$",
    "^/api/memories/[0-9]{4}-[0-9]{2}-[0-9]{2}$",
    f"^/api/comments/{UUID}$",
    f"^/api/realmojis/{UUID}$",
    f"^/api/follows/{UUID}$",
    f"^/api/blocks/{UUID}$",
    f"^/api/friendships/{UUID}$",
])


def ltsv_time(line):
    """LTSV 行の time を datetime にする。LTSV でない行 (LTSV 化前の combined 形式) は None。"""
    if not line.startswith("time:"):
        return None
    raw = line.split("\t", 1)[0][5:]
    return datetime.strptime(raw, "%d/%b/%Y:%H:%M:%S %z")


def bench_window(access_logs):
    """最後の POST /api/initialize から最後のリクエストまでをベンチ区間とする。"""
    start = end = first = None
    for path in access_logs:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                t = ltsv_time(line)
                if t is None:
                    continue
                first = t if first is None else min(first, t)
                end = t if end is None else max(end, t)
                if "\turi:/api/initialize" in line and (start is None or t > start):
                    start = t
    if start is None:
        print("warning: /api/initialize が見つからないので、ログ全体をベンチ区間とする", file=sys.stderr)
        start = first
    return start, end


def write_alp(bench_dir, access_logs, start, end):
    if shutil.which("alp") is None:
        print("warning: alp が無いので alp.txt は作らない", file=sys.stderr)
        return
    span = f"{start:%Y-%m-%d %H:%M:%S} 〜 {end:%H:%M:%S} UTC"
    hosts = [p.parent.name for p in access_logs]
    with tempfile.TemporaryDirectory() as tmp:
        per_host = {}
        all_path = Path(tmp) / "all.ltsv"
        with open(all_path, "w") as all_f:
            for path in access_logs:
                host_path = Path(tmp) / f"{path.parent.name}.ltsv"
                with open(path, encoding="utf-8", errors="replace") as src, open(host_path, "w") as dst:
                    for line in src:
                        t = ltsv_time(line)
                        if t is not None and t >= start:
                            dst.write(line)
                            all_f.write(line)
                per_host[path.parent.name] = host_path

        def alp(path, limit=None):
            cmd = ["alp", "ltsv", "--file", str(path), "--sort", "sum", "-r", "-m", ALP_MATCHING, "--format", "markdown"]
            out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
            return "\n".join(out.splitlines()[:limit]) + "\n" if limit else out

        with open(bench_dir / "alp.txt", "w") as out:
            out.write(f"# nginx アクセスログ集計 (アプリ{len(hosts)}台 {' / '.join(hosts)} 合計, {span})\n")
            out.write("# alp ltsv --sort sum -r (クエリ文字列は除外)\n\n")
            out.write(alp(all_path))
            out.write("\n## ホスト別 上位10 (sum)\n")
            for host, path in per_host.items():
                out.write(f"\n### {host}\n\n")
                out.write(alp(path, limit=12))


def write_slow_queries(bench_dir, pg_log, start):
    since = f"{start:%Y-%m-%d %H:%M:%S}"
    cmd = [sys.executable, "-I", str(TOOLS / "pgagg.py"), str(pg_log), since, "25"]
    with open(bench_dir / "slow-queries.txt", "w") as out:
        subprocess.run(cmd, check=True, stdout=out)


def write_gzip(src, dst, drop=None):
    with open(src, "rb") as f, gzip.open(dst, "wb", compresslevel=9) as g:
        for line in f:
            if drop is None or drop not in line:
                g.write(line)


def write_index(logs_dir):
    rows = []
    for meta_path in sorted(logs_dir.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        commits = sorted(set(meta.get("commits", {}).values()))
        commit = ", ".join(c[:7] for c in commits) or "-"
        if len(commits) > 1:
            commit += " ⚠️ 不一致"
        window = meta.get("bench_window_utc") or {}
        rows.append(
            f"| {meta['fetched_at']} | {meta.get('job_id') or '-'} | {meta.get('score') or '-'} "
            f"| {commit} | {window.get('start', '-')} 〜 {window.get('end', '-')} "
            f"| [{meta_path.parent.name}]({meta_path.parent.name}/) |"
        )
    lines = [
        "# ベンチ一覧",
        "",
        "`ansible/playbooks/bak.yml` (`tools/bench_report.py`) が logs/*/meta.json から自動生成する。手で編集しない。",
        "",
        "| 取得日時 | job | score | commit | ベンチ区間 (UTC) | ログ |",
        "|---|---|---|---|---|---|",
        *rows,
        "",
    ]
    (logs_dir / "INDEX.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bench_dir", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--score")
    parser.add_argument("--commit", action="append", default=[], help="host=sha")
    parser.add_argument("--index-only", action="store_true", help="logs/INDEX.md だけ作り直す (bench_dir には logs/ を渡す)")
    args = parser.parse_args()

    if args.index_only:
        write_index(args.bench_dir.resolve())
        return

    bench_dir = args.bench_dir.resolve()
    access_logs = sorted(bench_dir.glob("*/access.log.*"))
    pg_logs = sorted(bench_dir.glob("*/postgresql-*.log.*"))

    start = end = None
    if access_logs:
        start, end = bench_window(access_logs)
    if start is not None:
        write_alp(bench_dir, access_logs, start, end)
        for pg_log in pg_logs[:1]:
            write_slow_queries(bench_dir, pg_log, start)

    for path in access_logs:
        write_gzip(path, bench_dir / f"{path.parent.name}-access.log.gz")
    # パラメータ値は画像データやトークンのハッシュを含み、ログの大半を占めるので除く
    for path in pg_logs:
        write_gzip(path, bench_dir / f"{path.parent.name}-postgresql.log.gz", drop=b"DETAIL:  parameters:")

    meta_path = bench_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    fetched = datetime.strptime(bench_dir.name[:15], "%Y%m%d_%H%M%S")
    meta.update({
        "dir": bench_dir.name,
        "fetched_at": f"{fetched:%Y-%m-%d %H:%M:%S}",
        "hosts": sorted({p.parent.name for p in access_logs + pg_logs}),
    })
    if args.job_id:
        meta["job_id"] = args.job_id
    if args.score:
        meta["score"] = args.score
    if args.commit:
        meta["commits"] = dict(c.split("=", 1) for c in args.commit)
    if start is not None:
        meta["bench_window_utc"] = {
            "start": f"{start.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}",
            "end": f"{end.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S}",
        }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    write_index(bench_dir.parent)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
