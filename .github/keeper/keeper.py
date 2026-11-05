#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""github-repo-keeper 主流程。

对 config.json 中登记的仓库执行一次巡检：拉取上游 -> 运行生成器 -> 校验 diff
-> 提交推送 -> 记录状态。任何一步不满足前置条件都直接中止，不做兜底提交。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
TOKEN_FILE = SKILL_DIR / ".token"
CST = timezone(timedelta(hours=8))
COMMIT_TAIL = "Automated-by: github-repo-keeper"


def log(msg):
    print("[keeper] " + str(msg), flush=True)


def die(msg, code=1):
    print("[keeper:ERROR] " + str(msg), file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------- git / api


def git(path, *args, check=True, timeout=180):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C.UTF-8"
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        die("git %s 超时" % " ".join(args))
    if check and proc.returncode != 0:
        die("git %s 失败: %s" % (" ".join(args), (proc.stderr or proc.stdout).strip()))
    return proc


def load_token():
    token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        # utf-8-sig 会吃掉记事本保存时附带的 BOM，否则 token 前面会多一个不可见字符
        raw = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        raw = raw.strip().strip('"').strip("'").strip()
        if raw:
            return raw
    return None


def gh_api(path, token=None):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-repo-keeper",
        },
    )
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log("API %s 返回 %s" % (path, exc.code))
        return None
    except Exception as exc:  # noqa: BLE001 - 网络异常不应中断整轮巡检
        log("API %s 异常: %s" % (path, exc))
        return None


# ---------------------------------------------------------------- preflight


def ensure_repo(cfg, token, force_clean=False, dry_run=False):
    path = Path(cfg["local_path"])
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        log("克隆 %s/%s 到 %s" % (cfg["owner"], cfg["repo"], path))
        url = "https://github.com/%s/%s.git" % (cfg["owner"], cfg["repo"])
        try:
            proc = subprocess.run(
                ["git", "clone", "--branch", cfg["branch"], url, str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=300,
            )
        except subprocess.TimeoutExpired:
            die("git clone 超时")
        if proc.returncode != 0:
            die("git clone 失败: %s" % (proc.stderr or proc.stdout).strip())

    if dry_run:
        git(path, "fetch", "origin", cfg["branch"], check=False)
        log("dry-run：保留当前工作副本状态，不做 reset")
        return path

    dirty = git(path, "status", "--porcelain").stdout.strip()
    if dirty:
        if force_clean:
            log("工作副本有未提交内容，按 --force-clean 丢弃后重建")
            git(path, "reset", "--hard", check=False)
            git(path, "clean", "-fd", check=False)
        else:
            die("本地仓库存在未提交改动，为避免覆盖已中止（确认可丢弃后加 --force-clean）:\n" + dirty)

    git(path, "fetch", "origin", cfg["branch"], check=False)
    git(path, "checkout", cfg["branch"], check=False)
    git(path, "reset", "--hard", "origin/" + cfg["branch"])
    log("已同步到 origin/%s" % cfg["branch"])
    return path


# ---------------------------------------------------------------- generators


def run_generators(cfg, repo_path, token, now):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generators import docs_index, heartbeat  # noqa: E402

    registry = {"heartbeat": heartbeat, "docs_index": docs_index}
    # 兼容早期配置里的旧名称
    registry["snapshot"] = heartbeat
    registry["sync_index"] = docs_index

    results = []
    for name in cfg.get("generators", ["heartbeat"]):
        mod = registry.get(name)
        if mod is None:
            log("未知生成器 %s，跳过" % name)
            continue
        try:
            results.append(mod.run(cfg, repo_path, token, now))
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            die("生成器 %s 执行失败: %s" % (name, exc))
    return results


# ---------------------------------------------------------------- guard


def diff_stats(repo_path):
    files = [ln for ln in git(repo_path, "status", "--porcelain").stdout.splitlines() if ln.strip()]
    numstat = git(repo_path, "diff", "--numstat").stdout.strip()
    added = removed = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            removed += int(parts[1])
    return len(files), added + removed, files


def guard(repo_path, cfg):
    # -N 让未跟踪文件也进入 diff，否则新增文件的行数恒为 0，行数上限形同虚设
    git(repo_path, "add", "-A", "-N", check=False)
    file_count, line_count, files = diff_stats(repo_path)
    max_files = int(cfg.get("max_files", 3))
    max_lines = int(cfg.get("max_lines", 60))
    if file_count > max_files:
        die("改动文件数 %d 超过上限 %d，已中止:\n%s" % (file_count, max_files, "\n".join(files)))
    if line_count > max_lines:
        die("改动行数 %d 超过上限 %d，已中止" % (line_count, max_lines))
    return file_count, line_count


# ---------------------------------------------------------------- publish


def publish(repo_path, cfg, token, now, results):
    file_count, line_count = guard(repo_path, cfg)

    if file_count == 0:
        log("无内容变更，静默退出（不产生空提交）")
        return {"pushed": False, "reason": "no-change"}

    if not token:
        log("缺少 token，无法推送。本轮已生成内容但未提交。")
        log("配置方式：设置 GH_TOKEN 环境变量，或把 token 写入 %s" % TOKEN_FILE)
        return {"pushed": False, "reason": "no-token",
                "hint": "设置 GH_TOKEN 或写入 " + str(TOKEN_FILE)}

    # commit 主题：前缀可在配置里改。写成带 {} 的形式时会填入变更来源
    names = [r.get("name") for r in results if r.get("changed")]
    prefix = cfg.get("commit_prefix") or "chore: update"
    if "{}" in prefix:
        scope = "docs" if names == ["docs_index"] else "status"
        prefix = prefix.format(scope)
    subject = "%s: %s" % (prefix, now.strftime("%Y-%m-%d %H:%M"))

    git(repo_path, "add", "-A")
    git(repo_path, "commit", "-m", subject, "-m", COMMIT_TAIL)

    sha = git(repo_path, "rev-parse", "HEAD").stdout.strip()
    url = "https://x-access-token:%s@github.com/%s/%s.git" % (token, cfg["owner"], cfg["repo"])
    push = git(repo_path, "push", url, "HEAD:" + cfg["branch"], check=False)
    if push.returncode != 0:
        log("首次推送失败，重试一次")
        push = git(repo_path, "push", url, "HEAD:" + cfg["branch"], check=False)
    if push.returncode != 0:
        # 回到远端状态，避免暂存区残留导致下一次 preflight 被自己卡住
        git(repo_path, "reset", "--hard", "origin/" + cfg["branch"], check=False)
        git(repo_path, "clean", "-fd", check=False)
        die("推送失败，已恢复到 origin/%s:\n%s" % (cfg["branch"], (push.stderr or push.stdout).strip()))

    log("已推送 %s (%d 文件 / %d 行)" % (sha[:8], file_count, line_count))
    return {"pushed": True, "sha": sha, "files": file_count, "lines": line_count, "subject": subject}


# ---------------------------------------------------------------- state


def write_state(name, payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = STATE_DIR / (name + ".json")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- main


def in_active_hours(cfg, now):
    window = cfg.get("active_hours")
    if not window or len(window) != 2:
        return True
    start, end = int(window[0]), int(window[1])
    return start <= now.hour < end


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    repos = data.get("repos") or []
    if not repos:
        die("配置文件中没有 repos 条目")
    return repos


def run_one(cfg, token, dry_run, now, force_clean=False):
    label = "%s/%s" % (cfg["owner"], cfg["repo"])
    log("===== %s =====" % label)
    repo_path = ensure_repo(cfg, token, force_clean, dry_run)
    results = run_generators(cfg, repo_path, token, now)

    if dry_run:
        git(repo_path, "add", "-A", "-N", check=False)
        file_count, line_count, files = diff_stats(repo_path)
        log("dry-run：%d 个文件 / %d 行变更" % (file_count, line_count))
        for item in files:
            log("  " + item)
        if file_count:
            log("----- diff -----")
            print(git(repo_path, "diff").stdout, flush=True)
        return {"repo": label, "dry_run": True, "files": file_count, "lines": line_count,
                "generators": results}

    published = publish(repo_path, cfg, token, now, results)
    payload = {
        "repo": label,
        "checked_at": now.isoformat(),
        "generators": results,
        "publish": published,
    }
    write_state(cfg["repo"], payload)
    return payload


def main():
    parser = argparse.ArgumentParser(description="GitHub 仓库每日巡检与真实更新")
    parser.add_argument("--config", default=str(SKILL_DIR / "config.json"))
    parser.add_argument("--repo", default=None, help="只处理指定仓库名")
    parser.add_argument("--dry-run", action="store_true", help="只生成并展示 diff，不提交推送")
    parser.add_argument("--force-clean", action="store_true",
                        help="工作副本有未提交内容时先丢弃再继续（会丢失手工改动）")
    parser.add_argument("--force", action="store_true", help="忽略 active_hours 时段限制")
    parser.add_argument("--local-path", default=None,
                        help="覆盖配置里的 local_path，CI 环境用（仓库已由 checkout 准备好）")
    parser.add_argument("--now", default=None,
                        help="用指定时间跑一轮（ISO 格式，调试时段判断用）")
    args = parser.parse_args()

    repos = load_config(args.config)
    if args.repo:
        repos = [r for r in repos if r.get("repo") == args.repo]
        if not repos:
            die("配置中找不到仓库 %s" % args.repo)

    if args.local_path:
        override = str(Path(args.local_path).resolve())
        for cfg in repos:
            cfg["local_path"] = override
        log("local_path 已覆盖为 %s" % override)

    token = load_token()
    if not token and not args.dry_run:
        log("警告：未检测到 token，将在推送阶段中止")
    elif not token:
        log("未检测到 token（dry-run 不需要）")

    now = datetime.now(CST)
    if args.now:
        try:
            now = datetime.fromisoformat(args.now)
        except ValueError:
            die("--now 需要 ISO 格式，例如 2026-11-05T09:00:00")
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
    summary = []
    for cfg in repos:
        label = "%s/%s" % (cfg["owner"], cfg["repo"])
        if not args.force and not in_active_hours(cfg, now):
            window = cfg.get("active_hours")
            log("%s 当前 %02d:%02d 不在活跃时段 %02d:00-%02d:00，跳过"
                % (label, now.hour, now.minute, int(window[0]), int(window[1])))
            summary.append({"repo": label, "skipped": "out-of-hours"})
            continue
        summary.append(run_one(cfg, token, args.dry_run, now, args.force_clean))

    print(json.dumps({"run_at": now.isoformat(), "results": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
