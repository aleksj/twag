#!/usr/bin/env node

import readline from 'node:readline';

const ANSI = {
  clear: '\x1b[2J\x1b[H',
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  dim: '\x1b[2m',
  green: '\x1b[32m',
  cyan: '\x1b[36m',
  yellow: '\x1b[33m',
  red: '\x1b[31m',
};

const serverUrl = process.env.TWAG_TERMINAL_URL || 'http://127.0.0.1:8765';
const initialCity = process.env.TWAG_CITY || process.argv.find((arg) => arg.startsWith('--city='))?.split('=')[1];

const state = {
  connected: false,
  city: initialCity || '',
  sessionId: '',
  status: 'Starting...',
  input: '',
  messages: [],
  draft: '',
  ws: null,
};

function markdownToAnsi(text) {
  return String(text || '')
    .replace(/\*\*([^*\n][\s\S]*?[^*\n])\*\*/g, `${ANSI.bold}$1${ANSI.reset}`)
    .replace(/`([^`\n]+)`/g, `${ANSI.cyan}$1${ANSI.reset}`);
}

function stripAnsi(text) {
  return String(text).replace(/\x1b\[[0-9;]*m/g, '');
}

function wrapLine(line, width) {
  const visible = stripAnsi(line);
  if (visible.length <= width) return [line];

  const words = line.split(/(\s+)/);
  const rows = [];
  let current = '';
  for (const word of words) {
    const candidate = current + word;
    if (stripAnsi(candidate).length > width && current) {
      rows.push(current.trimEnd());
      current = word.trimStart();
    } else {
      current = candidate;
    }
  }
  if (current) rows.push(current.trimEnd());
  return rows.length ? rows : [''];
}

function wrapText(text, width) {
  return String(text || '')
    .split('\n')
    .flatMap((line) => wrapLine(line, Math.max(20, width)));
}

function roleLabel(role) {
  if (role === 'user') return `${ANSI.green}you${ANSI.reset}`;
  if (role === 'assistant') return `${ANSI.cyan}twag${ANSI.reset}`;
  if (role === 'system') return `${ANSI.yellow}system${ANSI.reset}`;
  return role;
}

function render() {
  const width = process.stdout.columns || 100;
  const height = process.stdout.rows || 32;
  const bodyHeight = Math.max(8, height - 6);
  const header = `${ANSI.bold}TWAG Local Terminal${ANSI.reset} ${ANSI.dim}${serverUrl}${ANSI.reset} ${state.city ? `city=${state.city}` : ''}`;
  const status = `${ANSI.dim}${state.status || ''}${ANSI.reset}`;
  const input = `> ${state.input}`;

  const lines = [header, ''.padEnd(Math.min(width, 120), '-').slice(0, width)];
  for (const message of state.messages) {
    lines.push(`${roleLabel(message.role)}:`);
    lines.push(...wrapText(markdownToAnsi(message.text), width));
    lines.push('');
  }
  if (state.draft) {
    lines.push(`${roleLabel('assistant')} ${ANSI.dim}(streaming)${ANSI.reset}:`);
    lines.push(...wrapText(markdownToAnsi(state.draft), width));
    lines.push('');
  }

  const visibleBody = lines.slice(Math.max(0, lines.length - bodyHeight));
  const footer = [
    ''.padEnd(Math.min(width, 120), '-').slice(0, width),
    status.slice(0, width),
    `${ANSI.dim}/city nyc | /city boston | /verbose | /quiet | /map | /help | /exit${ANSI.reset}`,
    input,
  ];

  process.stdout.write(ANSI.clear + visibleBody.concat(footer).join('\n'));
}

function addMessage(role, text) {
  state.messages.push({ role, text: String(text || '') });
  if (state.messages.length > 100) state.messages = state.messages.slice(-100);
}

function send(payload) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    state.status = 'Not connected.';
    render();
    return;
  }
  state.ws.send(JSON.stringify(payload));
}

function submitInput() {
  const text = state.input.trim();
  state.input = '';
  if (!text) {
    render();
    return;
  }
  if (text === '/exit' || text === '/quit') {
    shutdown(0);
    return;
  }
  const cityMatch = text.match(/^\/city\s+(.+)$/i);
  if (cityMatch) {
    send({ type: 'set_city', city: cityMatch[1].trim() });
    render();
    return;
  }
  addMessage('user', text);
  state.draft = '';
  state.status = 'Sent. Waiting for TWAG...';
  send({ type: 'message', text });
  render();
}

function handleKey(str, key) {
  if (key?.ctrl && key.name === 'c') shutdown(0);
  if (key?.name === 'return') return submitInput();
  if (key?.name === 'backspace') {
    state.input = state.input.slice(0, -1);
    render();
    return;
  }
  if (key?.name === 'escape') {
    state.input = '';
    render();
    return;
  }
  if (str && !key?.ctrl && !key?.meta && str >= ' ') {
    state.input += str;
    render();
  }
}

async function createSession() {
  const response = await fetch(`${serverUrl}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ city: initialCity || undefined }),
  });
  if (!response.ok) {
    throw new Error(`session create failed: HTTP ${response.status} ${await response.text()}`);
  }
  return response.json();
}

function connectWebSocket(session) {
  const wsUrl = serverUrl.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:') + session.websocket;
  const ws = new WebSocket(wsUrl);
  state.ws = ws;

  ws.addEventListener('open', () => {
    state.connected = true;
    state.status = 'Connected.';
    render();
  });

  ws.addEventListener('message', (message) => {
    const event = JSON.parse(message.data);
    if (event.type === 'ready') {
      state.sessionId = event.session_id;
      state.city = event.city;
      state.status = `Ready. Session ${event.session_id}.`;
    } else if (event.type === 'city') {
      state.city = event.city;
      state.status = event.message;
      addMessage('system', event.message);
    } else if (event.type === 'status') {
      state.status = event.step || event.text || '';
    } else if (event.type === 'delta') {
      if (event.mode === 'append') state.draft += event.text || '';
      else state.draft = event.text || '';
    } else if (event.type === 'final') {
      state.draft = '';
      addMessage('assistant', event.text || '');
      const tokens = event.usage?.total_tokens ? ` ${event.usage.total_tokens} tokens.` : '';
      state.status = `Done in ${event.duration_ms || 0}ms.${tokens}`;
    } else if (event.type === 'error') {
      state.draft = '';
      addMessage('system', `Error: ${event.error || 'unknown error'}`);
      state.status = 'Error.';
    }
    render();
  });

  ws.addEventListener('close', () => {
    state.connected = false;
    state.status = 'Disconnected.';
    render();
  });

  ws.addEventListener('error', () => {
    state.status = 'WebSocket error.';
    render();
  });
}

function shutdown(code) {
  try {
    if (state.ws) state.ws.close();
  } catch {
    // Ignore shutdown errors.
  }
  if (process.stdin.isTTY) process.stdin.setRawMode(false);
  process.stdout.write(`${ANSI.reset}\n`);
  process.exit(code);
}

async function main() {
  readline.emitKeypressEvents(process.stdin);
  if (process.stdin.isTTY) process.stdin.setRawMode(true);
  process.stdin.on('keypress', handleKey);
  process.stdout.on('resize', render);

  render();
  try {
    const session = await createSession();
    state.city = session.city;
    connectWebSocket(session);
  } catch (error) {
    state.status = String(error.message || error);
    addMessage('system', `Could not connect to ${serverUrl}. Start the backend with twag terminal-server.`);
    render();
  }
}

main();
