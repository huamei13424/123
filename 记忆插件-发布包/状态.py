# -*- coding: utf-8 -*-
"""
状态面板 v1
作用：一条命令看清记忆插件的全部状态（不调用模型、不花钱、不写任何东西）。
用法：python 状态.py
"""

import os
import sys
import json
import glob
import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    # 补默认值（和两个主脚本保持一致）
    if not cfg.get("memory_dir"):                      # 空字符串也算"没填"
        if cfg.get("memory_root") and cfg.get("chain_name"):
            cfg["memory_dir"] = os.path.join(cfg["memory_root"], cfg["chain_name"])
        else:
            cfg["memory_dir"] = os.path.join(cfg.get("workspace") or "", "记忆库")
    cfg.setdefault("turn_dir", "会话")
    cfg.setdefault("index_file", "回合索引.md")
    cfg.setdefault("memory_file_name", "转生提示词.md")
    cfg.setdefault("every_n_turns", 60)
    cfg.setdefault("memory_max_chars", 800)
    cfg.setdefault("model", "deepseek-chat")
    cfg.setdefault("session_dir_mark", "~65B0~5EFA~6587~4EF6~5939")
    cfg.setdefault("session_root", os.path.join(os.path.expanduser("~"), ".dsh", "sessions"))
    # 多链：memory_root 是记忆库那一层，chain_name 是当前要看的链
    cfg.setdefault("memory_root", os.path.dirname(os.path.abspath(cfg["memory_dir"])))
    cfg.setdefault("chain_name", os.path.basename(cfg["memory_dir"]))
    return cfg


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def line(title):
    print()
    print(f"── {title} " + "─" * max(0, 46 - len(title)))


def main():
    cfg = load_config()
    if cfg is not None and "--chain" in sys.argv:
        i = sys.argv.index("--chain")
        if i + 1 < len(sys.argv):
            name = sys.argv[i + 1].strip()
            if name:
                cfg["chain_name"] = name
                cfg["memory_dir"] = os.path.join(cfg["memory_root"], name)
    if cfg is None:
        print("✗ 找不到 config.json：", CONFIG_FILE)
        return

    ok, warn = [], []
    print("=" * 50)
    print("记忆插件 · 状态面板")
    print(f"时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 50)

    # ---------- 1. 基本配置 ----------
    line("基本配置")
    mem = cfg["memory_dir"]
    turn_dir = mem                      # v3：会话目录（会话1/会话2…）直接在链根下
    index_path = os.path.join(mem, cfg["index_file"])
    mem_file = os.path.join(mem, cfg["memory_file_name"])
    print(f"记忆库：{cfg['memory_root']}")
    print(f"当前链：{cfg['chain_name']} → {mem}")
    print(f"模型：  {cfg['model']}（{cfg.get('base_url', '')}）")
    if cfg["model"] in ("deepseek-chat", "deepseek-reasoner"):
        warn.append("模型名 deepseek-chat/deepseek-reasoner 已计划停用，建议改成 deepseek-flash")
    else:
        ok.append(f"模型名 {cfg['model']} 是最新的")
    if not os.path.isdir(mem):
        warn.append(f"记忆库目录不存在：{mem}")

    # ---------- 1.5 记忆链一览 ----------
    line("记忆链（多链）")
    root = cfg["memory_root"]
    chain_names = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "回合存档状态.json")):
                chain_names.append(name)
    chain_names.sort(key=lambda n: (0 if n == cfg["chain_name"] else 1, n))
    if not chain_names:
        print("（还没有任何链）")
    for name in chain_names:
        st = read_json(os.path.join(root, name, "回合存档状态.json"), {}) or {}
        sessions = st.get("sessions") or []
        live = [s for s in sessions if not s.get("inherited")]
        mains = [s for s in live if not (s.get("kind") == "subagent" or s.get("parent")) and s.get("numbered")]
        subs = [s for s in live if (s.get("kind") == "subagent" or s.get("parent")) and s.get("numbered")]
        turns = sum(s.get("numbered", 0) for s in mains)
        sub_turns = sum(s.get("numbered", 0) for s in subs)
        mark = "  ← 当前详情" if name == cfg["chain_name"] else ""
        extra = f" + 子代理 {len(subs)} 会话 / {sub_turns} 轮" if subs else ""
        persona = "（尚无转生提示词：不足 60 轮或还没生成）" if not os.path.exists(os.path.join(root, name, "转生提示词.md")) else ""
        print(f"  · {name}：{len(mains)} 会话 / {turns} 轮{extra} / 最近归档 {st.get('last_run') or '—'}{mark}{persona}")
    print(f"  其它链看详情：python 状态.py --chain {cfg['chain_name']}")

    # ---------- 2. 回合存档 ----------    line("回合存档")
    all_md = glob.glob(os.path.join(turn_dir, "**", "*.md"), recursive=True)
    main_files = [p for p in all_md
                  if not p.endswith(".think.md") and os.path.basename(p)[:-3].isdigit()
                  and os.sep + "子代理" + os.sep not in p]
    sub_files = [p for p in all_md
                 if not p.endswith(".think.md") and os.path.basename(p)[:-3].isdigit()
                 and os.sep + "子代理" + os.sep in p]
    thinks = glob.glob(os.path.join(turn_dir, "**", "*.think.md"), recursive=True)
    state = read_json(os.path.join(mem, "回合存档状态.json"), {}) or {}
    sessions = state.get("sessions") or []
    mains = [s for s in sessions if not (s.get("kind") == "subagent" or s.get("parent"))]
    subs = [s for s in sessions if (s.get("kind") == "subagent" or s.get("parent"))]
    total_sum = sum(len(s.get("summaries") or {}) for s in mains)
    print(f"对话档 N.md：      {len(main_files)} 个（主会话；子代理另有 {len(sub_files)} 个原文档）")
    print(f"思考档 N.think.md：{len(thinks)} 个（含子代理）")
    print(f"索引轮数：         {total_sum}")
    print(f"主会话数：         {len([s for s in mains if s.get('numbered')])} 个；子代理：{len([s for s in subs if s.get('numbered')])} 个")
    for s in mains:
        if s.get("numbered"):
            print(f"   · {s.get('archive_dir')}：{s.get('numbered')} 轮 → {s.get('archive_dir')}\\索引.md")
    for s in subs:
        if s.get("numbered"):
            print(f"   · 子代理/{s.get('archive_dir')}：{s.get('numbered')} 轮（只有原文）")
    print(f"最近一次归档：     {state.get('last_run') or '（从未运行）'}")
    if total_sum != len(main_files):
        warn.append(f"索引轮数（{total_sum}）与主会话对话档数量（{len(main_files)}）不一致")
    else:
        ok.append("索引与主会话对话档数量一致")

    # ---------- 3. 人格档案 ----------
    line("人格档案（第一层）")
    if os.path.exists(mem_file):
        text = open(mem_file, encoding="utf-8").read().strip()
        first = text.splitlines()[0] if text else ""
        n = len(text)
        print(f"文件：{os.path.basename(mem_file)}")
        print(f"版本：{first}")
        print(f"字数：{n} / 上限 {cfg['memory_max_chars']}" + ("  ⚠️ 超了" if n > cfg["memory_max_chars"] else "  ✓"))
        if n > cfg["memory_max_chars"]:
            warn.append(f"人格档案 {n} 字，超过上限 {cfg['memory_max_chars']}")
        else:
            ok.append("人格档案在字数上限内")
        for k in ("## 用户信息", "## AI 的性格与说话方式"):
            if k not in text:
                warn.append(f"人格档案缺少小节：{k}")
    else:
        warn.append("人格档案不存在（第一层还没生成过）")

    # ---------- 4. 下一步时机 ----------
    line("时间线")
    lvl1 = read_json(os.path.join(mem, "状态.json"), {}) or {}
    last_turn = lvl1.get("last_turn")
    turn_total = sum(int(s.get("numbered") or 0) for s in mains)
    if isinstance(last_turn, (int, float)):
        nxt = int(last_turn) + cfg["every_n_turns"]
        print(f"第一层上次生成于第 {int(last_turn)} 轮，下次在第 {nxt} 轮之后（当前累计 {turn_total} 轮）")
        if turn_total >= nxt:
            print("                    → 已到里程碑，下次运行会生新人格")
    else:
        print("第一层还没生成过（到达第一个周期时会生成）")

    # ---------- 5. 自动化（事件驱动，2026-10-20 起） ----------
    line("自动化（DSH 事件驱动）")
    event_log = os.path.join(SCRIPT_DIR, "日志-事件.txt")
    if os.path.exists(event_log):
        tail = open(event_log, encoding="utf-8", errors="replace").read().strip().splitlines()
        print(f"事件钩子最后一行：{tail[-1] if tail else '（空）'}")
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(event_log))
        age_min = (datetime.datetime.now() - mtime).total_seconds() / 60
        print(f"最后写入：{mtime:%Y-%m-%d %H:%M}"
              + ("（刚跑过）" if age_min < 30 else "（半小时没动静，正常：没聊天就不会跑）"))
        ok.append("事件钩子有运行日志")
    else:
        print("事件钩子日志不存在（Host 半要重启 DSH 后才会生成）")
        warn.append("没找到 日志-事件.txt：Host 事件钩子可能没加载")
    print("旧的计划任务/常驻监控已停用并移入 废弃杂项\\（事件钩子已接管）")

    log = os.path.join(SCRIPT_DIR, "日志.txt")
    if os.path.exists(log):
        tail = open(log, encoding="utf-8", errors="replace").read().strip().splitlines()
        print(f"旧日志最后一行：{tail[-1] if tail else '（空）'}")

    # ---------- 6. 健康检查 ----------
    line("健康检查")
    for s in ok:
        print("  ✓", s)
    for s in warn:
        print("  ⚠", s)
    if not warn:
        print("  全部正常，没有发现问题。")
    print()
    print("=" * 50)


if __name__ == "__main__":
    main()



