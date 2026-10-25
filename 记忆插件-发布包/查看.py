# -*- coding: utf-8 -*-
r"""
查看.py —— 在终端里看记忆库，不用打开文件（也就不会再有"是否保存更改"的提示）

用法：
    python 查看.py                        列出所有记忆链
    python 查看.py 主链                   看这条链的会话目录
    python 查看.py 记忆链1 会话1          看这个会话的索引
    python 查看.py 记忆链1 会话1 5        看第 5 轮全文
    python 查看.py 记忆链1 会话1 5 -t     第 5 轮连思考档一起看
    python 查看.py 记忆链1 会话1 -l 3     看最近 3 轮全文
    python 查看.py 主链 子代理            列出子代理会话（只有原文，没有索引）
    python 查看.py 主链 子代理 2          看子代理/会话2 的说明和第 2 轮的原文（子代理轮号就是内部轮号）

只读，不改任何文件。
"""

import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")


def load_config():
    with open(CONFIG_FILE, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    root = cfg.get("memory_root") or os.path.join(cfg.get("workspace", ""), "记忆库")
    return cfg, root


def chain_key(name):
    """排序：主链 → 记忆链1、记忆链2… → 其它。"""
    if name == "主链":
        return (0, 0, name)
    m = re.match(r"^记忆链(\d+)$", name)
    if m:
        return (1, int(m.group(1)), name)
    return (2, 0, name)


def list_chains(root):
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "回合存档状态.json")):
            out.append(name)
    return sorted(out, key=chain_key)


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def show_chains(root):
    chains = list_chains(root)
    print(f"记忆库：{root}")
    if not chains:
        print("（还没有任何链）")
        return
    for name in chains:
        st = json.loads(read(os.path.join(root, name, "回合存档状态.json")) or "{}")
        sessions = [s for s in (st.get("sessions") or []) if not s.get("inherited")]
        mains = [s for s in sessions if not (s.get("kind") == "subagent" or s.get("parent")) and s.get("numbered")]
        subs = [s for s in sessions if (s.get("kind") == "subagent" or s.get("parent")) and s.get("numbered")]
        turns = sum(s.get("numbered", 0) for s in mains)
        sub_turns = sum(s.get("numbered", 0) for s in subs)
        sub_note = f" + 子代理 {len(subs)} 会话 / {sub_turns} 轮" if subs else ""
        print(f"  · {name}：{len(mains)} 会话 / {turns} 轮{sub_note} / 最近归档 {st.get('last_run') or '—'}")
    print("\n下一步：python 查看.py <链名> 看会话目录")
    print("         python 查看.py <链名> 子代理   看子代理会话")


def show_catalog(root, chain):
    cdir = os.path.join(root, chain)
    if not os.path.isdir(cdir):
        print("没有这条链：", chain)
        return
    text = read(os.path.join(cdir, "会话目录.md"))
    print(text or "（还没有会话目录，说明这条链还没归档过）")


def show_session_index(root, chain, session):
    sdir = os.path.join(root, chain, session)
    path = os.path.join(sdir, "索引.md")
    text = read(path)
    if text is None:
        note = read(os.path.join(sdir, "说明.md"))
        if note is None:
            print("没有这个会话：", os.path.join(chain, session))
            return
        print(note)
        numbers = turn_numbers(sdir)
        if numbers:
            print("原文轮号：" + "、".join(str(n) for n in numbers))
            print("下一步：python 查看.py <链名> 子代理 <会话号> <轮号> 看全文（-t 加思考档）")
        return
    print(text)
    print("下一步：python 查看.py <链名> <会话名> <轮号> 看全文（-t 加思考档）")


def turn_numbers(sdir):
    try:
        names = os.listdir(sdir)
    except OSError:
        return []
    return sorted(
        int(os.path.basename(p)[:-3])
        for p in names
        if p.endswith(".md") and not p.endswith(".think.md") and os.path.basename(p)[:-3].isdigit()
    )


def show_subagents(root, chain):
    sdir = os.path.join(root, chain, "子代理")
    if not os.path.isdir(sdir):
        print(f"{chain} 还没有子代理会话。")
        return
    print(f"{chain} 的子代理（只有原文，不参与人格继承）：")
    for name in sorted(os.listdir(sdir), key=lambda x: (len(x), x)):
        p = os.path.join(sdir, name)
        if not os.path.isdir(p):
            continue
        nums = turn_numbers(p)
        print(f"  · {name}：{len(nums)} 轮 → 子代理\\{name}")
    print("\n下一步：python 查看.py <链名> 子代理 <会话号> [轮号] [-t]")


def show_turn(root, chain, session, number, with_think=False):
    sdir = os.path.join(root, chain, session)
    path = os.path.join(sdir, f"{number}.md")
    text = read(path)
    if text is None:
        print("没有这一轮：", path)
        return
    print(text)
    if with_think:
        think = read(os.path.join(sdir, f"{number}.think.md"))
        if think:
            print("\n" + "=" * 48 + "\n【思考档】\n" + think)


def show_last(root, chain, session, count):
    sdir = os.path.join(root, chain, session)
    if not os.path.isdir(sdir):
        print("没有这个会话：", sdir)
        return
    numbers = turn_numbers(sdir)
    if not numbers:
        print("这个会话还没有归档的回合")
        return
    for number in numbers[-count:]:
        print(f"\n{'=' * 48}\n【第 {number} 轮】\n")
        print(read(os.path.join(sdir, f"{number}.md")) or "")


def main():
    cfg, root = load_config()
    argv = sys.argv[1:]
    with_think = "-t" in argv
    argv = [a for a in argv if a != "-t"]
    last = None
    if "-l" in argv:
        i = argv.index("-l")
        try:
            last = int(argv[i + 1])
        except (IndexError, ValueError):
            print("-l 后面要跟数字，例如 -l 3")
            return
        argv = argv[:i] + argv[i + 2:]

    if not argv:
        show_chains(root)
        return
    chain = argv[0]
    if len(argv) == 1:
        show_catalog(root, chain)
        return
    if argv[1] == "子代理":
        rest = argv[2:]
        if not rest:
            show_subagents(root, chain)
            return
        sub = rest[0] if rest[0].startswith("会话") else f"会话{rest[0]}"
        rel = os.path.join("子代理", sub)
        if len(rest) == 1:
            show_session_index(root, chain, rel)
            return
        if last is not None:
            show_last(root, chain, rel, last)
            return
        try:
            number = int(rest[1])
        except ValueError:
            print("轮号要写数字，例如：python 查看.py 主链 子代理 2 3 -t")
            return
        show_turn(root, chain, rel, number, with_think)
        return
    session = argv[1]
    if len(argv) == 2:
        show_session_index(root, chain, session)
        return
    if last is not None:
        show_last(root, chain, session, last)
        return
    try:
        number = int(argv[2])
    except ValueError:
        print("轮号要写数字，例如：python 查看.py 记忆链1 会话1 5")
        return
    show_turn(root, chain, session, number, with_think)


if __name__ == "__main__":
    main()

