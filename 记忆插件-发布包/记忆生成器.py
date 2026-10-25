# -*- coding: utf-8 -*-
"""
记忆生成器 v3.3
功能：每 N 轮对话，在本地生成/更新一份"转生提示词"。
v2 改动：所有设置搬到 config.json。
v3 改动：① 单文件模式——只维护一份档案，每个周期覆写更新；
         ② 内容精简——只保留「用户信息」和「AI 的性格与说话方式」两节。
v3.1 改动：档案字数上限变成可调项（memory_max_chars，默认 1000）。
v3.2 改动：进度改成按"上次生成的轮数"记录，这样改 every_n_turns 不会再错乱。
v3.3 改动：① 真正读到 AI 回复（以前只取用户文本，人格里 AI 那一节等于凭空编）；
          ② 用户消息按 data.source.kind 过滤，压缩检查点/系统注入不再当人话；
          ③ 支持 --all 逐链生成（Host 后台会对每条链都跑一次）；
          ④ --chain 名字清洗；拿不到本链状态时不再退回"全工作区"防止吸别链内容。
"""

import os
import sys
import glob
import json
import re
import datetime

import zstandard
from openai import OpenAI


def atomic_write_text(path, text):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def pid_alive(pid):
    """Windows 下用 OpenProcess/GetExitCodeProcess 判断进程是否还活着（os.kill(pid,0) 不可靠）。"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
EXAMPLE_FILE = os.path.join(SCRIPT_DIR, "config.example.json")

# 配置文件里没写的项，用这里的值兜底
DEFAULTS = {
    "workspace": "",
    "session_root": os.path.join(os.path.expanduser("~"), ".dsh", "sessions"),
    "session_dir_mark": "",
    "source_cwd": "",
    "memory_dir": "",
    "key_file": "",
    "single_file": True,
    "memory_file_name": "转生提示词.md",
    "memory_max_chars": 1000,
    "every_n_turns": 60,
    "default_chain": "杂物箱",
    "char_budget": 40000,
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
}


def load_config():
    """读 config.json；不存在就根据示例生成一份，并提示用户去改。"""
    if not os.path.exists(CONFIG_FILE):
        example = "{}"
        if os.path.exists(EXAMPLE_FILE):
            example = open(EXAMPLE_FILE, encoding="utf-8").read()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(example)
        print("第一次运行：已根据示例生成配置文件")
        print("   ", CONFIG_FILE)
        print("请打开它，把里面的路径改成你自己的，然后重新运行。")
        sys.exit(0)

    with open(CONFIG_FILE, encoding="utf-8-sig") as f:
        cfg = json.load(f)

    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)

    # 留空的路径自动算出来
    if not cfg["memory_dir"]:
        if cfg.get("memory_root") and cfg.get("chain_name"):
            # 目录分类 v2：记忆库\<链名>\...
            cfg["memory_dir"] = os.path.join(cfg["memory_root"], cfg["chain_name"])
        else:
            cfg["memory_dir"] = os.path.join(cfg["workspace"], "记忆库")
    elif not os.path.isabs(cfg["memory_dir"]):
        cfg["memory_dir"] = os.path.join(cfg["workspace"], cfg["memory_dir"])
    if not cfg["key_file"]:
        cfg["key_file"] = os.path.join(SCRIPT_DIR, "钥匙.txt")
    return cfg


def read_key(cfg):
    """从单独的钥匙文件读 API key——绝不写死在代码里，方便上传 GitHub。"""
    if not os.path.exists(cfg["key_file"]):
        print("找不到钥匙文件：", cfg["key_file"])
        print("请在 config.json 里改 key_file，或在该位置放一个以 sk- 开头的 key。")
        sys.exit(1)
    key = open(cfg["key_file"], encoding="utf-8").read().strip()
    if not key.startswith("sk-"):
        print("钥匙文件的内容不像一个 API key。")
        sys.exit(1)
    return key


def find_session_file(cfg):
    """找到本工作区最近更新的会话文件（用于兜底读最新对话）。"""
    files = all_session_files(cfg)
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0] if files else None


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
    if path.endswith(".zstd"):
        with open(path, "rb") as f:
            return zstandard.ZstdDecompressor().stream_reader(f).read().decode("utf-8", "replace")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def load_records(path, first_only=False):
    """读会话日志（.zstd 解压 / 明文 .jsonl 直接读），逐行解析 JSON。"""
    text = read_text(path)
    lines = [l for l in text.splitlines() if l.strip()]
    if first_only:
        return [json.loads(lines[0])] if lines else []
    return [json.loads(l) for l in lines]


def load_first_record(path):
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


def session_dir_of_log(path):
    return os.path.dirname(path)


def count_valid_turns(records):
    """数"有效回合"：只有带 AI 回复文字的回合才算（和 回合存档.py 同一口径）。

    没有 AI 回复的回合（断网、取消、只剩思考/工具调用）不记录，**也不计入周期数**。
    """
    total = 0
    in_turn = False
    replied = False
    for r in records:
        typ = r.get("type")
        if typ == "turn/start":
            if in_turn and replied:
                total += 1
            in_turn, replied = True, False
        elif typ == "turn/end":
            if in_turn and replied:
                total += 1
            in_turn, replied = False, False
        elif typ == "assistant/message" and in_turn:
            parts = ((r.get("data") or {}).get("message") or {}).get("content") or []
            has_text = any(
                isinstance(p, dict) and p.get("type") == "text" and (p.get("text") or "").strip()
                for p in parts
            )
            has_tool = any(isinstance(p, dict) and p.get("type") == "tool-call" for p in parts)
            if has_text and not has_tool:
                replied = True
    if in_turn and replied:
        total += 1
    return total


def all_session_files(cfg):
    """本工作区下所有会话文件（扫目录读会话头 cwd 过滤；兼容 vN 格式与明文 jsonl）。"""
    root = cfg["session_root"]
    out = []
    if not os.path.isdir(root):
        print("没找到本工作区的会话文件，请检查 config.json 里的 session_root。")
        sys.exit(1)
    candidates = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        subs = [os.path.join(p, x) for x in sorted(os.listdir(p)) if os.path.isdir(os.path.join(p, x))]
        if not subs:
            candidates.append(p)
        candidates.extend(subs)
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
    if not out:
        print("没找到本工作区的会话文件，请检查 config.json 里的 session_root。")
        sys.exit(1)
    return out


def chain_session_files(cfg):
    """只取本链登记过的会话文件；拿不到本链状态就返回空（绝不退回"全工作区"）。

    子代理会话（kind=subagent）不算：它们不参与人格继承。
    """
    sp = os.path.join(cfg["memory_dir"], "回合存档状态.json")
    try:
        with open(sp, encoding="utf-8-sig") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        print(f"⚠️ 读不到本链状态文件（{sp}），本次不生成，防止把别链内容吸进来。")
        return []
    paths = [s.get("path") for s in (data.get("sessions") or [])
             if s.get("path") and os.path.exists(s["path"])
             and not (s.get("kind") == "subagent" or s.get("parent"))]
    if not paths:
        print("本链还没有已归档的会话文件。")
    return sorted(paths)


def archived_turn_count(cfg):
    """第二层（回合存档）已归档的回合数 = 各会话已编号轮数之和。
    拿不到就返回 None，让调用方退回旧算法。"""
    try:
        with open(os.path.join(cfg["memory_dir"], "回合存档状态.json"), encoding="utf-8-sig") as f:
            data = json.load(f) or {}
        if isinstance(data.get("sessions"), list):          # v3+：会话独立格式
            n = sum(int(s.get("numbered") or 0) for s in data["sessions"]
                    if not (s.get("kind") == "subagent" or s.get("parent")))
            return n or None
        summaries = data.get("summaries") or {}             # 旧格式兼容
        return len(summaries) or None
    except (OSError, ValueError):
        return None


def cumulative_turns(cfg):
    """跨窗口累计轮数：返回 (最新窗口路径, 该窗口记录, 总轮数)；本链没会话时返回 None。

    轮数优先取第二层已归档的回合数——口径统一在"有内容的、已结束的回合"上。
    只有拿不到第二层状态时，才退回"所有窗口 turn/start 之和"。
    """
    files = chain_session_files(cfg)
    if not files:
        return None
    newest = files[-1]
    n = archived_turn_count(cfg)
    if n is not None:
        return newest, load_records(newest), n
    total = 0
    for path in files:
        total += count_valid_turns(load_records(path))
    return newest, load_records(newest), total


def recent_dialogue(cfg, budget):
    """跨窗口取"最近的对话"，直到用满字符预算。
    以前只读最近一个会话文件 + last_seq，换窗口后接不上；现在按窗口倒着取，
    再按预算从后往前剪，拿到的永远是最新的一段对话。"""
    chunks = []
    used = 0
    for path in reversed(chain_session_files(cfg)):
        recs = load_records(path)
        piece = "\n\n".join(f"{role}：{text}" for role, text, _ in extract_dialogue(recs, 0))
        if not piece:
            continue
        chunks.append(piece)
        used += len(piece)
        if used > budget * 1.5:      # 够剪了就不用再往上翻
            break
    body = "\n\n".join(reversed(chunks))
    if len(body) > budget:
        body = "（更早的内容已省略）\n\n" + body[-budget:]
    return body


INJECT_TEXT_MARKS = (
    "Current runtime context",
    "Time sampled while preparing",
    "system-reminder",
    "A skill is a reusable set",
    "[SCHEDULE REMINDER]",
    "automatically generated checkpoint",
    "<compacted-summary>",
)


def is_injected_text(text):
    return any(mark in text for mark in INJECT_TEXT_MARKS)


def extract_dialogue(records, after_seq):
    """抽取 seq 大于 after_seq 的对话文字，返回 [(角色, 内容, seq)]。

    v3.3：用户只收 source.kind == "user"（老日志退回文字特征过滤）；
          AI 从 data.message.content 取文本（以前错取 data.content，AI 一条都取不到）。
    """
    out = []
    for r in records:
        typ = r.get("type")
        if typ not in ("user/message", "assistant/message"):
            continue
        if r.get("seq", 0) <= after_seq:
            continue
        data = r.get("data", {}) or {}

        if typ == "user/message":
            src = data.get("source")
            kind = src.get("kind") if isinstance(src, dict) else None
            texts = []
            for p in (data.get("content") or []):
                if isinstance(p, dict) and p.get("type") == "text":
                    texts.append(p.get("text", ""))
            text = "\n".join(texts).strip()
            if not text:
                continue
            if kind is not None:
                if kind != "user":
                    continue
            elif is_injected_text(text):
                continue
            out.append(("用户", text, r.get("seq", 0)))
            continue

        # assistant/message：正文在 data.message.content
        message = data.get("message") or {}
        parts = message.get("content") or []
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                t = (p.get("text") or "").strip()
                if t:
                    texts.append(t)
        text = "\n".join(texts).strip()
        if text:
            out.append(("AI", text, r.get("seq", 0)))
    return out


def clean_filename(name):
    """文件名清洗：Windows 不允许 \\ / : * ? \" < > | ，还要去掉换行。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:40] if name else "未命名"


def configured_memory_file(cfg):
    """单文件模式下的固定档案路径。"""
    return os.path.join(cfg["memory_dir"], cfg["memory_file_name"])


def newest_memory_file(cfg, index_file):
    """找旧档案：单文件模式就用那个固定文件；否则取最新的 .txt。"""
    fixed = configured_memory_file(cfg)
    if cfg.get("single_file") and os.path.exists(fixed):
        return fixed
    files = [
        p for p in glob.glob(os.path.join(cfg["memory_dir"], "*.txt"))
        if os.path.basename(p) != os.path.basename(index_file)
    ]
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def build_prompt(old_memory, dialogue_text, max_chars):
    """给模型的任务书：蒸馏成精简的转生提示词（只要用户信息 + AI 性格）。"""
    system = "你是一个对话记忆压缩器，负责把长对话蒸馏成精简、可长期继承的人物档案。"
    user = f"""任务：把【新增对话】合并进【已有的旧档案】，输出一份更新后的"转生提示词"。

要求（很重要）：
1. 第一行只写标题，格式为「转生提示词 vN」——旧档案里是 v几，就往上加 1；没有旧档案就从 v1 开始；
2. 正文**只能**出现下面这两个二级标题，一个不能多、一个不能少：
   ## 用户信息
   ## AI 的性格与说话方式
   旧档案里可能有别的小节（例如"设计共识""情绪与约定""必须保留的原句"）——那只是素材，**它们的小节结构一律不要继承、不要输出**；
3. 入选标准只有一条：**"三年后依然成立"**。成立才写，不成立就不写。
   - 「用户信息」＝关于"这个人"的稳定结论：性格、偏好、在意的东西、表达习惯、相处方式、情绪边界（什么会让他难受）。
   - 「AI 的性格与说话方式」＝人设骨架：称呼、语气、颜文字习惯、讲解风格、双方的相处规则。颜文字**举 5～8 个最典型的就够**，不要穷举列表。这份档案本身是文档，正文里不要出现颜文字——颜文字只作为"AI 说话风格"的举例列出。
4. 明确**不许写**的东西（它们属于第二层回合存档，不属于第一层）：
   - 项目进度、待办事项、谁改了什么代码、用了什么脚本或命令；
   - 技术细节：pip、编码、权限、路径、报错、提交哈希、版本号、配置项名称；
   - 一次性事件：某天发生了什么、某次争执、某次测试结果；
   - 文件路径与文件名清单。
   注意区分**事件**和**长期影响**：事件本身不写，它造成的长期影响才写。例如不写"某天同学藏了他的书包"，只写"他与同学保持距离、习惯独处"。
5. 严格区分说话人：用户的习惯、想法、处境写进「用户信息」；AI 角色的人设、语气、表情习惯、相处规则写进「AI 的性格与说话方式」。不要张冠李戴；
6. 不要摘抄对话原句——脱离语境的句子会显得逻辑断裂。要写就写关于"人"的稳定结论；
7. 只写对话里有依据的内容，绝不编造；旧档案里那些**现在已经不成立**的条目，直接删掉，不要为了保全面面俱到而留旧账；
8. 全文控制在 600～{max_chars} 字之间——超出上限说明太啰嗦，明显低于下限说明信息不全。

【已有的旧档案】（可能为空）
{old_memory if old_memory else "（无）"}

【新增对话】
{dialogue_text}
"""
    return system, user


def build_compress_prompt(memory_text, max_chars):
    """压缩通道：只做减法，把超长的档案压进字数上限。"""
    system = "你是一个只做减法的文本压缩器。"
    user = f"""把下面这份人物档案压缩到 {max_chars} 字以内。

铁律：
1. **只删不加**——不许新增任何事实，也不许改变原意；
2. 保留第一行「转生提示词 vN」（N 保持不变），以及两个二级标题「## 用户信息」「## AI 的性格与说话方式」；
3. **硬性结构**：每个小节最多 5 条，每条不超过 80 字；颜文字只留 5 个，且只能出现在描述 AI 说话风格那一条里——正文其他位置一个都不许有；
4. 优先删：一次性事件的具体经过（只留它造成的长期影响）、项目进度与机制、技术细节、多余的例子与修饰词、括号里的补充说明；
5. 必须留：情绪上最敏感、最需要小心对待的点（只写概括，不复述具体事件）、相处规则与边界、他讨厌什么（空泛安慰、被越权替他决定、被居高临下教育）；
6. 这不是润色，是**砍**。砍不干净就不合格。
7. 直接输出档案，不要任何解释。

【待压缩档案】
{memory_text}
"""
    return system, user


def generate_for_chain(cfg):
    """给一条链做一次人格更新（cfg 的 chain_name / memory_dir 已指向这条链）。"""
    if int(cfg.get("every_n_turns") or 0) <= 0:
        cfg["every_n_turns"] = 60
    chain_dir = cfg["memory_dir"]
    if not os.path.isdir(chain_dir) or not os.path.exists(os.path.join(chain_dir, "回合存档状态.json")):
        print(f"这条链还没有归档过（{chain_dir}），跳过。")
        return
    os.makedirs(chain_dir, exist_ok=True)

    state_file = os.path.join(cfg["memory_dir"], "状态.json")
    index_file = os.path.join(cfg["memory_dir"], "记忆目录.txt")

    print("配置文件：", CONFIG_FILE)
    print("记忆目录：", cfg["memory_dir"])
    print("模式：", "单文件（每周期覆写）" if cfg.get("single_file") else "每次新建文件")
    print(f"档案字数上限：{cfg['memory_max_chars']}")

    # v3.3：轮数改成跨会话窗口累计。
    # 原来只数"最近一个会话文件"的轮数，一换窗口就从头数，里程碑永远到不了。
    # 现在：轮数 = 各窗口 turn/start 之和（旧窗口的历史也补数进来），
    #       续读位置 last_seq 从第二层（回合存档状态.json）里取。
    got = cumulative_turns(cfg)
    if got is None:
        print("本链还没有已归档的会话，跳过。")
        return
    session_path, records, turns = got

    state = {}
    if os.path.exists(state_file):
        state = json.load(open(state_file, encoding="utf-8"))
    # 兼容旧状态：v3.1 之前记的是 last_milestone / turns
    last_turn = state.get("last_turn", state.get("turns", 0))
    if turns < last_turn:
        print(f"轮数从 {last_turn} 降到 {turns}（删过会话/链），里程碑回退到当前轮数。")
        last_turn = turns
        state["last_turn"] = turns
        state["turns"] = turns
        atomic_write_json(state_file, state)   # 必须落盘，否则下次又读回旧 last_turn，永远到不了 60

    print(f"当前轮数：{turns}，上次生成于第 {last_turn} 轮（每 {cfg['every_n_turns']} 轮一次）")
    if not cfg.get("_force") and turns - last_turn < cfg["every_n_turns"]:
        print(f"还没到下一个里程碑（还差 {cfg['every_n_turns'] - (turns - last_turn)} 轮），这次不用生成。")
        return
    if cfg.get("_force"):
        print("手动强制生成（--force）：忽略里程碑，按现有对话更新一次人格。")

    body = recent_dialogue(cfg, cfg["char_budget"])
    if not body.strip():
        print("没有抽到新的对话内容。")
        return

    old_file = newest_memory_file(cfg, index_file)
    old_memory = ""
    if old_file:
        old_memory = open(old_file, encoding="utf-8").read().strip()
        print(f"已找到旧档案：{os.path.basename(old_file)}")
    else:
        print("这是第一次生成，没有旧档案。")

    system, user = build_prompt(old_memory, body, cfg["memory_max_chars"])

    print(f"送入模型的字符数：{len(body)}（预算 {cfg['char_budget']}）")
    print("正在请求模型生成转生提示词……")

    # 防并发：计划任务 / 常驻监控撞车时可能重复调模型（重复花钱）。锁超过 10 分钟自动失效。
    lock_path = os.path.join(cfg["memory_dir"], ".生成锁")
    if os.path.exists(lock_path):
        age = datetime.datetime.now().timestamp() - os.path.getmtime(lock_path)
        pid = 0
        try:
            pid = int(open(lock_path, encoding="utf-8").read().strip() or "0")
        except (OSError, ValueError):
            pid = 0
        alive = pid_alive(pid)
        if alive and age < 1800:
            print(f"另一个生成进程正在运行（pid {pid}，锁建立于 {int(age)} 秒前），本次跳过。")
            return
        print(f"清理残留生成锁（pid {pid} 已不在 / 锁已过期 {int(age)} 秒）")
        try:
            os.remove(lock_path)
        except OSError:
            pass
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    client = OpenAI(api_key=read_key(cfg), base_url=cfg["base_url"], timeout=60, max_retries=1)
    try:
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        result = resp.choices[0].message.content.strip()
    except Exception as e:  # noqa: BLE001  断网/超时/余额不足都不该让进程崩掉
        print(f"⚠️ 调用模型失败（可能是断网/超时/余额不足）：{e}")
        print("本次跳过，进度不动，等下一次触发再试。")
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return

    # 护栏：检查格式和字数有没有守规矩（只警告，不擅自改内容）
    allowed = ["## 用户信息", "## AI 的性格与说话方式"]
    found = [l.strip() for l in result.splitlines() if l.strip().startswith("## ")]
    extra = [h for h in found if h not in allowed]
    missing = [h for h in allowed if h not in found]
    if extra:
        print("⚠️ 格式警告：出现了不该有的小节 →", "、".join(extra))
    if missing:
        print("⚠️ 格式警告：缺少小节 →", "、".join(missing))
    if not extra and not missing:
        print("✅ 格式检查通过：只有两个小节")
    if len(result) > cfg["memory_max_chars"]:
        # 压缩通道：最多压 3 次，一次比一次狠，达标就停（每次只多花一次调用）
        for attempt in range(1, 4):
            print(f"⚠️ 字数警告：{len(result)} 字，超过上限 {cfg['memory_max_chars']}（第 {attempt} 次压缩）")
            c_system, c_user = build_compress_prompt(result, cfg["memory_max_chars"])
            try:
                cresp = client.chat.completions.create(
                    model=cfg["model"],
                    messages=[
                        {"role": "system", "content": c_system},
                        {"role": "user", "content": c_user},
                    ],
                )
                packed = (cresp.choices[0].message.content or "").strip()
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ 压缩调用失败：{e}；保留当前稿（{len(result)} 字）")
                break
            if "## 用户信息" not in packed or len(packed) >= len(result):
                print("压缩结果不可用，保留当前稿")
                break
            result = packed
            print(f"第 {attempt} 次压缩后：{len(result)} 字")
            if len(result) <= cfg["memory_max_chars"]:
                break
        if len(result) > cfg["memory_max_chars"]:
            print(f"⚠️ 压到 {len(result)} 字仍未达标，保留此稿（下次周期会再试）")

    lines = [l for l in result.splitlines() if l.strip()]
    title = clean_filename(lines[0]) if lines else "未命名"
    today = datetime.date.today().strftime("%Y-%m-%d")

    if cfg.get("single_file"):
        out_path = configured_memory_file(cfg)
    else:
        out_path = os.path.join(cfg["memory_dir"], f"{today} {title}.txt")

    atomic_write_text(out_path, result + "\n")
    print(f"已写入：{out_path}（{len(result)} 字）")

    # 索引工作已交给第二层（回合索引.md），这里不再生成 记忆目录.txt

    # 记录进度：按"上次生成的轮数"记，只向前对齐、绝不让 last_turn 倒退
    every = int(cfg["every_n_turns"])
    old_last = int(state.get("last_turn", state.get("turns", 0)) or 0)
    if turns >= old_last:
        state["last_turn"] = old_last + ((turns - old_last) // every) * every
    else:
        state["last_turn"] = turns
    state["last_seq"] = max(r.get("seq", 0) for r in records)
    state["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["turns"] = turns
    atomic_write_json(state_file, state)
    print(f"进度已保存：第 {turns} 轮生成完成，下次在第 {state['last_turn'] + cfg['every_n_turns']} 轮之后")
    try:
        os.remove(lock_path)
    except OSError:
        pass


def safe_chain_name(name):
    """链名清洗（与 回合存档.py 规则一致；防止 --chain 把文件写到库外）。"""
    if not isinstance(name, str):
        return None
    if re.search(r'[\\/:*?"<>|\r\n\t]', name):
        return None
    name = name.strip(" .")
    if not name or name in (".", ".."):
        return None
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    if name.upper().split(".")[0] in reserved:
        return None
    return name[:40]


def chain_sort_key(name):
    if name == "主链":
        return (0, 0, name)
    m = re.match(r"^记忆链(\d+)$", name)
    if m:
        return (1, int(m.group(1)), name)
    return (2, 0, name)


def memory_root_of(cfg):
    return cfg.get("memory_root") or os.path.dirname(os.path.abspath(cfg["memory_dir"]))


def list_chains(cfg):
    root = memory_root_of(cfg)
    out = []
    if not os.path.isdir(root):
        return out
    hidden = {str(cfg.get("default_chain") or "杂物箱")}
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "回合存档状态.json")):
            continue
        if name in hidden or name.startswith("."):
            continue
        out.append(name)
    return sorted(out, key=chain_sort_key)


def point_to_chain(cfg, name):
    root = memory_root_of(cfg)
    cfg["chain_name"] = name
    cfg["memory_dir"] = os.path.join(root, name)
    return cfg


def main():
    cfg = load_config()
    argv = sys.argv[1:]
    if "--force" in argv:
        cfg["_force"] = True

    if "--all" in argv:
        chains = list_chains(cfg)
        if not chains:
            print("记忆库里还没有任何链。")
            return
        for name in chains:
            print(f"\n===== 链：{name} =====")
            sub = dict(cfg)
            point_to_chain(sub, name)
            try:
                generate_for_chain(sub)
            except Exception as e:  # noqa: BLE001  一条链失败不影响别的链
                print(f"⚠️ 链 {name} 生成失败：{e}")
        return

    if "--chain" in argv:
        i = argv.index("--chain")
        raw = argv[i + 1] if i + 1 < len(argv) else ""
        name = safe_chain_name(raw)
        if not name:
            print("--chain 后面的链名不合法（不能带路径符号）。")
            return
        point_to_chain(cfg, name)

    generate_for_chain(cfg)


if __name__ == "__main__":
    main()
