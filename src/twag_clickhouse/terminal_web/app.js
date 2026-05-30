const shell = document.querySelector('.shell');
const transcript = document.querySelector('#transcript');
const statusText = document.querySelector('#statusText');
const connectionState = document.querySelector('#connectionState');
const backendState = document.querySelector('#backendState');
const cityState = document.querySelector('#cityState');
const promptForm = document.querySelector('#promptForm');
const promptInput = document.querySelector('#promptInput');
const commandButtons = document.querySelectorAll('[data-command]');
const cityButtons = document.querySelectorAll('[data-city]');
const externalViewButtons = document.querySelectorAll('[data-open-view]');
const modeButtons = document.querySelectorAll('[data-mode]');
const thinkingButtons = document.querySelectorAll('[data-thinking]');
const COMMAND_HISTORY_KEY = 'twagTerminalCommandHistory';
const COMMAND_HISTORY_LIMIT = 100;

const state = {
  socket: null,
  sessionId: '',
  city: '',
  draftNode: null,
  detailBlock: null,
  detailRenderFrame: 0,
  draftRenderFrame: 0,
  operatorToken: '',
  greetingShown: false,
  lastStatusLine: '',
  lastBackendLine: '',
  inFlightPrompt: '',
  verbose: false,
  thinking: false,
  links: {},
  commandHistory: [],
  historyIndex: null,
  historyDraft: '',
};

const appBasePath = new URL('.', window.location.href).pathname;

function appPath(path) {
  return `${appBasePath.replace(/\/$/, '')}/${path.replace(/^\//, '')}`;
}

function setConnection(value) {
  shell.dataset.state = value;
  const canReconnect = value === 'closed' || value === 'error';
  connectionState.textContent = canReconnect ? 'reconnect' : value;
  connectionState.title = canReconnect
    ? 'Reconnect to TWAG; your current prompt will stay in the input.'
    : '';
  connectionState.tabIndex = canReconnect ? 0 : -1;
  connectionState.setAttribute('role', canReconnect ? 'button' : 'status');
}

function setCity(city) {
  state.city = city || state.city;
  cityState.textContent = `city: ${state.city || '--'}`;
  cityButtons.forEach((button) => {
    const active = button.dataset.city === state.city;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setPublicLinks(links) {
  state.links = { ...state.links, ...(links || {}) };
}

function setVerboseMode(value) {
  state.verbose = Boolean(value);
  modeButtons.forEach((button) => {
    const active = button.dataset.mode === (state.verbose ? 'verbose' : 'quiet');
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setThinkingMode(value) {
  state.thinking = Boolean(value);
  thinkingButtons.forEach((button) => {
    const active = button.dataset.thinking === (state.thinking ? 'on' : 'off');
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function loadCommandHistory() {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(COMMAND_HISTORY_KEY) || '[]');
    if (Array.isArray(parsed)) {
      state.commandHistory = parsed
        .filter((entry) => typeof entry === 'string' && entry.trim())
        .slice(-COMMAND_HISTORY_LIMIT);
    }
  } catch {
    state.commandHistory = [];
  }
}

function saveCommandHistory() {
  try {
    window.localStorage.setItem(
      COMMAND_HISTORY_KEY,
      JSON.stringify(state.commandHistory.slice(-COMMAND_HISTORY_LIMIT)),
    );
  } catch {
    // History is convenience state; ignore storage failures.
  }
}

function resetHistoryNavigation() {
  state.historyIndex = null;
  state.historyDraft = '';
}

function addCommandHistory(text) {
  const command = String(text || '').trim();
  if (!command) return;
  state.commandHistory = state.commandHistory.filter((entry) => entry !== command);
  state.commandHistory.push(command);
  state.commandHistory = state.commandHistory.slice(-COMMAND_HISTORY_LIMIT);
  saveCommandHistory();
  resetHistoryNavigation();
}

function setPromptValue(text) {
  promptInput.value = text;
  const end = promptInput.value.length;
  window.requestAnimationFrame(() => {
    promptInput.setSelectionRange(end, end);
  });
}

function navigateCommandHistory(direction) {
  if (!state.commandHistory.length) return false;

  if (direction < 0) {
    if (state.historyIndex === null) {
      state.historyDraft = promptInput.value;
      state.historyIndex = state.commandHistory.length - 1;
    } else {
      state.historyIndex = Math.max(0, state.historyIndex - 1);
    }
    setPromptValue(state.commandHistory[state.historyIndex] || '');
    return true;
  }

  if (state.historyIndex === null) return false;

  if (state.historyIndex < state.commandHistory.length - 1) {
    state.historyIndex += 1;
    setPromptValue(state.commandHistory[state.historyIndex] || '');
  } else {
    setPromptValue(state.historyDraft);
    resetHistoryNavigation();
  }
  return true;
}

function serviceShortState(value) {
  return ({
    ready: 'ready',
    working: 'working',
    warming: 'warming',
    configured: 'queued',
    unconfigured: 'off',
    error: 'error',
    unknown: 'check',
  })[value] || 'check';
}

function backendHealth(services) {
  const states = Object.values(services || {}).map(service => service?.state || 'unknown');
  if (states.includes('error') || states.includes('unconfigured')) return 'error';
  if (states.includes('warming') || states.includes('working')) return 'warming';
  if (states.length && states.every(value => value === 'ready' || value === 'configured')) {
    return 'ready';
  }
  return 'unknown';
}

function setBackendStatus(services) {
  const clickhouse = services?.clickhouse?.state || 'unknown';
  const subconscious = services?.subconscious?.state || 'unknown';
  const line = `services: CH ${serviceShortState(clickhouse)} / AI ${serviceShortState(subconscious)}`;
  backendState.textContent = line;
  backendState.dataset.health = backendHealth(services);
  backendState.title = 'ClickHouse and Subconscious readiness';
  if (line !== state.lastBackendLine) {
    appendStatus(line);
    state.lastBackendLine = line;
  }
}

function statusIsNearBottom() {
  return statusText.scrollHeight - statusText.scrollTop - statusText.clientHeight < 32;
}

function appendStatus(text) {
  const cleanText = String(text || '').trim();
  if (!cleanText || cleanText === state.lastStatusLine) return;

  const shouldScroll = statusIsNearBottom();
  const time = new Intl.DateTimeFormat([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date());
  const line = `[${time}] ${cleanText.replace(/\s+/g, ' ')}`;
  const lines = `${statusText.textContent || ''}${line}\n`.split('\n');
  statusText.textContent = lines.slice(-120).join('\n');
  state.lastStatusLine = cleanText;
  if (shouldScroll) statusText.scrollTop = statusText.scrollHeight;
}

function compactStatusText(text) {
  return String(text || '')
    .trim()
    .split(/\n+/)[0]
    .replace(/\s+/g, ' ');
}

function errorSummary(event) {
  if (event?.summary) return compactStatusText(event.summary);
  const text = compactStatusText(event?.error || 'Unknown error.');
  if (/try a narrower/i.test(text)) return 'Stopped. Try a narrower query.';
  return text.replace(/^Error:\s*/i, '');
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text || '');
  return div.innerHTML;
}

function safeUrl(url) {
  try {
    const value = String(url || '').trim();
    const routedUrl = value.startsWith('/terminal/') ? appPath(value) : value;
    const parsed = new URL(routedUrl, window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch {
    return '';
  }
  return '';
}

function openExternalView(kind) {
  const url = safeUrl(state.links?.[kind] || '');
  if (!url) {
    appendStatus(`${kind} link is not configured.`);
    appendMessage('system', `${kind} link is not configured.`);
    return;
  }
  const opened = window.open(url, '_blank', 'noopener,noreferrer');
  if (opened) opened.opener = null;
}

function anchorHtml(url, label = url) {
  const href = safeUrl(url);
  if (!href) return escapeHtml(label);
  return `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
}

function commandLinkHtml(command, label = command) {
  const normalized = String(command || '').trim();
  if (!normalized) return escapeHtml(label);
  return `<a href="#" data-command-link="${encodeURIComponent(normalized)}">${escapeHtml(label)}</a>`;
}

function linkifyPlainUrls(html) {
  return html.replace(/https?:\/\/[^\s<]+/g, (match) => {
    const trailing = match.match(/[),.;:!?]+$/)?.[0] || '';
    const url = trailing ? match.slice(0, -trailing.length) : match;
    return `${anchorHtml(url)}${escapeHtml(trailing)}`;
  });
}

function markdownToHtml(text) {
  const placeholders = [];
  const stash = (html) => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push([token, html]);
    return token;
  };

  const protectedText = String(text || '')
    .replace(/`([^`\n]+)`/g, (_match, code) => {
      if (code.trim().toLowerCase() === 'more') {
        return stash(commandLinkHtml('more', code));
      }
      return stash(`<code>${escapeHtml(code)}</code>`);
    })
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|\/[^)\s]+)\)/g, (_match, label, url) => stash(anchorHtml(url, label)));

  let html = escapeHtml(protectedText)
    .replace(/\*\*([^*\n][\s\S]*?[^*\n])\*\*/g, '<strong>$1</strong>')
  html = linkifyPlainUrls(html);

  for (const [token, value] of placeholders) {
    html = html.replace(escapeHtml(token), value);
  }

  return html;
}

function clipboardTextForNode(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    return node.nodeValue || '';
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return '';
  }

  const tagName = node.tagName.toLowerCase();
  if (tagName === 'br') {
    return '\n';
  }
  if (tagName === 'a') {
    const label = Array.from(node.childNodes).map(clipboardTextForNode).join('').trim();
    if (node.dataset.commandLink) return label;
    const href = safeUrl(node.getAttribute('href') || node.href || '');
    if (!href) return label;
    if (!label || label === href) return href;
    return `${label} (${href})`;
  }

  let text = Array.from(node.childNodes).map(clipboardTextForNode).join('');
  if (tagName === 'article' || tagName === 'div' || tagName === 'p' || tagName === 'pre') {
    text = text.replace(/[ \t]+\n/g, '\n');
    if (text && !text.endsWith('\n')) text += '\n';
  }
  return text;
}

function clipboardTextForSelection(selection) {
  const chunks = [];
  for (let index = 0; index < selection.rangeCount; index += 1) {
    const fragment = selection.getRangeAt(index).cloneContents();
    chunks.push(Array.from(fragment.childNodes).map(clipboardTextForNode).join(''));
  }
  return chunks.join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

transcript.addEventListener('copy', (event) => {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return;

  const copiedText = clipboardTextForSelection(selection);
  if (!copiedText) return;

  event.clipboardData.setData('text/plain', copiedText);
  event.preventDefault();
});

transcript.addEventListener('click', (event) => {
  const target = event.target instanceof Element ? event.target : event.target.parentElement;
  const link = target?.closest('a[data-command-link]');
  if (!link || !transcript.contains(link)) return;

  event.preventDefault();
  const command = decodeURIComponent(link.dataset.commandLink || '').trim();
  if (command) {
    submitPromptText(command);
  }
});

function transcriptIsNearBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 64;
}

function scrollTranscriptToBottom() {
  transcript.scrollTop = transcript.scrollHeight;
}

function appendMessage(role, text, className = '', options = {}) {
  const shouldScroll = options.forceScroll || transcriptIsNearBottom();
  const row = document.createElement('article');
  row.className = `message ${role} ${className}`.trim();

  const roleNode = document.createElement('div');
  roleNode.className = 'role';
  roleNode.textContent = role;

  const content = document.createElement('div');
  content.className = 'content';
  content.innerHTML = markdownToHtml(text);

  row.append(roleNode, content);
  transcript.append(row);
  if (shouldScroll) scrollTranscriptToBottom();
  return row;
}

function ensureDraftNode() {
  if (!state.draftNode) {
    state.draftNode = appendMessage('twag', '', 'draft', { forceScroll: transcriptIsNearBottom() });
  }
  return state.draftNode;
}

function ensureAnswerNode() {
  const row = ensureDraftNode();
  const content = row.querySelector('.content');
  let answer = content.querySelector(':scope > .answer-content');
  if (!answer) {
    answer = document.createElement('div');
    answer.className = 'answer-content';
    const existingHtml = content.innerHTML;
    const existingText = content.textContent || '';
    content.innerHTML = '';
    if (existingHtml && existingText.trim()) {
      answer.innerHTML = existingHtml;
    }
    content.prepend(answer);
  }
  return answer;
}

function renderDraftNow() {
  if (!state.draftNode) return;

  const content = ensureAnswerNode();
  const raw = content.dataset.raw || '';
  if (content.dataset.renderedRaw === raw) return;

  const shouldScroll = transcriptIsNearBottom();
  const beforeBottom = transcript.scrollHeight - transcript.scrollTop;
  content.innerHTML = markdownToHtml(raw);
  content.dataset.renderedRaw = raw;

  if (shouldScroll) scrollTranscriptToBottom();
  else transcript.scrollTop = transcript.scrollHeight - beforeBottom;
}

function scheduleDraftRender() {
  if (state.draftRenderFrame) return;
  state.draftRenderFrame = window.requestAnimationFrame(() => {
    state.draftRenderFrame = 0;
    renderDraftNow();
  });
}

function setDraft(text, mode) {
  const content = ensureAnswerNode();
  const previous = content.dataset.raw || '';
  const next = mode === 'append' ? previous + text : text;
  if (next === previous) return;
  content.dataset.raw = next;
  scheduleDraftRender();
}

function renderDetailNow() {
  if (!state.detailBlock) return;

  const content = state.detailBlock.querySelector('.detail-content');
  const raw = content.dataset.raw || '';
  if (content.dataset.renderedRaw === raw) return;

  const shouldScroll = transcriptIsNearBottom();
  const beforeBottom = transcript.scrollHeight - transcript.scrollTop;
  content.textContent = raw;
  content.dataset.renderedRaw = raw;

  if (shouldScroll) scrollTranscriptToBottom();
  else transcript.scrollTop = transcript.scrollHeight - beforeBottom;
}

function scheduleDetailRender() {
  if (state.detailRenderFrame) return;
  state.detailRenderFrame = window.requestAnimationFrame(() => {
    state.detailRenderFrame = 0;
    renderDetailNow();
  });
}

function appendDetail(text, expanded = false) {
  if (!state.detailBlock) {
    const row = ensureDraftNode();
    const content = row.querySelector('.content');
    ensureAnswerNode();

    const details = document.createElement('details');
    details.className = 'detail-details';
    details.open = Boolean(expanded);

    const summary = document.createElement('summary');
    summary.textContent = 'detail';

    const pre = document.createElement('pre');
    pre.className = 'detail-content';

    details.append(summary, pre);
    content.append(details);
    state.detailBlock = details;
  }

  const content = state.detailBlock.querySelector('.detail-content');
  const previous = content.dataset.raw || '';
  const next = previous + String(text || '');
  if (next === previous) return;
  content.dataset.raw = next;
  scheduleDetailRender();
}

function finishDetail() {
  if (state.detailRenderFrame) {
    window.cancelAnimationFrame(state.detailRenderFrame);
    state.detailRenderFrame = 0;
    renderDetailNow();
  }
  state.detailBlock = null;
}

function collapseDetail() {
  if (state.detailRenderFrame) {
    window.cancelAnimationFrame(state.detailRenderFrame);
    state.detailRenderFrame = 0;
    renderDetailNow();
  }
  if (state.detailBlock) {
    state.detailBlock.open = false;
  }
}

function clearDraft() {
  if (state.draftRenderFrame) {
    window.cancelAnimationFrame(state.draftRenderFrame);
    state.draftRenderFrame = 0;
  }
  if (state.draftNode) {
    state.draftNode.remove();
    state.draftNode = null;
  }
  finishDetail();
}

function socketIsOpen() {
  return navigator.onLine && state.socket && state.socket.readyState === WebSocket.OPEN;
}

function send(payload) {
  if (!socketIsOpen()) {
    setConnection('closed');
    appendStatus('Disconnected. Use reconnect, then send again.');
    appendMessage('system', 'Disconnected. Press reconnect, then send again.');
    promptInput.focus();
    return false;
  }
  try {
    state.socket.send(JSON.stringify(payload));
    return true;
  } catch {
    setConnection('closed');
    appendStatus('Disconnected. Use reconnect, then send again.');
    appendMessage('system', 'Disconnected. Press reconnect, then send again.');
    promptInput.focus();
    return false;
  }
}

function restoreInFlightPrompt() {
  const text = String(state.inFlightPrompt || '').trim();
  if (!text || promptInput.value.trim()) return;
  promptInput.value = text;
  appendStatus('Restored the unsent prompt after disconnect.');
}

function submitPromptText(rawText) {
  const text = String(rawText || '').trim();
  if (!text) return false;

  if (!socketIsOpen()) {
    send({ type: 'message', text });
    return false;
  }

  const cityMatch = text.match(/^\/city\s+(.+)$/i);
  if (cityMatch) {
    if (send({ type: 'set_city', city: cityMatch[1].trim() })) {
      addCommandHistory(text);
      promptInput.value = '';
      return true;
    }
    return false;
  }

  clearDraft();
  finishDetail();
  state.inFlightPrompt = text;
  if (!send({ type: 'message', text })) return false;
  addCommandHistory(text);
  promptInput.value = '';
  appendMessage('user', text, '', { forceScroll: true });
  appendStatus('Sent. Waiting for TWAG...');
  return true;
}

function readOperatorToken() {
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  const query = new URLSearchParams(window.location.search);
  const hashHadToken = hash.has('token');
  const token = hash.get('token') || query.get('token') || window.localStorage.getItem('twagTerminalToken') || '';
  if (token) {
    window.localStorage.setItem('twagTerminalToken', token);
  }
  if (query.has('token') || hashHadToken) {
    query.delete('token');
    const nextSearch = query.toString();
    const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}`;
    window.history.replaceState(null, '', nextUrl);
  }
  return token;
}

async function createSession() {
  const params = new URLSearchParams(window.location.search);
  const city = state.city || params.get('city') || undefined;
  const headers = { 'content-type': 'application/json' };
  if (state.operatorToken) {
    headers['x-twag-terminal-token'] = state.operatorToken;
  }
  const response = await fetch(appPath('/sessions'), {
    method: 'POST',
    headers,
    body: JSON.stringify({ city }),
  });
  if (response.status === 401) {
    throw new Error('Operator token required. Open this terminal with #token=YOUR_TOKEN.');
  }
  if (!response.ok) {
    throw new Error(`Session create failed: HTTP ${response.status}`);
  }
  return response.json();
}

function connect(session, options = {}) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${window.location.host}${appPath(session.websocket)}`);
  socket.twagResumeExisting = Boolean(options.resumeExisting);
  socket.twagReceivedReady = false;
  state.socket = socket;

  socket.addEventListener('open', () => {
    setConnection('connected');
    appendStatus('Connected. Ask a Tech Week question.');
    promptInput.focus();
  });

  socket.addEventListener('message', (message) => {
    const event = JSON.parse(message.data);
    if (event.type === 'ready') {
      socket.twagReceivedReady = true;
      state.sessionId = event.session_id;
      setCity(event.city);
      setPublicLinks(event.links);
      setVerboseMode(event.verbose);
      setThinkingMode(event.thinking);
      if (event.backend_status) setBackendStatus(event.backend_status);
      appendStatus(`Ready. Session: ${event.session_id}`);
      if (event.greeting && !state.greetingShown) {
        appendMessage('twag', event.greeting, '', { forceScroll: true });
        state.greetingShown = true;
      }
    } else if (event.type === 'city') {
      setCity(event.city);
      setPublicLinks(event.links);
      appendStatus(event.message);
      appendMessage('system', event.message);
    } else if (event.type === 'mode') {
      setVerboseMode(event.verbose);
      setThinkingMode(event.thinking);
      appendStatus(
        `${state.verbose ? 'Verbose' : 'Quiet'} output; thinking ${state.thinking ? 'on' : 'off'}.`,
      );
    } else if (event.type === 'backend_status') {
      setBackendStatus(event.services || {});
    } else if (event.type === 'status') {
      appendStatus(event.step || event.text || '');
    } else if (event.type === 'delta') {
      setDraft(event.text || '', event.mode);
    } else if (event.type === 'detail_delta' || event.type === 'thinking_delta') {
      appendDetail(event.text || '', event.expanded);
    } else if (event.type === 'detail_done') {
      collapseDetail();
    } else if (event.type === 'final') {
      state.inFlightPrompt = '';
      finishDetail();
      const shouldScroll = transcriptIsNearBottom();
      if (state.draftNode) {
        if (state.draftRenderFrame) {
          window.cancelAnimationFrame(state.draftRenderFrame);
          state.draftRenderFrame = 0;
          renderDraftNow();
        }
        const row = state.draftNode;
        const content = ensureAnswerNode();
        const raw = event.text || content.dataset.raw || '';
        const beforeBottom = transcript.scrollHeight - transcript.scrollTop;
        row.classList.remove('draft');
        content.dataset.raw = raw;
        if (content.dataset.renderedRaw !== raw) {
          content.innerHTML = markdownToHtml(raw);
          content.dataset.renderedRaw = raw;
        }
        state.draftNode = null;
        if (shouldScroll) scrollTranscriptToBottom();
        else transcript.scrollTop = transcript.scrollHeight - beforeBottom;
      } else {
        appendMessage('twag', event.text || '', '', { forceScroll: false });
      }
      const tokenLine = event.usage?.total_tokens ? `\nTokens: ${event.usage.total_tokens}` : '';
      appendStatus(`Done. Duration: ${event.duration_ms || 0}ms${tokenLine}`);
    } else if (event.type === 'error') {
      clearDraft();
      finishDetail();
      state.inFlightPrompt = '';
      setConnection('error');
      const summary = errorSummary(event);
      appendStatus(summary);
      appendMessage('system', event.error || 'Unknown error.');
    }
  });

  socket.addEventListener('close', () => {
    if (state.socket !== socket) return;
    if (socket.twagResumeExisting && !socket.twagReceivedReady) {
      state.sessionId = '';
      appendStatus('Previous session expired. Starting a new session.');
      reconnect({ newSession: true });
      return;
    }
    setConnection('closed');
    restoreInFlightPrompt();
    if (state.inFlightPrompt) appendStatus('Disconnected before this query finished.');
    appendStatus('Disconnected. Use reconnect, then send again.');
  });

  socket.addEventListener('error', () => {
    if (state.socket !== socket) return;
    setConnection('error');
    restoreInFlightPrompt();
    if (state.inFlightPrompt) appendStatus('Connection error before this query finished.');
    appendStatus('Connection error. Use reconnect, then send again.');
  });
}

promptForm.addEventListener('submit', (event) => {
  event.preventDefault();
  submitPromptText(promptInput.value);
});

promptInput.addEventListener('keydown', (event) => {
  if (event.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key === 'ArrowUp') {
    if (navigateCommandHistory(-1)) event.preventDefault();
  } else if (event.key === 'ArrowDown') {
    if (navigateCommandHistory(1)) event.preventDefault();
  }
});

commandButtons.forEach((button) => {
  button.addEventListener('click', () => {
    promptInput.value = button.dataset.command || '';
    promptInput.focus();
    promptForm.requestSubmit();
  });
});

cityButtons.forEach((button) => {
  button.addEventListener('click', () => {
    const nextCity = String(button.dataset.city || '').trim();
    if (!nextCity || nextCity === state.city) return;
    if (!send({ type: 'set_city', city: nextCity })) return;
    appendStatus(`Switching city to ${button.textContent.trim()}...`);
    promptInput.focus();
  });
});

externalViewButtons.forEach((button) => {
  button.addEventListener('click', () => {
    openExternalView(button.dataset.openView || '');
    promptInput.focus();
  });
});

modeButtons.forEach((button) => {
  button.addEventListener('click', () => {
    const mode = String(button.dataset.mode || '').trim();
    const next = mode === 'verbose';
    if (next === state.verbose) return;
    setVerboseMode(next);
    if (!send({ type: 'set_mode', verbose: next })) {
      setVerboseMode(!next);
    }
    promptInput.focus();
  });
});

thinkingButtons.forEach((button) => {
  button.addEventListener('click', () => {
    const next = button.dataset.thinking === 'on';
    if (next === state.thinking) return;
    setThinkingMode(next);
    if (!send({ type: 'set_mode', thinking: next })) {
      setThinkingMode(!next);
    }
    promptInput.focus();
  });
});

async function reconnect(options = {}) {
  try {
    if (state.socket && state.socket.readyState !== WebSocket.CLOSED) {
      state.socket.close();
    }
    setConnection('connecting');
    if (state.sessionId && !options.newSession) {
      appendStatus('Reconnecting to the current session...');
      connect(
        {
          session_id: state.sessionId,
          city: state.city,
          websocket: `/sessions/${state.sessionId}`,
        },
        { resumeExisting: true },
      );
      return;
    }
    appendStatus('Starting a new session...');
    const session = await createSession();
    setCity(session.city);
    connect(session);
  } catch (error) {
    setConnection('error');
    appendStatus(error.message || String(error));
    appendMessage('system', 'Could not reconnect. Try again.');
  }
}

connectionState.addEventListener('click', () => {
  if (shell.dataset.state === 'closed' || shell.dataset.state === 'error') {
    reconnect();
  }
});

connectionState.addEventListener('keydown', (event) => {
  if ((event.key === 'Enter' || event.key === ' ') &&
      (shell.dataset.state === 'closed' || shell.dataset.state === 'error')) {
    event.preventDefault();
    reconnect();
  }
});

window.addEventListener('offline', () => {
  setConnection('closed');
  appendStatus('Disconnected. Use reconnect, then send again.');
});

async function main() {
  try {
    setConnection('booting');
    statusText.textContent = '';
    appendStatus('Starting session...');
    state.operatorToken = readOperatorToken();
    await reconnect();
  } catch (error) {
    setConnection('error');
    appendStatus(error.message || String(error));
    appendMessage('system', 'Could not start a browser terminal session.');
  }
}

main();
