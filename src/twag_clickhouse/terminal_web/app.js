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
};

function setConnection(value) {
  shell.dataset.state = value;
  connectionState.textContent = value;
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
    .replace(/`([^`\n]+)`/g, (_match, code) => stash(`<code>${escapeHtml(code)}</code>`))
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_match, label, url) => stash(anchorHtml(url, label)));

  let html = escapeHtml(protectedText)
    .replace(/\*\*([^*\n][\s\S]*?[^*\n])\*\*/g, '<strong>$1</strong>')
  html = linkifyPlainUrls(html);

  for (const [token, value] of placeholders) {
    html = html.replace(escapeHtml(token), value);
  }

  return html;
}

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

function send(payload) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    appendMessage('system', 'The terminal is not connected.');
    return;
  }
  state.socket.send(JSON.stringify(payload));
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
  const city = params.get('city') || undefined;
  const headers = { 'content-type': 'application/json' };
  if (state.operatorToken) {
    headers['x-twag-terminal-token'] = state.operatorToken;
  }
  const response = await fetch('/sessions', {
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
  const socket = new WebSocket(`${protocol}//${window.location.host}${session.websocket}`);
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
      if (event.greeting) {
        appendMessage('twag', event.greeting, '', { forceScroll: true });
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
      clearDraft();
      appendMessage('twag', event.text || '', '', { forceScroll: shouldScroll });
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
    setConnection('closed');
    statusText.textContent = 'Disconnected. Refresh to create a new local session.';
  });

  socket.addEventListener('error', () => {
    setConnection('error');
    statusText.textContent = 'WebSocket error.';
  });
}

promptForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = promptInput.value.trim();
  if (!text) return;

  promptInput.value = '';
  const cityMatch = text.match(/^\/city\s+(.+)$/i);
  if (cityMatch) {
    send({ type: 'set_city', city: cityMatch[1].trim() });
    return;
  }

  clearDraft();
  appendMessage('user', text, '', { forceScroll: true });
  statusText.textContent = 'Sent. Waiting for TWAG...';
  send({ type: 'message', text });
});

commandButtons.forEach((button) => {
  button.addEventListener('click', () => {
    promptInput.value = button.dataset.command || '';
    promptInput.focus();
    promptForm.requestSubmit();
  });
});

async function main() {
  try {
    setConnection('booting');
    state.operatorToken = readOperatorToken();
    const session = await createSession();
    setCity(session.city);
    connect(session);
  } catch (error) {
    setConnection('error');
    statusText.textContent = error.message || String(error);
    appendMessage('system', 'Could not start a browser terminal session.');
  }
}

main();
