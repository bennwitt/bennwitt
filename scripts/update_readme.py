#!/usr/bin/env python3
"""Regenerate the dynamic sections of README.md from live GitHub data.

All API access goes through the `gh` CLI, so the script needs no token
handling of its own: locally it uses your `gh auth login`, and in GitHub
Actions it uses whatever token is exported as GH_TOKEN.

Sections are rewritten between <!--START_SECTION:name--> and
<!--END_SECTION:name--> markers; everything outside the markers is left alone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOGIN = os.environ.get("GH_LOGIN", "bennwitt")
# Repos that never appear on the profile page.
EXCLUDE = {"notty", LOGIN}

NOW_BUILDING_COUNT = 5       # repos shown in "Now building"
ACTIVITY_DAYS = 14           # window for "Recent activity"
ACTIVITY_MAX = 10            # max commits listed
ACTIVITY_PER_REPO = 3        # max commits per repo in the feed
COMMIT_WINDOW_DAYS = 30      # window for the commit count in the summary
LANGUAGE_COUNT = 6           # languages shown in the bar chart
BAR_WIDTH = 24

README = Path(__file__).resolve().parent.parent / "README.md"
NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# GitHub access
# --------------------------------------------------------------------------- #
def gh(path: str, paginate: bool = False):
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github+json"]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    cmd.append(path)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    if paginate:
        merged = []
        for page in data:
            merged.extend(page)
        return merged
    return data


def fetch_repos() -> list[dict]:
    """Owned repos, private ones included when the token can see them."""
    try:
        repos = gh("user/repos?affiliation=owner&per_page=100&sort=pushed", paginate=True)
        scope = "owner"
    except RuntimeError as err:
        # GITHUB_TOKEN from Actions cannot call /user; fall back to public data.
        print(f"owner view unavailable ({err}); falling back to public repos", file=sys.stderr)
        repos = gh(f"users/{LOGIN}/repos?type=owner&per_page=100&sort=pushed", paginate=True)
        scope = "public"
    repos = [
        r for r in repos
        if not r.get("fork") and not r.get("archived") and r["name"] not in EXCLUDE
    ]
    print(f"repos: {len(repos)} ({scope} view)", file=sys.stderr)
    return repos


def fetch_languages(repo: dict) -> dict[str, int]:
    try:
        return gh(f"repos/{repo['full_name']}/languages")
    except RuntimeError:
        return {}


def fetch_commits(repo: dict, since: datetime) -> list[dict]:
    try:
        return gh(
            f"repos/{repo['full_name']}/commits?since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}&per_page=100",
            paginate=True,
        )
    except RuntimeError:
        return []


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ago(ts: datetime) -> str:
    delta = NOW - ts
    days = delta.days
    if days <= 0:
        hours = delta.seconds // 3600
        return "just now" if hours == 0 else f"{hours}h ago"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months > 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years > 1 else ''} ago"


def status(ts: datetime) -> str:
    days = (NOW - ts).days
    if days < 30:
        return "🟢 active"
    if days < 90:
        return "🟡 recent"
    return "⚪ quiet"


def md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def repo_link(repo: dict) -> str:
    if repo["private"]:
        return f"🔒 **{repo['name']}**"
    return f"**[{repo['name']}]({repo['html_url']})**"


def first_line(message: str, limit: int = 72) -> str:
    line = message.strip().splitlines()[0] if message.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def build_now(repos: list[dict]) -> str:
    lines = []
    for r in repos[:NOW_BUILDING_COUNT]:
        bits = [f"`{r['language']}`" if r.get("language") else None,
                f"pushed {ago(parse_ts(r['pushed_at']))}",
                f"{r['open_issues_count']} open issue{'s' if r['open_issues_count'] != 1 else ''}"
                if r.get("open_issues_count") else None]
        meta = " · ".join(b for b in bits if b)
        desc = md_escape(r.get("description") or "")
        lines.append(f"- {repo_link(r)}" + (f" — {desc}" if desc else "") + f"  \n  <sub>{meta}</sub>")
    return "\n".join(lines) if lines else "_Nothing public to show yet._"


def build_projects(repos: list[dict]) -> str:
    head = "| Project | About | Lang | Last push | Status |\n|---|---|---|---|---|"
    rows = []
    for r in repos:
        ts = parse_ts(r["pushed_at"])
        rows.append(
            f"| {repo_link(r)} | {md_escape(r.get('description') or '')} | "
            f"{r.get('language') or ''} | {ts.strftime('%Y-%m-%d')} | {status(ts)} |"
        )
    return head + "\n" + "\n".join(rows)


def build_activity(commits: list[tuple[datetime, dict, dict]]) -> str:
    cutoff = NOW - timedelta(days=ACTIVITY_DAYS)
    recent: list[tuple[datetime, dict, dict]] = []
    per_repo: dict[str, int] = {}
    for ts, repo, commit in commits:
        if ts < cutoff or len(recent) >= ACTIVITY_MAX:
            break
        if len(commit.get("parents", [])) > 1:   # skip merge commits
            continue
        if per_repo.get(repo["name"], 0) >= ACTIVITY_PER_REPO:
            continue
        per_repo[repo["name"]] = per_repo.get(repo["name"], 0) + 1
        recent.append((ts, repo, commit))
    if not recent:
        return f"_No commits in the last {ACTIVITY_DAYS} days._"
    lines = []
    for ts, repo, commit in recent:
        msg = md_escape(first_line(commit["commit"]["message"]))
        if repo["private"]:
            where = f"🔒 **{repo['name']}**"
            what = msg
        else:
            where = f"**[{repo['name']}]({repo['html_url']})**"
            what = f"[{msg}]({commit['html_url']})"
        lines.append(f"- `{ts.strftime('%Y-%m-%d')}` {where} · {what}")
    return "\n".join(lines)


def build_languages(totals: dict[str, int]) -> str:
    if not totals:
        return "_No language data available._"
    grand = sum(totals.values())
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:LANGUAGE_COUNT]
    width = max(len(name) for name, _ in top)
    lines = ["```text"]
    for name, size in top:
        pct = size / grand * 100
        filled = round(pct / 100 * BAR_WIDTH)
        bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        lines.append(f"{name.ljust(width)}  {bar}  {pct:5.1f}%")
    lines.append("```")
    return "\n".join(lines)


def build_summary(repos: list[dict], commit_count: int, totals: dict[str, int]) -> str:
    public = sum(not r["private"] for r in repos)
    private = len(repos) - public
    top_lang = max(totals, key=totals.get) if totals else "n/a"
    active = sum((NOW - parse_ts(r["pushed_at"])).days < 30 for r in repos)
    return (
        f"**{len(repos)}** repos · **{public}** public · **{private}** private · "
        f"**{active}** active this month · **{commit_count}** commits in the last "
        f"{COMMIT_WINDOW_DAYS} days · most used: **{top_lang}**"
    )


# --------------------------------------------------------------------------- #
# README rewrite
# --------------------------------------------------------------------------- #
def replace_section(text: str, name: str, content: str, inline: bool = False) -> str:
    pattern = re.compile(
        rf"(<!--START_SECTION:{name}-->)(.*?)(<!--END_SECTION:{name}-->)", re.DOTALL
    )
    if not pattern.search(text):
        raise SystemExit(f"README is missing section markers for '{name}'")
    sep = "" if inline else "\n"
    return pattern.sub(lambda m: f"{m.group(1)}{sep}{content}{sep}{m.group(3)}", text)


def main() -> None:
    repos = fetch_repos()
    repos.sort(key=lambda r: r["pushed_at"], reverse=True)

    totals: dict[str, int] = {}
    for r in repos:
        for lang, size in fetch_languages(r).items():
            totals[lang] = totals.get(lang, 0) + size

    since = NOW - timedelta(days=COMMIT_WINDOW_DAYS)
    commits: list[tuple[datetime, dict, dict]] = []
    for r in repos:
        if parse_ts(r["pushed_at"]) < since:
            continue
        for c in fetch_commits(r, since):
            commits.append((parse_ts(c["commit"]["committer"]["date"]), r, c))
    commits.sort(key=lambda c: c[0], reverse=True)

    text = README.read_text(encoding="utf-8")
    text = replace_section(text, "now", build_now(repos))
    text = replace_section(text, "summary", build_summary(repos, len(commits), totals))
    text = replace_section(text, "projects", build_projects(repos))
    text = replace_section(text, "activity", build_activity(commits))
    text = replace_section(text, "languages", build_languages(totals))
    text = replace_section(
        text, "updated", f"Last refreshed {NOW.strftime('%Y-%m-%d %H:%M UTC')}.", inline=True
    )
    README.write_text(text, encoding="utf-8")
    print(
        f"updated README: {len(repos)} repos, {len(commits)} commits in {COMMIT_WINDOW_DAYS}d, "
        f"{len(totals)} languages",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
