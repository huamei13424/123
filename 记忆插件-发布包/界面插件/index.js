/**
 * 记忆插件 · Host 半
 *
 * 接 DSH 的会话事件：一轮结束（`turn/end`）并且属于本工作区时，安静 20 秒后
 * 自动跑一遍 `回合存档.py` + `记忆生成器.py`，把「定时/轮询归档」换成事件驱动。
 *
 * 几个设计取舍：
 * · **安静窗口**：同一串对话里每轮结束都会来事件，合并成一次运行，避免每轮都起进程；
 * · **冷却**：两次运行至少隔 90 秒，防止连发；
 * · **全局锁**：脚本自己也有 `.归档锁`，和计划任务/监控撞车时后到的直接跳过，不会写坏；
 * · **启动补跑**：DSH 启动 90 秒后跑一次，接住上次没归档完的回合；
 * · **找不到脚本目录就安静退出**（只写一行原因），不影响 DSH 启动。
 *
 * 脚本目录的定位顺序：
 *   1. 环境变量 `MEMORY_PANEL_SCRIPTS_DIR`；
 *   2. 本包在 `<profile>/node_modules/@local/memory-panel`，往上三级读 profile 的
 *      package.json，依赖里 `file:` 指向的包目录的**上一级**就是脚本目录
 *      （`...\记忆插件\界面插件` → `...\记忆插件`）；
 *   3. 都不行就禁用。
 *
 * 运行日志：`<脚本目录>\日志-事件.txt`（一次运行写一行，出问题先看它）。
 */
import { spawn } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, renameSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const envInt = (name, fallback) => {
  const value = Number(process.env[name]);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
};

const QUIET_MS = envInt('MEMORY_PANEL_QUIET_MS', 20_000);      // 一轮结束后安静多久再跑
const COOLDOWN_MS = envInt('MEMORY_PANEL_COOLDOWN_MS', 90_000); // 两次运行之间最少隔多久
const STARTUP_MS = envInt('MEMORY_PANEL_STARTUP_MS', 90_000);   // DSH 启动后多久补跑一次
const LOG_FILE = '日志-事件.txt';
const LOG_MAX_BYTES = 512 * 1024;

const readJson = (file) => {
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch (error) {
    return undefined;
  }
};

/** 找到记忆插件的脚本目录（里面应该有 回合存档.py / config.json）。 */
function resolveScriptsDir() {
  const fromEnv = process.env.MEMORY_PANEL_SCRIPTS_DIR;
  if (fromEnv && existsSync(path.join(fromEnv, '回合存档.py'))) return fromEnv;

  // <profile>/node_modules/@local/memory-panel → 上三级 = <profile>
  const profileDir = path.resolve(HERE, '../../..');
  const manifest = readJson(path.join(profileDir, 'package.json'));
  const dependencies = manifest === undefined || manifest === null ? undefined : manifest.dependencies;
  const spec = dependencies === undefined || dependencies === null ? undefined : dependencies['@local/memory-panel'];
  if (typeof spec === 'string' && spec.startsWith('file:')) {
    const packageDir = path.resolve(profileDir, spec.slice('file:'.length));
    const root = path.dirname(packageDir);
    for (const candidate of [root, path.join(root, 'scripts')]) {
      if (existsSync(path.join(candidate, '回合存档.py'))) return candidate;
    }
  }
  return undefined;
}

/** Python 解释器：优先用工作区自带的 python，其次 PATH 里的。 */
function resolvePython(scriptsDir) {
  const cfg = readJson(path.join(scriptsDir, 'config.json')) ?? {};
  const workspace = typeof cfg.workspace === 'string' && cfg.workspace !== ''
    ? cfg.workspace
    : path.dirname(scriptsDir);
  const bundled = path.join(workspace, 'python', 'python.exe');
  return existsSync(bundled) ? bundled : 'python';
}

const stamp = () => {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

const lastLines = (text, n) => {
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== '');
  return lines.slice(-n).join(' ⏎ ').slice(0, 400);
};

/**
 * 接 DSH 事件：turn/end 之后触发归档。
 * @param {import('cordis').Context} ctx - 组合进来的 Host 上下文。
 */
export function apply(ctx) {
  const scriptsDir = resolveScriptsDir();

  let writeLog = () => {};
  const log = (text) => {
    writeLog(`[${stamp()}] ${text}`);
    ctx.logger.info(`memory-panel: ${text}`);
  };

  if (scriptsDir === undefined) {
    log('没找到记忆插件脚本目录，事件钩子未启用（可用环境变量 MEMORY_PANEL_SCRIPTS_DIR 指定）');
    return;
  }

  const logFile = path.join(scriptsDir, LOG_FILE);
  writeLog = (line) => {
    try {
      if (existsSync(logFile) && statSync(logFile).size > LOG_MAX_BYTES) {
        renameSync(logFile, path.join(scriptsDir, '日志-事件.1.txt'));
      }
      appendFileSync(logFile, line + '\n', 'utf8');
    } catch (error) {
      ctx.logger.warn(`memory-panel: 写运行日志失败：${String(error)}`);
    }
  };

  const cfg = readJson(path.join(scriptsDir, 'config.json')) ?? {};
  const sourceCwd = String(cfg.source_cwd ?? cfg.workspace ?? '').toLowerCase();
  const python = resolvePython(scriptsDir);

  /* ---------- 记忆规则：注册为系统提示词段 ----------
   * 规则文本随插件包一起走（instructions.md），不依赖用户手写 ~/.dsh/AGENTS.md；
   * 老版本 DSH 没有 systemPrompt 服务时，这段注册会被跳过（用 AGENTS 模板兜底）。
   */
  const rulesFile = path.join(HERE, 'instructions.md');
  if (existsSync(rulesFile)) {
    let rulesText = '';
    try {
      rulesText = readFileSync(rulesFile, 'utf8').trim();
    } catch (error) {
      log(`读取 instructions.md 失败，跳过规则注入：${String(error)}`);
    }
    if (rulesText !== '') {
      ctx.inject(['systemPrompt'], (promptCtx) => {
        try {
          promptCtx.effect(() => promptCtx.systemPrompt.section({
            name: 'memory-panel:rules',
            order: 900,
            text: rulesText,
          }));
          log('记忆规则已注册为系统提示词段（memory-panel:rules）');
        } catch (error) {
          log(`注册系统提示词段失败：${String(error)}`);
        }
      });
    }
  }

  /* ---------- 链绑定：客户端把选中的链发过来，Host 写进记忆库的标记文件 ----------
   * 聊天里不出现任何信号；AI 在会话开头读 `记忆库\.链标记\当前.json` 自己判断。
   * 同时给归档脚本留一份按会话 id 命名的标记（.链标记\<id>.json）。
   */
  const memoryRoot = String(cfg.memory_root ?? '').trim() !== ''
    ? String(cfg.memory_root)
    : path.join(String(cfg.workspace ?? path.dirname(scriptsDir)), '记忆库');
  const markerDir = path.join(memoryRoot, '.链标记');

  const safeChain = (value) => {
    if (typeof value !== 'string') return undefined;
    const name = value.trim();
    if (name === '' || name.length > 40) return undefined;
    if (name === '.' || name === '..') return undefined;
    if (/[\\/:*?"<>|\r\n\t]/.test(name)) return undefined;
    return { 主链: '记忆链1' }[name] ?? name;   // 旧链名别名
  };

  const chainNamesOnDisk = () => {
    try {
      return readdirSync(memoryRoot, { withFileTypes: true })
        .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.'))
        .map((entry) => entry.name);
    } catch (error) {
      return [];
    }
  };

  const nextChainName = () => {
    let max = 0;
    for (const name of chainNamesOnDisk()) {
      const matched = /^记忆链(\d+)$/.exec(name);
      if (matched !== null) max = Math.max(max, Number(matched[1]));
    }
    return `记忆链${max + 1}`;
  };

  const chainOfSession = (sessionId) => {
    for (const name of chainNamesOnDisk()) {
      const st = readJson(path.join(memoryRoot, name, '回合存档状态.json'));
      if (st === undefined || st === null || !Array.isArray(st.sessions)) continue;
      if (st.sessions.some((item) => item !== null && typeof item === 'object' && item.id === sessionId)) return name;
    }
    return undefined;
  };

  const writeBind = (sessionId, chain, kind) => {
    mkdirSync(markerDir, { recursive: true });
    const data = {
      session: sessionId,
      chain,
      kind,
      time: new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 19),
      cwd: cfg.source_cwd ?? '',
    };
    const text = JSON.stringify(data, null, 2);
    writeFileSync(path.join(markerDir, '当前.json'), text, 'utf8');
    writeFileSync(path.join(markerDir, `${sessionId}.json`), text, 'utf8');
  };

  ctx.inject(['connection'], (connectionCtx) => {
    const readService = () => {
      try {
        if (typeof connectionCtx.get === 'function') return connectionCtx.get('connection');
        return connectionCtx.connection;
      } catch (error) {
        return undefined;
      }
    };
    try {
      const connection = readService();
      const registrar = connection === undefined || connection === null ? undefined : connection.fetch;
      if (registrar === undefined || registrar === null || typeof registrar.register !== 'function') {
        log('connection.fetch 不可用，链绑定通道未启用');
        return;
      }
      connectionCtx.effect(() => registrar.register({
        path: '/api/memory-panel/bind',
        methods: ['POST'],
        requestBody: 'buffered',
        fetch: async (request) => {
          let body;
          try {
            body = await request.json();
          } catch (error) {
            return new Response('body is not JSON', { status: 400 });
          }
          const sessionId = typeof body?.session === 'string' ? body.session.trim() : '';
          const kind = body?.kind === 'blank' ? 'blank' : 'inherit';
          if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) {
            return Response.json({ ok: false, error: 'bad bind request' }, { status: 400 });
          }
          const existingChain = chainOfSession(sessionId);
          if (existingChain !== undefined) {
            log(`链绑定跳过：会话 ${sessionId.slice(0, 12)} 已登记在 ${existingChain}，不写标记`);
            return Response.json({ ok: true, chain: existingChain, kind: 'inherit', applied: false, registered: true });
          }
          if (kind === 'blank') {
            // 新链：先只记“这是新链”，名字等第一个会话归档时按会话标题自动生成
            try {
              writeBind(sessionId, null, 'blank');
            } catch (error) {
              log(`写链标记失败：${String(error)}`);
              return Response.json({ ok: false, error: String(error) }, { status: 500 });
            }
            log(`链绑定：新链（自动命名）→ 会话 ${sessionId.slice(0, 12)}`);
            return Response.json({ ok: true, kind: 'blank', chain: null, applied: true });
          }
          const chain = safeChain(body?.chain);
          if (chain === undefined || !chainNamesOnDisk().includes(chain)) {
            return Response.json({ ok: false, error: 'bad chain name' }, { status: 400 });
          }
          try {
            writeBind(sessionId, chain, kind);
          } catch (error) {
            log(`写链标记失败：${String(error)}`);
            return Response.json({ ok: false, error: String(error) }, { status: 500 });
          }
          log(`链绑定：${chain}（${kind}）→ 会话 ${sessionId.slice(0, 12)}`);
          return Response.json({ ok: true, chain, kind, applied: true });
        },
      }), 'memory-panel: bind route');
      connectionCtx.effect(() => registrar.register({
        path: '/api/memory-panel/op',
        methods: ['POST'],
        requestBody: 'buffered',
        fetch: async (request) => {
          let body;
          try {
            body = await request.json();
          } catch (error) {
            return new Response('body is not JSON', { status: 400 });
          }
          const op = typeof body?.op === 'string' ? body.op : '';
          const args = [];
          if (op === 'list-chains') {
            args.push('list-chains');
          } else if (op === 'delete-session') {
            const id = typeof body?.sessionId === 'string' ? body.sessionId.trim() : '';
            if (!/^[A-Za-z0-9_-]{1,128}$/.test(id)) {
              return Response.json({ ok: false, error: 'bad session id' }, { status: 400 });
            }
            args.push('delete-session', id);
          } else if (op === 'list-junk') {
            args.push('list-junk');
          } else if (op === 'import-junk') {
            const rawIds = Array.isArray(body?.ids) ? body.ids : [];
            const ids = rawIds.filter((id) => typeof id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(id));
            if (ids.length === 0) {
              return Response.json({ ok: false, error: 'no sessions selected' }, { status: 400 });
            }
            args.push('import-junk', '--ids', ids.join(','));
            const target = typeof body?.to === 'string' && body.to.trim() !== '' ? safeChain(body.to) : undefined;
            if (target !== undefined) args.push('--to', target);
          } else if (op === 'rename-chain') {
            const oldDir = safeChain(body?.chain);
            const newName = typeof body?.newName === 'string' ? body.newName.trim() : '';
            if (oldDir === undefined || newName === '' || newName.length > 40 || /[\\/:*?"<>|\u0000-\u001f]/.test(newName)) {
              return Response.json({ ok: false, error: 'bad chain name' }, { status: 400 });
            }
            args.push('rename-chain', oldDir, newName);
          } else if (op === 'delete-chain') {
            const name = safeChain(body?.chain);
            if (name === undefined) {
              return Response.json({ ok: false, error: 'bad chain name' }, { status: 400 });
            }
            args.push('delete-chain', name);
          } else {
            return Response.json({ ok: false, error: 'unknown op' }, { status: 400 });
          }
          const result = await runScript('管理.py', [...args, '--json'], 300_000);
          let data;
          try {
            const lines = String(result.output ?? '').split(/\r?\n/).filter((line) => line.trim() !== '');
            data = JSON.parse(lines[lines.length - 1] ?? '{}');
          } catch (error) {
            data = { ok: false, error: '管理.py 输出不是 JSON', output: String(result.output ?? '').slice(-400) };
          }
          log(`管理操作 ${op}：${JSON.stringify(data).slice(0, 300)}`);
          return Response.json(data);
        },
      }), 'memory-panel: op route');
      log(`管理操作通道已启用：POST /api/memory-panel/op（管理.py）`);
      log(`链绑定通道已启用：POST /api/memory-panel/bind（标记目录 ${markerDir}）`);
    } catch (error) {
      log(`链绑定通道注册失败：${String(error)}`);
    }
  });

  let timer;
  let running = false;
  let lastRun = 0;

  function schedule(delay) {
    if (timer !== undefined) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = undefined;
      void fire();
    }, delay);
    if (typeof timer.unref === 'function') timer.unref();
  }

  const runScript = (script, args = [], timeoutMs = 300_000) => new Promise((resolve) => {
    let output = '';
    const collect = (chunk) => {
      output += chunk.toString();
      if (output.length > 1_000_000) output = output.slice(-1_000_000);
    };
    let child;
    try {
      child = spawn(python, [path.join(scriptsDir, script), ...args], {
        cwd: scriptsDir, windowsHide: true,
        env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
      });
    } catch (error) {
      resolve({ code: -1, tail: String(error) });
      return;
    }
    child.stdout.on('data', collect);
    child.stderr.on('data', collect);
    const killer = setTimeout(() => {
      log(`脚本 ${script} 超过 ${Math.round(timeoutMs / 1000)} 秒，强制结束`);
      try { child.kill(); } catch (error) { /* ignore */ }
    }, timeoutMs);
    if (typeof killer.unref === 'function') killer.unref();
    child.on('error', (error) => {
      clearTimeout(killer);
      resolve({ code: -1, tail: String(error), output });
    });
    child.on('close', (code) => {
      clearTimeout(killer);
      resolve({ code, tail: lastLines(output, 2), output });
    });
  });

  const runOnce = async () => {
    const started = Date.now();
    log('开始归档（事件触发）');
    const plan = [
      ['回合存档.py', [], 600_000],
      ['记忆生成器.py', ['--all'], 600_000],
    ];
    for (const [script, args, timeoutMs] of plan) {
      const result = await runScript(script, args, timeoutMs);
      const seconds = ((Date.now() - started) / 1000).toFixed(1);
      const killed = result.code === null || result.code === undefined;
      log(`${script} 结束：exit ${killed ? 'null（超时被强杀）' : result.code}（${seconds}s）${result.tail === '' ? '' : ' | ' + result.tail}`);
    }
  };

  const fire = async () => {
    if (running) {
      schedule(5_000);
      return;
    }
    const wait = COOLDOWN_MS - (Date.now() - lastRun);
    if (wait > 0) {
      schedule(wait);
      return;
    }
    running = true;
    try {
      await runOnce();
    } catch (error) {
      log(`归档运行出错：${String(error)}`);
    } finally {
      running = false;
      lastRun = Date.now();
    }
  };

  ctx.on('session/event', (session, event) => {
    if (event === undefined || event === null || event.type !== 'turn/end') return;
    const cwd = String(session?.header?.cwd ?? '').toLowerCase();
    const normCwd = String(cwd).replace(/\\/g, '/').replace(/\/+$/, '');
      const normSrc = String(sourceCwd).replace(/\\/g, '/').replace(/\/+$/, '');
      if (sourceCwd !== '' && cwd !== '' && normCwd !== normSrc && !normCwd.startsWith(normSrc + '/')) return;
    log(`turn/end：会话 ${session?.id ?? '?'}（${QUIET_MS / 1000} 秒后归档）`);
    schedule(QUIET_MS);
  });

  ctx.effect(() => () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  }, 'memory-panel: turn/end hook');

  log(`事件钩子已启用：脚本目录 ${scriptsDir}；Python ${python}；工作区 ${sourceCwd || '（不限）'}`);
  schedule(STARTUP_MS);
}


