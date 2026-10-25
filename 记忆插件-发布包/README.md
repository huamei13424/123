# 记忆插件 · memory-panel

> 给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（DSH）里的 AI 伙伴一套**本地长期记忆**：人格 + 对话原文，换会话、换窗口、换模型之后还能接上。

这是一个实验性的个人项目：它不追求通用记忆检索，也不做向量 RAG；它只解决一件事——**让同一个 AI 伙伴在下次见面时，还是它**。

## 它是什么 / 不是什么

**是**：

- 把 DSH 的对话自动沉淀成本地纯文本文件（`.md`），随时可以打开、搜索、审计；
- 分层的记忆：一条「人格档案」+ 每会话索引 + 每轮原文/思考档；
- 多记忆链、链选择、旧会话导入、可恢复的删除；
- 插件形式接入 DSH：Host 挂事件钩子与本地 HTTP 通道，Client 提供选链/管理界面。

**不是**：

- 不是云端记忆服务，不会上传你的对话；
- 不是 RAG：没有 embedding、没有向量库，取什么是规则和索引决定的；
- 不是“装完就自动继承全部历史”：旧会话默认收进隐藏的杂物箱，需要手动导入；
- 不承诺开箱即用跨平台：当前只支持 Windows + DSH。

## 功能

- **自动归档**：DSH 每轮 `turn/end` 后 20 秒自动跑（90 秒冷却、DSH 启动后补跑一次），不需要手动点；
- **人格档案**：每条链一份 `转生提示词.md`，每 60 轮更新一次，上限 800 字；
- **原文与索引**：每轮保存用户原话和 AI 回复（含思考档），每会话生成索引；新窗口先读索引，需要时再翻具体轮次；
- **多链**：链名默认取该链第一个会话的标题；新会话不选链就是开新链；隐藏兜底链「杂物箱」收编无法归链的会话；
- **旧会话导入**：管理记忆链 →「继承既存会话及其他」，可把旧会话各自变成独立链，或统一导入指定链；
- **删除**：删除链 / 删除会话档案都有二次确认，移到 `废弃杂项`，可恢复；
- **AI 读取规则随插件走**：规则由 Host 注册为 DSH systemPrompt 段，不依赖 `~/.dsh/AGENTS.md`。

## 环境要求

- Windows 10/11（当前只在 Windows 上验证过）；
- DSH 已安装并能正常启动 Web GUI；
- Python 3.10+，`pip install -r requirements.txt`（需要 `openai`、`zstandard`）；
- 一个可用的 DeepSeek API Key（`base_url` 可在配置里改）。

## 安装

### 一键安装（推荐）

1. 克隆/下载本仓库；
2. 在仓库目录运行：

   ```bash
   python 安装.py
   ```

3. 按提示填：工作区路径、DSH profile（默认 `desktop`）、API Key；
4. 重启 DSH。

安装器会做：把插件复制到 `<DSH_HOME>/memory-plugin/`、生成 `scripts/config.json` 与 `scripts/钥匙.txt`、在 profile 里登记依赖和 bundle、复制插件到 `node_modules`、跑一次空库冒烟测试。

### 手动安装

1. 把 `界面插件/` 复制到 `<DSH_HOME>/memory-plugin/plugin/`；
2. 把根目录的 `.py` 与 `requirements.txt` 复制到 `<DSH_HOME>/memory-plugin/scripts/`；
3. 在 `scripts/` 里放 `config.json`（参考 `config.example.json`）和 `钥匙.txt`（API Key）；
4. 编辑 DSH profile 的 `package.json`：`dependencies` 加 `"@local/memory-panel": "file:<安装目录>/plugin"`，`dsh.profile.bundles` 加 `"@local/memory-panel"`；
5. 把 `plugin/` 再复制一份到 `<profile>/node_modules/@local/memory-panel`；
6. 重启 DSH。

## 快速使用

1. 新建会话，点输入框上方的记忆链胶囊：
   - 选「继承 某链」→ 这次会话读那条链的记忆；
   - 不选 / 点 × 关闭 → 开一条新链；
2. 正常聊天。结束一轮约 20 秒后会自动归档，链名取该会话标题；
3. 点胶囊 →「管理记忆链」：查看各链、改名、删除、把旧会话从「继承既存会话及其他」导入；
4. 会话行 "..." 菜单里有「删除记忆档案」（只删记忆库副本，原始 DSH 对话不动）；旧会话导入中途被杀，可在 `scripts/` 里运行 `python 管理.py recover-import` 恢复。

## 记忆库结构

```
<DSH_HOME>/memory-plugin/记忆库/
├─ .默认链.txt                 默认链目录名（AI 读它找默认链）
├─ .链标记\                    每次选链写的标记（当前.json）
├─ <链名>\
│  ├─ 转生提示词.md             人格档案
│  ├─ 会话目录.md               链级导航
│  ├─ 会话N\
│  │  ├─ 索引.md                该会话的轮次索引
│  │  ├─ 1.md、1.think.md …     每轮原文 / 思考档
│  └─ 子代理\                   子代理会话原文（不参与人格）
└─ 杂物箱\                      隐藏兜底链，只归档不继承
```

## 配置

主要字段（完整见 `config.example.json`）：

| 字段 | 说明 |
|---|---|
| `workspace` / `source_cwd` | 被归档的 DSH 工作区（会话 cwd） |
| `session_root` | DSH 会话目录，通常 `C:\Users\<你>\.dsh\sessions` |
| `memory_root` | 记忆库根目录；安装器默认放在 `<DSH_HOME>/memory-plugin/记忆库` |
| `key_file` | API Key 文件；默认 `scripts/钥匙.txt` |
| `model` / `base_url` | 摘要与人格生成用的模型 |
| `every_n_turns` | 每多少轮生成一次人格（默认 60） |
| `memory_max_chars` | 人格档案字数上限（默认 800） |
| `default_chain` | 兜底链目录名（默认 `杂物箱`） |

## AI 怎么用这些文件

- 新会话第一次回答前，插件会在 `记忆库\.链标记\当前.json` 里留下选链记录（`inherit` 读对应链，`blank` 是新链）；
- 读取顺序：人格 → 会话目录 → 当轮需要的索引 → 具体轮次；
- 记忆库对 AI 只读，写入只由脚本或用户执行；
- 读取规则由插件注册为 systemPrompt 段，不依赖用户手写 AGENTS 文件。

## 已知限制

- 仅 Windows；依赖本机 Python 与 API Key；
- 客户端 / Host 改动需要**重启 DSH**，刷新页面不够；
- `.链标记\当前.json` 是全局单文件，多窗口同时选链会互相覆盖（10 分钟时效只能缓解）；
- 旧会话默认进隐藏杂物箱，不会自动变成可继承记忆，需要手动导入；
- 旧会话导入带 journal 与回滚：中途失败会尽量恢复；进程被强杀后运行 `python 管理.py recover-import` 恢复；极端情况下仍可能有人工检查；
- 手动链改名没有“目录被占用 → 搬孩子”的兜底（自动改名有）；
- `config.json` 的部分写入不是原子操作；`.已删链` / `.已删会话` 名单会累积；
- 旧脚本已移到 `旧脚本/`，不要运行；
- 没有 Issue / PR 支持承诺，个人项目，按现状提供。

## 隐私

- 对话、人格、API Key 全部只在本机；
- 仓库不包含记忆库、密钥、日志；
- 公开分享前请确认 `git ls-files` 里没有个人数据。

## 卸载

1. 删除 DSH profile `package.json` 里的 `@local/memory-panel` 依赖与 bundle；
2. 删除 `<profile>/node_modules/@local/memory-panel`；
3. 删除 `<DSH_HOME>/memory-plugin/`（记忆库就在里面，先备份再删）；
4. 重启 DSH。

## License

MIT（发布前请在仓库根目录补一个 `LICENSE` 文件；如果你想换协议，改这里和 LICENSE 即可）。

---

*本项目是个人实验，不是官方产品；README 里写的限制都是真实现状，遇到问题欢迎自己改。*
