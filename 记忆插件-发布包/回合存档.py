# -*- coding: utf-8 -*-
r"""
回合存档 v5（多记忆链 + 子代理册 + 自然数编号）
结构：
  记忆库\
      ├── 主链\                （第一条链，老数据都在这）
      │   ├── 转生提示词.md 会话目录.md 状态.json 回合存档状态.json 链信息.json
      │   ├── 会话N\（索引.md、1.md、1.think.md…）      ← 主会话（1..N 连续）
      │   └── 子代理\会话N\（1.md、1.think.md、说明.md） ← 子代理原文，无摘要/无索引
      ├── 记忆链1\             （新链，布局和主链一模一样）
      └── .归档锁              （全局锁：一次只跑一个归档进程）
规则：
  · 认链优先级：`记忆库\.链标记\<会话id>.json`（Host 写入）→ 首条消息信号 → 默认链
      「继承 主链」「继承 记忆链1」 → 接回已有链
      「新链 记忆链2」             → 新建空白链
      「新链 记忆链2 继承 主链」   → 新建链，并把来源链整份复制过去（会话文件 + 人格）
    子代理会话不认信号，跟父会话所在的链走
  · 编号：第一次真正归档（有有效回合）时才发号；每条链主会话 1..N、子代理 1..M 各自连续，
    删除/移出后由 整理（renumber）把剩下的按原顺序重排，不留洞
  · 子代理只存原文：不生成摘要、不计入人格周期、不进会话目录
  · 已登记的会话不再改链；复制来的历史会话标 inherited，不重复归档
  · 每个会话内部轮号从 1 开始（独立自然数列）
  · 没有 AI 回复的回合不记录（断网重发留下的残缺前身）
  · 思考与对话分家；已结束的回合才归档；防并发锁
"""

import os
import sys
import hashlib
import json
import glob
import re
import shutil
import datetime
import zstandard
from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

DEFAULTS = {
    "workspace": "",
    "session_root": os.path.join(os.path.expanduser("~"), ".dsh", "sessions"),
    "session_dir_mark": "",
    "source_cwd": "",
    "memory_root": "",
    "chain_name": "",
    "default_chain": "杂物箱",
    "memory_dir": "",
    "key_file": "",
    "index_file": "索引.md",
    "chain_index_file": "会话目录.md",
    "memory_file_name": "转生提示词.md",
    "summary_min_chars": 5,
    "summary_max_chars": 20,
    "archive_batch_chars": 40000,
    "store_reasoning": True,
    "store_tool_calls": False,
    "store_system_injected": False,
    "split_thinking": True,
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
}

INJECT_MARKS = (
    "Current runtime context",
    "Time sampled while preparing",
    "system-reminder",
    "A skill is a reusable set",
    "[SCHEDULE REMINDER]",
)

# 多链（v4）+ 子代理册 / 标记认链 / 自然数编号（v5）
CHAIN_META_FILE = "链信息.json"
DEFAULT_CHAIN = "主链"
CHAIN_PREFIX = "记忆链"
SUBAGENT_DIR = "子代理"
DEFAULT_JUNK = "杂物箱"               # 隐藏的兜底归档链（不是给人选的）
CHAIN_ALIASES = {"主链": "记忆链1"}    # 改名迁移后，旧信号「继承 主链」映射到 记忆链1
MARKER_DIR = ".链标记"
STATE_FORMAT = 4
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


# ---------------- 基础 ----------------

def load_config():
    with open(CONFIG_FILE, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        cfg[k] = cfg.get(k, v) if cfg.get(k) not in (None, "") else v
    if cfg.get("memory_root") and cfg.get("chain_name"):
        cfg["memory_dir"] = os.path.join(cfg["memory_root"], cfg["chain_name"])
    elif not cfg.get("memory_dir"):
        cfg["memory_dir"] = os.path.join(cfg["workspace"], "记忆库")
    # 多链：memory_root 是"记忆库"那一层；chain_name 是默认链（没有信号的老会话进这里）
    if not cfg.get("memory_root"):
        cfg["memory_root"] = os.path.dirname(os.path.abspath(cfg["memory_dir"]))
    if not cfg.get("chain_name"):
        cfg["chain_name"] = DEFAULT_CHAIN
    if not cfg.get("key_file"):
        cfg["key_file"] = os.path.join(SCRIPT_DIR, "钥匙.txt")
    return cfg


def atomic_write_text(path, text):
    """先写临时文件再替换，避免写到一半崩掉把状态文件截断。"""
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_key(cfg):
    with open(cfg["key_file"], encoding="utf-8") as f:
        key = f.read().strip()
    if not key.startswith("sk-"):
        print("钥匙文件内容不像 API key：", cfg["key_file"])
        sys.exit(1)
    return key


# ---------------- 会话发现 ----------------

def find_session_log(session_dir):
    """在会话目录里找版本最高的日志文件（session.vN.jsonl.zstd / session.vN.jsonl）。"""
    best = None
    try:
        names = os.listdir(session_dir)
    except OSError:
        return None
    for name in names:
        m = re.match(r"^session(?:\.v(\d+))?\.jsonl(?:\.zstd)?$", name)
        if not m:
            continue
        key = (int(m.group(1) or 0), 1 if name.endswith(".zstd") else 0)
        if best is None or key > best[0]:
            best = (key, os.path.join(session_dir, name))
    return best[1] if best else None


def read_text(path):
    """读会话日志文本：.zstd 解压，明文 .jsonl 直接读（DSH 的 compression:none 也认）。"""
    if path.endswith(".zstd"):
        with open(path, "rb") as f:
            return zstandard.ZstdDecompressor().stream_reader(f).read().decode("utf-8", "replace")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def load_records(path, first_only=False):
    text = read_text(path)
    lines = [l for l in text.splitlines() if l.strip()]
    if first_only:
        return [json.loads(lines[0])] if lines else []
    return [json.loads(l) for l in lines]


def load_first_record(path):
    """只取第一条记录（会话头），尽量少解压：首帧里就只有 header。"""
    try:
        with open(path, "rb") as f:
            if path.endswith(".zstd"):
                data = zstandard.ZstdDecompressor().stream_reader(f).read(256 * 1024)
                text = data.decode("utf-8", "replace")
            else:
                text = f.read(256 * 1024).decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                return json.loads(line)
    except Exception:
        return None
    return None


def session_id_of(path):
    return os.path.basename(os.path.dirname(path))


def list_session_files(cfg):
    """本工作区的全部会话日志。

    不再依赖会话目录名里的工作区编码：扫 session_root 下的目录，读会话头里的 cwd 来过滤。
    这样换机器 / DSH 改目录编码 / 会话格式升版都不会静默找不到会话。
    """
    root = cfg["session_root"]
    out = []
    if not os.path.isdir(root):
        print("没找到会话根目录，请检查 config.json 里的 session_root：", root)
        return out
    candidates = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        subdirs = [os.path.join(p, x) for x in sorted(os.listdir(p)) if os.path.isdir(os.path.join(p, x))]
        if not subdirs:
            candidates.append(p)          # 平铺布局（老版本）
        candidates.extend(subdirs)
    for sdir in candidates:
        log = find_session_log(sdir)
        if not log:
            continue
        head = load_first_record(log)
        if not head:
            continue
        head_cwd = str(head.get("cwd", "")).replace("\\", "/").rstrip("/")
        src_cwd = str(cfg.get("source_cwd") or "").replace("\\", "/").rstrip("/")
        if src_cwd == "" or head_cwd == src_cwd or head_cwd.startswith(src_cwd + "/"):
            out.append(log)
    return out



# ---------------- 链标记 / 子代理 / 自然数编号（v5） ----------------

def kind_of(session):
    """会话种类：main（主会话）或 subagent（子代理）。老数据没有 kind → 看有没有 parent。"""
    return session.get("kind") or ("subagent" if session.get("parent") else "main")


def markers_dir(cfg):
    return os.path.join(cfg["memory_root"], MARKER_DIR)


def load_markers(cfg):
    """读 Host 写的链标记：{会话id: {"chain": 链名, "parent": 来源链或None}}。

    Host（界面插件）在用户选中链时写好；归档器不依赖它，没有标记就退回首条消息信号。
    """
    out = {}
    d = markers_dir(cfg)
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if not name.endswith(".json") or name == "当前.json":   # 当前.json 是给 AI 读的最近绑定，不是会话标记
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            continue
        sid = str(data.get("session") or name[:-5])
        chain = safe_chain_name(data.get("chain"))
        kind = str(data.get("kind") or "")
        if sid and (chain or kind == "blank"):
            out[sid] = {"chain": chain, "kind": kind, "parent": safe_chain_name(data.get("parent"))}
    return out


def load_deleted_chains(cfg):
    """已删链名单（管理.py 写）：这些名字不再自动重建，路由时回落默认链。"""
    path = os.path.join(cfg["memory_root"], ".已删链.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return set()
    return {str(x.get("name")) for x in data.get("deleted") or [] if isinstance(x, dict)}


def load_chain_history(cfg):
    """每条链的历史名字（改名前的旧名列表）：自检时判断旧信号是否本来就属于这条链。"""
    path = os.path.join(cfg["memory_root"], ".链历史.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f) or {}
        return {str(k): [str(x) for x in v] for k, v in (data.get("chains") or {}).items()}
    except (OSError, ValueError):
        return {}


def load_deleted_sessions(cfg):
    """全局“已删会话”名单（管理.py 删链时写入）：这些会话不再自动重新收录。"""
    path = os.path.join(cfg["memory_root"], ".已删会话.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return set()
    return {str(x) for x in data.get("deleted") or []}


def used_numbers(state, kind):
    nums = []
    for s in state.get("sessions") or []:
        if kind_of(s) != kind:
            continue
        m = re.match(r"^会话(\d+)$", s.get("archive_dir") or "")
        if m:
            nums.append(int(m.group(1)))
    return nums


def alloc_number(state, kind):
    """下一个自然数：本链同类的现存编号最大值 + 1（编号保持连续，见 renumber_chain）。"""
    return max(used_numbers(state, kind), default=0) + 1


def chain_name_from_session(cfg, recs, states, deleted):
    """新链的名字：优先用 DSH 给这个会话生成的标题，没有就用第一句用户话，重名加（2）。"""
    title = session_title(recs).strip()
    if not title:
        text = first_user_text(recs, cfg).strip()
        title = re.sub(r"\s+", " ", text.splitlines()[0] if text else "")[:24]
    title = re.sub(r'[\\/:*?"<>|\r\n\t]', "", title).strip(" .")[:24]
    if not title:
        title = "未命名"
    reserved = {DEFAULT_JUNK, "主链", cfg.get("chain_name") or "", cfg.get("default_chain") or ""}
    name = title
    n = 2
    while name in states or name in deleted or name in reserved:
        name = f"{title}（{n}）"
        n += 1
    return name


def session_base_dir(chain_dir, session):
    """会话目录的父目录：主会话在链根，子代理在 <链>\子代理。"""
    if kind_of(session) == "subagent":
        return os.path.join(chain_dir, SUBAGENT_DIR)
    return chain_dir


def session_dir_of(chain_dir, session):
    return os.path.join(session_base_dir(chain_dir, session), session.get("archive_dir") or "")


def parent_lookup_of(cfg, states):
    """全库会话 id → (链名, 会话N, 标题)，给子代理的 说明.md 用。"""
    out = {}
    for chain, st in states.items():
        for s in st.get("sessions") or []:
            if s.get("parent"):
                continue
            out[s["id"]] = (chain, s.get("archive_dir") or "", s.get("title") or "")
    return out


def write_subagent_note(cfg, chain_dir, session, parent_lookup=None):
    """子代理目录里的 说明.md：交代它从哪来、父会话是谁。"""
    sdir = session_dir_of(chain_dir, session)
    if not os.path.isdir(sdir):
        return
    parent_id = session.get("parent") or ""
    pchain, pnum, ptitle = (parent_lookup or {}).get(parent_id, ("", "", ""))
    parent_label = parent_id
    if pnum:
        parent_label = f"{pchain}/{pnum}" + (f"（{ptitle}）" if ptitle else "")
    lines = [
        f"# 子代理会话 · {session.get('archive_dir')}",
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "这是子代理（subagent）会话的原文，不参与人格继承；只用于需要时回看推理过程。",
        "",
        f"- 父会话：{parent_label}",
        f"- 父会话id：{parent_id or '（未知）'}",
        f"- 来源：{session.get('origin') or 'subagent'}"
        + (f"（delegationDepth={session['delegation_depth']}）" if session.get("delegation_depth") is not None else ""),
        f"- 轮数：{session.get('numbered', 0)}",
    ]
    text = "\n".join(lines) + "\n"
    path = os.path.join(sdir, "说明.md")
    old = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
    if strip_stamp(old) == strip_stamp(text):
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def renumber_chain(cfg, chain_name, state, parent_lookup=None):
    """把一条链的主会话 / 子代理分别重排成 会话1..N（按现有编号相对顺序），不留洞。

    两阶段改名（旧名 → .重排临时N → 新名），失败时旧目录还在，能手动救。
    返回 {"main": [(旧,新)…], "subagent": [...]}，没变化就是空列表。
    """
    chain_dir = chain_dir_of(cfg, chain_name)
    changed = {"main": [], "subagent": []}
    if not os.path.isdir(chain_dir):
        return changed
    for kind in ("main", "subagent"):
        base = chain_dir if kind == "main" else os.path.join(chain_dir, SUBAGENT_DIR)
        items = [s for s in state.get("sessions") or []
                 if kind_of(s) == kind and s.get("archive_dir")
                 and os.path.isdir(os.path.join(base, s["archive_dir"]))]
        if not items:
            continue
        def order_key(s):
            m = re.match(r"^会话(\d+)$", s.get("archive_dir") or "")
            return (int(m.group(1)) if m else 10 ** 9, s["id"])
        items.sort(key=order_key)
        for i, s in enumerate(items, 1):
            old = s["archive_dir"]
            new = f"会话{i}"
            if old == new:
                continue
            tmp = f".重排临时{i}"
            tmp_path = os.path.join(base, tmp)
            if os.path.exists(tmp_path):
                raise RuntimeError(f"重排中断残留：{tmp_path}（先手动处理）")
            os.rename(os.path.join(base, old), tmp_path)
            s["_renumber_tmp"] = tmp
            changed[kind].append((old, new))
        for i, s in enumerate(items, 1):
            if "_renumber_tmp" not in s:
                continue
            tmp = s.pop("_renumber_tmp")
            new = f"会话{i}"
            os.rename(os.path.join(base, tmp), os.path.join(base, new))
            s["archive_dir"] = new
    # 改完名，重写索引标题 / 子代理说明
    for s in state.get("sessions") or []:
        if kind_of(s) == "subagent":
            write_subagent_note(cfg, chain_dir, s, parent_lookup)
        elif s.get("archive_dir"):
            retitle_session_index(chain_dir, s)
    return changed


def retitle_session_index(chain_dir, session):
    """重编号后把 会话N/索引.md 的标题行改成新编号（纯本地重写，不调模型）。"""
    sdir = session_dir_of(chain_dir, session)
    path = os.path.join(sdir, "索引.md")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not lines:
        return
    m = re.match(r"^(# 回合索引 · )会话\d+(.*)$", lines[0])
    if not m:
        return
    lines[0] = f"{m.group(1)}{session['archive_dir']}{m.group(2)}"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def register_sessions(cfg, state, chain_name, routing, metas):
    """登记会话：只登记，不急着发编号。

    发号时机后移到"第一次真正归档"（有有效回合、要建目录时）——
    否则"新建了会话还没说话"的空白会话会白占编号，留下一堆洞（2026-10-21 体检发现 17 个）。
    """
    sessions = state.get("sessions") or []
    known = {s["id"] for s in sessions}
    excluded = set(state.get("excluded") or [])   # 用户用 会话管理.py 明确移除的会话，不再自动收录
    by_id = {session_id_of(p): p for p in list_session_files(cfg)}
    for sid, path in sorted(by_id.items()):
        if sid in known or sid in excluded:
            continue
        if routing.get(sid) != chain_name:
            continue
        meta = metas.get(sid) or {}
        # 关键：只有"已经有有效回合"的会话才登记。
        # 会话一被创建（哪怕还没说话）就会在磁盘上留下 header 文件，这时候认链文本是空的，
        # 一登记就会落到默认链并且以后不再改——2026-10-21 桌游链测试会话就是这么错进主链的。
        if not meta.get("turns"):
            continue
        entry = {"id": sid, "path": path, "numbered": 0, "summaries": {}}
        if meta.get("kind") == "subagent":
            entry["kind"] = "subagent"
            entry["parent"] = meta.get("parent") or ""
            if meta.get("origin"):
                entry["origin"] = meta["origin"]
            if meta.get("delegation_depth") is not None:
                entry["delegation_depth"] = meta["delegation_depth"]
        sessions.append(entry)
        known.add(sid)
        tag = "子代理" if entry.get("kind") == "subagent" else "待编号"
        print(f"发现新会话 → {chain_name} / {tag}（{sid[:12]}）")
    # 路径可能变（一般不会），同步一下
    for s in sessions:
        if s["id"] in by_id:
            s["path"] = by_id[s["id"]]
    return sessions


# ---------------- 多记忆链：认链 ----------------

def safe_chain_name(name):
    """把信号里的链名清洗成安全的 Windows 文件夹名；不合格返回 None。"""
    if not name:
        return None
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(name)).strip(" .")
    if not name or name in (".", ".."):
        return None
    if name.upper().split(".")[0] in RESERVED_NAMES:
        return None
    name = name[:40]
    return CHAIN_ALIASES.get(name, name)


def chain_dir_of(cfg, chain_name):
    return os.path.join(cfg["memory_root"], chain_name)


def chain_state_path(cfg, chain_name):
    return os.path.join(chain_dir_of(cfg, chain_name), "回合存档状态.json")


def chain_sort_key(name):
    """排序：主链 → 记忆链1、记忆链2… → 其它链名字母序。"""
    if name == DEFAULT_CHAIN:
        return (0, 0, "")
    m = re.match(r"^记忆链(\d+)$", name)
    if m:
        return (1, int(m.group(1)), name)
    return (2, 0, name)


def load_chain_states(cfg):
    """扫记忆库下所有链，返回 {链名: 状态字典}；没有状态文件的目录不算链。"""
    states = {}
    root = cfg["memory_root"]
    if not os.path.isdir(root):
        return states
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not os.path.isdir(p) or name.startswith("."):
            continue
        sp = os.path.join(p, "回合存档状态.json")
        if not os.path.exists(sp):
            continue
        try:
            with open(sp, encoding="utf-8") as f:
                states[name] = json.load(f) or {"format": 3, "sessions": []}
        except (OSError, ValueError):
            continue
    return dict(sorted(states.items(), key=lambda kv: chain_sort_key(kv[0])))


def new_chain_name(states):
    n = 1
    while f"{CHAIN_PREFIX}{n}" in states:
        n += 1
    return f"{CHAIN_PREFIX}{n}"


def first_user_text(records, cfg):
    """会话里第一条"真正的"用户消息（跳过运行时注入），作为认链信号。"""
    for r in records:
        if r.get("type") != "user/message":
            continue
        for p in (r.get("data", {}).get("content") or []):
            if isinstance(p, dict) and p.get("type") == "text":
                txt = (p.get("text") or "").strip()
                if txt and not is_injected(txt):
                    return txt
    return ""


INHERIT_RE = re.compile(r"继\s*承\s*(主链|记忆链\d+)")
NEW_CHAIN_RE = re.compile(r"新\s*链\s*[：:]?\s*(记忆链\d+)?\s*(?:继\s*承\s*(主链|记忆链\d+))?")


def parse_signal(text):
    """解析认链信号，返回:
         ("inherit", 链名, None)           接回已有链
         ("new", 链名或None, 来源链或None) 新建链（有来源=继承复制）
         None                              没信号 → 默认链
    """
    if not text:
        return None
    m = NEW_CHAIN_RE.search(text)
    if m:
        return ("new", m.group(1), m.group(2))
    m = INHERIT_RE.search(text)
    if m:
        return ("inherit", m.group(1), None)
    return None


def decide_chain(cfg, text, states):
    """把一条会话分到哪条链。返回 (链名, 来源链或None)。"""
    signal = parse_signal(text)
    if signal is None:
        return cfg.get("default_chain") or cfg["chain_name"], None
    kind, name, parent = signal
    if kind == "inherit":
        return safe_chain_name(name) or cfg["chain_name"], None
    target = safe_chain_name(name) or new_chain_name(states)
    return target, (safe_chain_name(parent) if parent else None)


def write_chain_info(chain_dir, name, kind, parent=None):
    info = {
        "name": name,
        "kind": kind,
        "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if parent:
        info["parent"] = parent
    atomic_write_json(os.path.join(chain_dir, CHAIN_META_FILE), info)


def copy_chain(cfg, src_name, dst_name):
    """新链·继承会话文件：把来源链整份复制成目标链（会话文件 + 人格 + 索引）。"""
    src = chain_dir_of(cfg, src_name)
    dst = chain_dir_of(cfg, dst_name)
    ignore = shutil.ignore_patterns(".归档锁", ".生成锁", ".生成锁*", "*.lock")
    shutil.copytree(src, dst, ignore=ignore)
    sp = os.path.join(dst, "回合存档状态.json")
    if os.path.exists(sp):
        st = json.load(open(sp, encoding="utf-8"))
    else:
        st = {"format": 3, "sessions": []}
    for s in st.get("sessions", []):
        s["inherited"] = True      # 历史副本：不参与以后的路由，也不重复归档
    atomic_write_json(sp, st)
    return st


def ensure_chain(cfg, states, name, parent=None):
    """链不存在就建出来（有 parent 且存在 = 复制继承）。返回状态字典。"""
    dst = chain_dir_of(cfg, name)
    if name in states and os.path.isdir(dst):
        return states[name]
    st = None
    copied = False
    if parent and parent in states:
        try:
            st = copy_chain(cfg, parent, name)
            copied = True
            print(f"复制记忆链：{parent} → {name}（会话文件 + 人格整份继承）")
        except Exception as e:  # noqa: BLE001  复制失败不该让整次归档崩掉
            print(f"⚠️ 复制链失败（{e}），改为建空白链：{name}")
            st = None
    os.makedirs(dst, exist_ok=True)
    if st is None:
        sp = chain_state_path(cfg, name)
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                st = json.load(f)
        else:
            st = {"format": 3, "sessions": []}
            atomic_write_json(sp, st)
            print(f"新建记忆链：{name}（空白）")
    if copied:
        write_chain_info(dst, name, "copy", parent)
    elif not os.path.exists(os.path.join(dst, CHAIN_META_FILE)):
        write_chain_info(dst, name, "blank", None)
    states[name] = st
    return st


# ---------------- 切回合 ----------------

def is_injected(text):
    return any(m in text for m in INJECT_MARKS)


def split_turns(records, cfg):
    """只保留已结束（turn/end）的回合；未结束的丢弃（下次再收）。

    v6：用户消息按来源标签 `data.source.kind` 过滤（只收 kind == "user"；没有 source 的老日志
    退回文字特征判断），不再靠文字去猜。DSH 压缩检查点（compact-checkpoint）、AGENTS 提醒、
    时间上下文、子代理消息等都不会再被当成用户发言写进档案。
    `turn/end` 之后再来的消息不再并入已闭合的轮（旧版会把压缩检查点塞进上一轮）。
    """
    turns = []
    current = None
    turn_no = 0

    def source_kind(record):
        src = (record.get("data") or {}).get("source")
        return src.get("kind") if isinstance(src, dict) else None

    def is_real_user(record):
        kind = source_kind(record)
        if kind is not None:
            return kind == "user"
        # 很老的数据没有 source 字段：退回文字特征过滤
        for p in ((record.get("data") or {}).get("content") or []):
            if isinstance(p, dict) and p.get("type") == "text":
                if not is_injected((p.get("text") or "").strip()):
                    return True
        return False

    for r in records:
        t = r.get("type")
        if t == "turn/start":
            # 上一轮没有 turn/end 就丢弃（不完整回合）
            current = {"no": turn_no + 1, "time": r.get("time", 0), "items": [], "closed": False}
            turn_no += 1
            continue
        if t == "turn/end":
            if current is not None:
                current["closed"] = True
                if current["items"]:
                    turns.append(current)
                current = None      # 之后的记录（压缩检查点等）不再并入这一轮
            continue
        if current is None:
            continue

        if t == "user/message":
            if not is_real_user(r):
                if cfg.get("store_system_injected"):
                    for p in ((r.get("data") or {}).get("content") or []):
                        if isinstance(p, dict) and p.get("type") == "text":
                            txt = (p.get("text") or "").strip()
                            if txt:
                                current["items"].append(("系统注入", txt))
                continue
            for p in ((r.get("data") or {}).get("content") or []):
                if isinstance(p, dict) and p.get("type") == "text":
                    txt = (p.get("text") or "").strip()
                    if txt:
                        current["items"].append(("用户", txt))

        elif t == "assistant/message":
            msg = (r.get("data") or {}).get("message", {}) or {}
            parts = msg.get("content") or []
            # 工具调用旁白：这一条 assistant 消息里带工具调用时，text 归"AI过程"
            has_tool = any(isinstance(p, dict) and p.get("type") == "tool-call" for p in parts)
            for p in parts:
                if not isinstance(p, dict):
                    continue
                pt = p.get("type")
                if pt == "reasoning":
                    if not cfg.get("store_reasoning"):
                        continue
                    txt = (p.get("text") or "").strip()
                    if txt:
                        current["items"].append(("AI思考", txt))
                elif pt == "text":
                    txt = (p.get("text") or "").strip()
                    if txt:
                        current["items"].append(("AI过程" if has_tool else "AI回复", txt))
                elif pt == "tool-call":
                    if not cfg.get("store_tool_calls"):
                        continue
                    name = p.get("name", "?")
                    args = p.get("arguments") or ""
                    args = re.sub(r'[{}\[\]"]', " ", str(args))
                    args = re.sub(r"\s+", " ", args).strip()
                    if len(args) > 60:
                        args = args[:60] + "…"
                    current["items"].append(("AI工具", f"{name} {args}".strip()))

    return [t for t in turns if t["items"]]


def session_turns(cfg, session, records=None, merge_resends=False):
    """某会话的有效回合：已结束 + 有 AI 回复（残缺前身直接丢）。

    默认**不合并**"相邻且用户文本相同"的回合。老版本合并是为了对付断网重发，
    但它会把真实的重复提问（"继续、继续、继续"）误伤成一回合——
    2026-10-21 跑团测试踩到：8 轮的会话只记了 3 轮。
    只有在迁移旧数据、需要还原"旧算法当年算出几轮"时才传 merge_resends=True。
    """
    def norm(s):
        return re.sub(r"\s+", " ", s).strip()

    def reply_len(t):
        return sum(len(x) for k, x in t["items"] if k == "AI回复")

    raw = []
    for t in split_turns(records if records is not None else load_records(session["path"]), cfg):
        if not any(k == "AI回复" for k, _ in t["items"]):
            continue
        t["user_key"] = "\n".join(norm(x) for k, x in t["items"] if k == "用户")
        t["summary_chars"] = sum(len(x) for k, x in t["items"] if k in ("用户", "AI回复"))
        t["total_chars"] = sum(len(x) for k, x in t["items"])
        raw.append(t)

    if not merge_resends:
        return raw

    merged = []
    for t in raw:
        if merged and merged[-1]["user_key"] and merged[-1]["user_key"] == t["user_key"]:
            if reply_len(t) > reply_len(merged[-1]):
                merged[-1] = t          # 替换成回复更完整的那一份
            continue
        merged.append(t)
    return merged


def turn_fingerprint(turns, limit=None):
    """回合结构的指纹（按顺序把每轮的用户文本拼起来取 sha1）。

    只比**前 limit 轮**：会话正常变长（末尾追加新回合）不算结构变化，
    否则每跑一次都会把旧概要清掉重生成（2026-10-21 踩到，白白烧 API）。
    """
    picked = turns if limit is None else turns[:limit]
    keys = [t.get("user_key", "") for t in picked]
    return hashlib.sha1("\x1f".join(keys).encode("utf-8")).hexdigest()


def strip_stamp(text):
    """比较文件内容时去掉"生成时间"那一行。"""
    return "\n".join(line for line in text.splitlines() if not line.startswith("生成时间："))


# ---------------- 概要 ----------------

def fmt_time(ms):
    if not ms:
        return "未知"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def make_summaries(cfg, client, batch):
    """给一批回合生成概要，返回 {本地轮号: 概要}。"""
    lo, hi = cfg["summary_min_chars"], cfg["summary_max_chars"]
    blocks = []
    for t in batch:
        parts = [x for k, x in t["items"] if k in ("用户", "AI回复")]
        body = "\n".join(parts)
        if len(body) > 4000:
            body = body[:4000] + "…"
        blocks.append(f"【第{t['index']}轮】\n{body}")
    prompt = f"""下面是一场对话里若干回合的内容。请为每一轮写一行概要。

要求：
1. 每行格式：`轮号: 概要`（轮号用方括号里的数字）；
2. 概要 {lo}～{hi} 个字，写"用户做了什么/发生了什么"，用陈述句，不要写"用户问"这类套话；
3. 一轮一行，不要编号、不要解释、不要空行。

{chr(10).join(blocks)}
"""
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": "你是对话摘要器，只输出规定格式的概要行。"},
            {"role": "user", "content": prompt},
        ],
    )
    text = resp.choices[0].message.content or ""
    got = {}
    for line in text.splitlines():
        # 容忍各种写法：`95: 概要`、`[95] 概要`、`第95轮：概要`、`95. 概要`…
        m = re.match(r"^\s*[\[【(（]?\s*(?:第)?\s*(\d+)\s*(?:轮)?\s*[\]】)）]?\s*[:：.、)]?\s*(.+?)\s*$", line)
        if m:
            got[int(m.group(1))] = m.group(2)[:40]
    return got


# ---------------- 写文件 ----------------

def backup_summaries(cfg, chain_name, session):
    r"""清空概要前先留一份备份（放在 记忆库\.概要备份\），出问题能翻旧账、也方便对比。"""
    try:
        d = os.path.join(cfg["memory_root"], ".概要备份")
        os.makedirs(d, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = re.sub(r"[^\w\u4e00-\u9fa5.-]", "_", str(session.get("archive_dir") or session.get("id", "")))[:40]
        path = os.path.join(d, f"{chain_name}-{name}-{stamp}.json")
        payload = {
            "chain": chain_name,
            "session": session.get("id"),
            "archive_dir": session.get("archive_dir"),
            "numbered": session.get("numbered"),
            "fingerprint": session.get("fingerprint"),
            "fingerprint_len": session.get("fingerprint_len"),
            "summaries": session.get("summaries") or {},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"旧概要已备份：{path}（{len(payload['summaries'])} 条）")
    except Exception as e:  # noqa: BLE001  备份失败不该挡住归档
        print(f"⚠️ 概要备份失败：{e}")


def write_turn_files(cfg, sdir, turns):
    r"""写 会话N\N.md 与 N.think.md（本地轮号从 1 开始）；内容没变就跳过。"""
    os.makedirs(sdir, exist_ok=True)
    split = cfg.get("split_thinking", True)
    dialogue_kinds = ("用户", "AI回复", "系统注入")
    written, skipped = 0, 0

    def put(path, text):
        nonlocal written, skipped
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = f.read().replace("\r\n", "\n").replace("\r", "\n")
                if old == text.replace("\r\n", "\n").replace("\r", "\n"):
                    skipped += 1
                    return
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        written += 1

    def grouped(items):
        """#5 连续同类型合并成一个块：一个标记 + 多段内容，不再每句重复标记。"""
        blocks = []
        for kind, txt in items:
            if blocks and blocks[-1][0] == kind:
                blocks[-1][1].append(txt)
            else:
                blocks.append([kind, [txt]])
        return [(k, "\n\n".join(v)) for k, v in blocks]

    for t in turns:
        n = t["index"]
        dialogue = [(k, x) for k, x in t["items"] if k in dialogue_kinds]
        process = [(k, x) for k, x in t["items"] if k not in dialogue_kinds]
        if not split:
            dialogue, process = t["items"], []
        lines = [f"# 第{n}轮", f"时间：{fmt_time(t['time'])}", ""]
        for kind, txt in grouped(dialogue):
            lines.append(f"【{kind}】{txt}")
            lines.append("")
        put(os.path.join(sdir, f"{n}.md"), "\n".join(lines).rstrip() + "\n")
        if split and process:
            tlines = [f"# 第{n}轮 · 思考过程", f"时间：{fmt_time(t['time'])}", ""]
            for kind, txt in grouped(process):
                tlines.append(f"【{kind}】{txt}")
                tlines.append("")
            put(os.path.join(sdir, f"{n}.think.md"), "\n".join(tlines).rstrip() + "\n")
    return written, skipped


def session_title(records):
    """从会话日志里取最后一次 session/title（= DSH 界面上显示的那个会话名）。"""
    title = ""
    for r in records:
        if r.get("type") == "session/title":
            t = (r.get("data") or {}).get("title") or ""
            if t:
                title = str(t).strip()
    return title


def write_session_index(cfg, sdir, session, turns):
    r"""写 会话N\索引.md（只含本会话 1..n 轮）。

    内容没变就不重写——省得外部编辑器一直提示"文件已被改动/是否保存"。
    """
    os.makedirs(sdir, exist_ok=True)
    sums = session["summaries"]
    first = turns[0]["time"] if turns else 0
    last = turns[-1]["time"] if turns else 0
    title = session.get("title") or ""
    lines = [
        f"# 回合索引 · {session['archive_dir']}" + (f"（{title}）" if title else ""),
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"轮数：{len(turns)}（{fmt_time(first)} ~ {fmt_time(last)}）",
        "",
        "全文：同目录的 `<轮号>.md`；思考过程：`<轮号>.think.md`（按需再读）。",
        "",
    ]
    for t in turns:
        lines.append(f"{t['index']} {sums.get(str(t['index']), '（概要缺失）')}")
    text = "\n".join(lines) + "\n"
    path = os.path.join(sdir, "索引.md")
    old = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
    if strip_stamp(old) == strip_stamp(text):
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def write_chain_index(cfg, chain_name, sessions, chain_dir):
    """写链级导航 会话目录.md。"""
    lines = [
        f"# 会话目录（记忆链：{chain_name}）",
        f"生成时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"本文件只做导航。人格档案：`{cfg.get('memory_file_name', '转生提示词.md')}`；每个会话有自己的索引，轮号从 1 开始。",
        "",
    ]
    for s in sessions:
        if kind_of(s) != "main":
            continue
        n = s.get("numbered", 0)
        if not n:
            continue
        title = s.get("title") or ""
        label = f"{s['archive_dir']}（{title}）" if title else s["archive_dir"]
        lines.append(f"- {label}：{n} 轮 → 索引 `{s['archive_dir']}\\索引.md`")
    subs = [s for s in sessions if kind_of(s) == "subagent" and s.get("numbered")]
    if subs:
        lines.append("")
        lines.append(f"- 子代理：{len(subs)} 个 → `{SUBAGENT_DIR}\\`（只有原文，不参与人格继承）")
    text = "\n".join(lines) + "\n"
    path = os.path.join(chain_dir, cfg.get("chain_index_file", "会话目录.md"))
    old = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            old = ""
    if strip_stamp(old) == strip_stamp(text):
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


# ---------------- 主流程 ----------------

def main():
    cfg = load_config()

    # 可选：只处理某一条链（python 回合存档.py --chain 记忆链1）
    only_chain = None
    if "--chain" in sys.argv:
        i = sys.argv.index("--chain")
        if i + 1 < len(sys.argv):
            only_chain = safe_chain_name(sys.argv[i + 1])

    root = cfg["memory_root"]
    os.makedirs(root, exist_ok=True)

    # 全局防并发锁（10 分钟自动失效）
    lock_path = os.path.join(root, ".归档锁")
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = datetime.datetime.now().timestamp() - os.path.getmtime(lock_path)
            except OSError:
                continue
            if age < 900:
                print(f"另一个归档进程正在运行（锁建立于 {int(age)} 秒前），本次跳过。")
                return
            try:
                os.remove(lock_path)       # 过期锁（默认 15 分钟）
            except OSError:
                pass

    try:
        states = load_chain_states(cfg)
        if not states:
            ensure_chain(cfg, states, cfg.get("default_chain") or cfg["chain_name"] or DEFAULT_CHAIN)
        print("记忆库：", root)
        print("已有记忆链：", "、".join(states))

        # ---------- 1. 认链：Host 标记 > 首条消息信号 > 默认链；子代理跟父会话 ----------
        markers = load_markers(cfg)
        deleted_chains = load_deleted_chains(cfg)
        chain_history = load_chain_history(cfg)
        deleted_hits = []
        routing = {}          # 会话id → 链名
        metas = {}            # 会话id → {kind: subagent, parent: …}
        pending = {}          # 还没建出来的链 → 来源链（None=空白新链）
        excluded_all = set(load_deleted_sessions(cfg))  # 任何一条链里被移出的会话 / 已删链里的会话，都不再自动收录
        for name, st in states.items():
            for s in st.get("sessions") or []:
                if not s.get("inherited"):
                    routing[s["id"]] = name
                    if s.get("parent"):
                        metas[s["id"]] = {"kind": "subagent", "parent": s["parent"]}
            excluded_all.update(st.get("excluded") or [])

        for path in list_session_files(cfg):
            sid = session_id_of(path)
            if sid in routing or sid in excluded_all:
                continue
            try:
                recs = load_records(path)
                head = recs[0] if recs else {}
                text = first_user_text(recs, cfg)
                turn_count = len(session_turns(cfg, {"id": sid, "path": path}, recs))
            except Exception:
                continue
            meta = {}
            if head.get("origin") == "subagent" or head.get("parentSession"):
                meta = {
                    "kind": "subagent",
                    "parent": head.get("parentSession") or "",
                    "origin": head.get("origin") or "subagent",
                    "delegation_depth": head.get("delegationDepth"),
                }
            meta["turns"] = turn_count
            mk = markers.get(sid)
            if mk is not None and mk.get("chain"):
                if mk["chain"] in deleted_chains:
                    deleted_hits.append(f"{sid[:12]} 的标记指向已删链 {mk['chain']}，改走默认链")
                    mk = None
                elif mk["chain"] not in states:
                    deleted_hits.append(f"{sid[:12]} 的标记指向不存在的链 {mk['chain']}，改走默认链")
                    mk = None
            auto_blank = mk is not None and mk.get("kind") == "blank" and not mk.get("chain")
            if auto_blank:
                auto_name = chain_name_from_session(cfg, recs, states, deleted_chains)
                target = auto_name
                pending.setdefault(target, None)
                routing[sid] = target
                metas[sid] = meta
                continue
            if meta.get("kind") == "subagent" and meta.get("parent"):
                target = routing.get(meta["parent"]) or (markers.get(meta["parent"]) or {}).get("chain")
                if target is None:
                    target = cfg["chain_name"]
                    print(f"⚠️ 子代理 {sid[:12]} 找不到父会话所在链，先放 {target}，归档后自检会再报")
            elif mk:
                target = mk["chain"]
                if mk.get("parent"):
                    pending.setdefault(target, mk["parent"])
            else:
                target, parent = decide_chain(cfg, text, states)
                if target not in states:
                    pending.setdefault(target, parent)
            if target in deleted_chains and target not in states:
                deleted_hits.append(f"{sid[:12]} 的信号指向已删链 {target}，改走默认链")
                target = cfg.get("default_chain") or cfg["chain_name"]
            if target not in states:
                pending.setdefault(target, None)
            routing[sid] = target
            metas[sid] = meta

        parent_lookup = parent_lookup_of(cfg, states)

        client = None
        total_new = 0
        report = []
        warnings = list(dict.fromkeys(deleted_hits))

        # ---------- 2. 归档一条链 ----------
        def archive_chain(name):
            nonlocal client, total_new
            if only_chain and name != only_chain:
                return
            chain_dir = chain_dir_of(cfg, name)
            os.makedirs(chain_dir, exist_ok=True)
            state = states.get(name) or {"format": STATE_FORMAT, "sessions": []}
            state.setdefault("format", STATE_FORMAT)
            state.setdefault("sessions", [])
            sessions = register_sessions(cfg, state, name, routing, metas)
            for s in sessions:
                if s.get("inherited"):      # 继承来的历史副本：已冻结，不再跟踪
                    continue
                try:
                    recs = load_records(s["path"])
                except Exception as e:      # noqa: BLE001
                    print(f"⚠️ 读不了会话（{s['id']}）：{e}")
                    continue
                s["title"] = session_title(recs)
                kind = kind_of(s)
                short = s.get("archive_dir") or s["id"][:12]

                # 归属自检：标记/信号指向别的链 → 只报警，不自动搬（记忆库只读，迁移由脚本和用户点头）
                if kind == "main":
                    marker_chain = (markers.get(s["id"]) or {}).get("chain")
                    if marker_chain:
                        expected = safe_chain_name(marker_chain)
                    else:
                        expected = None
                        sig = parse_signal(first_user_text(recs, cfg))
                        if sig and sig[0] == "inherit":
                            expected = safe_chain_name(sig[1])
                    if expected and expected != name and expected not in (chain_history.get(name) or []):
                        # 旧名属于这条链（改名之前的名字）就不算错
                        warnings.append(f"{short} 按标记/信号应属于 {expected}，现在登记在 {name}")

                turns = session_turns(cfg, s, recs)
                for i, t in enumerate(turns, 1):
                    t["index"] = i
                if not turns:               # 空会话：不建目录、不发编号
                    s["numbered"] = 0
                    continue

                if kind == "subagent":
                    if not s.get("archive_dir"):
                        s["archive_dir"] = f"会话{alloc_number(state, 'subagent')}"
                        print(f"[{name}] 子代理发号：{s['archive_dir']}（{s['id'][:12]}）")
                    s["summaries"] = {}
                    s["fingerprint"] = None
                    s["fingerprint_len"] = len(turns)
                    s["numbered"] = len(turns)
                    write_turn_files(cfg, session_dir_of(chain_dir, s), turns)
                    write_subagent_note(cfg, chain_dir, s, parent_lookup)
                    continue

                # ---- 主会话 ----
                if not s.get("archive_dir"):
                    s["archive_dir"] = f"会话{alloc_number(state, 'main')}"
                    print(f"[{name}] 主会话发号：{s['archive_dir']}（{s['id'][:12]}）")
                parent_lookup[s["id"]] = (name, s.get("archive_dir") or "", s.get("title") or "")
                # 轮次结构变了（取消旧版合并、或其它重排）→ 旧概要会错位，清掉重生成。
                # 只比"已经看过的那一段前缀"：正常追加新回合不算结构变化。
                prev_fp = s.get("fingerprint")
                prev_len = int(s.get("fingerprint_len") or 0)
                changed = False
                if prev_fp is not None:
                    # 老数据只有 fingerprint 没有 fingerprint_len：用已记录的轮数当长度
                    check_len = prev_len if prev_len > 0 else int(s.get("numbered") or 0)
                    changed = check_len > 0 and turn_fingerprint(turns, min(check_len, len(turns))) != prev_fp
                else:
                    # 升级后第一次跑：用旧算法确认"以前的轮数"是合并算出来的
                    legacy = session_turns(cfg, s, recs, merge_resends=True)
                    changed = len(legacy) == int(s.get("numbered") or 0) and len(turns) != len(legacy)
                if changed and s.get("summaries"):
                    if len(turns) == int(s.get("numbered") or 0):
                        # 轮数没变、只是用户文本被清洗过（例如把压缩检查点剔掉）：保留旧概要，只更新指纹。
                        warnings.append(f"{short} 指纹变了但轮数没变（多半是清洗了注入文本），旧概要保留")
                    else:
                        backup_summaries(cfg, name, s)
                        print(f"{s['archive_dir']}：轮次结构变了（{s.get('numbered')} → {len(turns)}），旧概要已备份后作废，重新生成")
                        s["summaries"] = {}
                s["fingerprint"] = turn_fingerprint(turns)
                s["fingerprint_len"] = len(turns)
                s["summaries"] = {str(k): v for k, v in (s.get("summaries") or {}).items()}
                pending_turns = [t for t in turns if str(t["index"]) not in s["summaries"]]
                if pending_turns:
                    batch, used = [], 0
                    for t in pending_turns:
                        if batch and used + t["summary_chars"] > cfg["archive_batch_chars"]:
                            break
                        batch.append(t)
                        used += t["summary_chars"]
                    if client is None:
                        client = OpenAI(api_key=read_key(cfg), base_url=cfg["base_url"], timeout=60, max_retries=1)
                    print(f"[{name}] {s['archive_dir']}：待存档 {len(pending_turns)} 轮，本次处理 {len(batch)} 轮（约 {used} 字符）")
                    try:
                        got = make_summaries(cfg, client, batch)
                    except Exception as e:  # noqa: BLE001  断网/超时不该让整次归档崩掉
                        print(f"⚠️ 摘要生成失败（可能是断网/超时）：{e}；本次跳过，下次重试。")
                        got = {}
                    for t in batch:
                        if t["index"] in got:
                            s["summaries"][str(t["index"])] = got[t["index"]]
                    total_new += len(got)
                s["numbered"] = len(turns)
                write_turn_files(cfg, session_dir_of(chain_dir, s), turns)
                write_session_index(cfg, session_dir_of(chain_dir, s), s, turns)

            write_chain_index(cfg, name, sessions, chain_dir)
            state["sessions"] = sessions
            state["format"] = STATE_FORMAT
            state["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            atomic_write_json(chain_state_path(cfg, name), state)

            live = [s for s in sessions if not s.get("inherited")]
            mains = [s for s in live if kind_of(s) == "main" and s.get("numbered")]
            subs = [s for s in live if kind_of(s) == "subagent" and s.get("numbered")]
            total = sum(s.get("numbered", 0) for s in live)
            report.append(total)
            print(f"完成 [{name}]：主会话 {len(mains)} 个、子代理 {len(subs)} 个，共 {total} 轮")
            for s in mains + subs:
                if s.get("numbered"):
                    tag = "子代理/" if kind_of(s) == "subagent" else ""
                    print(f"  {name} / {tag}{s['archive_dir']}: {s['numbered']} 轮")

        # ---------- 3. 先归档已有的链（主链排第一：复制继承时来源就是最新的） ----------
        for name in sorted(states, key=chain_sort_key):
            archive_chain(name)

        # ---------- 4. 再新建链（复制继承 / 空白），并把落到新链的会话归档 ----------
        for name, parent in pending.items():
            if only_chain and name != only_chain:
                continue
            has_turn = any(
                (metas.get(sid) or {}).get("turns") and routing.get(sid) == name
                for sid in routing
            )
            if not has_turn and name not in states:
                print(f"（{name} 这次没有落到有回合的会话，先不建空链）")
                continue
            ensure_chain(cfg, states, name, parent)
            archive_chain(name)

        # 清理过期标记：只删“已经登记进某条链”的会话标记；还没归档的新会话必须保留，
        # 否则它的继承/新链选择会在第一次归档前被别的会话触发的归档顺手删掉。
        try:
            registered_ids = set()
            for st in states.values():
                for s in st.get("sessions") or []:
                    if isinstance(s, dict) and int(s.get("numbered") or 0) > 0:
                        registered_ids.add(s["id"])
            md = markers_dir(cfg)
            if os.path.isdir(md):
                for fname in os.listdir(md):
                    if not fname.endswith(".json") or fname == "当前.json":
                        continue
                    sid = fname[:-5]
                    if sid in registered_ids or sid in excluded_all:
                        os.remove(os.path.join(md, fname))
                        continue
                    try:
                        with open(os.path.join(md, fname), encoding="utf-8-sig") as f:
                            data = json.load(f) or {}
                        ch = data.get("chain")
                        kind = data.get("kind")
                        if ch and ch not in states and ch not in deleted_chains:
                            os.remove(os.path.join(md, fname))
                        elif kind == "blank" and not ch:
                            pass
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass

        print(f"全部完成：{len(report)} 条链，共 {sum(report)} 轮；本次新概要 {total_new} 条")
        for w in dict.fromkeys(warnings):
            print(f"⚠️ 归属自检：{w}（迁移请跑 整理/迁移 脚本，别手动搬）")
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
