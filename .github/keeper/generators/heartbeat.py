# -*- coding: utf-8 -*-
"""心跳快照：把当次真实观测写进仓库里的一个 JSON 文件。

产出内容全部来自真实读数：本次运行时间、当天累计运行次数、仓库上游指标、
以及配置指定的目录里现有多少个文件。没有任何一项是编造的，所以每次运行
都会自然地产生一次有意义的 diff。

配置（config.json 里的 heartbeat 段）：

    "heartbeat": {
      "path": "data/heartbeat.json",
      "collections": [
        {"key": "docs_count", "glob": "docs/**/*.md"},
        {"key": "archived_count", "glob": "archive/*.md"}
      ],
      "titles": {"key": "titles", "glob": "*.md", "exclude": ["README.md"]}
    }

collections 里每一项会在输出 JSON 的顶层生成一个整数计数，key 就是字段名。
titles 可选，用于把文件名对应的 H1 标题列出来，便于看内容结构。
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

GENERATOR = "heartbeat"
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)


def _api(path, token):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "github-repo-keeper"},
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None


def _match(repo_path, spec):
    """按配置匹配文件，返回相对仓库根的 posix 路径列表。"""
    pattern = spec.get("glob") or "*.md"
    exclude = {name.lower() for name in (spec.get("exclude") or [])}
    found = []
    for path in sorted(repo_path.glob(pattern)):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path).as_posix()
        if path.name.lower() in exclude or rel.lower() in exclude:
            continue
        found.append((rel, path))
    return found


def _title_of(path):
    try:
        match = TITLE_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return path.stem
    return match.group(1).strip() if match else path.stem


def run(cfg, repo_path, token, now):
    spec = cfg.get("heartbeat") or {}
    rel_path = spec.get("path") or cfg.get("snapshot_path") or "data/heartbeat.json"
    target = repo_path / rel_path

    old = {}
    if target.exists():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = {}

    stamp = now.isoformat(timespec="seconds")
    today = now.strftime("%Y-%m-%d")
    if old.get("date") == today:
        first_check = old.get("first_check_at") or stamp
        checks = int(old.get("checks_today") or 0) + 1
    else:
        first_check = stamp
        checks = 1

    payload = {
        "date": today,
        "first_check_at": first_check,
        "last_check_at": stamp,
        "checks_today": checks,
    }

    for item in spec.get("collections") or []:
        key = item.get("key")
        if not key:
            continue
        payload[key] = len(_match(repo_path, item))

    titles_spec = spec.get("titles")
    if titles_spec:
        payload[titles_spec.get("key") or "titles"] = [
            _title_of(path) for _, path in _match(repo_path, titles_spec)
        ]

    payload["generator"] = GENERATOR

    info = _api("/repos/%s/%s" % (cfg["owner"], cfg["repo"]), token)
    if info:
        payload["upstream"] = {
            "stars": info.get("stargazers_count", 0),
            "forks": info.get("forks_count", 0),
            "open_issues": info.get("open_issues_count", 0),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    new_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    changed = (not target.exists()) or target.read_text(encoding="utf-8") != new_text
    if changed:
        target.write_text(new_text, encoding="utf-8")

    return {
        "name": GENERATOR,
        "changed": changed,
        "path": rel_path,
        "checks_today": checks,
    }
