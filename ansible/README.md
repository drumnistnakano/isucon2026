# デプロイ手順（Ansible）

main ブランチの内容を **アプリ3台（asia1 / europe1 / america1）** と **DB 1台（asia2）** へ配布し、再起動と検証まで行う。

- サーバには git リポジトリが無く、GitHub へ fetch もできない（`Permission denied (publickey)`）。
  そのため **このPCから rsync / copy で配る方式** を採る。
- asia2 は PostgreSQL 専用。アプリコードは置かず、`etc/postgresql/` の設定だけを配る。
- リポジトリ全体は送らない。送るのは次の4つだけ。
  - `webapp/python/` → アプリ3台 `/home/isucon/webapp/python/`
  - `webapp/sql/` → アプリ3台 `/home/isucon/webapp/sql/`
  - `etc/nginx/` → アプリ3台 `/home/isucon/etc/nginx/`（`/etc/nginx/nginx.conf` はここへの symlink）
  - `etc/postgresql/` → DB 1台 `/etc/postgresql/16/main/postgresql.conf`

## 1. 構成

```
ansible/
  ansible.cfg           # inventory の場所、forks、SSH 多重化
  inventory.ini         # サーバの IP（変わったらここを直す）
  playbooks/deploy.yml  # 同期 → 記録 → 再起動 → 検証
  README.md             # このファイル
etc/
  nginx/nginx.conf                  # アプリ3台の /etc/nginx/nginx.conf（symlink 先）
  postgresql/16/main/postgresql.conf # DB 1台の /etc/postgresql/16/main/postgresql.conf
webapp/
  python/  sql/                     # アプリ3台へ配る
```

## 2. 前提

- このPCから `ssh isucon@<ip>` が鍵認証で通ること（`~/.ssh/id_ed25519`）
- サーバの `isucon` は `sudo` パスワードなしで次を実行できる
  - `systemctl restart isureal-python`（アプリ3台）
  - `nginx -t` と `systemctl restart nginx`（アプリ3台）
  - `postgresql.conf` の配置と `systemctl restart postgresql`（DB 1台）
- サーバに `rsync` がある（3.2.7 で確認済み）
- サーバに `/home/isucon/local/uv/uv` がある（0.13.5 で確認済み）
- ローカルの main が origin/main と一致し、`webapp/` と `etc/` に未コミット変更が無い

## 3. 初回セットアップ（このPCのみ）

```bash
uv tool install --python 3.13 ansible
ln -sf ~/.local/share/uv/tools/ansible/bin/ansible-playbook ~/.local/bin/ansible-playbook
```

`uv` が mise 経由で動かない場合はバイナリを直接使う。

```bash
UV=$(ls ~/.local/share/mise/installs/uv/*/uv-x86_64-unknown-linux-gnu/uv | tail -1)
$UV tool install --python 3.13 ansible
```

確認:

```bash
cd ansible
ansible-playbook --version        # core 2.21.5 / ansible 14.5.0 で確認
ansible-galaxy collection list | grep ansible.posix   # 2.2.2 で確認
```

`ansible.posix`（`synchronize` に必要）は `ansible` パッケージに同梱されている。無い場合のみ:

```bash
ansible-galaxy collection install ansible.posix
```

## 4. デプロイ

**必ず `ansible/` ディレクトリで実行する**（`ansible.cfg` はカレントディレクトリから読まれる）。

```bash
cd ansible
```

### 4.1 事前確認（何も変更しない）

```bash
ansible-playbook playbooks/deploy.yml -e rsync_dry_run=true
```

- 転送内容だけを表示し、依存関係更新・再起動・記録・検証はすべてスキップする
- ローカルの状態チェック（main と一致しているか、未コミットが無いか）は**本番実行と同じ**に走る

### 4.2 1台だけ先行

ローカル確認の play（`localhost`）は `--limit` の対象になるため、**`localhost` も指定する**。
指定を忘れると「ローカル確認の play が実行されていない」で止まる（何も変更しない）。

```bash
ansible-playbook playbooks/deploy.yml --limit 'asia1,localhost'
```

アプリ1台と nginx だけが更新される。DB 設定の配布（`hosts: db`）は対象外になる。

### 4.3 全体に適用

```bash
ansible-playbook playbooks/deploy.yml
```

`serial: 1` なのでアプリは 1台ずつ順番に流れる（クロスリージョンの回線を奪い合わない）。
DB 設定は 1台だけなので、アプリより先に反映される。

## 5. 実行される内容

**再起動は毎回行う**（設定だけの変更でもアプリを再起動し、ベンチ前の状態を毎回揃える）。
DB 設定を先に反映し、そのあとアプリを再起動する（DB 再起動で切れた接続を張り直すため）。

### DB 1台（asia2）

| # | タスク | 内容 |
|---|---|---|
| 1 | 設定の配置 | `etc/postgresql/16/main/postgresql.conf` → `/etc/postgresql/16/main/postgresql.conf`（`become`。dry-run では check mode） |
| 2 | 再起動 | `systemctl restart postgresql`（`become`） |
| 3 | 検証 | `is-active postgresql` / `pg_isready -h 127.0.0.1 -p 5432` / ログに `PANIC` が無い |

### アプリ3台（asia1 / europe1 / america1、1台ずつ）

| # | タスク | 内容 |
|---|---|---|
| 1 | ローカル確認 | `git fetch`、HEAD と origin/main の一致、`webapp/` と `etc/` の未コミット検出（このPC上で1回だけ） |
| 2 | python 同期 | `webapp/python/` → `/home/isucon/webapp/python/`（`--delete`） |
| 3 | SQL 同期 | `webapp/sql/` → `/home/isucon/webapp/sql/`（`--delete`） |
| 4 | nginx 設定同期 | `etc/nginx/` → `/home/isucon/etc/nginx/`（`--delete`） |
| 5 | 依存関係の同期 | python に変更があったホストだけ `uv sync --frozen --no-dev` を実行 |
| 6 | nginx 設定の検証 | `nginx -t`（`become`）。構文エラーなら再起動せずに止まる |
| 7 | 版の記録 | `/home/isucon/webapp/DEPLOYED_COMMIT` に SHA を書き込む |
| 8 | 再起動 | `systemctl restart isureal-python` → `systemctl restart nginx`（`become`） |
| 9 | 検証 | `is-active`（アプリ・nginx）/ `127.0.0.1:8080` の応答 / `DEPLOYED_COMMIT` の一致 / `journalctl` に `Traceback` が無い |

### 送らないもの（意図的）

| 対象 | 理由 |
|---|---|
| `~/env.sh` | サーバ固有（`ISUREAL_REGION` が asia/europe/america）＋秘密情報を含む |
| `webapp/public/` | 配布された静的ファイルは内容維持が必須 |
| `webapp/{go,nodejs,perl,php,ruby,rust}/` | 使わない言語実装 |
| `webapp/python/.venv/`, `__pycache__/` | rsync では消さず、`.venv` は `uv sync` で更新する |
| DB のデータ | `POST /api/initialize` が初期化する |

## 6. DB 変更（スキーマ・インデックス）

**`POST /api/initialize` ではスキーマは作られない。** `sql/init.sh` が流す `reset.sql` は
TRUNCATE と初期データ復元だけで、テーブル定義・インデックスは触らない。

設定ファイル（`postgresql.conf`）は `deploy.yml` が毎回配布して PostgreSQL を再起動する。
一方、**スキーマやインデックスの変更は配布されない**。インデックス追加やテーブル変更を含む
ブランチでは、`sql/001-schema.sql` を直すだけでは稼働中の asia2 に反映されない。適用用の
プレイブック（`playbooks/db-apply.yml`）を別途用意し、asia2 に対して psql で流す。
**必要になった時点で追加する。**

## 7. デプロイ後の確認（ベンチ前チェック）

```bash
# 3台の版が揃っているか（ローカルで実行）
for ip in 35.77.158.213 43.206.38.70 13.115.0.212; do
  ssh isucon@$ip 'printf "%s " $(hostname); cat ~/webapp/DEPLOYED_COMMIT'
done
```

- `systemctl is-active isureal-python` が3台とも `active`（playbook が自動で確認済み）
- `systemctl is-active nginx` が3台とも `active`、`pg_isready` が asia2 で `accepting connections`（同上）
- `df -h /` で空きがあるか（画像キャッシュ・ログが増える）
- nginx / アプリのログをクリアしてから計測する
- **暖機運転はしない**。再起動後の初回ベンチをスコアとして記録する
- 記録項目: 日時 / IP / コミット / netdata の有無 / slow log の状態 / score / success / fail / df / メモ

## 8. ロールバック

```bash
# 回帰したコミットを revert する PR を作る → main にマージ
git switch main && git pull
cd ansible && ansible-playbook playbooks/deploy.yml
```

main を配るだけなので、戻す操作もデプロイと同じ1コマンド。

## 9. 注意事項

- **ベンチマーク実行中にデプロイしない**（`isureal-python` の再起動で SSE と接続が切れる）
- **再起動は毎回走る**。`postgresql` の再起動でアプリの DB 接続は一度切れるため、
  必ず DB → アプリの順で実行する（playbook 側で固定済み）
- `--limit` を使うときは `localhost` も含める（例: `--limit 'asia1,localhost'`）。
  忘れると何も変更せずに止まる
- `inventory.ini` の IP が古いと別スタックへ配ってしまう。再構築したら `inventory.ini` を更新する
- `--delete` を使うので、除外指定（`.venv/` など）を消さないこと
- サーバ側で直接ファイルを編集しない。次のデプロイで上書きされる
  （`/etc/nginx/nginx.conf` は `~/etc/nginx/nginx.conf` への symlink、
  `postgresql.conf` は毎回 `copy` で上書きされる）

## 10. トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| `HEAD (xxx) が origin/main (yyy) と違う` | `git switch main && git pull` してから実行 |
| `webapp と設定ファイルに未コミットの変更が無いことを確認` で failed | コミットするか `git stash` する |
| `ローカル確認の play が実行されていない` | `--limit` に `localhost` を足す（例: `--limit 'asia1,localhost'`） |
| `Synchronize failed` / `rsync: command not found` | サーバの `rsync` とこのPCの `rsync` を確認 |
| `nginx 設定の構文を確認` で failed | 表示された `nginx -t` のエラー行を直す（再起動はまだ走っていない） |
| `デプロイ結果を検証` で failed | `ssh isucon@<ip> cat ~/webapp/DEPLOYED_COMMIT` と `journalctl -u isureal-python -n 50` を見る |
| `アプリが応答するか確認` で failed | `systemctl status isureal-python`、`.venv` が消えていないか確認 |
| `journalctl` の確認で failed | アプリが例外で落ちている。`journalctl -u isureal-python -n 100` を読む |
| `接続を受け付けているか確認` で failed | asia2 で `systemctl status postgresql` と `/var/log/postgresql/postgresql-16-main.log` を見る |
