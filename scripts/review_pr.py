#!/usr/bin/env python3
"""
Automated PR Review Script
Uses Claude AI to review pull requests with focus on security and quality.

Required env vars:
  GITHUB_TOKEN      - GitHub token with pull-requests:write permission
  ANTHROPIC_API_KEY - Anthropic API key
  LINEAR_API_KEY    - (optional) Linear personal API key
  REPO_OWNER        - GitHub repository owner
  REPO_NAME         - GitHub repository name
  PR_NUMBER         - Pull request number to review
"""

import os
import re
import sys
import requests
import anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")
REPO_OWNER = os.environ["REPO_OWNER"]
REPO_NAME = os.environ["REPO_NAME"]
PR_NUMBER = int(os.environ["PR_NUMBER"])

GITHUB_API = "https://api.github.com"
LINEAR_API = "https://api.linear.app/graphql"

# Truncate diff at this character count to stay within token limits
MAX_DIFF_CHARS = 80_000

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_headers(accept: str = "application/vnd.github.v3+json") -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr() -> dict:
    r = requests.get(
        f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",
        headers=_gh_headers(),
    )
    r.raise_for_status()
    return r.json()


def get_diff() -> str:
    r = requests.get(
        f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",
        headers=_gh_headers("application/vnd.github.v3.diff"),
    )
    r.raise_for_status()
    return r.text


def get_files() -> list[dict]:
    r = requests.get(
        f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/files",
        headers=_gh_headers(),
        params={"per_page": 100},
    )
    r.raise_for_status()
    return r.json()


def post_review(body: str, event: str = "COMMENT") -> dict:
    """Post a pull request review. event: APPROVE | REQUEST_CHANGES | COMMENT"""
    r = requests.post(
        f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews",
        headers=_gh_headers(),
        json={"body": body, "event": event},
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Linear helpers
# ---------------------------------------------------------------------------

def _linear_request(query: str, variables: dict | None = None) -> dict | None:
    if not LINEAR_API_KEY:
        return None
    r = requests.post(
        LINEAR_API,
        headers={"Authorization": LINEAR_API_KEY, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  ⚠️  Linear API error {r.status_code}: {r.text[:200]}")
        return None
    return r.json()


def find_linear_ids(text: str) -> list[str]:
    """Return unique Linear issue identifiers like ENG-123 found in text."""
    return list(set(re.findall(r"\b([A-Z]{2,10}-\d+)\b", text or "")))


def get_linear_issue(identifier: str) -> dict | None:
    """Fetch issue by identifier (e.g. 'ENG-123').
    Linear's issue(id:) field accepts both UUID and identifier string."""
    query = """
    query($id: String!) {
      issue(id: $id) {
        id
        identifier
        title
        description
        state { id name }
        team { id key }
        assignee { name }
      }
    }
    """
    data = _linear_request(query, {"id": identifier})
    if not data:
        return None
    return data.get("data", {}).get("issue")


def get_in_review_state_id(team_id: str) -> str | None:
    """Return the workflow state ID for 'In Review' (or equivalent) in the team."""
    query = """
    query($teamId: String!) {
      workflowStates(filter: { team: { id: { eq: $teamId } } }) {
        nodes { id name }
      }
    }
    """
    data = _linear_request(query, {"teamId": team_id})
    if not data:
        return None
    states = data.get("data", {}).get("workflowStates", {}).get("nodes", [])
    target_names = {"in review", "in_review", "レビュー中", "review", "reviewing"}
    for state in states:
        if state["name"].lower() in target_names:
            return state["id"]
    return None


def update_issue_to_in_review(issue_id: str, state_id: str) -> bool:
    mutation = """
    mutation($id: String!, $stateId: String!) {
      issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
        issue { state { name } }
      }
    }
    """
    data = _linear_request(mutation, {"id": issue_id, "stateId": state_id})
    if not data:
        return False
    return data.get("data", {}).get("issueUpdate", {}).get("success", False)


# ---------------------------------------------------------------------------
# Claude review
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
あなたはセキュリティ・品質を最重視するシニアコードレビュアーです。
You are a senior code reviewer specialising in security, correctness, and engineering quality.

## Review Priorities (highest to lowest)

1. **Security vulnerabilities** — especially in `/payments/` and `/auth/` directories:
   - Authentication/authorization bypasses
   - Injection attacks (SQL, XSS, command injection, SSTI)
   - Insecure token/secret/credential handling
   - CSRF / session fixation
   - Payment flow integrity violations (double-charge, negative amounts, race conditions)
   - Cryptographic weaknesses (weak algorithms, hardcoded keys, predictable randomness)
   - Missing input validation at trust boundaries

2. **Missing tests** — new business logic or security-critical code without corresponding tests

3. **Logic errors** — race conditions, incorrect calculations, null/undefined handling, off-by-one, edge cases

4. **Code quality** — maintainability, performance bottlenecks, missing error handling

## Output Format

Write a bilingual review in Markdown (Japanese primary, English secondary).

Structure:
- **Opening**: mention @{author} and give a 1–2 sentence summary
- Include only the sections that apply:
  - `## 🔒 セキュリティ / Security`  — tag critical items with `> ⛔ CRITICAL:`, warnings with `> ⚠️ WARNING:`
  - `## 🧪 テスト不足 / Missing Tests`
  - `## ⚠️ 論理的問題 / Logic Issues`
  - `## 💡 改善提案 / Suggestions`
  - `## ✅ 良い点 / Positives`
- **Closing line** (exactly one of):
  - `**判定 / Decision: ✅ APPROVE**`
  - `**判定 / Decision: 💬 COMMENT**`
  - `**判定 / Decision: 🚫 REQUEST_CHANGES**`

Be specific: cite `filename:approx-line` locations. Be constructive and actionable.\
"""


def build_review(
    pr: dict,
    diff: str,
    files: list[dict],
    linear_issues: dict[str, dict | None],
) -> tuple[str, str]:
    """Call Claude API and return (review_body, github_event)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    author = pr["user"]["login"]
    title = pr.get("title", "")
    pr_body = pr.get("body") or ""
    base_ref = pr.get("base", {}).get("ref", "main")

    # File summary
    file_lines = "\n".join(
        f"- `{f['filename']}` (+{f['additions']}/-{f['deletions']})"
        for f in files
    )

    # Flag sensitive paths
    sensitive = [
        f for f in files
        if re.search(r"(^|/)(payments|auth)/", f["filename"])
    ]
    sensitive_block = ""
    if sensitive:
        names = "\n".join(f"  - `{f['filename']}`" for f in sensitive)
        sensitive_block = (
            f"\n> ⛔ **セキュリティ重要ファイル / SENSITIVE FILES — extra scrutiny required:**\n{names}\n"
        )

    # Linear context
    linear_block = ""
    if linear_issues:
        parts = []
        for iid, issue in linear_issues.items():
            if issue:
                desc = (issue.get("description") or "")[:400]
                parts.append(f"**{iid}** — {issue['title']}\n> {desc}")
        if parts:
            linear_block = (
                "\n## 関連Linearイシュー / Related Linear Issues\n\n"
                + "\n\n".join(parts)
                + "\n"
            )

    # Truncate diff
    truncation_note = ""
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncation_note = (
            "\n\n> **注**: diffが大きいため先頭部分のみ表示 / "
            "Note: diff truncated to first ~80 000 chars due to size."
        )

    user_content = f"""\
## PR #{PR_NUMBER}: {title}
**Author**: @{author} → `{base_ref}`
{sensitive_block}
## Changed Files ({len(files)} total)
{file_lines}
{linear_block}
## Full Diff
```diff
{diff}
```{truncation_note}
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    review_text = response.content[0].text

    # Derive GitHub review event from closing line
    if "REQUEST_CHANGES" in review_text:
        event = "REQUEST_CHANGES"
    elif "APPROVE" in review_text:
        event = "APPROVE"
    else:
        event = "COMMENT"

    return review_text, event


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"🔍 PR #{PR_NUMBER} の自動レビューを開始します...")

    pr = get_pr()
    author = pr["user"]["login"]
    print(f"  タイトル : {pr['title']}")
    print(f"  作者     : @{author}")

    diff = get_diff()
    files = get_files()
    print(f"  変更ファイル: {len(files)} 件 / diff: {len(diff):,} chars")

    # Collect Linear issue IDs from title, body, and branch name
    head_ref = pr.get("head", {}).get("ref", "")
    search_text = f"{pr['title']} {pr.get('body') or ''} {head_ref}"
    linear_ids = find_linear_ids(search_text)
    print(f"  Linearイシュー候補: {linear_ids or '(なし)'}")

    linear_issues: dict[str, dict | None] = {}
    linear_meta: dict[str, dict] = {}  # identifier -> {id, team_id}
    for lid in linear_ids:
        issue = get_linear_issue(lid)
        linear_issues[lid] = issue
        if issue:
            linear_meta[lid] = {
                "id": issue["id"],
                "team_id": (issue.get("team") or {}).get("id"),
            }
            print(f"  Linear {lid}: {issue['title']} (状態: {issue['state']['name']})")
        else:
            print(f"  Linear {lid}: 取得できませんでした / not found")

    # Generate review with Claude
    print("  Claude でレビュー生成中...")
    review_body, event = build_review(pr, diff, files, linear_issues)
    print(f"  レビュー判定: {event}")

    # Post review to GitHub
    post_review(review_body, event)
    print(f"  ✅ レビューを投稿しました (event={event})")

    # Update Linear issues to "In Review"
    for lid, meta in linear_meta.items():
        team_id = meta.get("team_id")
        if not team_id:
            print(f"  ⚠️  Linear {lid}: team_id 不明のためスキップ")
            continue
        state_id = get_in_review_state_id(team_id)
        if not state_id:
            print(f"  ⚠️  Linear {lid}: 'In Review' ステートが見つかりません")
            continue
        ok = update_issue_to_in_review(meta["id"], state_id)
        icon = "✅" if ok else "❌"
        print(f"  {icon} Linear {lid} → 'In Review': {'成功' if ok else '失敗'}")

    print("✅ 自動レビュー完了!")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"❌ レビュースクリプトがエラーで終了しました: {exc}", file=sys.stderr)
        raise
