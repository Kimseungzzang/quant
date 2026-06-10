const BASE = window.api?.baseUrl || 'http://127.0.0.1:8000';

// "2026-06-10 14:32:00" → unix seconds (KST를 UTC처럼 취급해 표시 시각 보존)
function datetimeToUnix(dt) {
  const [date, time = '00:00:00'] = dt.split(' ');
  const [y, mo, d] = date.split('-').map(Number);
  const [h, mi, s] = time.split(':').map(Number);
  return Math.floor(Date.UTC(y, mo - 1, d, h, mi, s) / 1000);
}

// ISO timestamp → 분 버킷 (datetimeToUnix와 동일 기준)
function tsToMinuteBucket(ts) {
  const dt = (ts || '').replace('T', ' ').slice(0, 16) + ':00';
  return datetimeToUnix(dt);
}

const container = document.getElementById('panels-container');

// 패널 타입별로 하나씩 관리
const _panels = new Map();       // type → panel element
const _refreshTimers = new Map(); // type → intervalId
const _liveCharts = new Map();   // panelType → { code, series, currentCandle }

function showPanel(type, title, html) {
  let panel = _panels.get(type);
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'dynamic-panel';
    panel.dataset.type = type;
    container.appendChild(panel);
    _panels.set(type, panel);
  }
  panel.innerHTML = `
    <div class="panel-header">
      <span>${title}</span>
      <button class="panel-close">✕</button>
    </div>
    <div class="panel-content" data-panel-type="${type}">${html}</div>
  `;
  panel.querySelector('.panel-close').addEventListener('click', () => hidePanel(type));
}

function _getContent(type) {
  return document.querySelector(`.panel-content[data-panel-type="${type}"]`);
}

export function hidePanel(type) {
  if (type == null) {
    _panels.forEach((_, t) => _cleanupPanel(t));
    _panels.clear();
    return;
  }
  _cleanupPanel(type);
}

function _cleanupPanel(type) {
  const panel = _panels.get(type);
  if (panel) { panel.remove(); _panels.delete(type); }
  const tid = _refreshTimers.get(type);
  if (tid) { clearInterval(tid); _refreshTimers.delete(type); }
  _liveCharts.delete(type);
}

// 포지션 데이터 캐시 (avg_price, qty 등 — 틱으로 계산용)
const _positionCache = new Map(); // code → { qty, avgPrice, stockName }

// index.js의 WebSocket 가격 틱 → 실시간 차트 + 포지션 업데이트
export function onPriceTick(code, price, ts) {
  // 분봉 차트 현재봉 업데이트
  for (const [panelType, state] of _liveCharts) {
    if (state.code !== code) continue;
    const bucket = ts ? tsToMinuteBucket(ts) : Math.floor(Date.now() / 60000) * 60;
    const c = state.currentCandle;
    if (!c || c.time < bucket) {
      state.currentCandle = { time: bucket, open: price, high: price, low: price, close: price };
    } else {
      c.high = Math.max(c.high, price);
      c.low  = Math.min(c.low,  price);
      c.close = price;
    }
    try {
      state.series.update(state.currentCandle);
      // 차트 헤더 가격도 동기화
      const meta = document.getElementById(`chart-meta-${panelType}`);
      if (meta && state.prevClose != null) {
        const chg = ((price - state.prevClose) / state.prevClose * 100).toFixed(2);
        const cls = Number(chg) >= 0 ? 'color:#00ff99' : 'color:#ff4466';
        const sign = Number(chg) >= 0 ? '+' : '';
        meta.innerHTML = `
          <span>
            <span style="color:var(--accent);font-weight:700">${state.code}</span>
            &nbsp;${Number(price).toLocaleString()}
            &nbsp;<span style="${cls}">${sign}${chg}%</span>
          </span>
          <span style="color:#00ff99;font-size:10px">● LIVE</span>
        `;
      }
    } catch {}
  }

  // 포지션 패널 현재가/손익 즉시 업데이트
  const pos = _positionCache.get(code);
  if (!pos || !_panels.has('portfolio')) return;
  const el = document.getElementById(`pos-row-${code}`);
  if (!el) return;
  const pnlAmt = (price - pos.avgPrice) * pos.qty;
  const pnlPct = pos.avgPrice > 0 ? (price - pos.avgPrice) / pos.avgPrice * 100 : 0;
  const cls = pnlPct >= 0 ? '#00ff99' : '#ff4466';
  const sign = pnlPct >= 0 ? '+' : '';
  el.querySelector('.pos-current').textContent = `현재가 ${Number(price).toLocaleString()}원`;
  el.querySelector('.pos-pnl-pct').textContent = `${sign}${pnlPct.toFixed(2)}%`;
  el.querySelector('.pos-pnl-pct').style.color = cls;
  el.querySelector('.pos-pnl-amt').textContent = `${sign}${Number(pnlAmt).toLocaleString()}원`;
  el.querySelector('.pos-pnl-amt').style.color = cls;
}

export async function showChartPanelDirect(code, candleType = 'daily') {
  return showChartPanel(code, candleType);
}
export async function showWatchPanelDirect() {
  return showWatchPanel();
}
export async function showIndicatorPanelDirect(code) {
  return showIndicatorPanel(code);
}
export async function showPnlPanelDirect() {
  return showPnlPanel();
}

export async function detectAndShowPanel(text) {
  const t = text.toLowerCase();
  if (t.includes('그래프') || t.includes('차트') || t.includes('chart') || t.includes('일봉') || t.includes('분봉')) {
    const candleType = t.includes('분봉') ? 'minute' : 'daily';
    await showChartPanel(text, candleType);
  } else if (t.includes('감시') || t.includes('watch') || t.includes('watches')) {
    await showWatchPanel();
  } else if (t.includes('포지션') || t.includes('position') || t.includes('보유')) {
    await showPortfolioPanel();
  } else if (t.includes('rsi') || t.includes('지표') || t.includes('볼린저') || t.includes('indicator')) {
    await showIndicatorPanel(text);
  } else if (t.includes('수익률') || t.includes('손익') || t.includes('pnl') || t.includes('승률')) {
    await showPnlPanel();
  }
}

// ── Watch 패널 ──────────────────────────────────────────────────────────────

async function showWatchPanel() {
  showPanel('watch', '◈ 감시 종목', '<div style="color:var(--text-dim);font-size:12px">로딩 중...</div>');
  try {
    const res = await fetch(`${BASE}/ai/watches`);
    const data = await res.json();
    const watches = data.watches || {};
    if (!Object.keys(watches).length) {
      showPanel('watch', '◈ 감시 종목', '<div style="color:var(--text-dim);font-size:12px">설정된 감시 종목 없음</div>');
      return;
    }
    const html = Object.entries(watches).map(([code, w]) => `
      <div class="watch-item">
        <div class="watch-code">${code} <span style="color:var(--text-dim);font-size:11px">${w.stock_name || ''}</span></div>
        ${(w.conditions || []).map(c => `
          <div class="watch-formula">${c.formula || `${c.type} ${c.threshold ?? ''}`}</div>
        `).join('')}
        <div style="font-size:10px;color:var(--text-dim);margin-top:4px">
          기준가 ${Number(w.baseline_price || 0).toLocaleString()}원
        </div>
      </div>
    `).join('');
    showPanel('watch', '◈ 감시 종목', html);
  } catch {
    showPanel('watch', '◈ 감시 종목', '<div style="color:var(--danger)">데이터 로드 실패</div>');
  }
}

// ── Portfolio 패널 (10초 자동 갱신) ────────────────────────────────────────

async function showPortfolioPanel() {
  showPanel('portfolio', '◈ 보유 포지션', '<div style="color:var(--text-dim);font-size:12px">로딩 중...</div>');
  const el = _getContent('portfolio');
  if (!el) return;
  try {
    const res = await fetch(`${BASE}/trade/positions/live`);
    const data = await res.json();
    const positions = Array.isArray(data) ? data : (data.positions || []);
    if (!positions.length) {
      el.innerHTML = '<div style="color:var(--text-dim);font-size:12px">보유 포지션 없음</div>';
      return;
    }
    // 포지션 캐시 갱신 (틱 업데이트용)
    _positionCache.clear();
    positions.forEach(p => {
      _positionCache.set(p.stock_code, {
        qty: p.quantity,
        avgPrice: p.avg_price ?? p.entry_price ?? 0,
        stockName: p.stock_name || '',
      });
    });
    el.innerHTML = positions.map(p => {
      const pnl = p.pnl_pct ?? 0;
      const pnlAmt = p.pnl ?? 0;
      const avgPrice = p.avg_price ?? 0;
      const curPrice = p.current_price ?? 0;
      const cls = pnl >= 0 ? '#00ff99' : '#ff4466';
      const sign = pnl >= 0 ? '+' : '';
      return `
        <div id="pos-row-${p.stock_code}" style="display:flex;flex-direction:column;gap:4px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:var(--accent);font-weight:700">${p.stock_code}
              <span style="color:var(--text-dim);font-size:11px;font-weight:400">${p.stock_name || ''}</span>
            </span>
            <span class="pos-pnl-pct" style="font-size:14px;font-weight:700;color:${cls}">${sign}${Number(pnl).toFixed(2)}%</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim)">
            <span>${p.quantity}주 &nbsp;평균 ${Number(avgPrice).toLocaleString()}원</span>
            <span class="pos-pnl-amt" style="color:${cls}">${sign}${Number(pnlAmt).toLocaleString()}원</span>
          </div>
          <div class="pos-current" style="font-size:11px;color:var(--text-dim)">현재가 <span style="color:var(--text)">${Number(curPrice).toLocaleString()}원</span></div>
        </div>
      `;
    }).join('<hr style="border-color:var(--border);margin:6px 0">');
  } catch {
    if (el) el.innerHTML = '<div style="color:var(--danger)">데이터 로드 실패</div>';
  }
}

// ── Chart 패널 (분봉: 실시간 틱 업데이트, 일봉: 30초 폴링) ───────────────

async function showChartPanel(textOrCode, candleType = 'daily') {
  const codeMatch = /^\d{6}$/.test(textOrCode) || /^[A-Z]{2,6}$/.test(textOrCode)
    ? [null, textOrCode]
    : (textOrCode.match(/\b(\d{6})\b/) || textOrCode.match(/\b([A-Z]{2,5})\b/));
  const code = codeMatch?.[1] || 'NVDA';
  const count = candleType === 'minute' ? 60 : 30;
  const label = candleType === 'minute' ? '5분봉' : '일봉';
  const panelType = `chart-${candleType}-${code}`;

  // 기존 타이머·라이브 상태 정리
  const existing = _refreshTimers.get(panelType);
  if (existing) { clearInterval(existing); _refreshTimers.delete(panelType); }
  _liveCharts.delete(panelType);

  showPanel(panelType, `◈ ${code} ${label} 차트`, `
    <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px;display:flex;justify-content:space-between" id="chart-meta-${panelType}">
      <span></span><span style="color:var(--text-dim);font-size:10px">● LIVE</span>
    </div>
    <div id="lw-chart-${panelType}" style="height:220px;width:100%"></div>
  `);

  try {
    const res = await fetch(`${BASE}/ai/candles/${code}?candle_type=${candleType}&count=${count}`);
    const data = await res.json();
    const candles = (data.candles || []).filter(c => c.close > 0);
    if (!candles.length) {
      const el = document.getElementById(`lw-chart-${panelType}`);
      if (el) el.innerHTML = '<div style="color:var(--text-dim);padding:8px">데이터 없음</div>';
      return;
    }

    const LW = window.LightweightCharts;
    if (!LW) throw new Error('LightweightCharts 미로드');
    const chartEl = document.getElementById(`lw-chart-${panelType}`);
    if (!chartEl) return;

    const chart = LW.createChart(chartEl, {
      width: chartEl.clientWidth || 330,
      height: 220,
      layout: { background: { color: '#040d1a' }, textColor: '#4a7a99' },
      grid: {
        vertLines: { color: 'rgba(0,180,255,0.06)' },
        horzLines: { color: 'rgba(0,180,255,0.06)' },
      },
      timeScale: { borderColor: 'rgba(0,180,255,0.2)', timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: 'rgba(0,180,255,0.2)' },
      crosshair: { vertLine: { color: '#00d4ff44' }, horzLine: { color: '#00d4ff44' } },
    });

    const series = chart.addSeries(LW.CandlestickSeries, {
      upColor: '#00ff99', downColor: '#ff4466',
      borderUpColor: '#00ff99', borderDownColor: '#ff4466',
      wickUpColor: '#00ff99', wickDownColor: '#ff4466',
    });

    const chartData = candles.map(c => ({
      time: candleType === 'minute' ? datetimeToUnix(c.datetime) : c.datetime.slice(0, 10),
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    series.setData(chartData);
    chart.timeScale().fitContent();

    _updateChartMeta(panelType, code, candles[candles.length - 1], candles[candles.length - 2]);

    if (candleType === 'minute') {
      // 분봉: 마지막 봉을 현재봉으로 등록 → onPriceTick이 실시간 업데이트
      const last = chartData[chartData.length - 1];
      const prev = chartData[chartData.length - 2];
      _liveCharts.set(panelType, { code, series, currentCandle: { ...last }, prevClose: prev?.close ?? last.open });
    } else {
      // 일봉: 30초마다 마지막 봉 갱신
      _refreshTimers.set(panelType, setInterval(async () => {
        try {
          const r = await fetch(`${BASE}/ai/candles/${code}?candle_type=daily&count=2`);
          const d = await r.json();
          const cs = (d.candles || []).filter(c => c.close > 0);
          if (!cs.length) return;
          const latest = cs[cs.length - 1];
          series.update({ time: latest.datetime.slice(0, 10), open: latest.open, high: latest.high, low: latest.low, close: latest.close });
          _updateChartMeta(panelType, code, latest, cs[cs.length - 2]);
        } catch {}
      }, 30000));
    }
  } catch(e) {
    const el = document.getElementById(`lw-chart-${panelType}`);
    if (el) el.innerHTML = `<div style="color:var(--danger);padding:8px">로드 실패: ${e.message}</div>`;
  }
}

function _updateChartMeta(panelType, code, last, prev) {
  const meta = document.getElementById(`chart-meta-${panelType}`);
  if (!meta || !last) return;
  const chg = prev ? ((last.close - prev.close) / prev.close * 100).toFixed(2) : '0.00';
  const cls = Number(chg) >= 0 ? 'color:#00ff99' : 'color:#ff4466';
  meta.innerHTML = `
    <span>
      <span style="color:var(--accent);font-weight:700">${code}</span>
      &nbsp;${Number(last.close).toLocaleString()}
      &nbsp;<span style="${cls}">${Number(chg) >= 0 ? '+' : ''}${chg}%</span>
    </span>
    <span style="color:#00ff99;font-size:10px">● LIVE</span>
  `;
}

// ── P&L 패널 ────────────────────────────────────────────────────────────────

async function showPnlPanel() {
  showPanel('pnl', '◈ 수익률', '<div style="color:var(--text-dim);font-size:12px">로딩 중...</div>');
  try {
    const res = await fetch(`${BASE}/trade/pnl`);
    const d = await res.json();

    const fmt = (v, currency = '원') => {
      const n = Number(v || 0);
      const sign = n >= 0 ? '+' : '';
      const cls = n >= 0 ? '#00ff99' : '#ff4466';
      return `<span style="color:${cls};font-weight:700">${sign}${n.toLocaleString()}${currency}</span>`;
    };

    const totalPnl = Number(d.totalRealizedPnl || 0);
    const todayPnl = Number(d.todayRealizedPnl || 0);
    const unrealized = Number(d.unrealizedPnl || 0);
    const winRate = Number(d.winRate || 0);
    const total = Number(d.totalTrades || 0);
    const wins = Number(d.winningTrades || 0);

    const html = `
      <div class="panel-row" style="margin-bottom:8px">
        <span class="panel-label">누적 실현</span>
        ${fmt(totalPnl)}
      </div>
      <div class="panel-row" style="margin-bottom:8px">
        <span class="panel-label">오늘 실현</span>
        ${fmt(todayPnl)}
      </div>
      <div class="panel-row" style="margin-bottom:8px">
        <span class="panel-label">미실현</span>
        ${fmt(unrealized)}
      </div>
      <hr style="border-color:var(--border);margin:8px 0">
      <div class="panel-row">
        <span class="panel-label">승률</span>
        <span style="color:var(--accent);font-weight:700">${winRate.toFixed(1)}%</span>
        <span style="color:var(--text-dim);font-size:11px;margin-left:4px">(${wins}/${total})</span>
      </div>
    `;
    showPanel('pnl', '◈ 수익률', html);
  } catch {
    showPanel('pnl', '◈ 수익률', '<div style="color:var(--danger)">데이터 로드 실패</div>');
  }
}

// ── Indicator 패널 ──────────────────────────────────────────────────────────

async function showIndicatorPanel(textOrCode) {
  const codeMatch = /^\d{6}$/.test(textOrCode) || /^[A-Z]{2,6}$/.test(textOrCode)
    ? [null, textOrCode]
    : (textOrCode.match(/\b(\d{6})\b/) || textOrCode.match(/\b([A-Z]{2,5})\b/));
  const code = codeMatch?.[1] || 'NVDA';
  showPanel('indicator', `◈ ${code} 기술 지표`, '<div style="color:var(--text-dim);font-size:12px">로딩 중...</div>');
  try {
    const indRes = await fetch(`${BASE}/ai/indicators/${code}`);
    const indData = await indRes.json();
    const ind = indData.indicators || {};

    const items = [
      { label: 'RSI(14)', key: 'rsi', min: 0, max: 100, warn_low: 30, warn_high: 70 },
      { label: '볼린저 %B', key: 'bb_pct', min: 0, max: 1, warn_low: 0.1, warn_high: 0.9 },
      { label: 'Stoch %K', key: 'stoch_k', min: 0, max: 100, warn_low: 20, warn_high: 80 },
    ];

    const gauges = items.map(item => {
      const val = ind[item.key];
      if (val == null) return '';
      const pct = Math.min(100, Math.max(0, ((val - item.min) / (item.max - item.min)) * 100));
      const cls = val <= item.warn_low ? 'oversold' : val >= item.warn_high ? 'overbought' : '';
      return `
        <div class="indicator-gauge">
          <div class="gauge-label"><span>${item.label}</span><span>${Number(val).toFixed(2)}</span></div>
          <div class="gauge-bar"><div class="gauge-fill ${cls}" style="width:${pct}%"></div></div>
        </div>
      `;
    }).join('');

    const mas = ['ma5','ma10','ma20','ma60'].map(k => ind[k] ? `
      <div class="panel-row">
        <span class="panel-label">${k.toUpperCase()}</span>
        <span class="panel-value">${Number(ind[k]).toLocaleString()}</span>
      </div>
    ` : '').join('');

    showPanel('indicator', `◈ ${code} 기술 지표`, gauges + (mas ? `<div style="margin-top:10px">${mas}</div>` : ''));
  } catch {
    showPanel('indicator', `◈ 기술 지표`, '<div style="color:var(--danger)">데이터 로드 실패</div>');
  }
}
