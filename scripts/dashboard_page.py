"""株価ダッシュボードのページを生成する(build_static_site.py から呼ばれる)。

データは scripts/market_digest.py が平日18:00台に保存する market_watch/quotes/YYYY-MM-DD.json
(保有銘柄＋分析済み銘柄の終値・前日比・会社予想配当・目安利回り・保有情報)の最新日付分。
market_watch/ は gitignore 対象で hestia にだけあるため、無い環境では「まだデータがありません」を出す。
"""
import html
import json
from pathlib import Path

DASHBOARD_CSS = """
.container.wide { max-width: 1480px; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0 24px; }
.summary-card { background: var(--box-bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; }
.summary-card .label { color: var(--muted); font-size: 0.8em; }
.summary-card .value { font-size: 1.3em; font-weight: bold; }
.summary-card .sub { color: var(--muted); font-size: 0.8em; }
.table-wrap { overflow-x: auto; }
#quote-table { font-size: 0.86em; white-space: nowrap; }
#quote-table th { cursor: pointer; user-select: none; }
#quote-table th:hover { color: var(--pink); }
#quote-table th.sort-asc::after { content: " \\25B2"; }
#quote-table th.sort-desc::after { content: " \\25BC"; }
#quote-table td:nth-child(2) { text-align: left; }
#quote-table tr.reached td:first-child { border-left: 4px solid var(--orange); }
#quote-table tr.failed td { color: var(--muted); }
.up { color: var(--green); }
.down { color: var(--pink); }
.badge { display: inline-block; font-size: 0.75em; padding: 0 6px; border-radius: 4px; margin-left: 4px; }
.badge-held { background: var(--purple); color: var(--container-bg); }
.badge-reached { background: var(--orange); color: var(--container-bg); }
.badge-notrade { background: var(--border); color: var(--fg); }
.view-filters { display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 8px; }
.view-filters button { font: inherit; font-size: 0.85em; padding: 4px 12px; border-radius: 999px; border: 1px solid var(--border); background: var(--box-bg); color: var(--fg); cursor: pointer; }
.view-filters button.active { background: var(--purple); color: var(--container-bg); border-color: var(--purple); }
""".strip()

DASHBOARD_JS = """
(function() {
  var table = document.getElementById('quote-table');
  if (!table) return;
  var tbody = table.tBodies[0];
  var view = 'all';
  var query = '';
  function applyFilter() {
    Array.prototype.forEach.call(tbody.rows, function(tr) {
      var okView = view === 'all'
        || (view === 'held' && tr.dataset.held === '1')
        || (view === 'notheld' && tr.dataset.held !== '1')
        || (view === 'reached' && tr.classList.contains('reached'));
      var okQuery = !query || tr.textContent.toLowerCase().indexOf(query) >= 0;
      tr.style.display = okView && okQuery ? '' : 'none';
    });
  }
  document.getElementById('quote-filter').addEventListener('input', function(e) {
    query = e.target.value.toLowerCase();
    applyFilter();
  });
  document.querySelectorAll('.view-filters button').forEach(function(btn) {
    btn.addEventListener('click', function() {
      view = btn.dataset.view;
      document.querySelectorAll('.view-filters button').forEach(function(b) { b.classList.toggle('active', b === btn); });
      applyFilter();
    });
  });
  var sortState = {col: -1, asc: true};
  table.querySelectorAll('th').forEach(function(th, colIndex) {
    th.addEventListener('click', function() {
      var asc = sortState.col === colIndex ? !sortState.asc : th.dataset.type !== 'num';
      var rows = Array.prototype.slice.call(tbody.rows);
      rows.sort(function(a, b) {
        var va = a.cells[colIndex].dataset.sort, vb = b.cells[colIndex].dataset.sort;
        if (th.dataset.type === 'num') {
          // 値が無いセル(未取得・対象外)は昇順・降順どちらでも末尾に置く
          var na = va === '' ? null : parseFloat(va), nb = vb === '' ? null : parseFloat(vb);
          if (na === null && nb === null) return 0;
          if (na === null) return 1;
          if (nb === null) return -1;
          return asc ? na - nb : nb - na;
        }
        return asc ? va.localeCompare(vb, 'ja') : vb.localeCompare(va, 'ja');
      });
      rows.forEach(function(r) { tbody.appendChild(r); });
      sortState = {col: colIndex, asc: asc};
      table.querySelectorAll('th').forEach(function(h, i) {
        h.classList.remove('sort-asc', 'sort-desc');
        if (i === colIndex) h.classList.add(asc ? 'sort-asc' : 'sort-desc');
      });
    });
  });
})();
""".strip()


def load_latest_snapshot(quotes_dir: Path):
    """market_watch/quotes/ の最新日付のスナップショット。無ければNone。"""
    if not quotes_dir.is_dir():
        return None
    files = sorted(quotes_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _fmt_num(v, digits=0, suffix=""):
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def _fmt_signed(v, digits=2, suffix="%"):
    if v is None:
        return "—"
    cls = "up" if v > 0 else "down" if v < 0 else ""
    return f'<span class="{cls}">{v:+,.{digits}f}{suffix}</span>'


def _cell(display: str, sort_value=None) -> str:
    sort_attr = "" if sort_value is None else sort_value
    return f'<td data-sort="{html.escape(str(sort_attr))}">{display}</td>'


def _summary(stocks: list) -> str:
    held = [s for s in stocks if s.get("held")]
    market_value = sum(s.get("market_value") or 0 for s in held)
    cost = sum((s.get("quantity") or 0) * (s.get("avg_cost") or 0) for s in held if s.get("market_value") is not None)
    pl = market_value - cost
    annual_div = sum(s.get("annual_dividend") or 0 for s in held)
    reached = [s for s in stocks if s.get("reached")]
    reached_held = sum(1 for s in reached if s.get("held"))
    failed = sum(1 for s in stocks if s.get("error"))

    def card(label, value, sub=""):
        sub_html = f'<div class="sub">{sub}</div>' if sub else ""
        return f'<div class="summary-card"><div class="label">{label}</div><div class="value">{value}</div>{sub_html}</div>'

    cards = [
        card("評価額(保有)", f"{market_value:,.0f}円", f"{len(held)}銘柄"),
        card("評価損益", _fmt_signed(pl, 0, "円"), _fmt_signed(pl / cost * 100 if cost else None)),
        card("年間配当見込み", f"{annual_div:,.0f}円", "会社予想配当×株数(税引前)"),
        card("配当利回り(時価/簿価)",
             f"{annual_div / market_value * 100:.2f}%" if market_value else "—",
             f"簿価 {annual_div / cost * 100:.2f}%" if cost else ""),
        card("目安利回りに到達", f"{len(reached)}銘柄", f"保有 {reached_held} / 未保有 {len(reached) - reached_held}"),
    ]
    if failed:
        cards.append(card("株価の取得失敗", f"{failed}銘柄", "表の灰色の行"))
    return '<div class="summary-grid">' + "".join(cards) + "</div>"


def _indices(indices: list) -> str:
    if not indices:
        return ""
    cards = "".join(
        f'<div class="summary-card"><div class="label">{html.escape(i["name"])}</div>'
        f'<div class="value">{_fmt_num(i.get("price"), 2)}</div>'
        f'<div class="sub">{_fmt_signed(i.get("change_pct"))}・{html.escape(str(i.get("as_of") or ""))}</div></div>'
        for i in indices
    )
    return f'<h2>指数</h2><div class="summary-grid">{cards}</div>'


def _row(s: dict) -> str:
    classes = []
    if s.get("reached"):
        classes.append("reached")
    if s.get("error"):
        classes.append("failed")
    name = html.escape(s["name"])
    if s.get("analysis_dir"):
        name = f'<a href="{html.escape(s["analysis_dir"])}/index.html">{name}</a>'
    badges = ""
    if s.get("held"):
        badges += '<span class="badge badge-held">保有</span>'
    if s.get("reached"):
        badges += '<span class="badge badge-reached">到達</span>'
    if s.get("no_trade"):
        badges += f'<span class="badge badge-notrade" title="当日は売買が成立せず、{html.escape(str(s.get("as_of")))}の終値">売買なし</span>'
    title = f' title="{html.escape(s["error"])}"' if s.get("error") else ""

    target = s.get("target_yield")
    cur_yield = s.get("yield")
    gap = round(cur_yield - target, 2) if cur_yield is not None and target is not None else None
    cells = [
        _cell(html.escape(s["code"]), s["code"]),
        _cell(name + badges, s["name"]),
        _cell(_fmt_num(s.get("price"), 1 if s.get("price") and s["price"] % 1 else 0), s.get("price")),
        _cell(_fmt_signed(s.get("change_pct")), s.get("change_pct")),
        _cell(_fmt_num(s.get("dps"), 1 if s.get("dps") and s["dps"] % 1 else 0), s.get("dps")),
        _cell(_fmt_num(cur_yield, 2, "%"), cur_yield),
        _cell(_fmt_num(target, 2, "%"), target),
        _cell(_fmt_signed(gap, 2, "pt"), gap),
        _cell(_fmt_num(s.get("trigger_price")), s.get("trigger_price")),
        _cell(_fmt_num(s.get("book_yield"), 2, "%"), s.get("book_yield")),
        _cell(_fmt_num(s.get("quantity")), s.get("quantity")),
        _cell(_fmt_num(s.get("avg_cost"), 1), s.get("avg_cost")),
        _cell(_fmt_num(s.get("market_value")), s.get("market_value")),
        _cell(_fmt_signed(s.get("unrealized_pl_pct")), s.get("unrealized_pl_pct")),
    ]
    held_attr = "1" if s.get("held") else "0"
    return f'<tr class="{" ".join(classes)}" data-held="{held_attr}"{title}>' + "".join(cells) + "</tr>"


COLUMNS = [
    ("コード", "str"), ("銘柄", "str"), ("終値", "num"), ("前日比", "num"), ("予想配当", "num"),
    ("予想利回り", "num"), ("目安利回り", "num"), ("目安との差", "num"), ("目安株価", "num"),
    ("簿価利回り", "num"), ("株数", "num"), ("取得単価", "num"), ("評価額", "num"), ("評価損益率", "num"),
]


def render_dashboard_body(snapshot) -> str:
    head = '<h1>株価ダッシュボード</h1>'
    if snapshot is None:
        return head + '<p class="note">まだデータがありません。平日18:00台の押し目チェック(market_digest.py)の実行後に表示されます。</p>'

    stocks = snapshot.get("stocks", [])
    hd = snapshot.get("holdings_file_date") or ""
    hd_label = f"{hd[0:4]}/{hd[4:6]}/{hd[6:8]}" if len(hd) == 8 else "なし"
    meta = (f'<p class="note">{html.escape(snapshot.get("date", ""))}の終値(取得: '
            f'{html.escape(str(snapshot.get("generated_at", "")).replace("T", " "))})・保有データ: {hd_label}時点・'
            f'{len(stocks)}銘柄(保有＋分析済み)</p>')
    header_cells = "".join(f'<th data-type="{t}">{html.escape(c)}</th>' for c, t in COLUMNS)
    # 既定の並び: 目安との差が大きい順(到達している銘柄が上)。目安が無い銘柄は末尾。
    ordered = sorted(stocks, key=lambda s: (
        s.get("yield") is None or s.get("target_yield") is None,
        -((s.get("yield") or 0) - (s.get("target_yield") or 0)),
    ))
    rows = "\n".join(_row(s) for s in ordered)
    notes = """<p class="note">
目安利回り = 過去5年平均配当利回り(scripts/yield_targets.py)。予想利回りが目安以上の銘柄に「到達」が付き、表の左端がオレンジになります。
目安株価 = 予想配当 ÷ 目安利回り(この株価以下で到達)。簿価利回り = 予想配当 ÷ 平均取得単価。
目安との差は、予想利回り − 目安利回り(ポイント)です。灰色の行は株価を取得できなかった銘柄で、行にカーソルを合わせると理由が出ます。
</p>"""
    return f"""{head}
{meta}
{_summary(stocks)}
{_indices(snapshot.get("indices", []))}
<h2>銘柄一覧</h2>
<input type="text" id="quote-filter" class="filter-box" placeholder="銘柄コード・銘柄名で絞り込み">
<div class="view-filters">
<button type="button" data-view="all" class="active">すべて</button>
<button type="button" data-view="held">保有</button>
<button type="button" data-view="notheld">未保有</button>
<button type="button" data-view="reached">目安に到達</button>
</div>
<div class="table-wrap">
<table id="quote-table">
<thead><tr>{header_cells}</tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
{notes}
<script>
{DASHBOARD_JS}
</script>
"""
