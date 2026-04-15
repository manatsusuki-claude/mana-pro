# mana-pro — Claude Code Guide

## Repository Overview

This repository uses automated PR review powered by Claude AI.
When a pull request targeting `main` is opened, a GitHub Actions workflow
(`pr-review.yml`) invokes `scripts/review_pr.py`, which:

1. Fetches the full diff and changed-file list
2. Detects related Linear issues from the PR title, body, and branch name
3. Calls Claude (`claude-sonnet-4-6`) with security-focused instructions
4. Posts a bilingual (Japanese/English) review comment, mentioning the PR author
5. Updates any linked Linear issues to **"In Review"**

---

## Required Secrets (GitHub repo settings)

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API — used by `review_pr.py` |
| `LINEAR_API_KEY` | Linear personal API key — optional; enables Linear integration |
| `GITHUB_TOKEN` | Provided automatically by GitHub Actions |

---

## Sensitive Paths — Extra Scrutiny Required

Changes under the following paths trigger the most rigorous security review:

- **`/payments/`** — payment flows, charge logic, currency handling
- **`/auth/`** — authentication, authorisation, session management, tokens

For these paths the reviewer specifically checks:
- Authentication/authorisation bypasses
- Injection vulnerabilities (SQL, XSS, command)
- Insecure cryptographic primitives or hardcoded secrets
- CSRF risks and session fixation
- Payment integrity (double-charge, negative amounts, race conditions)
- Input validation at trust boundaries

---

## Session-Based PR Review (Claude Code)

When an active Claude Code session is running, you can watch a PR in real time:

```
# Subscribe to a specific PR
<use mcp__github__subscribe_pr_activity with owner, repo, pullNumber>
```

Upon receiving a `<github-webhook-activity>` event for a newly opened PR,
execute the full review flow below **autonomously** (no human confirmation needed):

### Review Checklist

1. **Fetch PR data**
   - `mcp__github__pull_request_read` → `get` (title, author, body)
   - `mcp__github__pull_request_read` → `get_diff`
   - `mcp__github__pull_request_read` → `get_files`

2. **Identify Linear issues**
   - Scan PR title, body, and branch name for identifiers like `ENG-123`
   - Use `mcp__Linear__get_issue` to fetch title, description, current state
   - If not found via identifier, try `mcp__Linear__list_issues` with a filter

3. **Analyse the diff**
   - Check ALL changed files for logic issues, error handling gaps, missing tests
   - Apply **heightened scrutiny** to `/payments/` and `/auth/` changes (see above)
   - Verify that new business logic has corresponding test files

4. **Write the review comment** (bilingual, Markdown)
   - Mention `@{author}` in the opening sentence
   - Use sections only where applicable:
     - `## 🔒 セキュリティ / Security` (mark critical: `> ⛔ CRITICAL:`)
     - `## 🧪 テスト不足 / Missing Tests`
     - `## ⚠️ 論理的問題 / Logic Issues`
     - `## 💡 改善提案 / Suggestions`
     - `## ✅ 良い点 / Positives`
   - Close with exactly one of:
     - `**判定 / Decision: ✅ APPROVE**`
     - `**判定 / Decision: 💬 COMMENT**`
     - `**判定 / Decision: 🚫 REQUEST_CHANGES**`

5. **Post the review**
   - `mcp__github__pull_request_review_write` with the appropriate event
     (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`)

6. **Update Linear**
   - For each linked issue, change state to **"In Review"**
   - Use `mcp__Linear__save_issue` with the new `stateId`
   - If "In Review" state doesn't exist, log a warning and skip

---

## Common Commands

```bash
# Install review script dependencies locally
pip install anthropic requests

# Run review manually against a PR
GITHUB_TOKEN=... ANTHROPIC_API_KEY=... LINEAR_API_KEY=... \
  REPO_OWNER=manatsusuki-claude REPO_NAME=mana-pro PR_NUMBER=42 \
  python scripts/review_pr.py
```

---

## Development Branch

Active development branch: `claude/jolly-lamport-HTB0F`
Always push to this branch. Do **not** push directly to `main`.
