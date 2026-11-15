/* Shared, authenticated resource cleanup UI. No network or filesystem access of its own. */
(function (global) {
  'use strict';
  const CATEGORIES = [
    ['terrain', '地形与游戏数据', true], ['catalog', '地图目录', true],
    ['names', '名称表', true], ['captures', '遇境录制登记（原始录制保留）', false],
    ['props', '道具形状（可单独补生成，不重做地形）', false]
  ];
  const PRESERVE = '即使全选也保留：账号与登录、AI 配置、主人与人设、聊天记忆。生成数据先移入本机回收区；原始录制文件始终保留，不会自动重新生成。';
  const GUIDE = '下一步：生成游戏数据 → 遇境采集 → 构建地图目录。请逐步点击，不会自动启动生成或采集。';
  const MODES = {
    cleanup: {title: '清理数据并从头生成', preview: 'preview', execute: 'execute', confirm: '确认清理所选数据/登记'},
    restore: {title: '恢复上次清理', preview: 'restore-preview', execute: 'restore', confirm: '确认恢复预览中的数据'},
    purge: {title: '彻底删除回收区', preview: 'purge-preview', execute: 'purge', confirm: '永久删除预览中的回收区数据'}
  };
  function formatBytes(value) {
    if (!Number.isFinite(value) || value < 0) return '未知大小';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let n = value, i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i ? n.toFixed(2) : String(n)) + ' ' + units[i];
  }
  function expiry(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
    return typeof value === 'string' ? Date.parse(value) : NaN;
  }
  function offlineProblem(status) {
    if (!status || status.ok === false || typeof status.online !== 'boolean'
        || typeof status.runner !== 'boolean' || typeof status.chat !== 'boolean') return '无法确认小光已离线，请刷新状态后重试。';
    if (status.online || status.runner || status.chat || status.going || status.changing) return '请先让小光下线并停止聊天/换图，再预览或执行清理。';
    const capture = status.captureState;
    if (capture && !['idle', 'stopped', 'stale', 'error'].includes(typeof capture === 'string' ? capture : capture.state)) return '请先停止遇境采集，再进行数据清理。';
    return '';
  }
  function messageOf(error) {
    const doc = error && (error.data || error);
    const code = doc && (doc.error_code || doc.code);
    return (doc && typeof doc.message === 'string' ? doc.message : '操作失败，请刷新后重试。') + (code ? '（' + String(code) + '）' : '');
  }
  function mount(host, options) {
    if (!host || host.dataset.resetMounted === 'true') return null;
    host.dataset.resetMounted = 'true';
    const doc = host.ownerDocument || global.document;
    const state = {mode: 'cleanup', busy: false, plan: null, open: false, serial: 0, operation: null, paused: false, discovering: false};
    const fields = {};
    function node(tag, className, text) {
      const n = doc.createElement(tag);
      if (className) n.className = className;
      if (text !== undefined) n.textContent = text;
      return n;
    }
    function button(text, fn, className) {
      const b = node('button', 'xg-reset-button ' + (className || ''), text);
      b.type = 'button'; b.addEventListener('click', fn); return b;
    }
    const heading = node('h3', '', '重新准备本机游戏数据');
    const note = node('p', 'xg-reset-note', PRESERVE);
    const row = node('div', 'xg-reset-actions');
    const start = button(MODES.cleanup.title, () => open('cleanup'), 'xg-reset-danger');
    const restore = button(MODES.restore.title, () => open('restore'));
    const purge = button(MODES.purge.title, () => open('purge'));
    row.append(start, restore, purge);
    const feedback = node('p', 'xg-reset-feedback'); feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite');
    const guide = node('div', 'xg-reset-guide'); guide.hidden = true;
    const guideText=node('p', '', GUIDE);guide.append(guideText);const guideButtons={};
    const guideActions = node('div', 'xg-reset-actions');
    [['data', '1 · 生成游戏数据'], ['capture', '2 · 遇境采集'], ['catalog', '3 · 构建地图目录'], ['props', '补生成道具形状']].forEach(([step, label]) => {
      const action=button(label, () => { if (typeof options.navigate === 'function') options.navigate(step); });guideButtons[step]=action;guideActions.append(action);
    });
    guide.append(guideActions); host.append(heading, note, row, feedback, guide);

    const dialog = node('dialog', 'xg-reset-dialog');
    const title = node('h2'); title.id = (host.id || 'xg-reset') + '-title'; dialog.setAttribute('aria-labelledby', title.id);
    const intro = node('p', 'xg-reset-note', PRESERVE + ' 必须保持小光离线；后台执行前会再次核验。');
    const selection = node('fieldset', 'xg-reset-selection'); selection.append(node('legend', '', '选择要清理的类别'));
    CATEGORIES.forEach(([key, label, checked]) => {
      const wrap = node('label', 'xg-reset-choice');
      const input = node('input'); input.type = 'checkbox'; input.checked = checked; input.name = key;
      fields[key] = input; input.addEventListener('change', invalidate);
      wrap.append(input, node('span', '', label)); selection.append(wrap);
    });
    const all = button('全选清理类别', () => { CATEGORIES.forEach(([key]) => { fields[key].checked = true; }); invalidate(); });
    selection.append(all, node('p', 'xg-reset-note', '录制登记默认保留；勾选后仅清除录制登记，原始录制文件保留，需要新采集或明确重新登记。' + PRESERVE));
    const previewBox = node('section', 'xg-reset-preview'); previewBox.hidden = true;
    const previewTitle = node('h3', '', '操作预览（相对数据目录）');
    const list = node('ul', 'xg-reset-items');
    const total = node('p', 'xg-reset-total');
    const expires = node('p', 'xg-reset-note');
    previewBox.append(previewTitle, list, total, expires);
    const confirmation = node('label', 'xg-reset-confirm'); confirmation.hidden = true;
    const confirm = node('input'); confirm.type = 'checkbox'; confirm.addEventListener('change', sync);
    const confirmCopy = node('span'); confirmation.append(confirm, confirmCopy);
    const status = node('p', 'xg-reset-feedback'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const actions = node('div', 'xg-reset-actions');
    const cancel = button('取消', close);
    const preview = button('先预览目录与大小', previewPlan);
    const execute = button('确认清理', commit, 'xg-reset-danger'); execute.hidden = true;
    const resume = button('继续查询任务', resumeTracking); resume.hidden = true;
    const resend = button('重新发送同一任务（原请求号）', resendSame); resend.hidden = true;
    actions.append(cancel, preview, execute, resume, resend);
    dialog.append(title, intro, selection, previewBox, confirmation, status, actions);
    (doc.body || host).append(dialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });

    function say(text, error) { status.textContent = text || ''; status.setAttribute('role', error ? 'alert' : 'status'); status.dataset.error = String(!!error); }
    function sync() {
      [start, restore, purge, preview, cancel, all].forEach(b => { b.disabled = state.busy; });
      CATEGORIES.forEach(([key]) => { fields[key].disabled = state.busy; });
      confirm.disabled = state.busy;
      resume.hidden = !state.paused; resume.disabled = !!polling;
      resend.hidden = !(state.paused && state.operation?.not_found && !state.operation.task_id && state.operation.payload);
      resend.disabled = !!polling;
      execute.disabled = state.busy || !state.plan || (!state.plan.items.length && state.mode !== 'cleanup') || !confirm.checked;
      dialog.setAttribute('aria-busy', String(state.busy));
      if (typeof options.onBusy === 'function') options.onBusy(state.busy);
    }
    function invalidate() {
      if (state.busy) return false;
      state.plan = null; confirm.checked = false; previewBox.hidden = confirmation.hidden = execute.hidden = true;
      say('请重新预览，目录和大小以本次后台返回为准。'); sync();
    }
    async function offline() {
      const reason = offlineProblem(await options.getStatus());
      if (reason) throw new Error(reason);
      if (typeof options.isBusy === 'function' && options.isBusy()) throw new Error('请先停止当前构建或采集任务，再进行数据清理。');
    }
    function open(mode, tracked = false) {
      if ((state.busy && !tracked) || !MODES[mode]) return false;
      state.serial += 1; state.mode = mode; state.open = true; state.plan = null;
      CATEGORIES.forEach(([key, _label, checked]) => { fields[key].checked = checked; });
      title.textContent = MODES[mode].title;
      selection.hidden = mode !== 'cleanup'; previewBox.hidden = confirmation.hidden = execute.hidden = true;
      confirm.checked = false; preview.textContent = mode === 'purge' ? '预览将永久删除的回收区' : '先预览目录与大小';
      execute.textContent = MODES[mode].confirm;
      confirmCopy.textContent = mode === 'purge' ? '我已逐项核对以上目录和大小，确认永久删除，且知道此操作不可恢复。'
        : mode === 'restore' ? '我已核对预览，确认恢复上次清理的数据；存在冲突时应停止而非覆盖。'
          : '我已逐项核对预览，确认清理所选生成数据/录制登记；生成数据移入回收区，原始录制、账号、登录、AI 配置及聊天记忆保留。';
      say(mode === 'purge' ? '永久删除不可恢复。必须先预览，再勾选确认并单独点击永久删除。' : '尚未执行任何操作。先预览，再进行第二次明确确认。');
      if (typeof dialog.showModal === 'function') { if (!dialog.open) dialog.showModal(); }
      else dialog.setAttribute('open', '');
      sync(); preview.focus(); return true;
    }
    function close() {
      if (state.busy) return false;
      state.serial += 1; state.open = false; state.plan = null; confirm.checked = false;
      if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
      start.focus(); sync(); return true;
    }
    function applyPreview(result) {
        confirm.checked = false;
        if (typeof result.plan_id !== 'string' || !result.plan_id || !Array.isArray(result.items)
            || !Number.isFinite(result.total_bytes) || result.total_bytes < 0
            || result.items.some(item => !item || typeof item.path !== 'string' || !item.path
              || !Number.isFinite(item.bytes) || item.bytes < 0 || !Number.isInteger(item.files) || item.files < 0)
            || !Number.isFinite(expiry(result.expires_at)) || expiry(result.expires_at) <= Date.now()) throw new Error('预览数据无效或已过期，请重新预览。');
        while (list.firstChild) list.removeChild(list.firstChild);
        result.items.forEach(item => {
          const line = node('li');
          const path = node('code', 'xg-reset-path', String(item.path || '（未提供相对目录）'));
          const label = CATEGORIES.find(([key]) => key === item.category);
          line.append(node('strong', '', (label ? label[1] : String(item.category || '数据')) + '：'), path,
            node('span', 'xg-reset-item-size', formatBytes(item.bytes) + ' · ' + (Number.isInteger(item.files) ? item.files : '?') + ' 个文件 · ' + String(item.action || '')));
          list.append(line);
        });
        if (!result.items.length) list.append(node('li', '', state.mode === 'cleanup'
          ? '未找到可回收文件，仅重置所选资源的生成/录制状态。原始录制文件仍保留。'
          : '没有符合条件的数据目录。'));
        total.textContent = '合计：' + formatBytes(result.total_bytes) + '，共 ' + result.items.length + ' 项。';
        expires.textContent = '预览有效至：' + new Date(expiry(result.expires_at)).toLocaleString() + '；过期需重新预览。';
        state.plan = result;
        previewBox.hidden = false; confirmation.hidden = execute.hidden = result.items.length === 0 && state.mode !== 'cleanup';
        execute.textContent = result.items.length === 0 && state.mode === 'cleanup' ? '确认重置所选资源状态' : MODES[state.mode].confirm;
        if (state.mode === 'cleanup') confirmCopy.textContent = result.items.length === 0
          ? '我确认没有可回收文件，仅重置所选资源的生成/录制状态；随后需新生成、新采集或明确重新登记。账号、登录与聊天配置保留。'
          : '我已逐项核对预览，确认清理所选生成数据/录制登记；生成数据移入回收区，原始录制、账号、登录、AI 配置及聊天记忆保留。';
        say(result.message || '请逐项核对相对目录和大小，再勾选确认。预览不会修改文件。');
        feedback.textContent = '预览已完成；尚未执行清理，请核对并确认';
        feedback.setAttribute('role', 'status');
    }
    async function applyCommit(result, mode, selected) {
        const warning = result.state_warning ? (typeof result.state_warning === 'string' ? result.state_warning
          : result.state_warning.message || '状态标记写入未完成，请重新核对资源状态后继续。') : '';
        const selection=result.selection || selected;
        const onlyProps=selection.props===true&&!['terrain','catalog','names','captures'].some(k=>selection[k]);
        const next=onlyProps?'下一步：单独补生成道具形状，不需要重做地形、录图或地图目录。':GUIDE;
        guideText.textContent=next;Object.entries(guideButtons).forEach(([key,button])=>{button.hidden=onlyProps&&key!=='props';});
        feedback.textContent = (result.message || (mode === 'purge' ? '回收区已按预览清除。' : mode === 'restore' ? '已恢复上次清理。' : '已清理所选数据。'))
          + (mode === 'purge' ? '' : ' ' + next) + (warning ? ' 警告：' + warning : '');
        feedback.setAttribute('role', warning ? 'alert' : 'status'); guide.hidden = mode === 'purge';
        try { if (typeof options.onSuccess === 'function') await options.onSuccess(mode, result); }
        catch (error) { feedback.textContent += ' 操作已完成，但状态刷新失败：' + messageOf(error); feedback.setAttribute('role', 'alert'); }
    }
    const STORAGE_KEY = 'xg.cleanup.active.v1';
    const ACTIONS = Object.values(MODES).flatMap(m => [m.preview, m.execute]);
    const wait = ms => options.delay ? options.delay(ms) : new Promise(resolve => global.setTimeout(resolve, ms));
    let polling = null;
    function remember(operation) {
      try {
        const storage = options.storage || global.localStorage;
        if (!storage) return;
        if (operation) storage.setItem(STORAGE_KEY, JSON.stringify(operation)); else storage.removeItem(STORAGE_KEY);
      } catch (_) { /* Backend current-job lookup remains available without storage. */ }
    }
    function remembered() {
      try {
        const raw = (options.storage || global.localStorage)?.getItem(STORAGE_KEY);
        if (!raw || raw.length > 5000) return null;
        const value = JSON.parse(raw);
        return value && ACTIONS.includes(value.action) && typeof value.request_id === 'string'
          && /^[A-Za-z0-9_.:-]{1,128}$/.test(value.request_id) ? value : null;
      } catch (_) { return null; }
    }
    function requestId() {
      return global.crypto?.randomUUID ? global.crypto.randomUUID()
        : 'cleanup-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2) + '-' + (++state.serial);
    }
    function modeOf(action) { return action.startsWith('restore') ? 'restore' : action.startsWith('purge') ? 'purge' : 'cleanup'; }
    function trackMessage(message, alert) {
      say(message, alert); feedback.textContent = message;
      feedback.setAttribute('role', alert ? 'alert' : 'status');
    }
    function showProgress(task) {
      const p = task.progress || {};
      const count = Number.isFinite(p.files_done) && p.files_done >= 0
        ? ' · 文件 ' + p.files_done + (Number.isFinite(p.files_total) && p.files_total >= p.files_done ? '/' + p.files_total : '') : '';
      const bytes = Number.isFinite(p.bytes_done) && p.bytes_done >= 0
        ? ' · ' + formatBytes(p.bytes_done) + (Number.isFinite(p.bytes_total) && p.bytes_total >= p.bytes_done ? '/' + formatBytes(p.bytes_total) : '') : '';
      trackMessage((typeof p.message === 'string' && p.message ? p.message
        : task.status === 'queued' ? '后台任务已排队，等待开始。' : task.action.endsWith('preview') ? '后台正在统计目录与大小…' : '后台正在处理已确认的清理计划…')
        + count + bytes + ' 请勿重复提交。', false);
    }
    function adopt(task, allowOther = false) {
      if (!task || typeof task.id !== 'string' || !task.id || !ACTIONS.includes(task.action)
          || typeof task.request_id !== 'string' || !['queued', 'running', 'succeeded', 'failed'].includes(task.status)) throw new Error('任务状态格式尚未确认');
      const old = state.operation;
      if (!allowOther && old && ((old.task_id && old.task_id !== task.id)
          || old.request_id !== task.request_id || old.action !== task.action)) throw new Error('任务身份不一致，继续查询原请求');
      const selected = task.selection || (old && old.selection) || null;
      state.operation = {request_id:task.request_id, task_id:task.id, action:task.action, mode:modeOf(task.action), selection:selected};
      state.busy = true; remember(state.operation);
      if (!state.open || state.mode !== state.operation.mode) open(state.operation.mode, true);
      if (selected) CATEGORIES.forEach(([key]) => { fields[key].checked = selected[key] === true; });
      sync(); return task;
    }
    function lookupPath() {
      const op = state.operation;
      if (!op) return '/api/wizard/cleanup/task';
      return '/api/wizard/cleanup/task?' + (op.task_id ? 'task_id=' + encodeURIComponent(op.task_id)
        : 'request_id=' + encodeURIComponent(op.request_id) + '&action=' + encodeURIComponent(op.action));
    }
    function pauseTracking() {
      state.paused = true; state.busy = true;
      trackMessage('暂时无法确认后台任务状态，任务可能仍在处理。已保留原请求并禁止重复提交；恢复连接后点“继续查询任务”，只查询、不重新执行。', true);
      sync(); return false;
    }
    async function finishTask(task) {
      const op = state.operation, result = task.result;
      try {
        if (!result || result.ok !== true || task.status !== 'succeeded') throw result || new Error('后台任务已结束，但未返回成功结果。');
        if (op.action.endsWith('preview')) applyPreview(result);
        else await applyCommit(result, op.mode, op.selection || {});
        return true;
      } catch (error) {
        state.plan = null; confirm.checked = false; execute.hidden = confirmation.hidden = true;
        let message = messageOf(error) + ' 如需再次操作，请先重新预览。';
        guide.hidden = true;
        if (!op.action.endsWith('preview') && typeof options.onUnknown === 'function') {
          try {
            const refreshed = await options.onUnknown();
            if (refreshed?.ok === false) throw new Error('资源刷新未确认');
          } catch (_) { message += ' 资源状态刷新未完成，请恢复连接后核对；原操作仍为失败。'; }
        }
        say(message, true);
        feedback.textContent = status.textContent; feedback.setAttribute('role', 'alert');
        return false;
      } finally {
        state.operation = null; state.paused = false; state.busy = false; remember(null); sync();
        if (result?.ok === true && task.status === 'succeeded' && !op.action.endsWith('preview')) close();
      }
    }
    function missingTask(error) {
      const body = error && (error.data || error), status = error?.status ?? body?.__http;
      return body?.error_code === 'task_not_found' && status === 404;
    }
    async function abandonExpired() {
      state.operation = null; state.discovering = false; state.paused = false; state.plan = null;
      confirm.checked = false; previewBox.hidden = execute.hidden = confirmation.hidden = true; guide.hidden = true;
      remember(null);
      let message = '历史任务记录已过期或后台已重启，原任务结果未知，未重放任何操作。请核对资源状态后重新预览。';
      try {
        if (typeof options.onUnknown === 'function') {
          const result = await options.onUnknown();
          if (result?.ok === false) throw new Error(result.message || '资源状态暂不可用');
        }
      } catch (_) { message += ' 当前资源状态刷新未完成，请恢复连接后刷新。'; }
      state.busy = false; trackMessage(message, true); sync(); return false;
    }
    async function follow(initial) {
      if (polling) return polling;
      polling = (async () => {
        let task = initial, failures = 0;
        state.paused = false; sync();
        while (state.operation || state.discovering) {
          try {
            if (!task) {
              const response = await options.request(lookupPath());
              if (!response || response.ok !== true) throw response || new Error('后台任务查询没有返回结果');
              if (!response.task && state.discovering && !state.operation) {
                state.discovering = false; state.busy = false; state.paused = false; sync(); return false;
              }
              task = adopt(response.task, state.discovering && !state.operation);
              state.discovering = false;
            }
            failures = 0;
            if (task.status === 'succeeded' || task.status === 'failed') return await finishTask(task);
            showProgress(task); task = null; await wait(750);
          } catch (error) {
            if (state.operation?.task_id && missingTask(error)) {
              try {
                const current = await options.request('/api/wizard/cleanup/task');
                if (current?.ok === true && current.task === null) return await abandonExpired();
                if (current?.ok === true && current.task) {
                  task = adopt(current.task, true); state.discovering = false; failures = 0; continue;
                }
              } catch (_) { /* No authoritative empty current job: stay blocked. */ }
            }
            if (state.operation && !state.operation.task_id) {
              state.operation.not_found = missingTask(error); remember(state.operation);
            }
            task = null; failures += 1;
            trackMessage('连接暂时中断，正在查询同一后台任务；尚未确认结束，不会重新提交操作。', false);
            if (failures >= 6) return pauseTracking();
            await wait(Math.min(8000, 500 * (2 ** (failures - 1))));
          }
        }
        return false;
      })();
      try { return await polling; } finally { polling = null; sync(); }
    }
    function rejected(error) {
      const body = error && (error.data || error);
      const status = error?.status ?? body?.__http;
      return body?.ok === false && (status === undefined || [400,401,403,404,409,422].includes(status));
    }
    async function begin(action, body, selection) {
      state.operation = {action, request_id:requestId(), task_id:null, mode:state.mode, selection:selection || null, payload:body, not_found:false};
      state.plan = null; confirm.checked = false; execute.hidden = confirmation.hidden = true;
      remember(state.operation); state.busy = true; sync();
      return submitOperation();
    }
    async function submitOperation() {
      const {action,payload,request_id} = state.operation;
      let task = null;
      try {
        const response = await options.request('/api/wizard/cleanup/' + action, {...payload, request_id});
        if (!response || response.ok !== true) throw response || new Error('提交结果未确认');
        task = adopt(response.task);
      } catch (error) {
        const body = error && (error.data || error);
        if (body?.task) {
          try { task = adopt(body.task, true); } catch (_) { /* Keep original request identity. */ }
        } else if (rejected(error)) {
          state.operation = null; remember(null); throw error;
        }
        if (!task) trackMessage('提交回执尚未确认，后台可能仍在处理。正在按原 request_id 查询；不会再次提交或提示重复清理。', false);
      }
      return follow(task);
    }
    async function resendSame() {
      const op = state.operation;
      if (polling || !state.paused || !op?.not_found || op.task_id || !op.payload) return false;
      state.paused = false; op.not_found = false; state.busy = true; remember(op); sync();
      trackMessage('后台明确未找到原请求；正在用同一个请求号和同一份已确认内容重新发送，不创建第二个操作。', false);
      try { return await submitOperation(); }
      catch (error) { say(messageOf(error) + ' 请重新预览后再操作。', true); return false; }
      finally { if (!state.operation) state.busy = false; sync(); }
    }
    async function resumeTracking() {
      if (polling) return false;
      state.busy = true; state.paused = false;
      if (!state.operation) state.discovering = true;
      trackMessage('正在继续查询同一后台任务，不会重新提交操作…', false);
      sync(); return follow(null);
    }
    async function reconnect() {
      if (state.busy || state.operation) return false;
      state.operation = remembered(); state.discovering = !state.operation; state.busy = true;
      if (state.operation) open(modeOf(state.operation.action), true);
      trackMessage('正在查询是否有未结束的后台维护任务，暂不重复提交…', false);
      sync(); return follow(null);
    }
    async function previewPlan() {
      if (state.busy || !state.open) return false;
      const chosen = Object.fromEntries(CATEGORIES.map(([key]) => [key, fields[key].checked === true]));
      if (state.mode === 'cleanup' && !Object.values(chosen).some(Boolean)) { say('请至少选择一个清理类别。', true); return false; }
      state.busy = true; state.plan = null; confirm.checked = false; previewBox.hidden = execute.hidden = confirmation.hidden = true; sync();
      say('正在确认离线状态并提交后台统计；尚未清理任何文件…');
      try {
        await offline();
        await begin(MODES[state.mode].preview, state.mode === 'cleanup' ? {selection:chosen} : {}, chosen);
      } catch (error) { state.plan = null; say(messageOf(error), true); }
      finally { if (!state.operation && !state.discovering) state.busy = false; sync(); }
      return !!state.plan;
    }
    async function commit() {
      if (state.busy || !state.open || !state.plan || (!state.plan.items.length && state.mode !== 'cleanup') || !confirm.checked) return false;
      if (expiry(state.plan.expires_at) <= Date.now()) { invalidate(); say('预览已过期，请重新预览后再确认。', true); return false; }
      const planId = state.plan.plan_id, mode = state.mode;
      const selected = state.plan.selection || Object.fromEntries(CATEGORIES.map(([key]) => [key,fields[key].checked===true]));
      state.busy = true; sync(); say('正在再次检查离线状态并提交已确认计划，请勿重复点击…');
      try {
        await offline();
        if (expiry(state.plan.expires_at) <= Date.now()) throw new Error('预览已过期，请重新预览后再确认。');
        return await begin(MODES[mode].execute, {plan_id:planId, confirm:true}, selected);
      } catch (error) {
        state.plan = null; confirm.checked = false; execute.hidden = confirmation.hidden = true;
        say(messageOf(error) + ' 如需再次操作，请先重新预览。', true); return false;
      } finally { if (!state.operation && !state.discovering) state.busy = false; sync(); }
    }
    sync();
    if (options.reconnect !== false) Promise.resolve().then(reconnect);
    return {open, close, preview: previewPlan, commit, reconnect, resume: resumeTracking, resend: resendSame, state,
      nodes: {start, restore, purge, dialog, fields, all, preview, execute, confirm, status, feedback, list, guide, selection, confirmation, resume, resend}};
  }
  global.XGDataCleanup = {mount, offlineProblem, formatBytes};
})(typeof window !== 'undefined' ? window : globalThis);
