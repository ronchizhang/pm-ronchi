#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送闸门：没有明确确认，一行都不许推。

为什么要有这一道：2026-11-03 那次改完直接 push，用户事后才说「以后别这样」。
写在文档里的规矩靠自觉，靠自觉的规矩一定会忘，所以做成必须多一步才过得去的程序。

批准绑三样东西，任何一样变了，批准立刻作废：

    1. HEAD 的 commit SHA   —— 又多提交了一版，你确认的不是这一版
    2. 远端地址             —— 推到别的仓库去了
    3. 工作区干净           —— 还有没提交的改动，推上去的东西你没见过

用法：

    python3 tools/push_gate.py status            # 推之前先看这一屏
    python3 tools/push_gate.py approve "可以发"  # 用户点头后，把他的原话记下来
    python3 tools/push_gate.py push -n           # 只校验
    python3 tools/push_gate.py push              # 真推

强推要另外确认：`approve "可以发，强推" --force`，普通 push 永远不带 --force。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, ".git", "pm-ronchi-approval.json")

# 哪些话算「明确确认」。问状态、沉默、我自己判断，都不算。
CONFIRM_WORDS = ("可以发", "发吧", "推吧", "发布吧", "没问题", "推上去", "可以推")
# 极简回复也算，但必须整句等于它——不然「研发那边」这种句子会被误判成批准。
TERSE_OK = {"发", "推", "好", "行", "可以", "ok", "OK"}


def is_confirm(quote):
    """一句话里有确认词就算；或者整句拆开全是肯定词（「好，发」「发」「ok」）。"""
    q = (quote or "").strip()
    if not q:
        return False
    if any(w in q for w in CONFIRM_WORDS):
        return True
    tokens = [t for t in re.split(r"[^\w]+", q) if t]
    return bool(tokens) and all(t in TERSE_OK for t in tokens)


def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def head():
    return run(["git", "rev-parse", "HEAD"])[1]


def branch():
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"])[1]


def remote():
    return run(["git", "remote", "get-url", "origin"])[1]


def dirty():
    return bool(run(["git", "status", "--porcelain"])[1].strip())


def ahead():
    rc, out, _ = run(["git", "rev-list", "--count", "@{u}..HEAD"])
    return int(out) if rc == 0 and out.isdigit() else None


def load_state():
    if not os.path.exists(STATE):
        return None
    with open(STATE, encoding="utf-8") as f:
        return json.load(f)


def save_state(data):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def status():
    st = load_state()
    h, b, r = head(), branch(), remote()
    print("\n仓库：%s" % ROOT)
    print("分支：%s" % b)
    print("远端：%s" % (r or "（未配置，推不了）"))
    print("HEAD：%s  %s" % (h[:8], run(["git", "log", "-1", "--pretty=%s"])[1]))
    print("未提交改动：%s" % ("有，先提交" if dirty() else "无"))
    n = ahead()
    print("领先远端：%s" % ("未知（没有上游分支，首次推送）" if n is None else "%d 个提交" % n))

    if not st:
        print("\n批准：❌ 没有。推之前必须先拿到用户的原话。")
        return 0
    same = st.get("head") == h
    print("\n批准：%s" % ("✅ 有效" if same else "❌ 已失效"))
    print("  原话：%s" % st.get("quote", "（空）"))
    print("  时间：%s" % st.get("at", "?"))
    print("  批次：%s" % st.get("head", "")[:8])
    if not same:
        print("  原因：HEAD 变了（那次批准的是 %s）" % st.get("head", "")[:8])
    return 0


def approve(quote, allow_force=False):
    quote = (quote or "").strip()
    if not quote:
        print('用法：push_gate.py approve "可以发"', file=sys.stderr)
        return 2
    if not is_confirm(quote):
        print("这句话不算明确确认：%s" % quote, file=sys.stderr)
        print("整句只有一个「发」「推」「好」也算；「改完了吗」「现在什么状态」不算；沉默不算；我自己判断不算。", file=sys.stderr)
        return 3
    if dirty():
        print("工作区不干净，先提交再来批。推上去的东西你没见过。", file=sys.stderr)
        return 4
    if not remote():
        print("还没配远端，先把 origin 加上再来。", file=sys.stderr)
        return 5

    data = {
        "head": head(),
        "branch": branch(),
        "remote": remote(),
        "quote": quote,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "allow_force": bool(allow_force),
    }
    save_state(data)
    print("已记录批准：%s" % quote)
    print("绑定 HEAD：%s（再提交一版，这条批准就作废）" % data["head"][:8])
    if allow_force:
        print("注意：这一条包含「允许强推」。")
    return 0


def push(dry_run=False, force=False):
    st = load_state()
    if not st:
        print("没有批准记录。先跑 status 看一眼，再让用户说一句「可以发」。", file=sys.stderr)
        return 1

    problems = []
    if st.get("head") != head():
        problems.append("HEAD 变了：批准的是 %s，现在是 %s" % (st.get("head", "")[:8], head()[:8]))
    if st.get("remote") != remote():
        problems.append("远端变了：批准的是 %s，现在是 %s" % (st.get("remote"), remote()))
    if st.get("branch") != branch():
        problems.append("分支变了：批准的是 %s，现在是 %s" % (st.get("branch"), branch()))
    if dirty():
        problems.append("工作区不干净：还有没提交的改动")
    n = ahead()
    if n == 0:
        problems.append("没有要推的提交（本地没领先远端）")
    if force and not st.get("allow_force"):
        problems.append("批准里没有包含强推。强推要单独问一次，并写进原话")

    if problems:
        print("拒绝推送：", file=sys.stderr)
        for p in problems:
            print("  ✗ %s" % p, file=sys.stderr)
        return 1

    cmd = ["git", "push", "-u", "origin", branch()]
    if force:
        cmd.append("--force-with-lease")
    print("即将执行：%s" % " ".join(cmd))
    print("批准原话：%s" % st.get("quote"))
    if dry_run:
        print("（-n 只校验，没有真推）")
        return 0

    rc, out, err = run(cmd)
    print(out or err)
    if rc == 0:
        print("推完了。记得把远端地址回填进 README 的在线示例链接。")
    return rc


def tag(name, at=None, message=None, dry_run=False):
    """给某个提交打 tag 并推上去。用同一个批准，不另开一条路。"""
    st = load_state()
    if not st:
        print("没有批准记录。先跑 status，再让用户说一句「发」。", file=sys.stderr)
        return 1

    problems = []
    if st.get("head") != head():
        problems.append("HEAD 变了：批准的是 %s" % st.get("head", "")[:8])
    if st.get("remote") != remote():
        problems.append("远端变了")
    if dirty():
        problems.append("工作区不干净")
    rc, sha, _ = run(["git", "rev-parse", "--verify", "%s^{commit}" % (at or "HEAD")])
    if rc != 0:
        problems.append("找不到要打 tag 的提交：%s" % (at or "HEAD"))
    rc2, _, _ = run(["git", "rev-parse", "--verify", "refs/tags/%s" % name])
    if rc2 == 0:
        problems.append("本地已经有这个 tag：%s" % name)
    if problems:
        print("拒绝打 tag：", file=sys.stderr)
        for p in problems:
            print("  ✗ %s" % p, file=sys.stderr)
        return 1

    print("即将给 %s 打 tag %s，并推到 origin" % (sha[:8], name))
    print("批准原话：%s" % st.get("quote"))
    if dry_run:
        print("（-n 只校验，没有真打真推）")
        return 0

    rc, out, err = run(["git", "tag", "-a", name, sha, "-m", message or name])
    if rc != 0:
        print(err, file=sys.stderr)
        return rc
    rc, out, err = run(["git", "push", "origin", "refs/tags/%s" % name])
    print(out or err)
    if rc == 0:
        print("tag 推上去了。Release 还要你在网页点一下发布，我没有那个权限。")
    return rc


def main():
    ap = argparse.ArgumentParser(description="推送闸门")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="看能不能推")

    a = sub.add_parser("approve", help="记录用户的明确确认")
    a.add_argument("quote")
    a.add_argument("--force", action="store_true", help="同时允许强推")

    p = sub.add_parser("push", help="校验通过才推")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("--force-with-lease", dest="force", action="store_true")

    t = sub.add_parser("tag", help="打 tag 并推送（同一套批准）")
    t.add_argument("name")
    t.add_argument("--at", default=None, help="给哪个提交打，默认 HEAD")
    t.add_argument("--message", default=None)
    t.add_argument("-n", "--dry-run", action="store_true")

    args = ap.parse_args()
    if args.cmd == "approve":
        return approve(args.quote, args.force)
    if args.cmd == "push":
        return push(args.dry_run, args.force)
    if args.cmd == "tag":
        return tag(args.name, args.at, args.message, args.dry_run)
    return status()


if __name__ == "__main__":
    sys.exit(main())
