# -*- coding: utf-8 -*-
"""目录索引同步：把某个目录下的文件补进 README 里指定的列表区块。

只补缺失项，不删除、不重排、不改写已有条目 —— 下线的内容应该由人工处理，
脚本不替人做判断。

配置（config.json 里的 index 段）：

    "index": {
      "section": "目录",
      "glob": "docs/*.md",
      "exclude": ["docs/README.md"]
    }

section 是 README 里二级标题的文字，必须已存在，脚本只在这个区块内追加。
"""

import re

GENERATOR = "docs_index"
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
LIST_RE = re.compile(r"^\s*[-*]\s+\[")
LINK_RE = re.compile(r"\]\(\./([^)]+)\)")


def _read(path):
    return path.read_text(encoding="utf-8", errors="replace")


def _section_bounds(lines, section):
    start = None
    for idx, line in enumerate(lines):
        if line.startswith("## ") and section in line:
            start = idx
            break
    if start is None:
        return None, None
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break
    return start, end


def _entry_for(rel, path):
    text = _read(path)
    match = TITLE_RE.search(text)
    title = (match.group(1) if match else path.stem).replace("[", "").replace("]", "")
    return "- [%s](./%s)" % (title, rel)


def _candidates(repo_path, spec):
    pattern = spec.get("glob") or "*.md"
    exclude = {name.lower() for name in (spec.get("exclude") or [])}
    result = []
    for path in sorted(repo_path.glob(pattern)):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path).as_posix()
        if path.name.lower() in exclude or rel.lower() in exclude:
            continue
        result.append((rel, path))
    return result


def run(cfg, repo_path, token, now):
    spec = cfg.get("index") or {}
    section = (spec.get("section") or "").strip()
    if not section:
        return {"name": GENERATOR, "changed": False, "reason": "未配置 index.section"}

    readme = repo_path / "README.md"
    if not readme.exists():
        return {"name": GENERATOR, "changed": False, "reason": "README.md 不存在"}

    lines = _read(readme).split("\n")
    start, end = _section_bounds(lines, section)
    if start is None:
        return {"name": GENERATOR, "changed": False,
                "reason": "README 中没有找到「%s」区块" % section}

    block = lines[start:end]
    indexed = set()
    for line in block:
        for name in LINK_RE.findall(line):
            indexed.add(name)

    candidates = _candidates(repo_path, spec)
    missing = [(rel, path) for rel, path in candidates if rel not in indexed]
    orphaned = sorted(name for name in indexed if not (repo_path / name).exists())

    if not missing:
        return {"name": GENERATOR, "changed": False, "indexed": len(indexed), "orphaned": orphaned}

    insert_at = None
    for idx in range(len(block) - 1, -1, -1):
        if LIST_RE.match(block[idx]):
            insert_at = idx + 1
            break
    if insert_at is None:
        insert_at = min(2, len(block))

    block[insert_at:insert_at] = [_entry_for(rel, path) for rel, path in missing]
    lines[start:end] = block
    readme.write_text("\n".join(lines), encoding="utf-8")

    return {
        "name": GENERATOR,
        "changed": True,
        "added": [rel for rel, _ in missing],
        "orphaned": orphaned,
    }
