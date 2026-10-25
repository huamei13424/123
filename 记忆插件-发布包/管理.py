# -*- coding: utf-8 -*-
r"""管理.py —— 删除会话档案 / 删除记忆链 / 列出链（给 Host 插件桥用；也支持命令行）。

用法：
    python 管理.py list-chains
    python 管理.py delete-session <会话id> [--json]
    python 管理.py delete-chain <链名> [--json]
    python 管理.py renumber-chains [--json]

规则：
  · 删除 = 目录移到 `废弃杂项\`（可恢复），并从状态里移除；不真删文件；
  · 删除会话后进 excluded 名单（防止归档器从原始会话文件重新收回来），并重排编号；
  · 删除链后写 `记忆库\.已删链.json`（归档器不会重建它），并把剩下的链按现存数量重排成 记忆链1..K；
  · 所有写操作都拿 `.归档锁`，状态文件原子写。
"""

import datetime
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load_archiver():
    import importlib.util
    spec = importlib.util.spec_from_file_location("turnarchive", os.path.join(HERE, "回合存档.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gg = load_archiver()
JUNK = gg.DEFAULT_JUNK


def now_tag():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def deleted_registry_path(cfg):
    return os.path.join(cfg["memory_root"], ".已删链.json")


def load_deleted(cfg):
    p = deleted_registry_path(cfg)
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f) or {}
        return data.get("deleted") or []
    except (OSError, ValueError):
        return []


def save_deleted(cfg, rows):
    gg.atomic_write_json(deleted_registry_path(cfg), {"deleted": rows})


def deleted_sessions_path(cfg):
    return os.path.join(cfg["memory_root"], ".已删会话.json")


def load_deleted_sessions(cfg):
    try:
        with open(deleted_sessions_path(cfg), encoding="utf-8-sig") as f:
            data = json.load(f) or {}
        return [str(x) for x in data.get("deleted") or []]
    except (OSError, ValueError):
        return []


def save_deleted_sessions(cfg, rows):
    gg.atomic_write_json(deleted_sessions_path(cfg), {"deleted": sorted(set(rows))})


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def acquire_lock(cfg):
    path = os.path.join(cfg["memory_root"], ".归档锁")
    os.makedirs(cfg["memory_root"], exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return path
        except FileExistsError:
            age = datetime.datetime.now().timestamp() - os.path.getmtime(path)
            pid = 0
            try:
                pid = int(open(path, encoding="utf-8").read().strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid):
                raise RuntimeError(f"后台归档正在运行（pid {pid}，锁 {int(age)} 秒），稍后再试")
            print(f"接管已退出进程留下的归档锁（pid {pid}，{int(age)} 秒）")
            os.remove(path)


def release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


def junk_dir(cfg):
    d = os.path.join(cfg["workspace"], "废弃杂项")
    os.makedirs(d, exist_ok=True)
    return d


def load_chain_state(cfg, chain):
    with open(gg.chain_state_path(cfg, chain), encoding="utf-8-sig") as f:
        return json.load(f) or {"format": gg.STATE_FORMAT, "sessions": []}


def save_chain_state(cfg, chain, state):
    gg.atomic_write_json(gg.chain_state_path(cfg, chain), state)


def sync_default_chain(cfg):
    """把 config.chain_name / .默认链.txt 同步到一个真实存在的链（删/改/重排后必须调用）。"""
    states = gg.load_chain_states(cfg)
    cfg_path = os.path.join(HERE, "config.json")
    try:
        raw = json.load(open(cfg_path, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    junk = junk_chain_name(cfg)
    name = str(raw.get("chain_name") or "")
    if name not in states:
        candidates = [n for n in states if n != junk]
        name = sorted(candidates, key=gg.chain_sort_key)[0] if candidates else junk
    raw["chain_name"] = name
    raw.setdefault("default_chain", junk)
    gg.atomic_write_json(cfg_path, raw)
    write_default_pointer(cfg, name)


def list_chains(cfg):
    rows = []
    for name in gg.load_chain_states(cfg):
        chain_dir = gg.chain_dir_of(cfg, name)
        try:
            st = load_chain_state(cfg, name)
        except (OSError, ValueError):
            continue
        sessions = st.get("sessions") or []
        mains = [s for s in sessions if gg.kind_of(s) == "main" and s.get("numbered")]
        subs = [s for s in sessions if gg.kind_of(s) == "subagent" and s.get("numbered")]
        shown = load_display_map(cfg).get(name, name)
        rows.append({
            "name": shown,
            "dir": name,
            "main": len(mains),
            "sub": len(subs),
            "turns": sum(int(s.get("numbered") or 0) for s in mains),
            "last_run": st.get("last_run") or "",
            "persona": os.path.exists(os.path.join(chain_dir, cfg.get("memory_file_name") or "转生提示词.md")),
            "hidden": name == str(cfg.get("default_chain") or JUNK),
        })
    rows.sort(key=lambda r: (r["hidden"], gg.chain_sort_key(r["name"])))
    return rows


def find_session(cfg, session_id):
    for chain in gg.load_chain_states(cfg):
        st = load_chain_state(cfg, chain)
        for s in st.get("sessions") or []:
            if s["id"] == session_id:
                return chain, st, s
    return None, None, None


def delete_session(cfg, session_id):
    chain, state, entry = find_session(cfg, session_id)
    if entry is None:
        return {"ok": False, "error": f"记忆库里没有这个会话：{session_id}"}
    chain_dir = gg.chain_dir_of(cfg, chain)
    label = entry.get("archive_dir") or entry["id"][:12]
    moved_to = ""
    src = ""
    if entry.get("archive_dir"):
        candidate = gg.session_dir_of(chain_dir, entry)
        if os.path.isdir(candidate):
            src = candidate
    if src:
        dest = os.path.join(junk_dir(cfg), f"删除的会话-{chain}-{label}-{now_tag()}")
        shutil.move(src, dest)
        moved_to = dest
    state["sessions"] = [x for x in state.get("sessions") or [] if x["id"] != session_id]
    state.setdefault("excluded", [])
    if session_id not in state["excluded"]:
        state["excluded"].append(session_id)
    save_deleted_sessions(cfg, load_deleted_sessions(cfg) + [session_id])
    lookup = gg.parent_lookup_of(cfg, {chain: state})
    changes = gg.renumber_chain(cfg, chain, state, lookup)
    save_chain_state(cfg, chain, state)
    gg.write_chain_index(cfg, chain, state.get("sessions") or [], chain_dir)
    chain_removed = False
    renamed = []
    if not (state.get("sessions") or []):
        # 删完变成空链：整条收走，先把本链 excluded 迁到根名单，再重排
        save_deleted_sessions(cfg, load_deleted_sessions(cfg) + list(state.get("excluded") or []))
        dest2 = os.path.join(junk_dir(cfg), f"删除的空链-{chain}-{now_tag()}")
        shutil.move(chain_dir, dest2)
        chain_removed = True
        renamed = [f"{a}→{b}" for a, b in renumber_chains(cfg, None).items()]
        sync_default_chain(cfg)
    # 顺手清掉这个会话的标记文件
    marker = os.path.join(gg.markers_dir(cfg), f"{session_id}.json")
    if os.path.exists(marker):
        os.remove(marker)
    return {
        "ok": True,
        "chain": chain,
        "label": label,
        "moved_to": moved_to,
        "chain_removed": chain_removed,
        "renamed": renamed,
        "renumbered": [f"{a}→{b}" for a, b in changes["main"] + changes["subagent"]],
    }


def renumber_chains(cfg, deleted_name):
    """剩下的 记忆链N 按现存顺序重排成 记忆链1..K（杂物箱不参与编号）。"""
    states = gg.load_chain_states(cfg)
    numbered = []
    for name in states:
        m = None
        import re as _re
        m = _re.match(r"^记忆链(\d+)$", name)
        if m:
            numbered.append((int(m.group(1)), name))
    numbered.sort()
    mapping = {}
    for i, (_, name) in enumerate(numbered, 1):
        new = f"记忆链{i}"
        if name != new:
            mapping[name] = new
    temp = {}
    failed = []
    for old in list(mapping):
        tmp = f".链改名临时-{len(temp)}"
        try:
            os.rename(gg.chain_dir_of(cfg, old), gg.chain_dir_of(cfg, tmp))
            temp[old] = tmp
        except OSError as e:
            failed.append(f"{old}（被占用：{e.strerror or e}）")
            del mapping[old]
    done = {}
    for old, tmp in list(temp.items()):
        new = mapping[old]
        try:
            os.rename(gg.chain_dir_of(cfg, tmp), gg.chain_dir_of(cfg, new))
            done[old] = new
        except OSError as e:
            failed.append(f"{old}（回滚改回原名：{e.strerror or e}）")
            try:
                os.rename(gg.chain_dir_of(cfg, tmp), gg.chain_dir_of(cfg, old))
            except OSError:
                pass
    mapping.clear()
    mapping.update(done)
    # 链信息 / 配置 / 标记
    for old, new in mapping.items():
        info_p = os.path.join(gg.chain_dir_of(cfg, new), "链信息.json")
        if os.path.exists(info_p):
            try:
                info = json.load(open(info_p, encoding="utf-8-sig"))
                info["name"] = new
                parent = info.get("parent")
                if parent in mapping:
                    info["parent"] = mapping[parent]
                gg.atomic_write_json(info_p, info)
            except (OSError, ValueError):
                pass
        # 重写会话目录
        try:
            st = load_chain_state(cfg, new)
            gg.write_chain_index(cfg, new, st.get("sessions") or [], gg.chain_dir_of(cfg, new))
        except (OSError, ValueError):
            pass
    cfg_path = os.path.join(HERE, "config.json")
    raw = json.load(open(cfg_path, encoding="utf-8-sig"))
    if raw.get("chain_name") in mapping:
        raw["chain_name"] = mapping[raw["chain_name"]]
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    marker_dir = gg.markers_dir(cfg)
    if os.path.isdir(marker_dir):
        for fname in os.listdir(marker_dir):
            if not fname.endswith(".json"):
                continue
            mp = os.path.join(marker_dir, fname)
            try:
                data = json.load(open(mp, encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if data.get("chain") in mapping:
                data["chain"] = mapping[data["chain"]]
                gg.atomic_write_json(mp, data)
    record_history(cfg, dict(mapping))
    return mapping


def rename_chain(cfg, old, new):
    states = gg.load_chain_states(cfg)
    if old not in states:
        return {"ok": False, "error": f"没有这条链：{old}"}
    if old == junk_chain_name(cfg):
        return {"ok": False, "error": "杂物箱是隐藏兜底链，不能改名"}
    new = gg.safe_chain_name(new)
    if new is None:
        return {"ok": False, "error": "新链名不合法"}
    if new in states:
        return {"ok": False, "error": f"链名已存在：{new}"}
    os.rename(gg.chain_dir_of(cfg, old), gg.chain_dir_of(cfg, new))
    info_p = os.path.join(gg.chain_dir_of(cfg, new), "链信息.json")
    if os.path.exists(info_p):
        try:
            info = json.load(open(info_p, encoding="utf-8-sig"))
            info["name"] = new
            gg.atomic_write_json(info_p, info)
        except (OSError, ValueError):
            pass
    raw = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8-sig"))
    if raw.get("chain_name") == old:
        raw["chain_name"] = new
        with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    write_default_pointer(cfg, raw.get("chain_name") or old)
    marker_dir = gg.markers_dir(cfg)
    if os.path.isdir(marker_dir):
        for fname in os.listdir(marker_dir):
            if not fname.endswith(".json"):
                continue
            mp = os.path.join(marker_dir, fname)
            try:
                data = json.load(open(mp, encoding="utf-8-sig"))
                if data.get("chain") == old:
                    data["chain"] = new
                    gg.atomic_write_json(mp, data)
            except (OSError, ValueError):
                pass
    try:
        st = load_chain_state(cfg, new)
        gg.write_chain_index(cfg, new, st.get("sessions") or [], gg.chain_dir_of(cfg, new))
    except (OSError, ValueError):
        pass
    display = load_display_map(cfg)
    if display.pop(old, None) is not None or display:
        save_display_map(cfg, display)
    record_history(cfg, {old: new})
    sync_default_chain(cfg)
    return {"ok": True, "renamed": [f"{old}→{new}"]}


def history_path(cfg):
    return os.path.join(cfg["memory_root"], ".链历史.json")


def load_history(cfg):
    try:
        with open(history_path(cfg), encoding="utf-8-sig") as f:
            data = json.load(f) or {}
        return {str(k): [str(x) for x in v] for k, v in (data.get("chains") or {}).items()}
    except (OSError, ValueError):
        return {}


def save_history(cfg, mapping):
    gg.atomic_write_json(history_path(cfg), {"chains": mapping})


def record_history(cfg, renames):
    """renames: {旧目录名: 新目录名}；新链名继承旧名列表 + 旧名本身。"""
    history = load_history(cfg)
    for old, new in renames.items():
        if old == new:
            continue
        names = set(history.get(old, [])) | {old}
        history.pop(old, None)
        merged = set(history.get(new, [])) | names
        history[new] = sorted(merged)
    # 丢掉已经不存在的目录
    states = gg.load_chain_states(cfg)
    history = {k: v for k, v in history.items() if k in states}
    save_history(cfg, history)


def display_map_path(cfg):
    return os.path.join(cfg["memory_root"], ".链显示名.json")


def load_display_map(cfg):
    try:
        with open(display_map_path(cfg), encoding="utf-8-sig") as f:
            data = json.load(f) or {}
        return {str(k): str(v) for k, v in (data.get("names") or {}).items()}
    except (OSError, ValueError):
        return {}


def save_display_map(cfg, mapping):
    gg.atomic_write_json(display_map_path(cfg), {"names": mapping})


def resolve_chain_dir(cfg, name):
    """把显示名或目录名统一解析成目录名。"""
    states = gg.load_chain_states(cfg)
    if name in states:
        return name
    for dir_name, shown in load_display_map(cfg).items():
        if shown == name and dir_name in states:
            return dir_name
    return None


def default_pointer_path(cfg):
    return os.path.join(cfg["memory_root"], ".默认链.txt")


def write_default_pointer(cfg, name):
    try:
        with open(default_pointer_path(cfg), "w", encoding="utf-8") as f:
            f.write(name + "\n")
    except OSError:
        pass


def chain_first_session(cfg, name):
    """取链里编号最小的主会话（按编号）。"""
    try:
        st = load_chain_state(cfg, name)
    except (OSError, ValueError):
        return None
    mains = [s for s in st.get("sessions") or []
             if gg.kind_of(s) == "main" and int(s.get("numbered") or 0) > 0 and s.get("archive_dir", "").startswith("会话")]
    if not mains:
        return None
    mains.sort(key=lambda s: int(s["archive_dir"][2:]))
    return mains[0]


def title_name_from_session(cfg, entry):
    """会话标题 → 链名（不合格返回 None）。"""
    title = str(entry.get("title") or "").strip()
    if not title:
        return None
    title = re.sub(r'[\\/:*?"<>|\r\n\t]', "", title).strip(" .")
    if not title or title.startswith("记忆链"):
        return None
    if re.match(r"^(新链|继承|Greeting|New Session)", title, re.I):
        return None
    return title[:20]


def auto_rename_chains(cfg):
    """把 记忆链N 改成第一个会话的标题；撞名加（2）；返回 {旧: 新}。"""
    states = gg.load_chain_states(cfg)
    deleted = {str(x.get("name")) for x in load_deleted(cfg) if isinstance(x, dict)}
    used = set(states.keys()) | deleted | {str(cfg.get("default_chain") or JUNK)}
    mapping = {}
    for name in states:
        if not re.match(r"^记忆链\d+$", name):
            continue
        entry = chain_first_session(cfg, name)
        if entry is None:
            continue
        title = title_name_from_session(cfg, entry)
        if not title:
            continue
        new = title
        n = 2
        while new in used:
            new = f"{title}（{n}）"
            n += 1
        if new != name:
            mapping[name] = new
            used.add(new)
    global CFG
    CFG = cfg
    temp = {}
    direct = {}
    failed = []
    failed_dirs = []
    for old in list(mapping):
        new = mapping[old]
        tmp = f".链改名临时-{len(temp)}"
        try:
            os.rename(gg.chain_dir_of(cfg, old), gg.chain_dir_of(cfg, tmp))
            temp[old] = tmp
        except OSError as e:
            try:
                rename_by_moving_children(gg.chain_dir_of(cfg, old), gg.chain_dir_of(cfg, new))
                direct[old] = new
                failed.append(f"{old}（目录被占用，已把内容搬进新目录，旧空壳待删）")
            except OSError:
                failed.append(f"{old}（被占用，先改用显示名：{e.strerror or e}）")
                failed_dirs.append(old)
                del mapping[old]
    done = dict(direct)
    for old, tmp in list(temp.items()):
        new = mapping[old]
        try:
            os.rename(gg.chain_dir_of(cfg, tmp), gg.chain_dir_of(cfg, new))
            done[old] = new
        except OSError as e:
            failed.append(f"{old}（回滚改回原名：{e.strerror or e}）")
            try:
                os.rename(gg.chain_dir_of(cfg, tmp), gg.chain_dir_of(cfg, old))
            except OSError:
                pass
    mapping.clear()
    mapping.update(done)
    for old, new in mapping.items():
        info_p = os.path.join(gg.chain_dir_of(cfg, new), "链信息.json")
        if os.path.exists(info_p):
            try:
                info = json.load(open(info_p, encoding="utf-8-sig"))
                info["name"] = new
                gg.atomic_write_json(info_p, info)
            except (OSError, ValueError):
                pass
        try:
            st = load_chain_state(cfg, new)
            gg.write_chain_index(cfg, new, st.get("sessions") or [], gg.chain_dir_of(cfg, new))
        except (OSError, ValueError):
            pass
    display = load_display_map(cfg)
    for dir_name in list(display):
        if not os.path.isdir(gg.chain_dir_of(cfg, dir_name)):
            display.pop(dir_name, None)
    for old in direct:
        display.pop(old, None)
    for old in failed_dirs:
        if old and old in states:
            entry = chain_first_session(cfg, old)
            title = title_name_from_session(cfg, entry) if entry else None
            if title:
                display[old] = title
    save_display_map(cfg, display)
    record_history(cfg, dict(mapping))

    cfg_path = os.path.join(HERE, "config.json")
    raw = json.load(open(cfg_path, encoding="utf-8-sig"))
    default_name = mapping.get(raw.get("chain_name"), raw.get("chain_name"))
    raw["chain_name"] = default_name
    gg.atomic_write_json(cfg_path, raw)
    write_default_pointer(cfg, default_name)
    marker_dir = gg.markers_dir(cfg)
    if os.path.isdir(marker_dir):
        for fname in os.listdir(marker_dir):
            if not fname.endswith(".json"):
                continue
            mp = os.path.join(marker_dir, fname)
            try:
                data = json.load(open(mp, encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if data.get("chain") in mapping:
                data["chain"] = mapping[data["chain"]]
                gg.atomic_write_json(mp, data)
    return mapping, failed


def pending_rmdir_path(cfg):
    return os.path.join(cfg["memory_root"], ".待删空目录.txt")


def note_pending_rmdir(cfg, path):
    try:
        with open(pending_rmdir_path(cfg), "a", encoding="utf-8") as f:
            f.write(path + "\n")
    except OSError:
        pass


def try_pending_rmdirs(cfg):
    p = pending_rmdir_path(cfg)
    if not os.path.exists(p):
        return []
    removed = []
    remaining = []
    try:
        with open(p, encoding="utf-8-sig") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return []
    for line in lines:
        try:
            if os.path.isdir(line) and not os.listdir(line):
                os.rmdir(line)
                removed.append(line)
            elif os.path.exists(line):
                remaining.append(line)
        except OSError:
            remaining.append(line)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(remaining) + ("\n" if remaining else ""))
    except OSError:
        pass
    return removed


def rename_by_moving_children(src, dst):
    """目录本身被句柄占用时：把里面的东西搬进新目录，留下空壳等以后删。"""
    if os.path.exists(dst):
        raise OSError(f"目标已存在：{dst}")
    os.makedirs(dst)
    moved = []
    try:
        for name in os.listdir(src):
            os.rename(os.path.join(src, name), os.path.join(dst, name))
            moved.append(name)
    except OSError:
        for name in reversed(moved):
            try:
                os.rename(os.path.join(dst, name), os.path.join(src, name))
            except OSError:
                pass
        try:
            os.rmdir(dst)
        except OSError:
            pass
        raise
    try:
        os.rmdir(src)
    except OSError:
        note_pending_rmdir(CFG, src)
    return True


JUNK_DISPLAY = "继承既存会话及其他"


def junk_chain_name(cfg):
    return str(cfg.get("default_chain") or JUNK)


def _clean_title(text, fallback):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(text or "")).strip(" .")[:20]
    if not name or name.startswith("."):
        name = fallback
    return name


def _unique_chain_name(cfg, states, base):
    used = set(states)
    used |= {str(x.get("name")) for x in load_deleted(cfg) if isinstance(x, dict)}
    used |= {junk_chain_name(cfg), str(cfg.get("chain_name") or "")}
    name = base
    n = 2
    while name in used:
        name = f"{base}（{n}）"
        n += 1
    return name


def list_junk(cfg):
    junk = junk_chain_name(cfg)
    states = gg.load_chain_states(cfg)
    if junk not in states:
        return {"ok": True, "junk": junk, "display": JUNK_DISPLAY, "sessions": []}
    try:
        st = load_chain_state(cfg, junk)
    except (OSError, ValueError):
        return {"ok": False, "error": "读不了杂物箱状态"}
    sessions = st.get("sessions") or []
    subs_by_parent = {}
    for s in sessions:
        if gg.kind_of(s) == "subagent" and s.get("parent"):
            subs_by_parent[s["parent"]] = subs_by_parent.get(s["parent"], 0) + 1
    rows = []
    for s in sessions:
        if gg.kind_of(s) != "main":
            continue
        if not int(s.get("numbered") or 0):
            continue
        rows.append({
            "id": s["id"],
            "title": s.get("title") or "",
            "turns": int(s.get("numbered") or 0),
            "dir": s.get("archive_dir") or "",
            "sub": subs_by_parent.get(s["id"], 0),
        })
    rows.sort(key=lambda r: gg.chain_sort_key(r["dir"]))
    return {"ok": True, "junk": junk, "display": JUNK_DISPLAY, "sessions": rows}


def import_journal_path(cfg):
    return os.path.join(cfg["memory_root"], ".导入日记.json")


def _deep_states(states, names):
    out = {}
    for name in names:
        if name in states:
            out[name] = json.loads(json.dumps(states[name], ensure_ascii=False))
    return out


def _next_session_name(base_dir, kind, used):
    n = 1
    while True:
        name = f"会话{n}"
        path = os.path.join(base_dir, name) if kind == "main" else os.path.join(base_dir, gg.SUBAGENT_DIR, name)
        if name not in used and not os.path.exists(path):
            return name
        n += 1


def rollback_import(cfg, journal):
    moves = list(journal.get("moves") or [])
    ok = True
    for src, dst in reversed(moves):
        try:
            if os.path.isdir(dst) and not os.path.exists(src):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(dst, src)
        except OSError:
            ok = False
    for name, state in (journal.get("states") or {}).items():
        try:
            save_chain_state(cfg, name, state)
        except OSError:
            ok = False
    for name in journal.get("created") or []:
        path = gg.chain_dir_of(cfg, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            ok = False
    try:
        os.remove(import_journal_path(cfg))
    except OSError:
        pass
    return ok


def recover_import(cfg):
    path = import_journal_path(cfg)
    if not os.path.exists(path):
        return {"ok": False, "error": "没有未完成的导入日记"}
    try:
        with open(path, encoding="utf-8-sig") as f:
            journal = json.load(f) or {}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"读不了导入日记：{e}"}
    ok = rollback_import(cfg, journal)
    return {"ok": ok, "recovered": len(journal.get("moves") or []), "restored_chains": list((journal.get("states") or {}).keys())}


def import_junk_sessions(cfg, ids, target=None):
    """把杂物箱里的既有会话搬出来：默认每条会话各自成链，也可以统一导入指定链。

    事务：先写 .导入日记.json（状态快照 + 每次移动），任何异常回滚；进程被杀可
    用 `python 管理.py recover-import` 恢复。
    """
    states = gg.load_chain_states(cfg)
    junk = junk_chain_name(cfg)
    if junk not in states:
        return {"ok": False, "error": "杂物箱是空的"}
    if os.path.exists(import_journal_path(cfg)):
        return {"ok": False, "error": "上一次导入还没收尾：先运行 python 管理.py recover-import"}
    if target is not None:
        target = resolve_chain_dir(cfg, target)
        if target is None or target not in states:
            return {"ok": False, "error": f"目标链不存在：{target}"}
        if target == junk:
            return {"ok": False, "error": "不能导入回杂物箱"}
    src_state = states[junk]
    wanted = set(str(x) for x in ids)
    chosen = [s for s in src_state.get("sessions") or []
              if s.get("id") in wanted and gg.kind_of(s) == "main" and int(s.get("numbered") or 0)]
    if not chosen:
        return {"ok": False, "error": "没有选中可导入的会话（只支持有轮次的主会话）"}
    src_dir = gg.chain_dir_of(cfg, junk)

    before_names = set(states.keys())
    plans = []
    created = []
    for idx, entry in enumerate(chosen):
        if not os.path.isdir(gg.session_dir_of(src_dir, entry)):
            return {"ok": False, "error": f"源目录不在（{entry.get('archive_dir')}），先跑一次归档再试"}
        if target is not None:
            dest = target
        else:
            base = _clean_title(entry.get("title"), f"既有会话{entry.get('archive_dir') or idx + 1}")
            dest = _unique_chain_name(cfg, states, base)
            states[dest] = gg.ensure_chain(cfg, states, dest)
            created.append(dest)
        plans.append((entry, dest))
    created = sorted(set(created))

    touched = {junk} | {dest for _, dest in plans}
    snapshots = _deep_states(states, touched)
    journal = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "junk": junk, "target": target,
        "moves": [], "states": snapshots, "created": created, "status": "running",
    }

    def save_journal():
        gg.atomic_write_json(import_journal_path(cfg), journal)

    save_journal()
    imported = []
    try:
        for entry, dest in plans:
            dst_state = states[dest]
            dst_dir = gg.chain_dir_of(cfg, dest)
            used = {s.get("archive_dir") for s in dst_state.get("sessions") or []}
            new_name = _next_session_name(dst_dir, "main", used)
            src_session_dir = gg.session_dir_of(src_dir, entry)
            dst_path = os.path.join(dst_dir, new_name)
            journal["moves"].append([src_session_dir, dst_path])
            save_journal()
            os.rename(src_session_dir, dst_path)
            used.add(new_name)
            new_entry = dict(entry)
            new_entry["archive_dir"] = new_name
            dst_state.setdefault("sessions", []).append(new_entry)
            subs = [s for s in src_state.get("sessions") or []
                    if gg.kind_of(s) == "subagent" and s.get("parent") == entry["id"]]
            if subs:
                os.makedirs(os.path.join(dst_dir, gg.SUBAGENT_DIR), exist_ok=True)
            for sub in subs:
                sub_src = gg.session_dir_of(src_dir, sub)
                if not os.path.isdir(sub_src):
                    continue
                sub_name = _next_session_name(dst_dir, "subagent", used)
                sub_dst = os.path.join(dst_dir, gg.SUBAGENT_DIR, sub_name)
                journal["moves"].append([sub_src, sub_dst])
                save_journal()
                os.rename(sub_src, sub_dst)
                used.add(sub_name)
                sub2 = dict(sub)
                sub2["archive_dir"] = sub_name
                dst_state.setdefault("sessions", []).append(sub2)
                src_state["sessions"] = [s for s in src_state.get("sessions") or [] if s.get("id") != sub.get("id")]
            src_state["sessions"] = [s for s in src_state.get("sessions") or [] if s.get("id") != entry["id"]]
            save_chain_state(cfg, junk, src_state)
            save_chain_state(cfg, dest, dst_state)
            imported.append({"id": entry["id"], "title": entry.get("title") or "", "chain": dest})

        for dest in created:
            st = states[dest]
            every = int(cfg.get("every_n_turns") or 60)
            total = sum(int(s.get("numbered") or 0) for s in st.get("sessions") or [] if gg.kind_of(s) == "main")
            st["turns"] = total
            st["last_turn"] = (total // every) * every
            save_chain_state(cfg, dest, st)
        for dest in touched:
            gg.write_chain_index(cfg, dest, states[dest].get("sessions") or [], gg.chain_dir_of(cfg, dest))
        journal["status"] = "done"
        save_journal()
        os.remove(import_journal_path(cfg))
    except Exception as e:  # noqa: BLE001
        rollback_import(cfg, journal)
        return {"ok": False, "error": f"导入失败，已回滚：{e}"}

    md = gg.markers_dir(cfg)
    os.makedirs(md, exist_ok=True)
    for item in imported:
        try:
            gg.atomic_write_json(os.path.join(md, f"{item['id']}.json"), {
                "session": item["id"], "chain": item["chain"], "kind": "inherit",
                "time": datetime.datetime.now().isoformat(timespec="seconds"), "cwd": cfg.get("workspace"),
            })
        except OSError:
            pass
    return {"ok": True, "imported": imported, "created": created}


def prune_empty_chains(cfg):
    """收走没有任何已归档会话的链（目录里只剩状态/目录文件的历史遗留），返回收走的链名。"""
    try_pending_rmdirs(cfg)
    removed = []
    states = gg.load_chain_states(cfg)
    for name in list(states):
        try:
            st = load_chain_state(cfg, name)
        except (OSError, ValueError):
            continue
        numbered = [s for s in st.get("sessions") or [] if int(s.get("numbered") or 0) > 0]
        if numbered:
            continue
        chain_dir = gg.chain_dir_of(cfg, name)
        if not os.path.isdir(chain_dir):
            continue
        dest = os.path.join(junk_dir(cfg), f"删除的空链-{name}-{now_tag()}")
        shutil.move(chain_dir, dest)
        removed.append(name)
    if removed:
        renumber_chains(cfg, None)
    sync_default_chain(cfg)
    return removed


def delete_chain(cfg, name):
    states = gg.load_chain_states(cfg)
    if name not in states:
        return {"ok": False, "error": f"没有这条链：{name}"}
    if name == junk_chain_name(cfg):
        return {"ok": False, "error": "杂物箱是隐藏兜底链，不能删除"}
    if name.startswith(".") or name == "." or name == "..":
        return {"ok": False, "error": "非法链名"}
    chain_dir = gg.chain_dir_of(cfg, name)
    state = load_chain_state(cfg, name)
    session_ids = [s["id"] for s in (state.get("sessions") or []) if isinstance(s, dict) and s.get("id")]
    dest = os.path.join(junk_dir(cfg), f"删除的记忆链-{name}-{now_tag()}")
    shutil.move(chain_dir, dest)
    if session_ids:
        save_deleted_sessions(cfg, load_deleted_sessions(cfg) + session_ids)
    rows = load_deleted(cfg)
    rows.append({"name": name, "time": now_tag(), "backup": dest})
    save_deleted(cfg, rows)
    history = load_history(cfg)
    if history.pop(name, None) is not None:
        save_history(cfg, history)
    history = load_history(cfg)
    if history.pop(name, None) is not None:
        save_history(cfg, history)
    mapping = renumber_chains(cfg, name)
    sync_default_chain(cfg)
    return {
        "ok": True,
        "deleted": name,
        "moved_to": dest,
        "renamed": [f"{a}→{b}" for a, b in mapping.items()],
    }


CFG = None


def main():
    global CFG
    argv = sys.argv[1:]
    cfg = gg.load_config()
    CFG = cfg
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        print(__doc__)
        return 0
    cmd = argv[0]
    lock = None
    try:
        if cmd == "list-chains":
            out = {"ok": True, "chains": list_chains(cfg)}
        elif cmd == "delete-session" and len(argv) >= 2:
            lock = acquire_lock(cfg)
            out = delete_session(cfg, argv[1].strip())
        elif cmd == "delete-chain" and len(argv) >= 2:
            lock = acquire_lock(cfg)
            target_dir = resolve_chain_dir(cfg, argv[1].strip()) or argv[1].strip()
            out = delete_chain(cfg, target_dir)
        elif cmd == "rename-chain" and len(argv) >= 3:
            lock = acquire_lock(cfg)
            old_dir = resolve_chain_dir(cfg, argv[1].strip()) or argv[1].strip()
            out = rename_chain(cfg, old_dir, argv[2].strip())
        elif cmd == "auto-rename-chains":
            lock = acquire_lock(cfg)
            mapping, failed = auto_rename_chains(cfg)
            out = {"ok": True, "renamed": [f"{a}→{b}" for a, b in mapping.items()], "failed": failed}
        elif cmd == "recover-import":
            lock = acquire_lock(cfg)
            out = recover_import(cfg)
        elif cmd == "list-junk":
            out = list_junk(cfg)
        elif cmd == "import-junk":
            lock = acquire_lock(cfg)
            ids = []
            to = None
            i = 1
            while i < len(argv):
                if argv[i] == "--ids" and i + 1 < len(argv):
                    ids = [x for x in argv[i + 1].split(",") if x.strip()]
                    i += 2
                elif argv[i] == "--to" and i + 1 < len(argv):
                    to = argv[i + 1]
                    i += 2
                else:
                    i += 1
            out = import_junk_sessions(cfg, ids, to)
        elif cmd == "prune-empty":
            lock = acquire_lock(cfg)
            removed = prune_empty_chains(cfg)
            out = {"ok": True, "removed": removed}
        elif cmd == "renumber-chains":
            lock = acquire_lock(cfg)
            out = {"ok": True, "renamed": [f"{a}→{b}" for a, b in renumber_chains(cfg, None).items()]}
        else:
            out = {"ok": False, "error": "用法：list-chains | delete-session <id> | delete-chain <名> | renumber-chains"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": str(e)}
    finally:
        if lock:
            release_lock(lock)
    print(json.dumps(out, ensure_ascii=False) if as_json else json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
