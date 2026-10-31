# デプロイ手順（Ansible）

main ブランチの内容を **アプリ3台（asia1 / europe1 / america1）** と **DB 1台（asia2）** へ配布し、再起動と検証まで行う。

- サーバには git リポジトリが無く、GitHub へ fetch もできない（`Permission denied (publickey)`）。
  そのため **このPCから rsync / copy で配る方式** を採る。
- asia2 は PostgreSQL 専用。アプリコードは置かず、`etc/postgresql/` の設定だけを配る。
- リポジトリ全体は送らない。送るのは次の4つだけ。
  - `webapp/python/` → アプリ3台 `/home/isucon/webapp/python/`
  - `webapp/sql/` → アプリ3台 `/home/isucon/webapp/sql/`
  - `etc/nginx/` → アプリ3台 `/home/isucon/etc/nginx/`
  - `etc/postgresql/` → DB 1台 `/home/isucon/etc/postgresql/`
- 反映先の `/etc` はリポジトリの `etc/` を指す **symlink**。playbook が毎回この形に揃える。
  - `/etc/nginx/nginx.conf` → `/home/isucon/etc/nginx/nginx.conf`
  - `/etc/postgresql/16/main/postgresql.conf` → `/home/isucon/etc/postgresql/16/main/postgresql.conf`
- `etc/` は `webapp/` の隣（リポジトリ直下）。`webapp/` の中には無い。

## 1. 構成

```
ansible/
  ansible.cfg           # inventory の場所、forks、SSH 多重化
  inventory.ini         # サーバの IP（変わったらここを直す）
  playbooks/deploy.yml  # 同期 → 記録 → 再起動 → 検証
  playbooks/bak.yml     # ベンチ前のログ退避・初期化と pg_stat_statements のリセット
  playbooks/netdata.yml # 全4台に netdata を入れる（デプロイとは独立）
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
- アプリ3台から GitHub のリリースへ HTTPS で取得できる（playbook が alp v1.0.21 を導入する）
- 全4台から `repository.netdata.cloud` へ HTTP で取得できる（`playbooks/netdata.yml` が使う）
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
| 1 | 設定の同期 | `etc/postgresql/` → `/home/isucon/etc/postgresql/`（`--delete`） |
| 2 | symlink 作成 | `/etc/postgresql/16/main/postgresql.conf` → `/home/isucon/etc/postgresql/16/main/postgresql.conf`（`become`） |
| 3 | 再起動 | `systemctl restart postgresql`（`become`） |
| 4 | 検証 | `is-active postgresql` / `pg_isready -h 127.0.0.1 -p 5432` / `ss -ltn` に `0.0.0.0:5432` がある / ログに `PANIC` が無い |

### アプリ3台（asia1 / europe1 / america1、1台ずつ）

| # | タスク | 内容 |
|---|---|---|
| 1 | ローカル確認 | `git fetch`、HEAD と origin/main の一致、`webapp/` と `etc/` の未コミット検出（このPC上で1回だけ） |
| 2 | python 同期 | `webapp/python/` → `/home/isucon/webapp/python/`（`--delete`） |
| 3 | SQL 同期 | `webapp/sql/` → `/home/isucon/webapp/sql/`（`--delete`） |
| 4 | nginx 設定同期 | `etc/nginx/` → `/home/isucon/etc/nginx/`（`--delete`） |
| 5 | 依存関係の同期 | python に変更があったホストだけ `uv sync --frozen --no-dev` を実行 |
| 6 | nginx 設定の検証 | `nginx -t -c /home/isucon/etc/nginx/nginx.conf`（`become`）。壊れていれば symlink を差し替えずに止まる |
| 7 | symlink 作成 | `/etc/nginx/nginx.conf` → `/home/isucon/etc/nginx/nginx.conf`（`become`） |
| 8 | 版の記録 | `/home/isucon/webapp/DEPLOYED_COMMIT` に SHA を書き込む |
| 9 | 再起動 | `systemctl restart isureal-python` → `systemctl restart nginx`（`become`） |
| 10 | 検証 | `is-active`（アプリ・nginx）/ `127.0.0.1:8080` の応答 / `DEPLOYED_COMMIT` の一致 / `journalctl` に `Traceback` が無い |
| 11 | alp の導入 | `alp` v1.0.21 を `/usr/local/bin/alp` に配置（同じ版が入っていれば skip） |

### 送らないもの（意図的）

| 対象 | 理由 |
|---|---|
| `~/env.sh` | サーバ固有（`ISUREAL_REGION` が asia/europe/america）＋秘密情報を含む |
| `webapp/public/` | 配布された静的ファイルは内容維持が必須 |
| `webapp/{go,nodejs,perl,php,ruby,rust}/` | 使わない言語実装 |
| `webapp/python/.venv/`, `__pycache__/` | rsync では消さず、`.venv` は `uv sync` で更新する |
| DB のデータ | `POST /api/initialize` が初期化する |

## 6. DB 変更（スキーマ・インデックス）

**`sql/` 配下は置いてあるだけでは適用されない。** 実行されるのは `reset.sql` だけ。

| ファイル | 誰が実行するか | タイミング |
|---|---|---|
| `reset.sql` | `init.sh` ← `initialize.py` ← `POST /api/initialize` | ベンチ開始時（毎回） |
| `001-schema.sql` | なし（初期構築用。リポジトリ内に実行箇所が無い） | 手作業 |
| `003-initial-snapshot.sql` | なし | 手作業 |

`deploy.yml` は `webapp/sql/` を rsync で配るだけで、psql は叩かない
（`postgresql.conf` だけは配布して PostgreSQL を再起動する）。

稼働中の DB に DDL を効かせたいときは、**次の2箇所に同じ定義を書く**。

1. `ansible/vars/pg_schema.yml` の `pg_schema_ddl` … `deploy.yml` がデプロイのたびに流す
2. `webapp/sql/reset.sql` … `/api/initialize` のたびに流れる（保険）
3. `webapp/sql/001-schema.sql` … 初期構築用の正（実行はされない）

どれも `IF NOT EXISTS` を付けて、何度流しても壊れないようにする。
デプロイ時点で作っておくのは、`/api/initialize` より先にリクエストが来ても困らないため
（例: `ON CONFLICT (username)` は一意インデックスが無いとエラーになる）。

単体で適用したいときだけ `playbooks/db-apply.yml` を使う。

```bash
cd ansible
ansible-playbook playbooks/db-apply.yml            # 何度実行してもよい
ansible-playbook playbooks/db-apply.yml --check    # 変更点の確認だけ
```

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
- nginx / アプリのログをクリアしてから計測する（下記の `bak.yml`）
- **暖機運転はしない**。再起動後の初回ベンチをスコアとして記録する
- 記録項目: 日時 / IP / コミット / netdata の有無 / slow log の状態 / score / success / fail / df / メモ

### ベンチ前のログ初期化

```bash
ansible-playbook playbooks/bak.yml
```

| 対象 | 内容 |
|---|---|
| アプリ3台 | `/var/log/nginx/access.log` を `/var/log/nginx/backup/access.log.<日時>` に退避して 0 バイトにする |
| DB 1台 | `/var/log/postgresql/postgresql-16-main.log` を `/var/log/postgresql/backup/` に退避して 0 バイトにする |
| DB 1台 | `pg_stat_statements_reset()` で集計をリセットする |
| このPC | 退避したログを `logs/<日時>/<ホスト名>/` に取ってくる（`logs/` は `.gitignore` 済み） |

```text
logs/20261031_130000/
├── asia1/access.log.20261031_130000
├── europe1/access.log.20261031_130000
├── america1/access.log.20261031_130000
└── asia2/postgresql-16-main.log.20261031_130000
```

- サービスは再起動しない（nginx も PostgreSQL もログを追記モードで開いているため、truncate 後も書き込みは続く）
- ローカルの main との一致は確認しないので、どのブランチからでも実行できる
- ローカルに取ってこない場合は `-e bak_fetch=false`
- `--limit` を使うときは `localhost` も含める（含めないと日時がホストごとに別になる）
- サーバー側の退避ファイルは消さないので、溜まったら `df -h /` を見て手で消す

## 8. netdata の導入（全4台）

計測用に netdata を全4台へ入れる。**デプロイとは独立**したプレイブックで、`deploy.yml` からは呼ばない。

```bash
cd ansible
ansible-playbook playbooks/netdata.yml                  # 4台すべて
ansible-playbook playbooks/netdata.yml --limit asia2    # 1台だけ
```

- asia1 と同じ **Netdata 公式 apt リポジトリ（edge チャンネル）** のネイティブパッケージを使う
- リポジトリと鍵は `netdata-repo-edge` の .deb で入れる（`/etc/apt/sources.list.d/netdata-edge.sources`）
- **既に入っていればダウンロードもインストールもしない**（何度実行してもよい）
- `netdata` を自動起動にし、`netdata-updater.timer` は止める（競技中に勝手に更新・再起動されないように）
- 検証: `is-active netdata` / `ss -ltn` に `:19999` / `127.0.0.1:19999/api/v1/info` が 200 / 最後にバージョンを表示

| # | タスク | 内容 |
|---|---|---|
| 1 | 前提確認 | Ubuntu 24.04 (noble) 以外では止まる |
| 2 | 導入状況の確認 | `dpkg-query` で netdata の有無を判定 |
| 3 | リポジトリ設定 | 未導入のときだけ .deb を取得して `apt` に渡す |
| 4 | インストール | `apt install netdata`（`update_cache`） |
| 5 | 自動更新の停止 | `netdata-updater.timer` を disable + stop |
| 6 | 起動 | `systemctl enable --now netdata` |
| 7 | 検証 | active / 19999 待ち受け / API 200 / バージョン表示 |

計測の前後は AGENTS.md のルールに従う。**ベンチ前に `sudo systemctl stop netdata` で止める**（CPU を使わせない）。
ダッシュボードは `http://<public-ip>:19999/`。セキュリティグループで 19999 が開いていること。
`/var/cache/netdata` に十数MB書くので、`df -h /` を確認する。

## 9. ロールバック

```bash
# 回帰したコミットを revert する PR を作る → main にマージ
git switch main && git pull
cd ansible && ansible-playbook playbooks/deploy.yml
```

main を配るだけなので、戻す操作もデプロイと同じ1コマンド。

## 10. 注意事項

- **ベンチマーク実行中にデプロイしない**（`isureal-python` の再起動で SSE と接続が切れる）
- alp は**アプリ3台にだけ**入れる（asia2 は DB 専用で nginx のログが無い）。
  デプロイのたびに `alp --version` を確認し、`1.0.21` と同じならダウンロードしない
- **再起動は毎回走る**。`postgresql` の再起動でアプリの DB 接続は一度切れるため、
  必ず DB → アプリの順で実行する（playbook 側で固定済み）
- `--limit` を使うときは `localhost` も含める（例: `--limit 'asia1,localhost'`）。
  忘れると何も変更せずに止まる
- `inventory.ini` の IP が古いと別スタックへ配ってしまう。再構築したら `inventory.ini` を更新する
- `--delete` を使うので、除外指定（`.venv/` など）を消さないこと
- サーバ側で直接ファイルを編集しない。次のデプロイで上書きされる
  - `/etc/nginx/nginx.conf` → `~/etc/nginx/nginx.conf` への symlink
  - `/etc/postgresql/16/main/postgresql.conf` → `~/etc/postgresql/16/main/postgresql.conf` への symlink
- **初回だけ `/etc` の実ファイルが symlink に置き換わる**（`force: true`）。asia2 の
  `postgresql.conf` と、europe1 / america1 の既定 `nginx.conf` がこれに当たる。
  置き換え後の内容はリポジトリの `etc/` と同じになる
- `postgresql.conf` の `include_dir = 'conf.d'` は `/etc/postgresql/16/main/conf.d` を指す
  （相対パスは参照元ファイルの位置を基準に解決される）。`conf.d/isucon.conf` の
  `listen_addresses = '*'` はそのまま有効

## 11. トラブルシュート

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
| `alp のアーカイブをダウンロード` で failed | サーバから GitHub へ HTTPS で出られるか確認（`ssh isucon@<ip> curl -I https://github.com`） |
| `リポジトリ設定の deb をダウンロード` で failed | サーバから `repository.netdata.cloud` へ HTTP で出られるか確認（`ssh isucon@<ip> curl -I http://repository.netdata.cloud`） |
| `netdata をインストール` で failed | `apt policy netdata` で edge リポジトリが見えているか、`sudo apt update` のエラーを読む |
| `19999 で待ち受けているか確認` で failed | `systemctl status netdata` と `/var/log/netdata/error.log` を見る。`netdata.conf` の `bind to` を変えていないか確認 |
| ダッシュボードに外から繋がらない | セキュリティグループで 19999/tcp が開いているか確認（サーバ側の ufw は既定で inactive） |
