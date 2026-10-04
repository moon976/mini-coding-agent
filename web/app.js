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
  els.messages.appendChild(wrap);
  scrollDown();
  return wrap;
}

function addUserMsg(text) {
  clearHint();
  const wrap = document.createElement('div');
  wrap.className = 'msg user';
  wrap.innerHTML =
    '<div class="msg-role">你</div><div class="msg-body">' + escapeHtml(text) + '</div>';
  els.messages.appendChild(wrap);
  scrollDown(true);
}

function addStep(n) {
  clearHint();
  const d = document.createElement('div');
  d.className = 'step-line';
  d.textContent = '第 ' + n + ' 步';
  els.messages.appendChild(d);
  scrollDown();
}

function addStats(text) {
  const d = document.createElement('div');
  d.className = 'stats';
  d.textContent = text;
  els.messages.appendChild(d);
  scrollDown();
}

function addNotice(text) {
  const d = document.createElement('div');
  d.className = 'stats';
  d.textContent = text;
  els.messages.appendChild(d);
  scrollDown();
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
  card.className = 'tool';
  card.innerHTML =
    '<div class="tool-head">' +
      '<span class="tool-arrow">▼</span>' +
      '<span class="tool-name">' + escapeHtml(name) + '</span>' +
      '<span class="tool-badge">' + escapeHtml(String(label).slice(0, 90)) + '</span>' +
    '</div>' +
    '<div class="tool-body">' +
      '<div class="tool-label">参数</div><pre>' + escapeHtml(args) + '</pre>' +
      '<div class="tool-label">返回</div><pre class="tool-result">等待执行…</pre>' +
    '</div>';
  card.querySelector('.tool-head').addEventListener('click', () => {
    card.classList.toggle('collapsed');
  });
  els.messages.appendChild(card);
  scrollDown();
  pendingTools[id] = card;
  return card;
}

function fillToolResult(id, result) {
  const card = pendingTools[id];
  if (!card) return;
  const pre = card.querySelector('.tool-result');
  if (pre) pre.textContent = result || '(空)';
  const badge = card.querySelector('.tool-badge');
  const failed = /^(错误|已取消)/.test(String(result || '')) || /\[退出码 [^0]\]/.test(String(result || ''));
  if (badge && failed) {
    badge.textContent = '失败 · ' + badge.textContent;
    badge.classList.add('deny');
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
      addNotice('本次使用：' + ev.mode);
      break;
    case 'start':
      break;
    case 'step':
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
      addBlock('error', '<div class="msg-role">出错</div><div class="msg-body">' + escapeHtml(ev.message) + '</div>');
      setStatus('err', '出错');
      break;
    case 'done':
      addStats(ev.stats);
      setStatus('ok', '完成');
      finishRun();
      break;
    case 'max_steps':
      addStats(ev.stats + '｜' + ev.summary);
      setStatus('err', '达到步数上限');
      finishRun();
      break;
    case 'stopped':
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
