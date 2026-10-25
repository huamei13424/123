# -*- coding: utf-8 -*-
"""记忆插件安装器（Windows / DSH）

用法（交互）：
    python 安装.py

非交互：
    python 安装.py --dsh-home C:\\Users\\你\\.dsh --profile desktop \\
        --workspace D:\\你的工作区 --session-root C:\\Users\\你\\.dsh\\sessions \\
        --key sk-xxx --python python --yes

做四件事：
1. 把插件复制到 <DSH_HOME>/memory-plugin/（plugin/ + scripts/ + 记忆库/）；
2. 生成 scripts/config.json 和 scripts/钥匙.txt；
3. 在 DSH profile 里登记依赖与 bundle，并把 plugin 复制进 profile 的 node_modules；
4. 跑一次 list-chains 冒烟测试，然后提示重启 DSH。

不会修改 ~/.dsh/AGENTS.md（记忆规则由插件 systemPrompt 段注入）。
重复运行会覆盖插件与脚本，但不会动记忆库；记忆库请自行备份。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_SRC = next(
    (p for p in (os.path.join(HERE, "界面插件"), os.path.join(HERE, "plugin"), os.path.join(HERE, "插件"))
     if os.path.isdir(p)),
    os.path.join(HERE, "界面插件"),
)
SCRIPT_DIR = next((p for p in (HERE, os.path.join(HERE, "scripts")) if os.path.isfile(os.path.join(p, "回合存档.py"))), HERE)
PLUGIN_NAME = "@local/memory-panel"
SCRIPTS = ["回合存档.py", "记忆生成器.py", "管理.py", "查看.py", "状态.py", "requirements.txt"]


def log(text):
    print(f"[安装] {text}")


def ask(prompt, default=""):
    value = input(f"{prompt}{('（默认 ' + default + '）') if default else ''}：").strip()
    return value or default


def build_config(args):
    workspace = args.workspace
    return {
        "workspace": workspace,
        "session_root": args.session_root,
        "session_dir_mark": "",
        "source_cwd": workspace,
        "memory_root": os.path.join(args.install_root, "记忆库"),
        "chain_name": "记忆链1",
        "memory_dir": "",
        "key_file": os.path.join(args.install_root, "scripts", "钥匙.txt"),
        "single_file": True,
        "split_thinking": True,
        "store_reasoning": True,
        "store_tool_calls": False,
        "store_system_injected": False,
        "memory_file_name": "转生提示词.md",
        "turn_dir": "会话",
        "index_file": "索引.md",
        "chain_index_file": "会话目录.md",
        "memory_max_chars": 800,
        "every_n_turns": 60,
        "char_budget": 40000,
        "summary_min_chars": 5,
        "summary_max_chars": 20,
        "archive_batch_chars": 40000,
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "default_chain": "杂物箱",
    }


def copy_plugin(target):
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(PLUGIN_SRC, target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsh-home", default=os.path.join(os.path.expanduser("~"), ".dsh"))
    parser.add_argument("--profile", default="desktop")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--session-root", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--python", default=sys.executable or "python")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-smoke", action="store_true")
    args = parser.parse_args()

    dsh_home = os.path.abspath(os.path.expanduser(args.dsh_home))
    if not os.path.isdir(dsh_home):
        log(f"找不到 DSH home：{dsh_home}，请先安装并运行一次 DSH。")
        return 1
    if args.workspace == "":
        if args.yes:
            log("缺少 --workspace")
            return 1
        args.workspace = ask("你的工作区绝对路径（DSH 会话的 cwd）")
    if args.session_root == "":
        args.session_root = os.path.join(dsh_home, "sessions")
    if args.key == "" and not args.yes:
        args.key = ask("DeepSeek API Key（只写本地文件，不会上传）")
    if args.key == "":
        log("缺少 API key：之后可手动写 <安装目录>/scripts/钥匙.txt")
    args.install_root = os.path.join(dsh_home, "memory-plugin")

    profile_dir = os.path.join(dsh_home, "profiles", args.profile)
    if not os.path.isdir(profile_dir):
        log(f"找不到 DSH profile：{profile_dir}")
        return 1
    profile_pkg = os.path.join(profile_dir, "package.json")
    try:
        manifest = json.load(open(profile_pkg, encoding="utf-8-sig"))
    except (OSError, ValueError):
        log(f"读不了 profile 的 package.json：{profile_pkg}")
        return 1

    # 1) 安装目录
    os.makedirs(args.install_root, exist_ok=True)
    scripts_dir = os.path.join(args.install_root, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    plugin_target = os.path.join(args.install_root, "plugin")
    copy_plugin(plugin_target)
    for name in SCRIPTS:
        src = os.path.join(SCRIPT_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(scripts_dir, name))
    memory_root = os.path.join(args.install_root, "记忆库")
    os.makedirs(memory_root, exist_ok=True)
    pointer = os.path.join(memory_root, ".默认链.txt")
    if not os.path.exists(pointer):
        with open(pointer, "w", encoding="utf-8") as f:
            f.write("记忆链1\n")
    config_file = os.path.join(scripts_dir, "config.json")
    config = build_config(args)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    if args.key:
        with open(os.path.join(scripts_dir, "钥匙.txt"), "w", encoding="utf-8") as f:
            f.write(args.key.strip() + "\n")
    log(f"插件/脚本已就位：{args.install_root}")

    # 2) profile 依赖 + bundle
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, dict):
        dependencies = {}
    dependencies[PLUGIN_NAME] = "file:" + plugin_target.replace("\\", "/")
    manifest["dependencies"] = dependencies
    dsh = manifest.get("dsh")
    if not isinstance(dsh, dict):
        dsh = {}
    profile = dsh.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    bundles = profile.get("bundles") if isinstance(profile.get("bundles"), list) else []
    if PLUGIN_NAME not in bundles:
        bundles.append(PLUGIN_NAME)
    profile["bundles"] = bundles
    dsh["profile"] = profile
    manifest["dsh"] = dsh
    with open(profile_pkg, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    node_target = os.path.join(profile_dir, "node_modules", "@local", "memory-panel")
    copy_plugin(node_target)
    log(f"已登记到 profile：{profile_pkg}，并复制到 {node_target}")

    # 3) 冒烟：空库 list-chains
    if not args.no_smoke:
        python = args.python
        if not os.path.isfile(python):
            python = "python"
        try:
            result = subprocess.run(
                [python, os.path.join(scripts_dir, "管理.py"), "list-chains", "--json"],
                cwd=scripts_dir, capture_output=True, text=True, timeout=60,
            )
            data = json.loads((result.stdout or "").strip().splitlines()[-1] if result.stdout.strip() else "{}")
            if data.get("ok"):
                log("冒烟测试通过：管理.py 能读配置（空库）")
            else:
                log(f"冒烟测试返回失败：{data}")
        except Exception as e:  # noqa: BLE001
            log(f"冒烟测试跳过/失败：{e}")

    log("完成。现在重启 DSH；规则由插件注入，不需要改 AGENTS.md。")
    log("如需卸载：删除 profile 里对 @local/memory-panel 的依赖/ bundle，并删除 " + args.install_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
