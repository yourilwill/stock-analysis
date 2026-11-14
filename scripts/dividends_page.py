"""受取配当金のページ(dividends.html)を生成する(build_static_site.py から呼ばれる)。

データは scripts/dividend_history.py の merged_monthly()(楽天の配当金一覧CSVの実績＋
dividend_income.py の見積もり)。国内株を本表、米国株は実績だけを参考表にする。
"""
import html
import json
from datetime import date

from dividend_history import merged_monthly

DIVIDENDS_CSS = """
.badge-est { background: var(--yellow); color: var(--container-bg); }
.badge-forecast { background: var(--border); color: var(--fg); }
#div-table td.est, #div-table td.forecast { font-style: italic; }
#div-table td.forecast { color: var(--muted); }
#div-table tfoot td { font-weight: bold; }
#div-table th.year { border-left: 2px solid var(--purple); }
#div-table td.year-start { border-left: 2px solid var(--purple); }
.stock-list { columns: 2; column-gap: 32px; padding-left: 1.2em; }
.stock-list li { break-inside: avoid; }
details { margin: 8px 0; }
details summary { cursor: pointer; color: var(--cyan); }
""".strip()

CHART_JS = """
(function() {
  var data = JSON.parse(document.getElementById('div-chart-data').textContent);
  var selected = data.default_years.slice();
  var chart = null;
  function render() {
    if (typeof Chart === 'undefined') return;
    // 配色(テーマ)は描画のたびにCSS変数から読み直す。テーマ切り替え前の色で描くと、
    // latte(明るい背景)で軸や凡例の文字が見えなくなるため。
    var css = getComputedStyle(document.documentElement);
    var palette = ['--purple', '--cyan', '--green', '--orange', '--pink', '--yellow'].map(function(v) {
      return css.getPropertyValue(v).trim();
    });
    var fg = css.getPropertyValue('--fg').trim();
    var grid = css.getPropertyValue('--border').trim();
    var datasets = [];
    data.years.forEach(function(y, i) {
      if (selected.indexOf(y) < 0) return;
      var color = palette[i % palette.length];  // 年ごとの色は、表示する年を変えても変わらない
      datasets.push({
        label: y + '年', data: data.values[y],
        // 推定・見込みを含む月は半透明にする
        backgroundColor: data.values[y].map(function(_, m) { return data.kinds[y][m] === '実績' ? color : color + '66'; })
      });
    });
    if (chart) chart.destroy();
    chart = new Chart(document.getElementById('div-chart'), {
      type: 'bar',
      data: { labels: data.labels, datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: fg } },
                   tooltip: { callbacks: { label: function(c) { return c.dataset.label + ': ' + (c.raw || 0).toLocaleString() + '円'; } } } },
        scales: { x: { ticks: { color: fg }, grid: { color: grid } },
                  y: { ticks: { color: fg, callback: function(v) { return v.toLocaleString(); } }, grid: { color: grid } } }
      }
    });
  }
  function applyTable() {
    document.querySelectorAll('#div-table [data-year]').forEach(function(el) {
      el.style.display = selected.indexOf(el.dataset.year) >= 0 ? '' : 'none';
    });
    document.querySelectorAll('.year-filters button[data-year]').forEach(function(b) {
      b.classList.toggle('active', selected.indexOf(b.dataset.year) >= 0);
    });
  }
  document.querySelectorAll('.year-filters button[data-year]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var y = btn.dataset.year, i = selected.indexOf(y);
      if (i >= 0) { if (selected.length > 1) selected.splice(i, 1); } else { selected.push(y); selected.sort(); }
      applyTable(); render();
    });
  });
  applyTable();
  // テーマ(site.js)が適用されてから描き、テーマが切り替わったら描き直す
  window.addEventListener('load', render);
  new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
""".strip()


def _yen(v) -> str:
    return "—" if v is None else f"{v:,.0f}円"


def _signed(v, suffix="%", digits=1) -> str:
    if v is None:
        return "—"
    cls = "up" if v > 0 else "down" if v < 0 else ""
    return f'<span class="{cls}">{v:+,.{digits}f}{suffix}</span>'


def _pct(cur, prev):
    if cur is None or not prev:
        return None
    return (cur / prev - 1) * 100


def render_dividends_body(today: date = None) -> str:
    today = today or date.today()
    m = merged_monthly(today)
    dom, fgn = m["domestic"], m["foreign"]
    head = "<h1>受取配当金</h1>"
    if not dom and not fgn:
        return head + '<p class="note">まだデータがありません。楽天証券の配当金一覧CSVを dividends_raw/ に置くと表示されます。</p>'

    years = sorted({ym[:4] for ym in list(dom) + list(fgn)})
    this_year = str(today.year)
    # 「年初来」の比較に使う月数(今年の前月まで)。1月なら比較しない。
    ytd_months = today.month - 1

    def dget(y, mo):
        return dom.get(f"{y}-{mo:02d}")

    def year_total(y, months=range(1, 13), kinds=None, key="gross"):
        vals = [dget(y, mo) for mo in months]
        return sum(v[key] for v in vals if v and (kinds is None or v["kind"] in kinds))

    def fgn_total(y, months=range(1, 13), key="gross"):
        return sum(fgn.get(f"{y}-{mo:02d}", {}).get(key, 0) for mo in months)

    # ---- サマリー ----
    received_kinds = {"実績", "一部推定", "推定"}
    all_actual = sum(v["gross"] for v in dom.values() if v["kind"] == "実績") + sum(v["gross"] for v in fgn.values())
    all_actual_net = sum(v["net"] for v in dom.values() if v["kind"] == "実績") + sum(v["net"] for v in fgn.values())
    est_past = sum(v["gross"] for ym, v in dom.items() if v["kind"] in ("推定", "一部推定") and ym < today.strftime("%Y-%m"))
    prev_year = str(today.year - 1)
    ytd_cur = year_total(this_year, range(1, ytd_months + 1), received_kinds) + fgn_total(this_year, range(1, ytd_months + 1))
    ytd_prev = year_total(prev_year, range(1, ytd_months + 1)) + fgn_total(prev_year, range(1, ytd_months + 1))
    landing = year_total(this_year) + fgn_total(this_year)
    prev_total = year_total(prev_year) + fgn_total(prev_year)

    def card(label, value, sub=""):
        sub_html = f'<div class="sub">{sub}</div>' if sub else ""
        return f'<div class="summary-card"><div class="label">{label}</div><div class="value">{value}</div>{sub_html}</div>'

    first = m["first_month"] or ""
    cards = [
        card("これまでの受取総額(実績)", _yen(all_actual),
             f"税引後 {_yen(all_actual_net)}・{first.replace('-', '/')}〜" + (f"<br>＋推定 {_yen(est_past)}(CSV未反映)" if est_past else "")),
        card(f"{this_year}年 1〜{ytd_months}月の受取額" if ytd_months else f"{this_year}年の受取額",
             _yen(ytd_cur), f"前年同期 {_yen(ytd_prev)}({_signed(_pct(ytd_cur, ytd_prev))})" if ytd_months else ""),
        card(f"{this_year}年の着地見込み", _yen(landing),
             f"前年 {_yen(prev_total)}・{_signed(landing - prev_total, '円', 0)}({_signed(_pct(landing, prev_total))})"),
        card(f"{prev_year}年の受取額", _yen(prev_total),
             f"税引後 {_yen(year_total(prev_year, key='net') + fgn_total(prev_year, key='net'))}"),
    ]
    summary = '<div class="summary-grid">' + "".join(cards) + "</div>"

    # ---- グラフ用データ(国内株、月別×年) ----
    chart_data = {
        "labels": [f"{mo}月" for mo in range(1, 13)],
        "years": years,
        "values": {y: [(dget(y, mo) or {}).get("gross", 0) for mo in range(1, 13)] for y in years},
        "kinds": {y: [(dget(y, mo) or {}).get("kind", "実績") for mo in range(1, 13)] for y in years},
        # 年が増えても横に伸びすぎないよう、既定では直近3年だけ表示する(ボタンで切り替え)
        "default_years": years[-3:],
    }
    year_buttons = ('<div class="view-filters year-filters"><span class="note">表示する年:</span>'
                    + "".join(f'<button type="button" data-year="{y}">{y}年</button>' for y in years) + "</div>")
    chart = (f'<h2>月別の受取配当金(国内株・税引前)</h2>{year_buttons}'
             f'<div class="chart-box" style="height: 340px;"><canvas id="div-chart"></canvas></div>'
             f'<p class="note">薄い色の棒は推定・見込みを含む月。年のボタンで、グラフと下の月別の表に出す年を切り替えられます(既定は直近3年)。</p>'
             f'<script type="application/json" id="div-chart-data">{json.dumps(chart_data, ensure_ascii=False)}</script>')

    # ---- 年別サマリー(年が増えても縦に伸びるだけ) ----
    arows = []
    for y in years:
        t, pt = year_total(y), year_total(str(int(y) - 1))
        ft = fgn_total(y)
        months = [mo for mo in range(1, 13) if dget(y, mo)]
        partial = []
        if y == years[0] and months and months[0] > 1:
            partial.append(f"{months[0]}月〜")
        if any(dget(y, mo)["kind"] != "実績" for mo in months):
            partial.append("推定・見込みを含む")
        arows.append(
            f"<tr><td>{y}年{'(' + '、'.join(partial) + ')' if partial else ''}</td><td>{t:,}</td>"
            f"<td>{_signed(t - pt, '円', 0) if pt else '—'}</td><td>{_signed(_pct(t, pt))}</td>"
            f"<td>{year_total(y, key='net'):,}</td><td>{ft:,}</td><td>{t + ft:,}</td>"
            f"<td>{len(m['by_stock'].get(y, {}))}</td></tr>")
    annual = ('<h2>年別の受取配当金</h2><table><thead><tr><th>年</th><th>国内株(税引前)</th><th>前年差</th>'
              '<th>前年比</th><th>国内株(税引後)</th><th>米国株(参考)</th><th>合計(税引前)</th><th>銘柄数</th>'
              f'</tr></thead><tbody>{"".join(arows)}</tbody></table>')

    # ---- 国内株の月別表 ----
    head_cells = "<th>月</th>" + "".join(
        f'<th class="year" data-year="{y}">{y}年</th><th data-year="{y}">前年同月比</th>' for y in years)
    body_rows = []
    for mo in range(1, 13):
        cells = [f"<td>{mo}月</td>"]
        for y in years:
            v = dget(y, mo)
            prev = dget(str(int(y) - 1), mo)
            if v is None:
                cells.append(f'<td class="year-start" data-year="{y}">—</td><td data-year="{y}">—</td>')
                continue
            cls = {"実績": "", "一部推定": "est", "推定": "est", "一部見込み": "forecast", "見込み": "forecast"}[v["kind"]]
            badge = "" if v["kind"] == "実績" else (
                f' <span class="badge {"badge-forecast" if "見込み" in v["kind"] else "badge-est"}">{v["kind"]}</span>')
            yoy = _pct(v["gross"], prev["gross"] if prev else None)
            cells.append(f'<td class="year-start {cls}" data-year="{y}">{v["gross"]:,}{badge}</td>'
                         f'<td data-year="{y}">{_signed(yoy)}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    def foot_row(label, fn):
        return f"<tr><td>{label}</td>" + "".join(fn(y) for y in years) + "</tr>"

    def total_cell(y):
        t = year_total(y)
        pt = year_total(str(int(y) - 1))
        return f'<td class="year-start" data-year="{y}">{t:,}</td><td data-year="{y}">{_signed(_pct(t, pt))}</td>'

    def net_cell(y):
        return f'<td class="year-start" data-year="{y}">{year_total(y, key="net"):,}</td><td data-year="{y}"></td>'

    def diff_cell(y):
        t, pt = year_total(y), year_total(str(int(y) - 1))
        return f'<td class="year-start" data-year="{y}">{_signed(t - pt, "円", 0) if pt else "—"}</td><td data-year="{y}"></td>'

    def ytd_cell(y):
        t = year_total(y, range(1, ytd_months + 1))
        pt = year_total(str(int(y) - 1), range(1, ytd_months + 1))
        return f'<td class="year-start" data-year="{y}">{t:,}</td><td data-year="{y}">{_signed(_pct(t, pt))}</td>'

    foot = [
        foot_row("年合計(税引前)", total_cell),
        foot_row("年合計(税引後)", net_cell),
        foot_row("前年差", diff_cell),
    ]
    if ytd_months:
        foot.append(foot_row(f"1〜{ytd_months}月累計", ytd_cell))
    table = (f'<h2>国内株</h2><div class="table-wrap" style="max-height: none;"><table id="div-table">'
             f'<thead><tr>{head_cells}</tr></thead><tbody>{"".join(body_rows)}</tbody>'
             f'<tfoot>{"".join(foot)}</tfoot></table></div>')

    # ---- 米国株(参考) ----
    fyears = sorted({ym[:4] for ym in fgn})
    if fyears:
        frows = []
        for mo in range(1, 13):
            frows.append(f"<tr><td>{mo}月</td>" + "".join(
                f'<td>{fgn[f"{y}-{mo:02d}"]["gross"]:,}</td>' if f"{y}-{mo:02d}" in fgn else "<td>—</td>"
                for y in fyears) + "</tr>")
        ftot = "<tr><td>年合計(税引前)</td>" + "".join(f"<td>{fgn_total(y):,}</td>" for y in fyears) + "</tr>"
        foreign = (f'<h2>米国株(参考)</h2><p class="note">楽天の配当金一覧CSVの実績のみ(円換算、見積もりは無し)。'
                   f'CSVを置いたときだけ更新されます。</p>'
                   f'<table><thead><tr><th>月</th>{"".join(f"<th>{y}年</th>" for y in fyears)}</tr></thead>'
                   f'<tbody>{"".join(frows)}</tbody><tfoot>{ftot}</tfoot></table>')
    else:
        foreign = ""

    # ---- 銘柄別 ----
    details = []
    for y in sorted(m["by_stock"], reverse=True):
        items = sorted(m["by_stock"][y].items(), key=lambda kv: -kv[1])
        lis = "".join(f"<li>{html.escape(code)} {html.escape(name)}: {v:,}円</li>" for (code, name), v in items)
        details.append(f"<details><summary>{y}年({len(items)}銘柄、{sum(v for _, v in items):,}円)</summary>"
                       f'<ol class="stock-list">{lis}</ol></details>')
    stocks = "<h2>銘柄別(税引前、多い順)</h2>" + "".join(details)

    as_of = m["as_of"]
    meta = (f'<p class="note">実績: 楽天証券の配当金一覧CSV({as_of.replace("-", "/") if as_of else "なし"}時点)。'
            f'それより後の月は、取引履歴とIRBANKの1株配当からの見積もりで埋めています'
            f'(<span class="badge badge-est">推定</span>=入金済みのはずだがCSV未反映、'
            f'<span class="badge badge-forecast">見込み</span>=今月以降、一部推定・一部見込み=実績＋未入金分の見積もり)。'
            f'見積もりは国内株のみで、予想が非開示の銘柄(証券・メガバンク等)は含みません。</p>')
    return f"""{head}
{meta}
{summary}
{annual}
{chart}
{table}
{foreign}
{stocks}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
{CHART_JS}
</script>
"""
