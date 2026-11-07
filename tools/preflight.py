#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送前体检：五项，红一项就不许推。

    python3 tools/preflight.py            # 跑全部
    python3 tools/preflight.py --json     # 给 CI 用

它只判「这一版能不能推出去」，不判「内容写得好不好」。
上次那套九门禁是给「脱敏出包」用的，这次本地只有一份、不做脱敏，所以只剩五项：

    S1 结构    frontmatter 三件套、被引用的文件都在、README 链接不 404
    S2 痕迹    绝对用户路径、凭据字样
    S3 可运行  取材脚本能跑，冒烟测试能过
    S4 一致    版本号三处一致、文档里写的路径真实存在
    S5 卫生    垃圾文件没被跟踪、没有超大文件
"""
import argparse
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_EXT = {".md", ".py", ".html", ".htm", ".svg", ".yml", ".yaml", ".json", ".txt", ".gitignore"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "docs/shots"}


# ---------- 基础工具 ----------

def run(cmd, cwd=ROOT):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def tracked_files():
    rc, out, _ = run(["git", "ls-files"])
    if rc == 0 and out.strip():
        return [line for line in out.splitlines() if line.strip()]
    files = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            files.append(os.path.relpath(os.path.join(base, name), ROOT))
    return sorted(files)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    data = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


# ---------- 五项检查 ----------

def s1_structure(files):
    problems, notes = [], []
    if "SKILL.md" not in files:
        return ["缺 SKILL.md"], notes

    fm = frontmatter(read("SKILL.md"))
    for key in ("name", "version", "description"):
        if not fm.get(key):
            problems.append("SKILL.md frontmatter 缺 `%s`" % key)

    referenced = set()
    for rel in files:
        if not rel.endswith(".md"):
            continue
        text = read(rel)
        for m in re.finditer(r"(references/[\w\-.]+\.md|assets/[\w\-.]+|scripts/[\w\-.]+\.py)", text):
            referenced.add(m.group(1))
    for ref in sorted(referenced):
        if not os.path.exists(os.path.join(ROOT, ref)):
            problems.append("被引用的文件不存在：%s" % ref)

    # README 里的相对链接与图片
    if "README.md" in files:
        text = read("README.md")
        for m in re.finditer(r"\]\(\./([^)#]+)\)|src=\"\./([^\"#]+)\"", text):
            target = (m.group(1) or m.group(2)).strip()
            if target.startswith("http"):
                continue
            if not os.path.exists(os.path.join(ROOT, target)):
                problems.append("README 链接指向不存在的文件：%s" % target)
    notes.append("被引用的文件 %d 个" % len(referenced))
    return problems, notes


def s2_traces(files):
    problems = []
    patterns = [
        (re.compile(r"/Users/[A-Za-z0-9._-]+/"), "绝对用户路径（/Users/…）"),
        (re.compile(r"/home/[A-Za-z0-9._-]+/"), "绝对用户路径（/home/…）"),
        (re.compile(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+"), "绝对用户路径（C:\\Users\\…）"),
        (re.compile(r"(?i)(api[_-]?key|secret|passwd|password)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"), "疑似凭据"),
        (re.compile(r"github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}"), "疑似 GitHub token"),
    ]
    for rel in files:
        if os.path.splitext(rel)[1].lower() not in TEXT_EXT:
            continue
        if any(rel.startswith(d) for d in ("docs/shots",)):
            continue
        try:
            text = read(rel)
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for pat, label in patterns:
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                problems.append("%s:%d · %s · %s" % (rel, line, label, m.group(0)[:40]))
    return problems, []


def s3_runnable(files):
    problems, notes = [], []
    if "scripts/extract_prd.py" in files:
        rc, _, err = run([sys.executable, "scripts/extract_prd.py", "--help"])
        if rc != 0:
            problems.append("extract_prd.py --help 非 0 退出：%s" % err.strip()[:120])
        else:
            notes.append("取材脚本可运行")
    rc, out, err = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    if rc != 0:
        tail = (err or out).strip().splitlines()[-3:]
        problems.append("冒烟测试失败：%s" % " / ".join(tail))
    else:
        notes.append("冒烟测试通过")
    return problems, notes


def s4_consistency(files):
    problems, notes = [], []
    fm = frontmatter(read("SKILL.md")) if "SKILL.md" in files else {}
    version = fm.get("version")

    for rel in ("SKILL.md", "README.md", "docs/GUIDE.md"):
        if rel not in files or not version:
            continue
        text = read(rel)
        found = set(re.findall(r"v?(\d+\.\d+)", text))
        if version not in found:
            problems.append("%s 里找不到当前版本 %s" % (rel, version))
    if version:
        notes.append("版本口径 %s" % version)

    if "CHANGELOG.md" in files:
        first = re.search(r"^##\s*v?([\d.]+)", read("CHANGELOG.md"), re.M)
        if not first:
            problems.append("CHANGELOG.md 没有形如 `## v1.9` 的首条")
        elif version and first.group(1) != version:
            problems.append("CHANGELOG 首条是 %s，frontmatter 是 %s" % (first.group(1), version))

    # 文档里点名的路径必须真实存在
    for rel in ("README.md", "docs/GUIDE.md"):
        if rel not in files:
            continue
        for m in re.finditer(r"((?:references|assets|scripts|tests|tools)/[\w\-.]+\.\w+)", read(rel)):
            path = m.group(1)
            if not os.path.exists(os.path.join(ROOT, path)):
                problems.append("%s 提到不存在的路径：%s" % (rel, path))
    return problems, notes


def s5_hygiene(files):
    problems, notes = [], []
    junk = [f for f in files if os.path.basename(f) == ".DS_Store"
            or "__pycache__" in f or f.endswith((".pyc", ".pyo", ".env"))]
    for f in junk:
        problems.append("垃圾文件被跟踪：%s" % f)
    big = []
    for rel in files:
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p) and os.path.getsize(p) > 3 * 1024 * 1024:
            big.append("%s (%.1f MB)" % (rel, os.path.getsize(p) / 1048576))
    for b in big:
        problems.append("单文件超过 3MB：%s" % b)
    notes.append("跟踪 %d 个文件" % len(files))
    return problems, notes


CHECKS = [
    ("S1 结构", s1_structure),
    ("S2 本机痕迹", s2_traces),
    ("S3 可运行", s3_runnable),
    ("S4 一致性", s4_consistency),
    ("S5 卫生", s5_hygiene),
]


def main():
    ap = argparse.ArgumentParser(description="推送前体检")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    files = tracked_files()
    results, failed = [], 0
    for name, fn in CHECKS:
        problems, notes = fn(files)
        if problems:
            failed += 1
        results.append({"check": name, "ok": not problems, "problems": problems, "notes": notes})

    if args.json:
        print(json.dumps({"root": ROOT, "failed": failed, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print("\n推送前体检 · %s\n" % ROOT)
        for r in results:
            print("%s %s" % ("✅" if r["ok"] else "❌", r["check"]))
            for n in r["notes"]:
                print("     · %s" % n)
            for p in r["problems"]:
                print("     ✗ %s" % p)
        print("")
        if failed:
            print("结论：%d 项不通过，不许推。" % failed)
        else:
            print("结论：五项全过。可以进入确认环节（不是可以推——要人点头）。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
