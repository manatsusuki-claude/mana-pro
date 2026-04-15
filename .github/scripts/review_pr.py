#!/usr/bin/env python3
"""
Automated PR Review Script

Pull Requestが開かれると自動実行され、以下を行います：
  1. PRの差分・ファイル一覧を取得
  2. PR説明から関連Linear issueを検出・取得
  3. Claude APIでコードレビューを生成
  4. PRにレビューコメントを投稿（作者メンション付き）
  5. Linear issueのステータスを「In Review」に更新
"""

import os
import re
import sys
import json
import requests
import anthropic

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")
PR_NUMBER = int(os.environ["PR_NUMBER"])
PR_REPO = os.environ["PR_REPO"]  # e.g. "owner/repo"
PR_AUTHOR = os.environ["PR_AUTHOR"]
PR_TITLE = os.environ.get("PR_TITLE", "")
PR_BODY = os.environ.get("PR_BODY", "") or ""

REPO_OWNER, REPO_NAME = PR_REPO.split("/", 1)

GITHUB_API = "https://api.github.com"
LINEAR_API = "https://api.linear.app/graphql"

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# パスに含まれると特に注意するキーワード
SENSITIVE_KEYWORDS = [
    "/payments/", "/auth/", "payment", "auth", "login",
    "token", "secret", "password", "credential",
]


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def get_pr_diff() -> str:
    """PRの差分（unified diff形式）を取得する。"""
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}"
    headers = {**GH_HEADERS, "Accept": "application/vnd.github.v3.diff"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def get_pr_files() -> list[dict]:
    """PRで変更されたファイル一覧を取得する。"""
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/files"
    resp = requests.get(url, headers=GH_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def post_pr_review(body: str) -> dict:
    """PRにレビューコメントを投稿する。"""
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"
    payload = {"body": body, "event": "COMMENT"}
    resp = requests.post(url, headers=GH_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Linear helpers
# ---------------------------------------------------------------------------

def _linear_post(query: str, variables: dict | None = None) -> dict:
    """Linear GraphQL APIにリクエストを送信する。"""
    headers = {
        "Authorization": LINEAR_API_KEY,
        "Content-Type": "application/json",
    }
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(LINEAR_API, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def extract_linear_issue_ids(text: str) -> list[str]:
    """テキストからLinear issue識別子（例: MANA-123）を抽出する。"""
    # Linear URL 形式: linear.app/.../issue/TEAM-123
    url_ids = re.findall(r"linear\.app/[^/]+/issue/([A-Z]{2,10}-\d+)", text)
    # 単体識別子形式: TEAM-123
    bare_ids = re.findall(r"\b([A-Z]{2,10}-\d+)\b", text)
    return list(dict.fromkeys(url_ids + bare_ids))  # 重複除去・順序保持


def get_linear_issue(identifier: str) -> dict | None:
    """Linear issue の詳細を取得する。"""
    if not LINEAR_API_KEY:
        return None
    query = """
    query GetIssue($id: String!) {
        issue(id: $id) {
            id
            identifier
            title
            description
            state { id name }
            assignee { name }
        }
    }
    """
    try:
        data = _linear_post(query, {"id": identifier})
        return (data.get("data") or {}).get("issue")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Linear issue fetch failed ({identifier}): {exc}")
        return None


def find_in_review_state_id() -> str | None:
    """Linear の 'In Review' ワークフロー状態IDを検索して返す。"""
    if not LINEAR_API_KEY:
        return None
    query = """
    query {
        workflowStates(filter: { name: { eq: "In Review" } }) {
            nodes { id name }
        }
    }
    """
    try:
        data = _linear_post(query)
        nodes = (data.get("data") or {}).get("workflowStates", {}).get("nodes", [])
        return nodes[0]["id"] if nodes else None
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Linear state lookup failed: {exc}")
        return None


def update_linear_status(issue_id: str, state_id: str) -> bool:
    """Linear issueのステータスを更新する。"""
    if not LINEAR_API_KEY or not state_id:
        return False
    mutation = """
    mutation UpdateIssue($id: String!, $stateId: String!) {
        issueUpdate(id: $id, input: { stateId: $stateId }) {
            success
            issue { id state { name } }
        }
    }
    """
    try:
        data = _linear_post(mutation, {"id": issue_id, "stateId": state_id})
        return (data.get("data") or {}).get("issueUpdate", {}).get("success", False)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Linear status update failed ({issue_id}): {exc}")
        return False


# ---------------------------------------------------------------------------
# Claude review
# ---------------------------------------------------------------------------

def generate_review(
    diff: str,
    files: list[dict],
    linear_issues: list[dict],
) -> str:
    """Claude APIを使ってコードレビューを生成する。"""
    changed_files = [f["filename"] for f in files]
    sensitive_files = [
        f for f in changed_files
        if any(kw in f for kw in SENSITIVE_KEYWORDS)
    ]

    # Linear issueのコンテキスト文字列を構築
    linear_ctx = ""
    if linear_issues:
        lines = ["\n## 関連Linear Issues"]
        for issue in linear_issues:
            lines.append(f"- **{issue.get('identifier')}**: {issue.get('title')}")
            if issue.get("description"):
                lines.append(f"  > {issue['description'][:400]}")
        linear_ctx = "\n".join(lines)

    security_note = ""
    if sensitive_files:
        security_note = (
            f"\n> ⚠️ セキュリティ注意が必要なファイルが含まれています: "
            f"`{'`, `'.join(sensitive_files)}`\n"
        )

    prompt = f"""あなたは経験豊富なシニアソフトウェアエンジニアです。
以下のPull Requestを丁寧かつ建設的にレビューしてください。

## PR情報
- **タイトル**: {PR_TITLE}
- **説明**: {PR_BODY[:1000] or '(なし)'}
- **変更ファイル数**: {len(changed_files)}
- **変更ファイル**: {', '.join(changed_files[:30])}
{security_note}{linear_ctx}

## 差分
```diff
{diff[:10000]}
```

---
以下の観点でレビューし、GitHub PRコメントとしてMarkdown形式で出力してください。

### レビュー観点
1. **テストの欠落** — 新機能・バグ修正に対応するテストが不足していないか
2. **ロジックの問題** — バグ、エッジケース未処理、誤ったアルゴリズム
3. **セキュリティリスク** — `/payments/` や `/auth/` 配下は特に厳密に確認すること
   （SQLインジェクション、XSS、認証バイパス、機密情報の漏洩 etc.）
4. **コード品質** — 可読性・命名規則・重複・不要なコード
5. **パフォーマンス** — 明らかな非効率やN+1問題

### 出力フォーマット
```
## レビュー概要
（全体的な評価を2〜3文で）

## 指摘事項
（各指摘はファイルパスと具体的な説明を含める）

## 改善提案
（コードサンプルを含む具体的な提案）

## 総評
（良い点を含め、建設的に締めくくる）
```

丁寧で建設的なトーンを維持し、良い実装には積極的に称賛してください。
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"=== PR #{PR_NUMBER} の自動レビューを開始します ===")

    # 1. PRの差分・ファイル一覧を取得
    print("[1/5] 差分を取得中...")
    diff = get_pr_diff()
    print("[2/5] 変更ファイル一覧を取得中...")
    files = get_pr_files()

    # 2. Linear issueを検出・取得
    print("[3/5] Linear issueを確認中...")
    issue_ids = extract_linear_issue_ids(f"{PR_TITLE} {PR_BODY}")
    linear_issues: list[dict] = []
    fetched_linear_ids: list[str] = []
    for iid in issue_ids:
        issue = get_linear_issue(iid)
        if issue:
            linear_issues.append(issue)
            fetched_linear_ids.append(issue["id"])
            print(f"  -> Linear issue 取得: {issue.get('identifier')} — {issue.get('title')}")

    # 3. Claude でレビュー生成
    print("[4/5] Claude でレビューを生成中...")
    review_body = generate_review(diff, files, linear_issues)

    # 4. PRにレビューを投稿（作者メンション付き）
    print("[5/5] PRにレビューを投稿中...")
    full_comment = (
        f"@{PR_AUTHOR} レビュー結果をお届けします。\n\n"
        f"{review_body}\n\n"
        "---\n"
        "*このレビューは [Claude AI](https://claude.ai) によって自動生成されました。*"
    )
    post_pr_review(full_comment)
    print("  -> レビュー投稿完了")

    # 5. Linear issueのステータスを「In Review」に更新
    if fetched_linear_ids and LINEAR_API_KEY:
        state_id = find_in_review_state_id()
        if state_id:
            for lid in fetched_linear_ids:
                ok = update_linear_status(lid, state_id)
                print(f"  -> Linear {lid} ステータス更新: {'成功' if ok else '失敗'}")
        else:
            print("  -> 'In Review' ステータスが見つかりませんでした")

    print("=== 自動レビュー完了 ===")


if __name__ == "__main__":
    main()
