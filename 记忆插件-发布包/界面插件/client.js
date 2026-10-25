/**
 * 记忆链面板 v6.5 · 客户端半
 *
 * 这版做四件事：
 *   1. 新会话页面（hero 行）的「记忆链」胶囊：选已有链 / 开新链；
 *      选择只发给 Host（写 .链标记 文件），**聊天和输入框里都不出现任何信号文字**；
 *   2. 正文会话的输入框上方一行醒目状态：记忆链 + 锁定标记 + 本会话回合数 + 档位刻度；
 *   3. 提示周期（默认 60 轮/档）可在选择弹窗里调，存在本机；
 *   4. 选择弹窗里每条链右边有个 ×：二次确认后只从本机列表移除（不动记忆库档案）。
 *
 * 回合数来自 DSH 自带的 `sessionStats` 投影（本会话），不是读记忆库——
 * 文件数据浏览器读不到，全链累计数要等以后接 Host 端。
 *
 * 信号词和 AGENTS.md 第 10 条、回合存档.py 认链逻辑约好的格式，别随手改：
 *   「继承 <链名>」   「新链 <链名>」
 */
window.__ModuleLoader__.load({
  id: '@local/memory-panel',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const NS = 'memoryPanel';

    /* ---------- 0. 常量 ---------- */
    const MAIN_CHAIN = '记忆链1';
    const HIDDEN_CHAIN = '杂物箱';   // 隐藏的归档兜底链，选择器里不显示
    const CHAIN_PREFIX = '记忆链';
    const LS_KEY = 'memory-panel:chains:v5';
    const DEFAULT_PERIOD = 60;          // 提示周期：每多少轮算一档（也对应人格更新周期）
    const PERIOD_MIN = 10;
    const PERIOD_MAX = 600;
    const PERIOD_STEP = 10;
    const SCALE_CELLS = 6;              // 一档画 6 格

    /* ---------- 1. 状态 ---------- */
    const readStore = () => {
      try {
        const raw = window.localStorage.getItem(LS_KEY);
        const parsed = raw === null ? null : JSON.parse(raw);
        if (parsed === null || typeof parsed !== 'object') return { chains: [], bySession: {}, period: DEFAULT_PERIOD, turnsCollapsed: false };
        const period = Number(parsed.period);
        return {
          chains: Array.isArray(parsed.chains) ? parsed.chains.filter((x) => typeof x === 'string') : [],
          bySession: parsed.bySession !== null && typeof parsed.bySession === 'object' ? parsed.bySession : {},
          period: Number.isFinite(period) ? Math.min(PERIOD_MAX, Math.max(PERIOD_MIN, Math.round(period))) : DEFAULT_PERIOD,
          turnsCollapsed: parsed.turnsCollapsed === true,
        };
      } catch (error) {
        return { chains: [], bySession: {}, period: DEFAULT_PERIOD, turnsCollapsed: false };
      }
    };
    const writeStore = (data) => {
      try { window.localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch (error) { /* 存不了就只活这一次 */ }
    };
    const stored = readStore();

    let state = Object.freeze({
      open: false,           // 选择弹窗是否打开
      target: undefined,     // 正在为哪个会话选
      chains: stored.chains, // 本机建过的链（不含主链）
      bySession: stored.bySession,
      period: stored.period, // 提示周期（轮/档）
      turnsCollapsed: stored.turnsCollapsed, // 回合刻度是否收起（红色催命感，给个开关）
      notice: null,          // 弹窗内的提示（写入失败/被保留/已复制…）
      confirm: null,         // 正在二次确认的删除（{kind:'chain'|'session', ...}）
      manage: false,         // 是否在“管理记忆链”视图
      manageRows: [],        // 磁盘上的链列表（Host list-chains）
      manageBusy: false,     // 删除请求进行中
      diskChains: [],        // 磁盘上的真实链（Host list-chains，{name,dir}）
      renaming: null,        // 正在改名的链 {dir, name}
      renameError: null,     // 改名失败原因
      junkOpen: false,       // “继承既存会话及其他”面板
      junkRows: null,        // null=加载中，[]=空
      junkSelected: {},      // id -> true
      junkMode: 'each',      // each=各自成链；to=导入到指定链
      junkTarget: '',        // 目标链目录名
    });
    const listeners = new Set();
    const store = {
      getSnapshot: () => state,
      subscribe(listener) {
        listeners.add(listener);
        return () => { listeners.delete(listener); };
      },
    };
    const update = (patch) => {
      state = Object.freeze({ ...state, ...patch });
      for (const listener of [...listeners]) listener();
    };
    const persist = () => writeStore({ chains: state.chains, bySession: state.bySession, period: state.period, turnsCollapsed: state.turnsCollapsed });

    const nextChainName = (chains) => {
      let n = 1;
      for (const name of chains) {
        const m = /^记忆链(\d+)$/.exec(name);
        if (m) n = Math.max(n, Number(m[1]) + 1);
      }
      return CHAIN_PREFIX + n;
    };

    const signalOf = (choice) => (choice.kind === 'inherit' ? '继承 ' + choice.chain : '新链 ' + choice.chain);

    /* ---------- 2. 复制信号（兜底方案，只在草稿写不进去时用） ---------- */
    function copyText(text) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text);
          return true;
        }
      } catch (error) { /* 走降级 */ }
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        return ok;
      } catch (error) {
        return false;
      }
    }

    /* ---------- 3. 文案 ---------- */
    const zh = {
      'chip.prefix': '记忆链',
      'chip.unset': '未选',
      'chip.hint': '给这个新会话选记忆链；选完聊天里不会出现任何文字，AI 自己按标记读链',
      'chip.lockedHint': '本会话已锁定；要换请新建会话',
      'dock.label': '记忆链',
      'dock.locked': '已锁定',
      'dock.unset': '未选',
      'dock.default': '默认链',
      'dock.none': '（本会话未选链）',
      'turns.unit': '第 {n} 轮',
      'turns.cycle': '{a}/{b}',
      'turns.reached': '已到周期',
      'turns.label': '回合刻度',
      'turns.collapse': '收起',
      'turns.collapseHint': '收起回合刻度',
      'turns.expand': '展开',
      'turns.expandHint': '展开回合刻度',
      'turns.hint': '本会话回合数；每 {b} 轮为一档（到档提醒更新人格）',
      'dialog.title': '选择记忆链',
      'dialog.hint': '选一条链继承；不选、或点 × 关闭 = 开一条新链（不读旧记忆）。聊天里不会出现任何文字。',
      'group.inherit': '继承已有链',
      'group.new': '开新链',
      'opt.inheritMainDesc': '读主链记忆，接着上次',
      'opt.inheritDesc': '读这条链的记忆，接着上次',
      'opt.blank': '新链 · 空白',
      'opt.blankDesc': '从零开始，不读任何记忆（发出第一条消息后才真正建链）',
      'period.label': '提示周期',
      'period.unit': '轮/档',
      'period.hint': '每多少轮提醒一次（同时作为转生提示词的更新周期，默认 60）；新链不到 60 轮时还没有转生提示词',
      'dialog.cancel': '关闭',
      'notice.preserved': '输入框里已经有内容，我没敢覆盖。请把这句手动加进消息：',
      'notice.blocked': '输入框正在处理上一条消息，暂时写不进去。稍后再选，或手动发送：',
      'notice.unavailable': '这个版本的界面读不到草稿接口，只能手动发送：',
      'notice.failed': '写入草稿失败，请手动发送：',
      'notice.copied': '✓ 已复制，粘贴到输入框即可',
      'notice.copyFailed': '复制失败，请手打：',
      'notice.bound': '✓ 已绑定 ',
      'notice.fallbackDraft': 'Host 桥未就绪（改完 Host 要重启一次 DSH）；本次先写进草稿，按回车发送：',
      'chain.remove': '移除',
      'chain.removeHint': '从本机选择器移除（不动记忆库档案）',
      'notice.deleted': '✓ 已删除：',
      'menu.delete': '删除记忆档案',
      'dialog.manage': '管理记忆链',
      'manage.title': '管理记忆链',
      'manage.hint': '删除 = 移进 废弃杂项（可恢复）；删完链编号会按现存数量重排。',
      'manage.delete': '删除',
      'manage.rename': '改名',
      'manage.save': '保存',
      'manage.cancel': '取消',
      'junk.open': '继承既存会话及其他',
      'junk.title': '继承既存会话及其他',
      'junk.hint': '勾选旧会话，默认每条会话各自成为一条记忆链；也可以统一导入所选链。导入只搬档案，不额外调用模型。',
      'junk.empty': '杂物箱是空的，没有待处理的旧会话。',
      'junk.each': '各自成链',
      'junk.to': '导入到',
      'junk.chainName': '继承既存会话及其他（隐藏）',
      'junk.selectAll': '全选',
      'junk.import': '导入',
      'junk.cancel': '取消',
      'notice.imported': '✓ 已导入：',
      'notice.renamed': '✓ 已改名：',
      'manage.hidden': '隐藏',
      'confirm.title': '确认删除',
      'confirm.deleteChain': '要删除整条链「{name}」吗？链里的会话档案会一起移走（可恢复），删完剩下的链会按数量重排编号。',
      'confirm.deleteSession': '要删除会话「{name}」的记忆档案吗？回合、索引、思考档会移到 废弃杂项（可恢复），以后不再自动归档这条会话。原始对话文件不动。',
      'confirm.ok': '确认删除',
      'confirm.cancel': '取消',
      'manage.close': '关闭',
      'chain.removeConfirm': '从本机选择器移除「{name}」？只动列表，不删记忆库档案。',
      'btn.copy': '复制信号',
      'btn.close': '关闭',
    };
    const en = {
      'chip.prefix': 'Memory',
      'chip.unset': 'none',
      'chip.hint': 'Pick a memory chain; nothing appears in the chat - the AI reads the pick itself',
      'chip.lockedHint': 'Locked for this session; start a new session to change',
      'dock.label': 'Memory',
      'dock.locked': 'locked',
      'dock.unset': 'not chosen',
      'dock.default': 'default chain',
      'dock.none': '(no chain)',
      'turns.unit': 'turn {n}',
      'turns.cycle': '{a}/{b}',
      'turns.hint': 'Session turns; one scale step every {b} turns',
      'turns.reached': 'cycle reached',
      'turns.label': 'Turn scale',
      'turns.collapse': 'Hide',
      'turns.collapseHint': 'Hide the turn scale',
      'turns.expand': 'Show',
      'turns.expandHint': 'Show the turn scale',
      'dialog.title': 'Choose memory chain',
      'dialog.hint': 'Pick a chain to inherit. No pick (or ×) = a fresh new chain with no memory. Nothing appears in the chat.',
      'group.inherit': 'Continue an existing chain',
      'group.new': 'Start a new chain',
      'opt.inheritMainDesc': 'Read the main chain memory and continue',
      'opt.inheritDesc': 'Read this chain and continue',
      'opt.blank': 'New chain · blank',
      'opt.blankDesc': 'Start from zero, read nothing (the chain is created after your first message)',
      'period.label': 'Notice period',
      'period.unit': 'turns',
      'period.hint': 'Turns per scale step; also the persona-refresh period (default 60). A new chain has no persona file until it reaches 60 turns.',
      'dialog.cancel': 'Close',
      'notice.preserved': 'The composer already has text, so I did not overwrite it. Add this manually: ',
      'notice.blocked': 'The composer is busy. Try again later, or send this manually: ',
      'notice.unavailable': 'This UI version exposes no draft API. Send this manually: ',
      'notice.failed': 'Could not write the draft. Send this manually: ',
      'notice.copied': '✓ Copied — paste it into the composer',
      'notice.copyFailed': 'Copy failed, type it manually: ',
      'notice.bound': '✓ Bound ',
      'notice.fallbackDraft': 'Host bridge not ready (restart DSH after Host changes); falling back to a composer draft: ',
      'chain.remove': 'Remove',
      'chain.removeHint': 'Remove from this machine picker (memory archive untouched)',
      'notice.deleted': '✓ Deleted: ',
      'menu.delete': 'Delete memory archive',
      'dialog.manage': 'Manage chains',
      'manage.title': 'Manage memory chains',
      'manage.hint': 'Delete moves the chain into 废弃杂项 (recoverable); remaining chains are renumbered by count.',
      'manage.delete': 'Delete',
      'manage.rename': 'Rename',
      'manage.save': 'Save',
      'manage.cancel': 'Cancel',
      'junk.open': 'Inherit existing sessions',
      'junk.title': 'Inherit existing sessions',
      'junk.hint': 'Pick old sessions; each becomes its own memory chain by default, or import them all into one chain. Import only moves archives, no model calls.',
      'junk.empty': 'The junk box is empty.',
      'junk.each': 'Each its own chain',
      'junk.to': 'Import into',
      'junk.chainName': 'Existing sessions (hidden)',
      'junk.selectAll': 'Select all',
      'junk.import': 'Import',
      'junk.cancel': 'Cancel',
      'notice.imported': '✓ Imported: ',
      'notice.renamed': '✓ Renamed: ',
      'manage.hidden': 'hidden',
      'confirm.title': 'Confirm delete',
      'confirm.deleteChain': 'Delete the whole chain "{name}"? Its session archives move out too (recoverable), and remaining chains are renumbered.',
      'confirm.deleteSession': 'Delete the memory archive of "{name}"? Turns, index and thinking files move to 废弃杂项 (recoverable); this session will no longer be auto-archived. The raw DSH conversation is untouched.',
      'confirm.ok': 'Delete',
      'confirm.cancel': 'Cancel',
      'manage.close': 'Close',
      'chain.removeConfirm': 'Remove "{name}" from this machine picker? Only the list changes; the memory archive is untouched.',
      'btn.copy': 'Copy signal',
      'btn.close': 'Close',
    };

    /* ---------- 4. 样式（跟随主题） ---------- */
    const CSS = `
      .mp-chip { display:inline-flex; align-items:center; gap:6px; padding:3px 10px 3px 11px;
                 border-radius:999px; border:1px solid var(--dsw-alias-border-l2);
                 background:transparent; color:var(--dsw-alias-label-secondary);
                 font:inherit; font-size:12px; line-height:1.4; cursor:pointer; }
      .mp-chip:hover:not(:disabled) { background:var(--dsw-alias-interactive-bg-hover); color:var(--dsw-alias-label-primary); }
      .mp-chip:disabled { cursor:default; opacity:1; }
      .mp-chip-value { color:var(--dsw-alias-label-primary); }
      .mp-chip[data-locked="true"] { border-color:var(--dsw-alias-brand-primary); }
      .mp-chip[data-locked="true"] .mp-chip-value { color:var(--dsw-alias-brand-primary); font-weight:600; }
      .mp-chip-chev { opacity:.55; }

      .mp-dock { display:flex; align-items:center; gap:8px; padding:4px 2px; font:inherit; font-size:12px; }
      .mp-dock-label { color:var(--dsw-alias-label-caption); }
      .mp-dock-value { color:var(--dsw-alias-label-primary); font-weight:600; }
      .mp-dock-state { padding:1px 8px; border-radius:999px; border:1px solid var(--dsw-alias-border-l2);
                       color:var(--dsw-alias-brand-primary); font-size:11px; }
      .mp-dock-note { color:var(--dsw-alias-label-caption); font-size:11px; }

      .mp-turns { margin-left:auto; display:flex; align-items:center; gap:7px;
                  padding:2px 10px; border-radius:999px;
                  border:1px solid var(--dsw-alias-border-l2);
                  background:var(--dsw-alias-bg-layer-1); }
      .mp-turns-num { color:var(--dsw-alias-label-primary); font-weight:700; font-size:12px; }
      .mp-turns-num[data-milestone="true"] { color:var(--dsw-alias-brand-primary); }
      .mp-turns[data-reached="true"] { border-color:var(--dsw-alias-state-error-primary); }
      .mp-turns[data-reached="true"] .mp-turns-num,
      .mp-turns[data-reached="true"] .mp-turns-cycle { color:var(--dsw-alias-state-error-primary); }
      .mp-turns[data-reached="true"] .mp-scale i[data-on="true"] { background:var(--dsw-alias-state-error-primary); }
      .mp-turns-toggle { border:0; background:transparent; cursor:pointer; color:inherit; opacity:.55;
                         padding:0 1px; font:inherit; font-size:12px; line-height:1; }
      .mp-turns-toggle:hover { opacity:1; }
      .mp-turns-collapsed { margin-left:auto; border:1px solid var(--dsw-alias-border-l1);
                            background:transparent; cursor:pointer; color:var(--dsw-alias-label-caption);
                            border-radius:999px; padding:1px 8px; font:inherit; font-size:11px; }
      .mp-turns-collapsed:hover { background:var(--dsw-alias-interactive-bg-hover); color:var(--dsw-alias-label-secondary); }
      .mp-scale { display:inline-flex; gap:2px; }
      .mp-scale i { display:block; width:11px; height:5px; border-radius:2px;
                    background:var(--dsw-alias-border-l2); }
      .mp-scale i[data-on="true"] { background:var(--dsw-alias-brand-primary); }
      .mp-turns-cycle { color:var(--dsw-alias-label-caption); font-size:11px; font-variant-numeric:tabular-nums; }

      .mp-overlay { position:fixed; inset:0; z-index:1200; display:grid; place-items:center; }
      .mp-mask { position:absolute; inset:0; background:var(--dsw-alias-bg-mask-1); }
      .mp-panel { position:relative; position:relative; min-width:360px; max-width:480px; max-height:82vh; overflow:auto; padding:18px 20px;
                  border-radius:14px; border:1px solid var(--dsw-alias-border-l2);
                  background:var(--dsw-alias-bg-layer-2);
                  box-shadow:var(--dsw-elevation-panel, 0 12px 40px rgba(0,0,0,.24));
                  color:var(--dsw-alias-label-primary); }
      .mp-menu-item { display:block; width:100%; text-align:left; padding:7px 12px; border:0;
                      background:transparent; color:var(--dsw-alias-state-error-primary); font:inherit;
                      font-size:13px; border-radius:8px; cursor:pointer; }
      .mp-menu-item:hover { background:var(--dsw-alias-interactive-bg-hover); }
      .mp-danger { border:0; border-radius:9px; padding:6px 14px; cursor:pointer; font-size:13px;
                   background:var(--dsw-alias-state-error-primary); color:var(--dsw-alias-label-primary-inverted); }
      .mp-danger:hover { filter:brightness(1.05); }
      .mp-danger:disabled { opacity:.6; cursor:default; }
      .mp-manage-row { pointer-events:none; }
      .mp-manage-rename { width:auto; padding:0 10px; font-size:12px; border-radius:9px; margin-right:6px;
                          border:1px solid var(--dsw-alias-border-secondary); background:transparent;
                          color:var(--dsw-alias-label-secondary); cursor:pointer; }
      .mp-manage-rename:hover { background:var(--dsw-alias-interactive-bg-hover); }
      .mp-junk-row { display:flex; align-items:center; gap:8px; padding:6px 4px; cursor:pointer; }
      .mp-junk-row input { accent-color: var(--dsw-alias-state-business-primary); }
      .mp-junk-row .mp-opt-desc { margin-left:auto; }
      .mp-junk-mode { display:flex; align-items:center; gap:14px; margin-top:8px; flex-wrap:wrap; }
      .mp-radio { display:inline-flex; align-items:center; gap:6px; font-size:13px; color:var(--dsw-alias-label-secondary); cursor:pointer; }
      .mp-select { padding:4px 8px; font:inherit; font-size:13px; color:var(--dsw-alias-label-primary);
                   background:var(--dsw-alias-bg-primary); border:1px solid var(--dsw-alias-border-secondary);
                   border-radius:8px; outline:none; }
      .mp-small { padding:4px 10px; font-size:12px; }
      .mp-rename-input { flex:1; min-width:0; margin:0 8px; padding:4px 8px; font:inherit; font-size:13px;
                         color:var(--dsw-alias-label-primary); background:var(--dsw-alias-bg-primary);
                         border:1px solid var(--dsw-alias-border-secondary); border-radius:8px; outline:none; }
      .mp-rename-input:focus { border-color:var(--dsw-alias-state-business-primary); }
      .mp-manage-delete { width:auto; padding:0 10px; font-size:12px; border-radius:9px;
                          border:1px solid var(--dsw-alias-state-error-primary);
                          background:transparent; color:var(--dsw-alias-state-error-primary); cursor:pointer; }
      .mp-manage-delete:hover { background:var(--dsw-alias-state-error-primary); color:var(--dsw-alias-label-primary-inverted); }
      .mp-close { position:absolute; top:8px; right:10px; width:26px; height:26px; border:0; border-radius:8px;
                  background:transparent; color:var(--dsw-alias-label-secondary); font-size:18px; line-height:1; cursor:pointer; }
      .mp-close:hover { background:var(--dsw-alias-interactive-bg-hover); color:var(--dsw-alias-label-primary); }
      .mp-title { margin:0 0 4px; font-size:15px; font-weight:600; }
      .mp-hint { margin:0 0 14px; font-size:12px; line-height:1.55; color:var(--dsw-alias-label-secondary); }
      .mp-group { margin:0 0 6px; font-size:11px; font-weight:600; letter-spacing:.04em;
                  color:var(--dsw-alias-label-caption); }
      .mp-opt { display:flex; flex-direction:column; align-items:flex-start; gap:2px; width:100%;
                text-align:left; padding:9px 12px; margin-bottom:8px; cursor:pointer;
                border-radius:10px; border:1px solid var(--dsw-alias-border-l1);
                background:var(--dsw-alias-bg-layer-1); color:var(--dsw-alias-label-primary); font:inherit; }
      .mp-opt:hover { background:var(--dsw-alias-interactive-bg-hover); border-color:var(--dsw-alias-border-l2); }
      .mp-opt-title { font-size:13px; font-weight:600; }
      .mp-opt-desc { font-size:12px; color:var(--dsw-alias-label-secondary); }
      .mp-chain-row { display:flex; align-items:stretch; gap:8px; margin-bottom:8px; }
      .mp-chain-row .mp-opt { margin-bottom:0; flex:1; }
      .mp-chain-remove { width:34px; border-radius:10px; cursor:pointer; font-size:15px; line-height:1;
                         border:1px solid var(--dsw-alias-border-l1); background:transparent;
                         color:var(--dsw-alias-label-caption); }
      .mp-chain-remove:hover { border-color:var(--dsw-alias-state-error-primary);
                               color:var(--dsw-alias-state-error-primary); }
      .mp-period { display:flex; align-items:center; gap:8px; margin:14px 0 4px; padding-top:12px;
                   border-top:1px solid var(--dsw-alias-border-l1); font-size:12px;
                   color:var(--dsw-alias-label-secondary); }
      .mp-period-num { min-width:34px; text-align:center; font-weight:700; font-size:14px;
                       color:var(--dsw-alias-label-primary); font-variant-numeric:tabular-nums; }
      .mp-step { width:22px; height:22px; border-radius:6px; cursor:pointer; font-size:13px; line-height:1;
                 border:1px solid var(--dsw-alias-border-l2); background:var(--dsw-alias-bg-layer-1);
                 color:var(--dsw-alias-label-primary); }
      .mp-step:hover { background:var(--dsw-alias-interactive-bg-hover); }
      .mp-period-hint { margin:2px 0 0; font-size:11px; line-height:1.5; color:var(--dsw-alias-label-caption); }
      .mp-notice { margin:10px 0 0; padding:8px 10px; border-radius:8px;
                   background:var(--dsw-alias-bg-layer-1); border:1px solid var(--dsw-alias-border-l1);
                   font-size:12px; line-height:1.55; color:var(--dsw-alias-label-secondary); }
      .mp-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:12px; }
      .mp-primary { border:0; border-radius:9px; padding:6px 14px; cursor:pointer; font-size:13px;
                    background:var(--dsw-alias-button-primary-fill);
                    color:var(--dsw-alias-label-primary-inverted); }
      .mp-primary:hover { background:var(--dsw-alias-button-primary-hover); }
      .mp-ghost { border:1px solid var(--dsw-alias-border-l2); background:transparent;
                  border-radius:9px; padding:6px 12px; cursor:pointer; font-size:13px;
                  color:var(--dsw-alias-label-secondary); }
    `;

    /* ---------- 5. 小工具 ---------- */
    /* 会话 id 从两条路取：注册时 inject 的入参，或会话作用域自动给的 useSession。 */
    function useSessionIdOf(props) {
      const session = typeof props.useSession === 'function' ? props.useSession((s) => s) : undefined;
      const fromScope = session === undefined || session === null ? undefined : session.sessionId;
      return props.sessionId === undefined ? fromScope : props.sessionId;
    }

    const choiceName = (choice) => (choice === undefined ? '' : String(choice.label || choice.chain || ''));
    const kindLabel = (choice) => (choice === undefined ? '' : choice.kind === 'inherit' ? '继承' : '新链');

    /* 本会话回合数：DSH 自带的 sessionStats 投影（不是在读记忆库）；拿不到就退回数节点。 */
    function useTurnsOf(props) {
      const projection = typeof props.useProjection === 'function' ? props.useProjection('sessionStats') : undefined;
      const nodes = typeof props.useChat === 'function'
        ? props.useChat((s) => (s !== undefined && s !== null && s.legacy !== undefined ? s.legacy.nodes : undefined))
        : undefined;
      if (projection !== undefined && projection !== null && typeof projection.turns === 'number') return projection.turns;
      if (Array.isArray(nodes)) {
        const turns = new Set();
        for (const node of nodes) {
          if (node !== null && node !== undefined && node.kind === 'assistant' && typeof node.turn === 'number') turns.add(node.turn);
        }
        return turns.size > 0 ? turns.size : undefined;
      }
      return undefined;
    }

    /* 一档里走到第几格；到了周期就**一直满格**（变红 = 该更新人格了），不再随周期回零。 */
    const scaleOf = (turns, period) => {
      const reached = turns >= period;
      const inCycle = reached ? period : turns % period;
      const filled = reached ? SCALE_CELLS : Math.min(SCALE_CELLS, Math.ceil((inCycle / period) * SCALE_CELLS));
      return { inCycle, filled, reached };
    };

    /* ---------- 6. 选择胶囊（新会话页面 · 工作区/模式那一行） ---------- */
    function MemoryChainChip(props) {
      const { usePanel, t, open, autoChain } = props;
      const snap = usePanel((s) => s);
      const sessionId = useSessionIdOf(props);
      const choice = sessionId === undefined ? undefined : snap.bySession[sessionId];

      React.useEffect(() => {
        if (sessionId === undefined || typeof autoChain !== 'function') return;
        if (choice !== undefined) return;
        autoChain(sessionId);
      }, [autoChain, choice, sessionId]);

      if (sessionId === undefined) return null;
      const locked = choice !== undefined;
      return h('button', {
        type: 'button',
        className: 'mp-chip',
        'data-locked': locked ? 'true' : undefined,
        disabled: locked,
        title: locked ? t('chip.lockedHint') : t('chip.hint'),
        onClick: () => { if (!locked) open(); },
      },
        h('span', null, t('chip.prefix') + '：'),
        h('span', { className: 'mp-chip-value' }, locked ? choiceName(choice) : t('chip.unset')),
        locked ? null : h('span', { className: 'mp-chip-chev' }, '›'),
      );
    }

    /* ---------- 7. 正文会话状态行（输入框上方）：链 + 锁定 + 回合刻度 ---------- */
    function MemoryDock(props) {
      const { usePanel, t, commit, toggleTurns, autoChain } = props;
      const snap = usePanel((s) => s);
      const sessionId = useSessionIdOf(props);
      const turns = useTurnsOf(props);
      const choice = sessionId === undefined ? undefined : snap.bySession[sessionId];
      const period = snap.period;
      const scale = turns === undefined ? undefined : scaleOf(turns, period);
      const reached = scale !== undefined && scale.reached;

      React.useEffect(() => {
        if (sessionId === undefined || typeof autoChain !== 'function') return;
        if (choice !== undefined) return;
        autoChain(sessionId);
      }, [autoChain, choice, sessionId]);

      React.useEffect(() => {
        if (typeof commit !== 'function') return;
        if (choice === undefined || choice.kind === 'inherit') return;
        if (turns === undefined || turns <= 0) return;
        commit(choice.chain);
      }, [commit, choice, turns]);

      return h('div', { className: 'mp-dock' },
        h('span', { className: 'mp-dock-label' }, t('dock.label') + '：'),
        h('span', { className: 'mp-dock-value' }, choice === undefined ? t('dock.default') : choiceName(choice)),
        h('span', { className: 'mp-dock-state' }, choice === undefined ? t('dock.unset') : kindLabel(choice)),
        h('span', { className: 'mp-dock-note' }, choice === undefined ? '' : t('dock.locked')),
        scale === undefined ? null : (snap.turnsCollapsed
          ? h('button', {
            type: 'button',
            className: 'mp-turns-collapsed',
            title: t('turns.expandHint'),
            onClick: () => { if (typeof toggleTurns === 'function') toggleTurns(); },
          }, '▸ ' + t('turns.label'))
          : h('span', {
            className: 'mp-turns',
            'data-reached': reached ? 'true' : undefined,
            title: t('turns.hint').replace('{b}', String(period)),
          },
            h('span', {
              className: 'mp-turns-num',
              'data-milestone': reached ? 'true' : undefined,
            }, t('turns.unit').replace('{n}', String(turns))),
            h('span', { className: 'mp-scale', 'aria-hidden': 'true' },
              Array.from({ length: SCALE_CELLS }, (_, i) => h('i', { key: i, 'data-on': i < scale.filled ? 'true' : undefined })),
            ),
            h('span', { className: 'mp-turns-cycle' },
              reached ? t('turns.reached') : t('turns.cycle').replace('{a}', String(scale.inCycle)).replace('{b}', String(period))),
            h('button', {
              type: 'button',
              className: 'mp-turns-toggle',
              title: t('turns.collapseHint'),
              onClick: () => { if (typeof toggleTurns === 'function') toggleTurns(); },
            }, '▾'),
          )),
      );
    }

    /* ---------- 8. 选择弹窗 ---------- */
    function MemoryDialog({
      usePanel, t, choose, close, copyCommand, setPeriod, toggleTurns, removeChain,
      openManage, manageDelete, askDeleteSession, cancelConfirm, runConfirm,
      renaming, renameError, startRename, cancelRename, submitRename,
      openJunk, junkToggle, junkToggleAll, junkSetMode, junkSetTarget, junkImport, junkClose,
    }) {
      const snap = usePanel((s) => s);
      const panelRef = React.useRef(null);
      const renameInputRef = React.useRef(null);

      const active = snap.open || snap.manage === true || snap.confirm !== null || snap.junkOpen === true;
      React.useEffect(() => {
        if (!active) return undefined;
        const onKeyDown = (event) => {
          if (event.key === 'Escape') { event.stopPropagation(); close(); }
        };
        document.addEventListener('keydown', onKeyDown, true);
        if (panelRef.current) panelRef.current.focus();
        return () => document.removeEventListener('keydown', onKeyDown, true);
      }, [active]);

      if (!snap.open && snap.confirm === null && snap.manage !== true && snap.junkOpen !== true) return null;

      const notice = snap.notice;
      let noticeText = '';
      if (notice !== null) {
        if (notice.kind === 'bound') {
          noticeText = t('notice.bound') + (notice.cmd ? '「' + notice.cmd + '」' : '');
        } else if (notice.kind === 'fallbackDraft') {
          noticeText = t('notice.fallbackDraft') + (notice.cmd ? '「' + notice.cmd + '」' : '');
        } else {
          const plain = {
            preserved: 'notice.preserved',
            blocked: 'notice.blocked',
            unavailable: 'notice.unavailable',
            failed: 'notice.failed',
            copied: 'notice.copied',
            copyFailed: 'notice.copyFailed',
            deleted: 'notice.deleted',
            renamed: 'notice.renamed',
            imported: 'notice.imported',
          }[notice.kind];
          noticeText = plain === undefined ? '' : t(plain) + (notice.cmd ? '「' + notice.cmd + '」' : '');
        }
      }

      const row = (key, title, desc, run) => h('button',
        { key, type: 'button', className: 'mp-opt', onClick: run },
        h('span', { className: 'mp-opt-title' }, title),
        desc ? h('span', { className: 'mp-opt-desc' }, desc) : null,
      );

      const sourceRows = (snap.diskChains && snap.diskChains.length > 0)
        ? snap.diskChains.map((item) => (typeof item === 'string' ? { name: item, dir: item } : item))
        : snap.chains.map((name) => ({ name, dir: name }));
      const inheritRows = sourceRows
        .filter((item) => item.name !== HIDDEN_CHAIN && item.dir !== HIDDEN_CHAIN)
        .map((item) => row('row:' + item.dir, item.name, '', () => choose({ kind: 'inherit', chain: item.dir, label: item.name })));

      const overlay = (children) => h('div', { className: 'mp-overlay', role: 'presentation' },
        h('div', { className: 'mp-mask', 'aria-hidden': 'true', onClick: close }),
        h('div', {
          className: 'mp-panel', role: 'dialog', 'aria-modal': 'true',
          'aria-labelledby': 'mp-title', tabIndex: -1, ref: panelRef,
        }, children));

      const closeBtn = (label) => h('div', { className: 'mp-actions' },
        h('button', { type: 'button', className: 'mp-primary', onClick: close }, label));

      if (snap.junkOpen === true) {
        const rows = snap.junkRows;
        const selected = snap.junkSelected || {};
        const count = Object.keys(selected).filter((k) => selected[k] === true).length;
        const chainOptions = (snap.manageRows || []).filter((row) => row.hidden !== true);
        const junkRows = (rows === null) ? h('p', { className: 'mp-hint' }, '…')
          : rows.length === 0 ? h('p', { className: 'mp-hint' }, t('junk.empty'))
          : rows.map((row) => h('label', { key: 'j:' + row.id, className: 'mp-junk-row' },
              h('input', { type: 'checkbox', checked: selected[row.id] === true, onChange: () => junkToggle(row.id) }),
              h('span', { className: 'mp-opt-title' }, row.title || row.id.slice(0, 10)),
              h('span', { className: 'mp-opt-desc' }, String(row.turns) + ' 轮' + (row.sub ? ' / 子代理 ' + String(row.sub) : '') + ' / ' + row.dir),
            ));
        return overlay([
          h('button', { type: 'button', className: 'mp-close', title: t('junk.cancel'), 'aria-label': t('junk.cancel'), onClick: junkClose }, '×'),
          h('h2', { className: 'mp-title', id: 'mp-title' }, t('junk.title')),
          h('p', { className: 'mp-hint' }, t('junk.hint')),
          notice === null ? null : h('div', { className: 'mp-notice' }, noticeText),
          rows !== null && rows.length > 0 ? h('button', { type: 'button', className: 'mp-ghost mp-small', onClick: junkToggleAll }, t('junk.selectAll')) : null,
          junkRows,
          rows !== null && rows.length > 0 ? h('div', { className: 'mp-junk-mode' },
            h('label', { className: 'mp-radio' },
              h('input', { type: 'radio', name: 'junkmode', checked: snap.junkMode === 'each', onChange: () => junkSetMode('each') }),
              t('junk.each'),
            ),
            h('label', { className: 'mp-radio' },
              h('input', { type: 'radio', name: 'junkmode', checked: snap.junkMode === 'to', onChange: () => junkSetMode('to') }),
              t('junk.to'),
            ),
            snap.junkMode === 'to' ? h('select', {
              className: 'mp-select',
              value: snap.junkTarget || '',
              onChange: (event) => junkSetTarget(event.target.value),
            }, [
              h('option', { key: '', value: '' }, '…'),
              ...chainOptions.map((row) => h('option', { key: row.dir, value: row.dir }, row.name)),
            ]) : null,
          ) : null,
          h('div', { className: 'mp-actions' },
            h('button', { type: 'button', className: 'mp-ghost', onClick: junkClose }, t('junk.cancel')),
            h('button', {
              type: 'button', className: 'mp-primary',
              disabled: count === 0 || (snap.junkMode === 'to' && !snap.junkTarget),
              onClick: junkImport,
            }, t('junk.import') + (count ? '（' + String(count) + '）' : '')),
          ),
        ]);
      }



      if (snap.confirm !== null) {
        const c = snap.confirm;
        const name = c.kind === 'chain' ? (c.label || c.chain) : (c.title || String(c.sessionId).slice(0, 10));
        const body = c.kind === 'chain'
          ? t('confirm.deleteChain').replace('{name}', name)
          : t('confirm.deleteSession').replace('{name}', name);
        return overlay([
          h('button', { type: 'button', className: 'mp-close', title: t('confirm.cancel'), 'aria-label': t('confirm.cancel'), onClick: cancelConfirm }, '×'),
          h('h2', { className: 'mp-title', id: 'mp-title' }, t('confirm.title')),
          h('p', { className: 'mp-hint' }, body),
          snap.notice === null ? null : h('div', { className: 'mp-notice' }, noticeText),
          h('div', { className: 'mp-actions' },
            h('button', { type: 'button', className: 'mp-ghost', onClick: cancelConfirm }, t('confirm.cancel')),
            h('button', {
              type: 'button',
              className: 'mp-danger',
              disabled: snap.manageBusy === true,
              onClick: runConfirm,
            }, snap.manageBusy === true ? '…' : t('confirm.ok')),
          ),
        ]);
      }

      const doRename = () => {
        if (renameInputRef.current !== null && typeof submitRename === 'function') {
          submitRename(renameInputRef.current.value);
        }
      };

      if (snap.manage === true) {
        const rows = snap.manageRows.map((row) => {
          const dir = row.dir || row.name;
          const isRenaming = snap.renaming !== null && snap.renaming !== undefined && snap.renaming.dir === dir;
          if (isRenaming) {
            return h('div', { key: 'm:' + dir, className: 'mp-chain-row mp-renaming' },
              h('div', { className: 'mp-opt mp-manage-row' },
                h('span', { className: 'mp-opt-title' }, row.name),
                h('input', {
                  className: 'mp-rename-input',
                  defaultValue: snap.renaming.name,
                  maxLength: 40,
                  ref: renameInputRef,
                  onKeyDown: (event) => {
                    if (event.key === 'Enter') { event.preventDefault(); doRename(); }
                    if (event.key === 'Escape') { event.preventDefault(); cancelRename(); }
                  },
                }),
              ),
              h('button', { type: 'button', className: 'mp-primary mp-small', onClick: doRename }, t('manage.save')),
              h('button', { type: 'button', className: 'mp-ghost mp-small', onClick: cancelRename }, t('manage.cancel')),
            );
          }
          return h('div', { key: 'm:' + dir, className: 'mp-chain-row' },
            h('div', { className: 'mp-opt mp-manage-row' },
              h('span', { className: 'mp-opt-title' }, row.hidden ? t('junk.chainName') : row.name),
              h('span', { className: 'mp-opt-desc' },
                String(row.main) + ' 主 / ' + String(row.sub) + ' 子代理 / ' + String(row.turns) + ' 轮'
                + (row.persona ? '' : ' / 无人格') + (row.last_run ? ' / ' + row.last_run : '')),
            ),
            row.hidden === true ? null : h('button', {
              type: 'button', className: 'mp-chain-remove mp-manage-rename',
              title: t('manage.rename') + ' ' + row.name,
              onClick: () => startRename(row),
            }, t('manage.rename')),
            row.hidden === true ? null : h('button', {
              type: 'button', className: 'mp-chain-remove mp-manage-delete',
              title: t('manage.delete') + ' ' + row.name,
              onClick: () => manageDelete(dir, row.name || dir),
            }, t('manage.delete')),
          );
        });
        return overlay([
          h('button', { type: 'button', className: 'mp-close', title: t('manage.close'), 'aria-label': t('manage.close'), onClick: close }, '×'),
          h('h2', { className: 'mp-title', id: 'mp-title' }, t('manage.title')),
          h('p', { className: 'mp-hint' }, t('manage.hint')),
          snap.renameError ? h('div', { className: 'mp-notice' }, String(snap.renameError)) : null,
          rows.length === 0 ? h('p', { className: 'mp-hint' }, '（Host 桥未就绪，读不到链列表）') : rows,
          h('div', { className: 'mp-actions' },
            h('button', { type: 'button', className: 'mp-ghost', onClick: openJunk }, t('junk.open')),
            h('button', { type: 'button', className: 'mp-primary', onClick: close }, t('manage.close')),
          ),
        ]);
      }

      return h('div', { className: 'mp-overlay', role: 'presentation' },
        h('div', { className: 'mp-mask', 'aria-hidden': 'true', onClick: close }),
        h('div', {
          className: 'mp-panel', role: 'dialog', 'aria-modal': 'true',
          'aria-labelledby': 'mp-title', tabIndex: -1, ref: panelRef,
        },
          h('button', {
            type: 'button',
            className: 'mp-close',
            title: t('btn.close'),
            'aria-label': t('btn.close'),
            onClick: close,
          }, '×'),
          h('h2', { className: 'mp-title', id: 'mp-title' }, t('dialog.title')),
          h('p', { className: 'mp-hint' }, t('dialog.hint')),
          h('div', { className: 'mp-group' }, t('group.inherit')),
          inheritRows,
          h('div', { className: 'mp-period' },
            h('span', null, t('period.label') + '：'),
            h('button', { type: 'button', className: 'mp-step', onClick: () => setPeriod(-PERIOD_STEP), 'aria-label': 'decrease' }, '−'),
            h('span', { className: 'mp-period-num' }, String(snap.period)),
            h('button', { type: 'button', className: 'mp-step', onClick: () => setPeriod(PERIOD_STEP), 'aria-label': 'increase' }, '+'),
            h('span', null, t('period.unit')),
          ),
          h('p', { className: 'mp-period-hint' }, t('period.hint')),
          h('div', { className: 'mp-period' },
            h('span', null, t('turns.label') + '：'),
            h('button', {
              type: 'button',
              className: 'mp-step',
              style: { width: 'auto', padding: '0 10px', fontSize: '12px' },
              onClick: () => { if (typeof toggleTurns === 'function') toggleTurns(); },
            }, snap.turnsCollapsed ? t('turns.expand') : t('turns.collapse')),
          ),
          notice === null ? null : h('div', { className: 'mp-notice' }, noticeText),
          h('div', { className: 'mp-actions' },
            notice !== null && notice.cmd
              ? h('button', { type: 'button', className: 'mp-ghost', onClick: () => copyCommand(notice.cmd) }, t('btn.copy'))
              : null,
            h('button', { type: 'button', className: 'mp-ghost', onClick: openManage }, t('dialog.manage')),
            h('button', { type: 'button', className: 'mp-primary', onClick: close }, t('btn.close')),
          ),
        ),
      );
    }

    /* 会话行 "..." 菜单：删除记忆档案 */
    function MemorySessionMenuItem(props) {
      const { t, sessionId, displayTitle, useMenuOpenState, askDeleteSession } = props;
      const [, setMenuOpen] = typeof useMenuOpenState === 'function' ? useMenuOpenState() : [false, () => {}];
      return h('button', {
        type: 'button',
        role: 'menuitem',
        className: 'mp-menu-item',
        onClick: () => {
          setMenuOpen(false);
          if (typeof askDeleteSession === 'function') askDeleteSession(sessionId, displayTitle);
        },
      }, t('menu.delete'));
    }

    /* ---------- 9. 插件体 ---------- */
    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        /* 选链：聊天里不写任何文字，把「会话id + 链名」发给 Host。
           Host 写进 `记忆库\.链标记\当前.json`，AI 在新会话开头自己读。
           不选（或点 ×）：由 dock 自动发一次 kind=blank，Host 按磁盘链数分配新链名。 */
        const postBind = async (payload) => {
          try {
            const url = new URL('api/memory-panel/bind', window.location.href);
            const response = await fetch(url, {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify(payload),
            });
            if (!response.ok) return null;
            const data = await response.json();
            return data !== null && typeof data === 'object' ? data : null;
          } catch (error) {
            return null;
          }
        };

        const inflight = new Set();
        const ensureBound = (sessionId) => {
          if (sessionId === undefined || sessionId === null) return;
          if (state.bySession[sessionId] !== undefined) return;
          if (inflight.has(sessionId)) return;
          inflight.add(sessionId);
          postBind({ session: sessionId, kind: 'blank' }).then((data) => {
            inflight.delete(sessionId);
            if (data === null || data.ok !== true) return;
            if (data.applied === true) return;   // 新链：只记标记，胶囊保持可点（之后归档再回填显示）
            if (data.registered === true && typeof data.chain === 'string') {
              const dir = data.chain;
              const shown = (state.diskChains.find((item) => item.dir === dir) || {}).name || dir;
              const bySession = { ...state.bySession, [sessionId]: { kind: 'inherit', chain: dir, label: shown } };
              update({ bySession });
              persist();
            }
          }).catch(() => { inflight.delete(sessionId); });
        };

        /* 兜底：Host 桥还没就绪（改完 Host 还没重启 DSH）时，退回旧办法把信号写进草稿。 */
        const fallbackDraft = (sessionId, choice) => {
          const cmd = signalOf(choice);
          let conversation;
          try { conversation = ctx.get('conversation'); } catch (error) { conversation = undefined; }
          const input = conversation === undefined || conversation === null ? undefined : conversation.input;
          if (input === undefined || input === null || typeof input.requestDraftInitialization !== 'function') {
            update({ notice: { kind: 'unavailable', cmd } });
            return;
          }
          let sessions;
          try { sessions = ctx.get('sessions'); } catch (error) { sessions = undefined; }
          const binding = sessions === undefined || sessions === null ? undefined : sessions.binding(sessionId);
          if (binding === undefined) { update({ notice: { kind: 'unavailable', cmd } }); return; }
          let result;
          try {
            result = input.requestDraftInitialization(binding, { prompt: cmd });
          } catch (error) {
            update({ notice: { kind: 'failed', cmd } });
            return;
          }
          if (result === 'applied') {
            const bySession = { ...state.bySession, [sessionId]: choice };
            update({ bySession, open: true, notice: { kind: 'fallbackDraft', cmd } });
            persist();
            return;
          }
          if (result === 'preserved') { update({ notice: { kind: 'preserved', cmd } }); return; }
          if (result === 'blocked') { update({ notice: { kind: 'blocked', cmd } }); return; }
          update({ notice: { kind: 'failed', cmd } });
        };

        const applyChoice = (sessionId, choice) => {
          const cmd = signalOf(choice);
          postBind({ session: sessionId, chain: choice.chain, kind: 'inherit' }).then((data) => {
            const ok = data !== null && data.ok === true;
            if (!ok) { fallbackDraft(sessionId, choice); return; }
            const dir = data.chain || choice.chain;
            const shown = (state.diskChains.find((item) => item.dir === dir) || {}).name || choice.label || dir;
            const bySession = { ...state.bySession, [sessionId]: { kind: 'inherit', chain: dir, label: shown } };
            update({ bySession, open: true, notice: { kind: 'bound', cmd } });
            persist();
            setTimeout(() => { update({ open: false, target: undefined, notice: null }); }, 1500);
          });
        };

        const refreshChains = () => {
          postOp({ op: 'list-chains' }).then((data) => {
            if (data === null || data.ok !== true || !Array.isArray(data.chains)) return;
            update({ diskChains: data.chains.map((row) => ({ name: row.name || row.dir, dir: row.dir || row.name })) });
          });
        };
        const openPicker = (sessionId) => {
          update({ open: true, target: sessionId, notice: null });
          refreshChains();
        };
        const closePicker = () => update({
          open: false, target: undefined, notice: null,
          manage: false, manageRows: [], confirm: null,
          renaming: null, renameError: null, manageBusy: false,
        });
        const choose = (raw) => {
          const target = state.target;
          if (target === undefined) { update({ notice: { kind: 'unavailable', cmd: signalOf(raw) } }); return; }
          applyChoice(target, { kind: 'inherit', chain: raw.chain, label: raw.label || raw.chain });
        };
        const copyCommand = (cmd) => {
          const ok = copyText(cmd);
          update({ notice: { kind: ok ? 'copied' : 'copyFailed', cmd } });
        };

        /* 从本机选择器列表移除一条链（空壳/不想再看到的链）。
           只动浏览器里的列表，不碰记忆库档案；真删链等 Host 桥做好后再做。 */
        const removeChain = (name) => {
          if (typeof name !== 'string' || name === '' || name === MAIN_CHAIN) return;
          const chains = state.chains.filter((x) => x !== name);
          const bySession = {};
          for (const sid of Object.keys(state.bySession)) {
            const picked = state.bySession[sid];
            if (picked !== null && typeof picked === 'object' && picked.chain === name) continue;
            bySession[sid] = picked;
          }
          update({ chains, bySession, notice: null });
          persist();
        };

        /* ---------- 管理操作（删除会话档案 / 删除链） ---------- */
        const postOp = async (payload) => {
          try {
            const url = new URL('api/memory-panel/op', window.location.href);
            const response = await fetch(url, {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify(payload),
            });
            if (!response.ok) return null;
            const data = await response.json();
            return data !== null && typeof data === 'object' ? data : null;
          } catch (error) {
            return null;
          }
        };

        const refreshManage = (openManager) => {
          postOp({ op: 'list-chains' }).then((data) => {
            if (data === null || data.ok !== true) {
              update({ manage: openManager === true, manageRows: [] });
              return;
            }
            update({ manage: openManager === true, manageRows: Array.isArray(data.chains) ? data.chains : [] });
          });
        };

        const openManage = () => {
          update({ open: false, target: undefined, confirm: null, notice: null, manage: true, manageRows: [] });
          refreshManage(true);
        };
        const manageDelete = (dir, shown) => update({ confirm: { kind: 'chain', chain: dir, label: shown || dir }, notice: null });
        const junkReset = (patch) => update({
          junkOpen: false, junkRows: null, junkSelected: {}, junkMode: 'each', junkTarget: '', ...patch,
        });
        const openJunk = () => {
          update({ junkOpen: true, junkRows: null, junkSelected: {}, junkMode: 'each', junkTarget: '', notice: null });
          postOp({ op: 'list-junk' }).then((data) => {
            if (data === null || data.ok !== true || !Array.isArray(data.sessions)) {
              update({ junkRows: [], notice: { kind: 'failed', cmd: '读不到杂物箱' } });
              return;
            }
            update({ junkRows: data.sessions });
          });
        };
        const junkClose = () => junkReset();
        const junkToggle = (id) => {
          const next = { ...state.junkSelected };
          if (next[id] === true) delete next[id]; else next[id] = true;
          update({ junkSelected: next });
        };
        const junkToggleAll = () => {
          const rows = state.junkRows || [];
          const all = rows.every((row) => state.junkSelected[row.id] === true);
          const next = {};
          if (!all) for (const row of rows) next[row.id] = true;
          update({ junkSelected: next });
        };
        const junkSetMode = (mode) => update({ junkMode: mode });
        const junkSetTarget = (dir) => update({ junkTarget: dir });
        const junkImport = () => {
          if (state.manageBusy === true) return;
          const ids = Object.keys(state.junkSelected).filter((id) => state.junkSelected[id] === true);
          if (ids.length === 0) return;
          if (state.junkMode === 'to' && state.junkTarget === '') return;
          update({ manageBusy: true });
          const payload = { op: 'import-junk', ids };
          if (state.junkMode === 'to') payload.to = state.junkTarget;
          postOp(payload).then((data) => {
            if (data === null || data.ok !== true) {
              update({ manageBusy: false, notice: { kind: 'failed', cmd: String((data && data.error) || '导入失败') } });
              return;
            }
            const n = Array.isArray(data.imported) ? data.imported.length : ids.length;
            junkReset({ manageBusy: false, notice: { kind: 'imported', cmd: String(n) + ' 条' } });
            refreshChains();
            refreshManage(true);
          });
        };
        const startRename = (row) => update({
          renaming: { dir: row.dir || row.name, name: row.name || row.dir },
          renameError: null,
          manageBusy: false,
          notice: null,
        });
        const cancelRename = () => update({ renaming: null, renameError: null });
        const submitRename = (value) => {
          const target = state.renaming;
          if (target === null || typeof target === 'undefined' || state.manageBusy === true) return;
          const name = String(value || '').trim();
          if (name === '' || name === target.name) { cancelRename(); return; }
          update({ manageBusy: true, renameError: null });
          postOp({ op: 'rename-chain', chain: target.dir, newName: name }).then((data) => {
            if (data === null || data.ok !== true) {
              update({ manageBusy: false, renameError: String((data && data.error) || '改名失败') });
              return;
            }
            let newDir = name;
            const first = Array.isArray(data.renamed) && typeof data.renamed[0] === 'string' ? data.renamed[0] : '';
            const arrow = first.indexOf('→');
            if (arrow >= 0) newDir = first.slice(arrow + 1);
            const bySession = {};
            for (const sid of Object.keys(state.bySession)) {
              const picked = state.bySession[sid];
              if (picked !== null && typeof picked === 'object' && picked.chain === target.dir) {
                bySession[sid] = { ...picked, chain: newDir, label: name };
              } else {
                bySession[sid] = picked;
              }
            }
            const chains = state.chains.map((item) => (item === target.dir ? name : item));
            update({
              renaming: null, renameError: null, manageBusy: false,
              bySession, chains, notice: { kind: 'renamed', cmd: name },
            });
            persist();
            refreshChains();
            postOp({ op: 'list-chains' }).then((again) => {
              if (again !== null && again.ok === true && Array.isArray(again.chains)) {
                update({ manageRows: again.chains, notice: { kind: 'renamed', cmd: name } });
              }
            });
          });
        };
        const askDeleteSession = (sessionId, title) => {
          if (typeof sessionId !== 'string' || sessionId === '') return;
          update({ open: false, target: undefined, notice: null, confirm: { kind: 'session', sessionId, title: title || '' } });
        };
        const cancelConfirm = () => update({ confirm: null, manageBusy: false, notice: null });
        const runConfirm = () => {
          const current = state.confirm;
          if (current === null || state.manageBusy === true) return;
          update({ manageBusy: true });
          const payload = current.kind === 'chain'
            ? { op: 'delete-chain', chain: current.chain }
            : { op: 'delete-session', sessionId: current.sessionId };
          postOp(payload).then((data) => {
            if (data === null || data.ok !== true) {
              update({ manageBusy: false, notice: { kind: 'failed', cmd: String((data && data.error) || '操作失败') } });
              return;
            }
            let chains = state.chains;
            let bySession = {};
            if (current.kind === 'chain') {
              chains = state.chains.filter((name) => name !== current.chain);
              for (const sid of Object.keys(state.bySession)) {
                const picked = state.bySession[sid];
                if (picked !== null && typeof picked === 'object' && picked.chain === current.chain) continue;
                bySession[sid] = picked;
              }
            } else {
              bySession = { ...state.bySession };
              delete bySession[current.sessionId];
            }
            const label = current.kind === 'chain' ? (current.label || current.chain) : (current.title || String(current.sessionId).slice(0, 10));
            update({ confirm: null, manageBusy: false, chains, bySession, notice: { kind: 'deleted', cmd: label } });
            persist();
            refreshChains();
            if (current.kind === 'chain' && state.manage === true) {
              postOp({ op: 'list-chains' }).then((again) => {
                if (again !== null && again.ok === true && Array.isArray(again.chains)) {
                  update({ manageRows: again.chains, notice: { kind: 'deleted', cmd: label } });
                }
              });
            }
          });
        };

        /* 会话真的跑过至少一轮之后，才把它那条新链记进本机链列表（空壳链不出现）。 */
        const commitChain = (chain) => {
          if (typeof chain !== 'string' || chain === '' || chain === MAIN_CHAIN) return;
          if (state.chains.indexOf(chain) >= 0) return;
          update({ chains: [...state.chains, chain] });
          persist();
        };
        const toggleTurns = () => {
          update({ turnsCollapsed: !state.turnsCollapsed });
          persist();
        };
        const setPeriod = (delta) => {
          const next = Math.min(PERIOD_MAX, Math.max(PERIOD_MIN, state.period + delta));
          if (next === state.period) return;
          update({ period: next });
          persist();
        };

        ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'memory-panel: dictionaries');
        void refreshChains();

        ctx.effect(() => {
          const tag = document.createElement('style');
          tag.dataset.plugin = '@local/memory-panel';
          tag.textContent = CSS;
          document.head.appendChild(tag);
          return () => { tag.remove(); };
        }, 'memory-panel: styles');

        /* 新会话页面：工作区/模式那一行的第三个胶囊。 */
        ctx.slots.inject('conversation.hero.options', () => ctx.slots.register({
          name: 'conversation.hero.options',
          id: 'memory-chain',
          order: 10,
          locale: NS,
          inject: (sessionId) => ({
            hooks: { panel: store },
            sessionId,
            open: () => openPicker(sessionId),
            autoChain: ensureBound,
          }),
        }, MemoryChainChip));

        /* 正文会话：状态行 + 回合刻度。 */
        ctx.slots.inject('conversation.composer.dock', () => ctx.slots.register({
          name: 'conversation.composer.dock',
          id: 'memory-chain-lock',
          order: 20,
          locale: NS,
          inject: (sessionId) => ({ hooks: { panel: store }, sessionId, commit: commitChain, toggleTurns, autoChain: ensureBound }),
        }, MemoryDock));

        /* 会话行 "..." 菜单：删除记忆档案。 */
        ctx.slots.inject('sidebar.workspaces.session.menu.item', () => ctx.slots.register({
          name: 'sidebar.workspaces.session.menu.item',
          id: 'memory-panel.session-delete',
          order: 500,
          locale: NS,
          inject: () => ({ askDeleteSession }),
        }, MemorySessionMenuItem));

        /* 选择弹窗：挂在 shell.overlay 上，不占会话槽位。 */
        ctx.slots.inject('shell.overlay', () => ctx.slots.register({
          name: 'shell.overlay',
          id: 'memory-panel.dialog',
          order: 100,
          locale: NS,
          inject: () => ({
            hooks: { panel: store },
            choose,
            close: closePicker,
            copyCommand,
            setPeriod,
            toggleTurns,
            removeChain,
            openManage,
            manageDelete,
            askDeleteSession,
            cancelConfirm,
            runConfirm,
            renaming: state.renaming,
            renameError: state.renameError,
            startRename,
            cancelRename,
            submitRename,
            openJunk, junkToggle, junkToggleAll, junkSetMode, junkSetTarget, junkImport, junkClose,
          }),
        }, MemoryDialog));
      },
    };
  },
});

