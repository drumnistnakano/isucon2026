# デプロイ手順（Ansible）

main ブランチの内容を **アプリ3台（asia1 / europe1 / america1）** へ配布し、再起動と検証まで行う。

- サーバには git リポジトリが無く、GitHub へ fetch もできない（`Permission denied (publickey)`）。
  そのため **このPCから rsync で配る方式** を採る。
- asia2 は PostgreSQL 専用。アプリコードは置かない（スキーマ変更は「6. DB 変更」を参照）。
- リポジトリ全体は送らない。`webapp/python/` と `webapp/sql/` だけを送る。

## 1. 構成

```
ansible/
  ansible.cfg           # inventory の場所、forks、SSH 多重化
  inventory.ini         # サーバの IP（変わったらここを直す）
  playbooks/deploy.yml  # 同期 → 記録 → 再起動 → 検証
  README.md             # このファイル
```

## 2. 前提

- このPCから `ssh isucon@<ip>` が鍵認証で通ること（`~/.ssh/id_ed25519`）
- サーバの `isucon` は `sudo` パスワードなしで `systemctl restart isureal-python` を実行できる
- サーバに `rsync` がある（3.2.7 で確認済み）
- サーバに `/home/isucon/local/uv/uv` がある（0.13.5 で確認済み）
- ローカルの main が origin/main と一致し、`webapp/` に未コミット変更が無い

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

```bash
ansible-playbook playbooks/deploy.yml --limit asia1
```

問題なければ戻す・進めるの判断をする（`--limit` を外せば3台）。

### 4.3 3台に適用

```bash
ansible-playbook playbooks/deploy.yml
```

`serial: 1` なので 1台ずつ順番に流れる（クロスリージョンの回線を奪い合わない）。

## 5. 実行される内容

| # | タスク | 内容 |
|---|---|---|
| 1 | ローカル確認 | `git fetch`、HEAD と origin/main の一致、`webapp/` の未コミット検出（このPC上、1回だけ） |
| 2 | python 同期 | `webapp/python/` → `/home/isucon/webapp/python/`（`--delete`） |
| 3 | SQL 同期 | `webapp/sql/` → `/home/isucon/webapp/sql/`（`--delete`） |
| 4 | 依存関係の同期 | python に変更があったホストだけ `uv sync --frozen --no-dev` を実行 |
| 5 | 版の記録 | `/home/isucon/webapp/DEPLOYED_COMMIT` に SHA を書き込む |
| 6 | 再起動 | 変更があったホストだけ `systemctl restart isureal-python`（`become`） |
| 7 | 検証 | `is-active` / `127.0.0.1:8080` の応答 / `DEPLOYED_COMMIT` の一致 / `journalctl` に `Traceback` が無い |

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

そのため、インデックス追加やテーブル変更を含むブランチでは、`sql/001-schema.sql` を直すだけでは
稼働中の asia2 に反映されない。適用用のプレイブック（`playbooks/db-apply.yml`）を別途用意し、
asia2 に対して psql で流す。**必要になった時点で追加する。**

## 7. デプロイ後の確認（ベンチ前チェック）

```bash
# 3台の版が揃っているか（ローカルで実行）
for ip in 35.77.158.213 43.206.38.70 13.115.0.212; do
  ssh isucon@$ip 'printf "%s " $(hostname); cat ~/webapp/DEPLOYED_COMMIT'
done
```

- `systemctl is-active isureal-python` が3台とも `active`（playbook が自動で確認済み）
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
- `inventory.ini` の IP が古いと別スタックへ配ってしまう。再構築したら `inventory.ini` を更新する
- `--delete` を使うので、除外指定（`.venv/` など）を消さないこと
- サーバ側で直接ファイルを編集しない。次のデプロイで上書きされる

## 10. トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| `HEAD (xxx) が origin/main (yyy) と違う` | `git switch main && git pull` してから実行 |
| `webapp に未コミットの変更が無いことを確認` で failed | コミットするか `git stash` する |
| `Synchronize failed` / `rsync: command not found` | サーバの `rsync` とこのPCの `rsync` を確認 |
| `デプロイ結果を検証` で failed | `ssh isucon@<ip> cat ~/webapp/DEPLOYED_COMMIT` と `journalctl -u isureal-python -n 50` を見る |
| `アプリが応答するか確認` で failed | `systemctl status isureal-python`、`.venv` が消えていないか確認 |
| `journalctl` の確認で failed | アプリが例外で落ちている。`journalctl -u isureal-python -n 100` を読む |
