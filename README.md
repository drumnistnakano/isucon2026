# isucon2026

ISUReal（ISUCON2026）のリファレンス実装と、競技環境の運用ファイル。

## 構成

| パス | 内容 |
|---|---|
| `webapp/` | リファレンス実装（Go / Node.js / Perl / PHP / Python / Ruby / Rust）と `sql/` |
| `ansible/` | アプリ3台へのデプロイ手順（Ansible） |

## サーバ構成

| name | 役割 | Public IPv4 | Private IPv4 |
|---|---|---|---|
| asia1 | nginx + アプリ（region: asia） | 35.77.158.213 | 192.168.1.10 |
| europe1 | nginx + アプリ（region: europe） | 43.206.38.70 | 192.168.2.10 |
| america1 | nginx + アプリ（region: america） | 13.115.0.212 | 192.168.3.10 |
| asia2 | PostgreSQL 16 | 52.198.70.111 | 192.168.4.10 |

- 使用実装: **Python**（`isureal-python.service` が稼働。`isureal-go.service` は停止済み）
- IP は再構築で変わる。変わったら `ansible/inventory.ini` と `ip-list.md` を両方直す。

## デプロイ

```bash
cd ansible
ansible-playbook playbooks/deploy.yml                       # 3台に適用
ansible-playbook playbooks/deploy.yml -e rsync_dry_run=true # 事前確認（変更しない）
```

詳細・注意事項は [ansible/README.md](ansible/README.md) を参照。

## 運用の流れ

1. `perf/xxx` ブランチで変更 → commit → push → PR
2. レビューして main にマージ
3. `git switch main && git pull`
4. `ansible-playbook playbooks/deploy.yml`
5. ポータルからベンチマーク実行、`DEPLOYED_COMMIT` とスコアを記録
6. 回帰したら revert の PR → main → 4 を再実行
