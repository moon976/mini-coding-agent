// 前端逻辑：接 SSE 事件 → 渲染成对话卡片 → 把人的指令发回后端。
// 这里刻意不引任何前端框架和 CDN：整个项目保持零依赖，离线也能跑。

const $ = (id) => document.getElementById(id);

const els = {
  messages: $('messages'),
  form: $('input-form'),
  input: $('task-input'),
  send: $('send-btn'),
  stop: $('stop-btn'),
  statusDot: $('status-dot'),
  statusText: $('status-text'),
  tree: $('file-tree'),
  refresh: $('refresh-tree'),
  previewName: $('preview-name'),
  previewBody: $('preview-body'),
  mode: $('mode-select'),
  scriptField: $('script-field'),
  script: $('script-select'),
  maxSteps: $('max-steps'),
  confirmBox: $('confirm-box'),
  confirmReason: $('confirm-reason'),
  confirmCmd: $('confirm-cmd'),
  confirmYes: $('confirm-yes'),
  confirmNo: $('confirm-no'),
};

let busy = false;
let autoScroll = true;
let currentPreviewPath = null;

// 「当前任务」的容器：运行中所有过程卡片都装进它；
// 任务正常结束后，把过程整体折叠，只留最终回答在外面。
let currentTask = null;
let taskSteps = 0;
// 运行中顶部的「正在思考…」状态条。结束时移除 —— 它只服务于运行时，
// 结束后由「思考过程（N 步）」那一行接替它的位置，不要留两个。
let currentTaskLive = null;
// 「本次用的是哪个模型」由后端在 start 之前就发过来，先存着，
// 等 start 建好任务容器再放进去 —— 它属于这个任务的过程信息，
// 不该孤零零漂在当前任务外面。
let pendingMode = null;

// 运行中追加到任务容器，空闲时追加到消息区根部
function target() {
  return currentTask || els.messages;
}

// ---------------------------------------------------------------- 工具函数

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// 极简 markdown：够显示模型输出即可（标题、代码块、行内代码、列表、粗体）
function renderMarkdown(text) {
  const blocks = [];
  let t = String(text).replace(/```[a-zA-Z]*\n?([\s\S]*?)```/g, (_m, code) => {
    blocks.push('<pre><code>' + escapeHtml(code.replace(/\n$/, '')) + '</code></pre>');
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });
  t = escapeHtml(t);
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/^###\s+(.*)$/gm, '<h3>$1</h3>');
  t = t.replace(/^##\s+(.*)$/gm, '<h2>$1</h2>');
  t = t.replace(/^#\s+(.*)$/gm, '<h1>$1</h1>');
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/(?:^-\s+.+\n?)+/gm, (m) =>
    '<ul>' + m.trim().split('\n').map((l) => '<li>' + l.replace(/^-\s+/, '') + '</li>').join('') + '</ul>');
  t = t.split(/\n{2,}/).map((p) => {
    const s = p.trim();
    if (!s) return '';
    if (/^<(h1|h2|h3|ul|pre)/.test(s)) return s;
    return '<p>' + s.replace(/\n/g, '<br>') + '</p>';
  }).join('');
  return t.replace(/\u0000B(\d+)\u0000/g, (_m, i) => blocks[Number(i)]);
}

function scrollDown(force) {
  if (autoScroll || force) {
    els.messages.scrollTop = els.messages.scrollHeight;
  }
}

els.messages.addEventListener('scroll', () => {
  const nearBottom =
    els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 80;
  autoScroll = nearBottom;
});

function clearHint() {
  const h = els.messages.querySelector('.empty-hint');
  if (h) h.remove();
}

function addBlock(cls, html) {
  clearHint();
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + cls;
  wrap.innerHTML = html;
  target().appendChild(wrap);
  scrollDown();
  return wrap;
}

function addUserMsg(text) {
  clearHint();
  const wrap = document.createElement('div');
  wrap.className = 'msg user';
  wrap.innerHTML =
    '<div class="msg-role">你</div><div class="msg-body">' + escapeHtml(text) + '</div>';
  els.messages.appendChild(wrap);  // 用户消息永远留在根部，不进折叠区
  scrollDown(true);
}

function addStep(n) {
  clearHint();
  const d = document.createElement('div');
  d.className = 'step-line';
  d.textContent = '第 ' + n + ' 步';
  target().appendChild(d);
  scrollDown();
}

function addStats(text) {
  const d = document.createElement('div');
  d.className = 'stats';
  d.textContent = text;
  target().appendChild(d);
  scrollDown();
}

function addNotice(text) {
  const d = document.createElement('div');
  d.className = 'stats';
  d.textContent = text;
  target().appendChild(d);
  scrollDown();
}

// ---------------------------------------------------------------- 思考过程折叠

// 任务结束时调用：把除最终回答外的所有过程装进 <details>，默认收起。
// 为什么折叠而不是删掉？过程是「它是怎么想的」的证据，面试演示时
// 点开就能讲；删了就再也找不回来了。
function collapseTask() {
  dropLive();
  if (!currentTask) return;
  const box = currentTask;
  const kids = Array.from(box.children);
  let lastAgent = null;
  for (let i = kids.length - 1; i >= 0; i--) {
    if (kids[i].classList.contains('msg') && kids[i].classList.contains('agent')) {
      lastAgent = kids[i];
      break;
    }
  }
  if (lastAgent && kids.length > 1) {
    const details = document.createElement('details');
    details.className = 'thinking';
    const sum = document.createElement('summary');
    sum.textContent = '思考过程（' + taskSteps + ' 步 · 点击展开）';
    details.appendChild(sum);
    kids.forEach((el) => {
      if (el !== lastAgent) details.appendChild(el);
    });
    box.insertBefore(details, lastAgent);
  }
  currentTask = null;
  scrollBackToQuestion(box);
}

// 任务跑完后把视线拉回「我问了什么」那一处。
// 不改的话会很难解释：运行时页面一路向下追，等跑完只剩一个孤零零的答案，
// 用户根本不知道这是在回答自己的哪个问题。
function scrollBackToQuestion(box) {
  const anchor = box.previousElementSibling || box;  // 紧挨着 task-box 的通常就是用户提问
  if (anchor && typeof anchor.scrollIntoView === 'function') {
    anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// 移除「正在思考…」状态条
function dropLive() {
  if (currentTaskLive) {
    currentTaskLive.remove();
    currentTaskLive = null;
  }
}

// 放弃折叠：出错/手动停止时保持展开 —— 这些情况用户正需要看细节。
function abortTask() {
  dropLive();
  currentTask = null;
}

// 运行中更新状态条：让人知道它没卡死，并且知道走到第几步了。
function setLive(text) {
  if (!currentTaskLive) return;
  currentTaskLive.innerHTML =
    '<span class="spin"></span><span>' + escapeHtml(text) + '</span>';
}

// ---------------------------------------------------------------- 工具卡片

const pendingTools = {};  // tool_call_id -> 卡片元素

function addToolCard(id, name, args) {
  clearHint();
  let label = args;
  try {
    const obj = JSON.parse(args);
    if (typeof obj.command === 'string') label = obj.command;
    else if (typeof obj.path === 'string') label = obj.path + (obj.pattern ? ' ← ' + obj.pattern : '');
    else label = JSON.stringify(obj, null, 2);
  } catch (_e) { /* 不是合法 JSON 就原样显示 */ }

  const card = document.createElement('div');
  // 默认折叠：运行时屏幕应该显示「它在做什么」的一行行动作，
  // 而不是铺满大段参数与返回值。细节留给需要时才点开。
  card.className = 'tool collapsed';
  card.innerHTML =
    '<div class="tool-head">' +
      '<span class="tool-arrow">▶</span>' +
      '<span class="tool-name">' + escapeHtml(name) + '</span>' +
      '<span class="tool-badge">' + escapeHtml(String(label).slice(0, 90)) + '</span>' +
      '<span class="tool-state pending">…</span>' +
    '</div>' +
    '<div class="tool-body">' +
      '<div class="tool-label">参数</div><pre>' + escapeHtml(args) + '</pre>' +
      '<div class="tool-label">返回</div><pre class="tool-result">等待执行…</pre>' +
    '</div>';
  card.querySelector('.tool-head').addEventListener('click', () => {
    card.classList.toggle('collapsed');
  });
  target().appendChild(card);
  scrollDown();
  pendingTools[id] = card;
  return card;
}

function fillToolResult(id, result) {
  const card = pendingTools[id];
  if (!card) return;
  const pre = card.querySelector('.tool-result');
  if (pre) pre.textContent = result || '(空)';
  const failed = /^(错误|已取消)/.test(String(result || '')) || /\[退出码 [^0]\]/.test(String(result || ''));
  // 折叠状态下也要一眼看出成功与否，所以把结果做成一个 ✓ / ✗ 小标记
  const state = card.querySelector('.tool-state');
  if (state) {
    state.textContent = failed ? '✗' : '✓';
    state.className = 'tool-state ' + (failed ? 'bad' : 'ok');
  }
  if (failed) {
    const badge = card.querySelector('.tool-badge');
    if (badge) badge.classList.add('deny');
  }
  delete pendingTools[id];
  scrollDown();
}

// ---------------------------------------------------------------- 状态

function setBusy(v) {
  busy = v;
  els.send.disabled = v;
  els.stop.disabled = !v;
  els.statusDot.className = 'dot ' + (v ? 'run' : 'idle');
  els.statusText.textContent = v ? '运行中…' : '空闲';
}

function setStatus(cls, text) {
  els.statusDot.className = 'dot ' + cls;
  els.statusText.textContent = text;
}

// ---------------------------------------------------------------- 文件树

async function loadTree() {
  try {
    const res = await fetch('/api/files');
    const data = await res.json();
    els.tree.innerHTML = '';
    renderNode(data, 0);
  } catch (_e) {
    els.tree.textContent = '读取失败';
  }
}

function renderNode(node, depth) {
  const children = node.children || [];
  if (node.type === 'dir') {
    const d = document.createElement('div');
    d.className = 'tree-item tree-dir';
    d.style.paddingLeft = (6 + depth * 12) + 'px';
    d.innerHTML = '<span class="tree-icon">📁</span>' + escapeHtml(node.name);
    els.tree.appendChild(d);
    children.forEach((c) => renderNode(c, depth + 1));
    return;
  }
  const f = document.createElement('div');
  f.className = 'tree-item';
  f.style.paddingLeft = (6 + depth * 12) + 'px';
  f.innerHTML = '<span class="tree-icon">📄</span>' + escapeHtml(node.name) +
    '<span style="margin-left:auto;color:#9ca3af;font-size:11px">' + node.size + 'B</span>';
  f.addEventListener('click', () => {
    document.querySelectorAll('.tree-item.active').forEach((x) => x.classList.remove('active'));
    f.classList.add('active');
    loadFile(node.path);
  });
  els.tree.appendChild(f);
}

async function loadFile(path) {
  currentPreviewPath = path;
  els.previewName.textContent = path;
  try {
    const res = await fetch('/api/file?path=' + encodeURIComponent(path));
    const data = await res.json();
    els.previewBody.textContent = data.content !== undefined ? data.content : data.error;
  } catch (_e) {
    els.previewBody.textContent = '读取失败';
  }
}

// 刷新预览（agent 改完文件后自动更新右栏）
async function refreshPreview() {
  if (currentPreviewPath) loadFile(currentPreviewPath);
}

// ---------------------------------------------------------------- 事件流

function handleEvent(ev) {
  switch (ev.type) {
    case 'mode':
      pendingMode = ev.mode;
      break;
    case 'start': {
      // 为这个任务开一个专属容器：之后的步骤、工具卡片都装进它，
      // 结束时好整体折叠
      const box = document.createElement('div');
      box.className = 'task-box';
      els.messages.appendChild(box);
      currentTask = box;
      taskSteps = 0;
      if (pendingMode) {
        addNotice('本次使用：' + pendingMode);
        pendingMode = null;
      }
      const live = document.createElement('div');
      live.className = 'thinking-live';
      box.appendChild(live);
      currentTaskLive = live;
      setLive('正在思考…');
      break;
    }
    case 'step':
      taskSteps = ev.step;
      setLive('正在思考… 第 ' + ev.step + ' 步');
      addStep(ev.step);
      break;
    case 'compact':
      addNotice('上下文超过预算，已压缩 ' + ev.rounds + ' 次');
      break;
    case 'message':
      if (ev.content) addBlock('agent', '<div class="msg-role">agent</div><div class="msg-body">' + renderMarkdown(ev.content) + '</div>');
      break;
    case 'tool_call':
      addToolCard(ev.id, ev.name, ev.arguments);
      break;
    case 'tool_result':
      fillToolResult(ev.id, ev.result);
      break;
    case 'confirm':
      showConfirm(ev.command, ev.reason);
      break;
    case 'notice':
      addNotice(ev.message);
      break;
    case 'error':
      abortTask();   // 出错保持展开，方便看细节
      addBlock('error', '<div class="msg-role">出错</div><div class="msg-body">' + escapeHtml(ev.message) + '</div>');
      setStatus('err', '出错');
      break;
    case 'done':
      collapseTask();   // 正常结束：过程收起，只留最终回答
      addStats(ev.stats);
      setStatus('ok', '完成');
      finishRun();
      break;
    case 'max_steps':
      abortTask();      // 异常结束：保持展开，看是哪一步卡住的
      addStats(ev.stats + '｜' + ev.summary);
      setStatus('err', '达到步数上限');
      finishRun();
      break;
    case 'stopped':
      abortTask();
      addStats(ev.stats + '｜' + ev.summary);
      setStatus('idle', '已停止');
      finishRun();
      break;
    case 'end':
      finishRun();
      break;
  }
}

function finishRun() {
  setBusy(false);
  loadTree();
  refreshPreview();
}

const es = new EventSource('/api/events');
es.onmessage = (e) => {
  try {
    handleEvent(JSON.parse(e.data));
  } catch (_err) { /* 忽略无法解析的帧 */ }
};
es.onerror = () => setStatus('err', '连接断开，正在重连…');
es.onopen = () => { if (!busy) setStatus('idle', '空闲'); };

// ---------------------------------------------------------------- 交互

els.form.addEventListener('submit', (e) => {
  e.preventDefault();
  send();
});

els.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    send();
  }
});

function send() {
  const task = els.input.value.trim();
  if (!task || busy) return;
  addUserMsg(task);
  els.input.value = '';
  setBusy(true);
  fetch('/api/task', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task: task,
      mock: els.mode.value === 'mock',
      script: els.script.value,
      max_steps: parseInt(els.maxSteps.value, 10) || 20,
    }),
  })
    .then((r) => r.json())
    .then((d) => {
      if (d.error) {
        addBlock('error', '<div class="msg-role">出错</div><div class="msg-body">' + escapeHtml(d.error) + '</div>');
        setBusy(false);
      }
    })
    .catch((err) => {
      addBlock('error', '<div class="msg-role">出错</div><div class="msg-body">' + escapeHtml(String(err)) + '</div>');
      setBusy(false);
    });
}

els.stop.addEventListener('click', () => {
  fetch('/api/stop', { method: 'POST' });
  setStatus('idle', '正在停止…');
});

els.mode.addEventListener('change', () => {
  els.scriptField.hidden = els.mode.value !== 'mock';
});

els.refresh.addEventListener('click', loadTree);

function showConfirm(command, reason) {
  els.confirmReason.textContent = reason;
  els.confirmCmd.textContent = command;
  els.confirmBox.hidden = false;
}

function answerConfirm(allow) {
  els.confirmBox.hidden = true;
  fetch('/api/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allow: allow }),
  });
}

els.confirmYes.addEventListener('click', () => answerConfirm(true));
els.confirmNo.addEventListener('click', () => answerConfirm(false));

// ---------------------------------------------------------------- 启动

loadTree();
els.input.focus();
