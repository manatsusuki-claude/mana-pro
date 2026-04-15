# mana-pro

Pull Requestが開かれると自動でコードレビューを実行するリポジトリです。

## 自動PRレビュー機能

`main` ブランチへのPRが作成されると、以下を自動実行します：

1. PRのタイトル・説明・全diffを取得
2. 関連するLinear issueの確認（`/payments/` や `/auth/` 配下の変更は特に注意）
3. テスト欠落・ロジック問題・セキュリティリスクのレビュー
4. 具体的な指摘と改善提案をPRにコメント投稿
5. PRの作者をメンション（`@ユーザー名`）
6. Linear issueのステータスを「In Review」に更新

## 必要なSecretsの設定

GitHub repository の Settings > Secrets and variables > Actions に以下を設定してください：

| Secret名 | 説明 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude APIキー（必須） |
| `LINEAR_API_KEY` | Linear APIキー（任意・Linear連携時） |

`GITHUB_TOKEN` はActions実行時に自動提供されます。
