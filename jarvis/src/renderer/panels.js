const BASE = window.api?.baseUrl || 'http://127.0.0.1:8000';

const panel = document.getElementById('dynamic-panel');
const panelTitle = document.getElementById('panel-title');
const panelContent = document.getElementById('panel-content');
document.getElementById('panel-close').addEventListener('click', hidePanel);

function showPanel(title, html) {
  panelTitle.textContent = title;
  panelContent.innerHTML = html;
  panel.classList.remove('hidden');
}

export function hidePanel() {
  panel.classList.add('hidden');
}

// 슬래시 명령어에서 직접 호출
export async function showChartPanelDirect(code, candleType = 'daily') {
  return showChartPanel(code, candleType);
}
export async function showWatchPanelDirect() {
  return showWatchPanel();
}
export async function showIndicatorPanelDirect(code) {
  return showIndicatorPanel(code);
}

// AI 응답 텍스트 분석 → 관련 패널 자동 표시
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
  }
}

async function showWatchPanel() {
  try {
    const res = await fetch(`${BASE}/ai/watches`);
    const data = await res.json();
    const watches = data.watches || {};
    if (!Object.keys(watches).length) {
      showPanel('◈ 감시 종목', '<div style="color:var(--text-dim);font-size:12px">설정된 감시 종목 없음</div>');
      return;
    }
    const html = Object.entries(watches).map(([code, w]) => `
      <div class="watch-item">
        <div class="watch-code">${code} <span style="color:var(--text-dim);font-size:11px">${w.stock_name || ''}</span></div>
        ${(w.conditions || []).map(c => `
          <div class="watch-formula">${c.formula || `${c.type} ${c.threshold ?? ''}`}</div>
        `).join('')}
        <div style="font-size:10px;color:var(--text-dim);margin-top:4px">
          기준가 $${Number(w.baseline_price || 0).toFixed(2)}
        </div>
      </div>
    `).join('');
    showPanel('◈ 감시 종목', html);
  } catch {
    showPanel('◈ 감시 종목', '<div style="color:var(--danger)">데이터 로드 실패</div>');
  }
}

async function showPortfolioPanel() {
  try {
    const res = await fetch(`${BASE}/trade/positions/live`);
    const data = await res.json();
    const positions = data.positions || [];
    if (!positions.length) {
      showPanel('◈ 보유 포지션', '<div style="color:var(--text-dim);font-size:12px">보유 포지션 없음</div>');
      return;
    }
    const html = positions.map(p => {
      const pnl = p.unrealized_pnl_pct ?? 0;
      const cls = pnl >= 0 ? 'up' : 'down';
      return `
        <div class="panel-row">
          <div>
            <div style="color:var(--accent);font-weight:700">${p.stock_code}</div>
            <div class="panel-label">${p.quantity}주 @ ${p.entry_price}</div>
          </div>
          <div class="panel-value ${cls}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%</div>
        </div>
      `;
    }).join('');
    showPanel('◈ 보유 포지션', html);
  } catch {
    showPanel('◈ 보유 포지션', '<div style="color:var(--danger)">데이터 로드 실패</div>');
  }
}

async function showChartPanel(textOrCode, candleType = 'daily') {
  const codeMatch = /^[A-Z]{2,6}$/.test(textOrCode)
    ? [null, textOrCode]
    : (textOrCode.match(/\b([A-Z]{2,5})\b/) || textOrCode.match(/\b(\d{6})\b/));
  const code = codeMatch?.[1] || 'NVDA';
  const count = candleType === 'minute' ? 60 : 30;
  const label = candleType === 'minute' ? '분봉' : '일봉';

  showPanel(`◈ ${code} ${label} 차트`, `
    <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px" id="chart-meta"></div>
    <div id="lw-chart" style="height:240px;width:100%"></div>
  `);

  try {
    const res = await fetch(`${BASE}/ai/candles/${code}?candle_type=${candleType}&count=${count}`);
    const data = await res.json();
    const candles = (data.candles || []).filter(c => c.close > 0);
    if (!candles.length) {
      document.getElementById('lw-chart').innerHTML = '<div style="color:var(--text-dim);padding:8px">데이터 없음</div>';
      return;
    }

    const LW = window.LightweightCharts;
    if (!LW) throw new Error('LightweightCharts 미로드');
    const container = document.getElementById('lw-chart');
    if (!container) return;

    const chart = LW.createChart(container, {
      width: container.clientWidth || 330,
      height: 240,
      layout: { background: { color: '#040d1a' }, textColor: '#4a7a99' },
      grid: {
        vertLines: { color: 'rgba(0,180,255,0.06)' },
        horzLines: { color: 'rgba(0,180,255,0.06)' },
      },
      timeScale: { borderColor: 'rgba(0,180,255,0.2)', timeVisible: true },
      rightPriceScale: { borderColor: 'rgba(0,180,255,0.2)' },
      crosshair: { vertLine: { color: '#00d4ff44' }, horzLine: { color: '#00d4ff44' } },
    });

    const series = chart.addSeries(LW.CandlestickSeries, {
      upColor: '#00ff99',
      downColor: '#ff4466',
      borderUpColor: '#00ff99',
      borderDownColor: '#ff4466',
      wickUpColor: '#00ff99',
      wickDownColor: '#ff4466',
    });

    const chartData = candles.map(c => ({
      time: c.datetime.slice(0, 10),
      open: c.open, high: c.high, low: c.low, close: c.close,
    }));
    series.setData(chartData);
    chart.timeScale().fitContent();

    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    const chg = prev ? ((last.close - prev.close) / prev.close * 100).toFixed(2) : '0.00';
    const cls = Number(chg) >= 0 ? 'color:#00ff99' : 'color:#ff4466';
    const meta = document.getElementById('chart-meta');
    if (meta) meta.innerHTML = `
      <span style="color:var(--accent);font-weight:700">${code}</span>
      &nbsp;${last.close.toFixed(2)}
      &nbsp;<span style="${cls}">${Number(chg) >= 0 ? '+' : ''}${chg}%</span>
      &nbsp;<span style="color:var(--text-dim)">${count}봉</span>`;
  } catch(e) {
    const el = document.getElementById('lw-chart');
    if (el) el.innerHTML = `<div style="color:var(--danger);padding:8px">로드 실패: ${e.message}</div>`;
  }
}

async function showIndicatorPanel(textOrCode) {
  const codeMatch = /^[A-Z]{2,6}$/.test(textOrCode)
    ? [null, textOrCode]
    : (textOrCode.match(/\b([A-Z]{2,5})\b/) || textOrCode.match(/\b(\d{6})\b/));
  const code = codeMatch?.[1] || 'NVDA';
  try {
    const res = await fetch(`${BASE}/ai/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: `get_indicators ${code}` }),
    });
    const data = await res.json();
    // indicators 직접 Redis에서 fetch
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
        <span class="panel-value">${Number(ind[k]).toFixed(2)}</span>
      </div>
    ` : '').join('');

    showPanel(`◈ ${code} 기술 지표`, gauges + (mas ? `<div style="margin-top:10px">${mas}</div>` : ''));
  } catch {
    showPanel('◈ 기술 지표', '<div style="color:var(--danger)">데이터 로드 실패</div>');
  }
}
