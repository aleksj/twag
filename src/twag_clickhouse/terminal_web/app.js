const shell = document.querySelector('.shell');
const transcript = document.querySelector('#transcript');
const statusText = document.querySelector('#statusText');
const connectionState = document.querySelector('#connectionState');
const cityState = document.querySelector('#cityState');
const promptForm = document.querySelector('#promptForm');
const promptInput = document.querySelector('#promptInput');
const commandButtons = document.querySelectorAll('[data-command]');

const state = {
  socket: null,
  sessionId: '',
  city: '',
  draftNode: null,
  operatorToken: '',
  greetingShown: false,
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
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text || '');
  return div.innerHTML;
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return parsed.href;
    }
  } catch {
    return '';
  }
  return '';
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
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_match, label, url) => stash(anchorHtml(url, label)));

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

function setDraft(text, mode) {
  const shouldScroll = transcriptIsNearBottom();
  if (!state.draftNode) {
    state.draftNode = appendMessage('twag', '', 'draft', { forceScroll: shouldScroll });
  }
  const content = state.draftNode.querySelector('.content');
  const previous = content.dataset.raw || '';
  const next = mode === 'append' ? previous + text : text;
  if (next === previous) return;
  content.dataset.raw = next;
  content.innerHTML = markdownToHtml(next);
  if (shouldScroll) scrollTranscriptToBottom();
}

function clearDraft() {
  if (state.draftNode) {
    state.draftNode.remove();
    state.draftNode = null;
  }
}

function socketIsOpen() {
  return navigator.onLine && state.socket && state.socket.readyState === WebSocket.OPEN;
}

function send(payload) {
  if (!socketIsOpen()) {
    setConnection('closed');
    statusText.textContent = 'Disconnected. Use reconnect, then send again.';
    appendMessage('system', 'Disconnected. Press reconnect, then send again.');
    promptInput.focus();
    return false;
  }
  try {
    state.socket.send(JSON.stringify(payload));
    return true;
  } catch {
    setConnection('closed');
    statusText.textContent = 'Disconnected. Use reconnect, then send again.';
    appendMessage('system', 'Disconnected. Press reconnect, then send again.');
    promptInput.focus();
    return false;
  }
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
      promptInput.value = '';
      return true;
    }
    return false;
  }

  clearDraft();
  if (!send({ type: 'message', text })) return false;
  promptInput.value = '';
  appendMessage('user', text, '', { forceScroll: true });
  statusText.textContent = 'Sent. Waiting for TWAG...';
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

function connect(session) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${window.location.host}${appPath(session.websocket)}`);
  state.socket = socket;

  socket.addEventListener('open', () => {
    setConnection('connected');
    statusText.textContent = 'Connected. Ask a Tech Week question.';
    promptInput.focus();
  });

  socket.addEventListener('message', (message) => {
    const event = JSON.parse(message.data);
    if (event.type === 'ready') {
      state.sessionId = event.session_id;
      setCity(event.city);
      statusText.textContent = `Ready.\nSession: ${event.session_id}`;
      if (event.greeting && !state.greetingShown) {
        appendMessage('twag', event.greeting, '', { forceScroll: true });
        state.greetingShown = true;
      }
    } else if (event.type === 'city') {
      setCity(event.city);
      statusText.textContent = event.message;
      appendMessage('system', event.message);
    } else if (event.type === 'status') {
      statusText.textContent = event.text || event.step || '';
    } else if (event.type === 'delta') {
      setDraft(event.text || '', event.mode);
    } else if (event.type === 'final') {
      const shouldScroll = transcriptIsNearBottom();
      if (state.draftNode) {
        const row = state.draftNode;
        const content = row.querySelector('.content');
        row.classList.remove('draft');
        content.dataset.raw = event.text || content.dataset.raw || '';
        content.innerHTML = markdownToHtml(content.dataset.raw);
        state.draftNode = null;
        if (shouldScroll) scrollTranscriptToBottom();
      } else {
        appendMessage('twag', event.text || '', '', { forceScroll: shouldScroll });
      }
      const tokenLine = event.usage?.total_tokens ? `\nTokens: ${event.usage.total_tokens}` : '';
      statusText.textContent = `Done.\nDuration: ${event.duration_ms || 0}ms${tokenLine}`;
    } else if (event.type === 'error') {
      clearDraft();
      setConnection('error');
      statusText.textContent = event.error || 'Unknown error.';
      appendMessage('system', `Error: ${event.error || 'unknown error'}`);
    }
  });

  socket.addEventListener('close', () => {
    if (state.socket !== socket) return;
    setConnection('closed');
    statusText.textContent = 'Disconnected. Use reconnect, then send again.';
  });

  socket.addEventListener('error', () => {
    if (state.socket !== socket) return;
    setConnection('error');
    statusText.textContent = 'Connection error. Use reconnect, then send again.';
  });
}

promptForm.addEventListener('submit', (event) => {
  event.preventDefault();
  submitPromptText(promptInput.value);
});

commandButtons.forEach((button) => {
  button.addEventListener('click', () => {
    promptInput.value = button.dataset.command || '';
    promptInput.focus();
    promptForm.requestSubmit();
  });
});

async function reconnect() {
  try {
    if (state.socket && state.socket.readyState !== WebSocket.CLOSED) {
      state.socket.close();
    }
    setConnection('connecting');
    statusText.textContent = 'Reconnecting...';
    const session = await createSession();
    setCity(session.city);
    connect(session);
  } catch (error) {
    setConnection('error');
    statusText.textContent = error.message || String(error);
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
  statusText.textContent = 'Disconnected. Use reconnect, then send again.';
});

async function main() {
  try {
    setConnection('booting');
    state.operatorToken = readOperatorToken();
    await reconnect();
  } catch (error) {
    setConnection('error');
    statusText.textContent = error.message || String(error);
    appendMessage('system', 'Could not start a browser terminal session.');
  }
}

main();
