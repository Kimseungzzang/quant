import { JarvisSphere } from './sphere.js';
import { detectAndShowPanel, hidePanel, showChartPanelDirect, showWatchPanelDirect, showIndicatorPanelDirect, showPnlPanelDirect, onPriceTick } from './panels.js';

const BASE = window.api?.baseUrl || 'http://127.0.0.1:8000';

// ── 구체 초기화
const canvas = document.getElementById('sphere-canvas');
canvas.width = canvas.clientWidth * window.devicePixelRatio;
canvas.height = canvas.clientHeight * window.devicePixelRatio;
const sphere = new JarvisSphere(canvas);

// ── 상태 표시
const aiStateEl = document.getElementById('ai-state');
const modeBadge = document.getElementById('mode-badge');
const connDot = document.getElementById('connection-dot');
const timeEl = document.getElementById('time-display');

function setSphereState(state) {
  sphere.setState(state);
  aiStateEl.className = state === 'idle' ? '' : state;
  const labels = { idle: 'STANDBY', thinking: 'PROCESSING...', speaking: 'RESPONDING' };
  aiStateEl.textContent = labels[state] || 'STANDBY';
}

// 시간 업데이트
setInterval(() => {
  timeEl.textContent = new Date().toLocaleTimeString('ko-KR', { hour12: false });
}, 1000);

// 헬스 체크
async function checkHealth() {
  try {
    const res = await fetch(`${BASE}/health`);
    const data = await res.json();
    connDot.className = 'dot connected';
    const mode = data.mode || 'live';
    modeBadge.textContent = `■ ${mode.toUpperCase()}`;
    modeBadge.className = `badge ${mode}`;
  } catch {
    connDot.className = 'dot';
  }
}
checkHealth();
setInterval(checkHealth, 10000);

// ── 채팅
const messagesEl = document.getElementById('chat-messages');
const inputEl = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');

function addMessage(role, text) {
  const msg = document.createElement('div');
  msg.className = `msg ${role}`;
  const avatarText = role === 'ai' ? 'J' : 'U';
  msg.innerHTML = `
    <div class="msg-avatar">${avatarText}</div>
    <div class="msg-bubble">${escHtml(text)}</div>
  `;
  messagesEl.appendChild(msg);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return msg;
}

function addTyping() {
  const msg = document.createElement('div');
  msg.className = 'msg ai';
  msg.id = 'typing-indicator';
  msg.innerHTML = `
    <div class="msg-avatar">J</div>
    <div class="msg-bubble">
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
    </div>
  `;
  messagesEl.appendChild(msg);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeTyping() {
  document.getElementById('typing-indicator')?.remove();
}

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const SLASH_HELP = `사용 가능한 명령어:
/clear          — 채팅 초기화
/chart [CODE]   — 일봉 차트  (예: /chart NVDA)
/min [CODE]     — 분봉 차트  (예: /min AAPL)
/watch          — 감시 종목 목록
/ind [CODE]     — 기술 지표  (예: /ind NVDA)
/pnl            — 수익률 패널
/help           — 이 목록`;

async function handleSlashCommand(text) {
  const [cmd, ...args] = text.trim().split(/\s+/);
  const code = args[0]?.toUpperCase() || 'NVDA';

  switch (cmd.toLowerCase()) {
    case '/help':
      addMessage('ai', SLASH_HELP);
      return true;
    case '/clear':
      messagesEl.innerHTML = '';
      hidePanel();
      addMessage('ai', '채팅 기록을 초기화했습니다.');
      return true;
    case '/chart':
      await showChartPanelDirect(code, 'daily');
      addMessage('ai', `${code} 일봉 차트를 표시합니다.`);
      return true;
    case '/min':
      await showChartPanelDirect(code, 'minute');
      addMessage('ai', `${code} 5분봉 차트를 표시합니다.`);
      return true;
    case '/watch':
      await showWatchPanelDirect();
      addMessage('ai', '감시 종목 목록을 표시합니다.');
      return true;
    case '/ind':
      await showIndicatorPanelDirect(code);
      addMessage('ai', `${code} 기술 지표를 표시합니다.`);
      return true;
    case '/pnl':
      await showPnlPanelDirect();
      addMessage('ai', '수익률 패널을 표시합니다.');
      return true;
    case '/mode': {
      const mode = args[0] || 'paper';
      try {
        const res = await fetch(`${BASE}/ai/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: `set_trading_mode ${mode}` }),
        });
        const d = await res.json();
        addMessage('ai', d.response || `모드를 ${mode}로 변경했습니다.`);
      } catch { addMessage('ai', `모드 변경 실패`); }
      return true;
    }
    default:
      return false;
  }
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  sendBtn.disabled = true;

  if (text.startsWith('/')) {
    addMessage('user', text);
    const handled = await handleSlashCommand(text);
    if (handled) { sendBtn.disabled = false; inputEl.focus(); return; }
  }

  addMessage('user', text);
  addTyping();
  setSphereState('thinking');

  try {
    const res = await fetch(`${BASE}/ai/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    const reply = data.response || data.error || '응답 없음';

    removeTyping();
    setSphereState('speaking');
    addMessage('ai', reply);

    // 동적 패널 감지
    await detectAndShowPanel(reply);

    setTimeout(() => setSphereState('idle'), 3000);
  } catch (err) {
    removeTyping();
    setSphereState('idle');
    addMessage('ai', `⚠ 연결 오류: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

// ── 슬래시 메뉴
const slashMenu = document.getElementById('slash-menu');
const COMMANDS = [
  { cmd: '/chart',  arg: '[종목]',   desc: '일봉 차트 표시' },
  { cmd: '/min',    arg: '[종목]',   desc: '분봉 차트 표시' },
  { cmd: '/watch',  arg: '',         desc: '감시 종목 목록' },
  { cmd: '/ind',    arg: '[종목]',   desc: '기술 지표 표시' },
  { cmd: '/pnl',    arg: '',         desc: '수익률 패널' },
  { cmd: '/clear',  arg: '',         desc: '채팅 초기화' },
  { cmd: '/help',   arg: '',         desc: '명령어 목록' },
];
let selectedIdx = -1;

function renderSlashMenu(filter) {
  const items = COMMANDS.filter(c => c.cmd.startsWith(filter));
  if (!items.length || filter === '') {
    // '/' 단독이면 전체 표시
    const all = filter === '/' ? COMMANDS : items;
    if (!all.length) { slashMenu.classList.add('hidden'); return; }
    slashMenu.innerHTML = all.map((c, i) => `
      <div class="slash-item" data-cmd="${c.cmd}" data-idx="${i}">
        <span class="slash-cmd">${c.cmd} <span style="color:var(--text-dim);font-weight:400">${c.arg}</span></span>
        <span class="slash-desc">${c.desc}</span>
      </div>`).join('');
    selectedIdx = -1;
    slashMenu.classList.remove('hidden');
    slashMenu.querySelectorAll('.slash-item').forEach(el => {
      el.addEventListener('mousedown', e => {
        e.preventDefault();
        inputEl.value = el.dataset.cmd + ' ';
        slashMenu.classList.add('hidden');
        inputEl.focus();
      });
    });
    return;
  }
  if (!items.length) { slashMenu.classList.add('hidden'); return; }
  slashMenu.innerHTML = items.map((c, i) => `
    <div class="slash-item" data-cmd="${c.cmd}" data-idx="${i}">
      <span class="slash-cmd">${c.cmd} <span style="color:var(--text-dim);font-weight:400">${c.arg}</span></span>
      <span class="slash-desc">${c.desc}</span>
    </div>`).join('');
  selectedIdx = -1;
  slashMenu.classList.remove('hidden');
  slashMenu.querySelectorAll('.slash-item').forEach(el => {
    el.addEventListener('mousedown', e => {
      e.preventDefault();
      inputEl.value = el.dataset.cmd + ' ';
      slashMenu.classList.add('hidden');
      inputEl.focus();
    });
  });
}

inputEl.addEventListener('input', () => {
  const val = inputEl.value;
  if (val === '/') { renderSlashMenu('/'); return; }
  if (val.startsWith('/') && !val.includes(' ')) {
    renderSlashMenu(val);
  } else {
    slashMenu.classList.add('hidden');
  }
});

sendBtn.addEventListener('click', sendMessage);
inputEl.addEventListener('keydown', e => {
  if (!slashMenu.classList.contains('hidden')) {
    const items = slashMenu.querySelectorAll('.slash-item');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIdx = Math.min(selectedIdx + 1, items.length - 1);
      items.forEach((el, i) => el.classList.toggle('selected', i === selectedIdx));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIdx = Math.max(selectedIdx - 1, 0);
      items.forEach((el, i) => el.classList.toggle('selected', i === selectedIdx));
      return;
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && selectedIdx >= 0)) {
      e.preventDefault();
      const sel = items[selectedIdx >= 0 ? selectedIdx : 0];
      if (sel) { inputEl.value = sel.dataset.cmd + ' '; slashMenu.classList.add('hidden'); }
      return;
    }
    if (e.key === 'Escape') { slashMenu.classList.add('hidden'); return; }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendMessage();
  }
});

// 시작 메시지
setTimeout(() => {
  setSphereState('speaking');
  addMessage('ai', 'JARVIS 온라인. 안녕하세요. 트레이딩을 시작하겠습니다.');
  setTimeout(() => setSphereState('idle'), 2500);
}, 500);

// ── 실시간 가격 스트림 (WebSocket)
function connectPriceStream() {
  const wsUrl = BASE.replace(/^http/, 'ws') + '/ws/stream';
  const ws = new WebSocket(wsUrl);
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'price' && msg.code) {
        onPriceTick(msg.code, msg.price, msg.ts);
      }
    } catch {}
  };
  ws.onclose = () => setTimeout(connectPriceStream, 3000);
}
connectPriceStream();

// ── 서버 알림 polling (이벤트/체결 알림)
let _lastNotifTs = Date.now() / 1000;
async function pollNotifications() {
  try {
    const res = await fetch(`${BASE}/ai/notifications?since=${_lastNotifTs}`);
    const items = await res.json();
    for (const item of items) {
      if (item._ts) _lastNotifTs = Math.max(_lastNotifTs, item._ts);
      if (item.type === 'ai_message') {
        setSphereState('speaking');
        addMessage('ai', `[${item.source}] ${item.message}`);
        setTimeout(() => setSphereState('idle'), 3000);
      } else if (item.type === 'fill_notice') {
        addMessage('ai', `🔔 ${item.message}`);
      }
    }
  } catch { /* 서버 미응답 시 무시 */ }
}
setInterval(pollNotifications, 3000);
