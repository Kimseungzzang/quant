import logging
from datetime import date
from pathlib import Path
from report.logger import TradeLogger

logger = logging.getLogger(__name__)

REPORTS_DIR = Path("data/reports")


class ReportGenerator:
    def __init__(self, trade_logger: TradeLogger):
        self.logger = trade_logger
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def generate_daily(self, target_date: date | None = None) -> Path:
        if target_date is None:
            target_date = date.today()
        date_str = target_date.isoformat()
        summary = self.logger.get_daily_summary(date_str)
        html = self._render_daily(date_str, summary)
        path = REPORTS_DIR / f"daily_{date_str}.html"
        path.write_text(html, encoding="utf-8")
        logger.info("일일 리포트 생성: %s", path)
        return path

    def generate_cumulative(self) -> Path:
        rows = self.logger.get_closed_positions()
        html = self._render_cumulative(rows)
        path = REPORTS_DIR / "cumulative.html"
        path.write_text(html, encoding="utf-8")
        logger.info("누적 리포트 생성: %s", path)
        return path

    # ── HTML 렌더링 ─────────────────────────────────────────────────────

    def _render_daily(self, date_str: str, summary: dict) -> str:
        trades = summary.get("details", [])
        rows_html = ""
        for t in trades:
            pnl = t["pnl_pct"]
            color = "#2ecc71" if pnl >= 0 else "#e74c3c"
            rows_html += f"""
            <tr>
                <td>{t['stock_code']}</td>
                <td>{t['name']}</td>
                <td>{t['exchange']}</td>
                <td>{t['qty']}</td>
                <td>{t['entry_price']:,.2f}</td>
                <td>{t['exit_price']:,.2f}</td>
                <td style="color:{color};font-weight:bold">{pnl:+.2f}%</td>
                <td>{t['close_reason']}</td>
                <td>{t['exit_at'][:16]}</td>
            </tr>"""

        total_pnl = summary.get("total_pnl", 0)
        pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"

        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>일일 매매 리포트 {date_str}</title>
<style>
  body {{ font-family: 'Malgun Gothic', sans-serif; margin: 20px; background: #f5f5f5; }}
  .card {{ background: white; padding: 20px; margin: 10px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,.1); }}
  .stat {{ display: inline-block; margin: 10px 20px; text-align: center; }}
  .stat .value {{ font-size: 2em; font-weight: bold; }}
  .stat .label {{ color: #666; font-size: .9em; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  th, td {{ padding: 8px 12px; border: 1px solid #ddd; text-align: center; }}
  th {{ background: #2c3e50; color: white; }}
  tr:nth-child(even) {{ background: #f9f9f9; }}
</style>
</head>
<body>
<h1>일일 매매 리포트</h1>
<p style="color:#666">{date_str}</p>

<div class="card">
  <div class="stat"><div class="value">{summary['trades']}</div><div class="label">총 거래</div></div>
  <div class="stat"><div class="value">{summary['wins']}</div><div class="label">수익 거래</div></div>
  <div class="stat"><div class="value">{summary['win_rate']}%</div><div class="label">승률</div></div>
  <div class="stat"><div class="value" style="color:{pnl_color}">{total_pnl:+.2f}%</div><div class="label">총 손익</div></div>
</div>

<div class="card">
  <h2>거래 내역</h2>
  <table>
    <tr>
      <th>종목코드</th><th>종목명</th><th>시장</th><th>수량</th>
      <th>매수가</th><th>매도가</th><th>손익</th><th>청산사유</th><th>청산시각</th>
    </tr>
    {rows_html or '<tr><td colspan="9">거래 내역 없음</td></tr>'}
  </table>
</div>
</body></html>"""

    def _render_cumulative(self, rows: list[dict]) -> str:
        if not rows:
            return "<html><body><p>데이터 없음</p></body></html>"

        wins = [r for r in rows if r["pnl_pct"] > 0]
        losses = [r for r in rows if r["pnl_pct"] <= 0]
        win_rate = round(len(wins) / len(rows) * 100, 1) if rows else 0
        total_pnl = round(sum(r["pnl_pct"] for r in rows), 2)
        avg_win = round(sum(r["pnl_pct"] for r in wins) / len(wins), 2) if wins else 0
        avg_loss = round(sum(r["pnl_pct"] for r in losses) / len(losses), 2) if losses else 0

        rows_html = ""
        for t in rows[:100]:
            pnl = t["pnl_pct"]
            color = "#2ecc71" if pnl >= 0 else "#e74c3c"
            rows_html += f"""
            <tr>
                <td>{t['stock_code']}</td><td>{t['name']}</td><td>{t['exchange']}</td>
                <td>{t['qty']}</td><td>{t['entry_price']:,.2f}</td><td>{t['exit_price']:,.2f}</td>
                <td style="color:{color};font-weight:bold">{pnl:+.2f}%</td>
                <td>{t['close_reason']}</td><td>{t['exit_at'][:10]}</td>
            </tr>"""

        pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"
        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>누적 매매 리포트</title>
<style>
  body {{ font-family: 'Malgun Gothic', sans-serif; margin: 20px; background: #f5f5f5; }}
  .card {{ background: white; padding: 20px; margin: 10px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,.1); }}
  .stat {{ display: inline-block; margin: 10px 20px; text-align: center; }}
  .stat .value {{ font-size: 2em; font-weight: bold; }}
  .stat .label {{ color: #666; font-size: .9em; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  th, td {{ padding: 8px 12px; border: 1px solid #ddd; text-align: center; }}
  th {{ background: #2c3e50; color: white; }}
  tr:nth-child(even) {{ background: #f9f9f9; }}
</style>
</head>
<body>
<h1>누적 매매 리포트</h1>
<div class="card">
  <div class="stat"><div class="value">{len(rows)}</div><div class="label">총 거래</div></div>
  <div class="stat"><div class="value">{win_rate}%</div><div class="label">승률</div></div>
  <div class="stat"><div class="value" style="color:{pnl_color}">{total_pnl:+.2f}%</div><div class="label">누적 손익</div></div>
  <div class="stat"><div class="value" style="color:#2ecc71">{avg_win:+.2f}%</div><div class="label">평균 수익</div></div>
  <div class="stat"><div class="value" style="color:#e74c3c">{avg_loss:+.2f}%</div><div class="label">평균 손실</div></div>
</div>
<div class="card">
  <h2>거래 내역 (최근 100건)</h2>
  <table>
    <tr>
      <th>종목코드</th><th>종목명</th><th>시장</th><th>수량</th>
      <th>매수가</th><th>매도가</th><th>손익</th><th>청산사유</th><th>날짜</th>
    </tr>
    {rows_html}
  </table>
</div>
</body></html>"""
