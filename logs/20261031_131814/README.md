# ベンチ 2026-10-31 04:13:50 〜 04:16:05 UTC

`bak.yml` で 13:18 (JST) に取得したログのうち、最後のベンチ (asia1 で `POST /api/initialize` が来た 04:13:50 以降) を集計した。

- デプロイ済みコミット: `b343779` (pg_stat_statements / 全クエリログ有効)
- 生ログは `.gitignore` 済み (取得した人の手元の `logs/20261031_131814/<ホスト名>/` にある)

| ファイル | 内容 |
|---|---|
| [alp.txt](alp.txt) | nginx アクセスログ (アプリ3台合計 + ホスト別)。`alp ltsv --sort sum -r` |
| [slow-queries.txt](slow-queries.txt) | PostgreSQL のクエリログを正規化して集計。`python3 tools/pgagg.py <log> "2026-10-31 04:13:50" 25` |

## 目立つもの

### DB (execute / statement の合計 688 秒)

| 割合 | クエリ | メモ |
|---|---|---|
| 48% | `SELECT i.data, i.media_type FROM image_objects ...` | 画像本体を DB から返している (1260 回, 平均 264ms)。`GET /api/images/:id` |
| 19% | `SELECT ... FROM posting_windows WHERE region = $1 AND ended_at IS NULL FOR UPDATE` | 168 回で平均 776ms。行ロック待ち。`POST /api/posts` |
| 11% | `SELECT id FROM users WHERE id = $1 AND status = ? FOR NO KEY UPDATE` | 1022 回, 最大 4.2 秒。ロック待ち |
| 6% | `LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE` | 62 回で平均 708ms |
| 2% | `INSERT INTO notifications ... SELECT ... FROM users WHERE region = $1` | 18 回で平均 865ms |

### nginx

- `GET /api/notifications` は最大 60 秒 (ロングポーリング) なので SUM が大きいのは想定どおり
- `GET /api/images/:id` が SUM 545 秒で実質 1 位。上の DB 48% と同じ原因
- `POST /api/posts` は平均 1.7 秒。`posting_windows` の `FOR UPDATE` 待ち
