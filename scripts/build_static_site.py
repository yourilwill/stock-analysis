#!/usr/bin/env python3
"""analysis_results/ から静的サイト（Web公開用）を生成する。

各銘柄ディレクトリのHTMLレポートはそのままコピーし、raw_data_*.mdは
整形表示（Markdown→HTML）と生データ表示（<pre>）の2種類のページを生成する。
併せて銘柄一覧のトップページ・各銘柄ディレクトリの一覧ページも生成する。

使い方: python3 scripts/build_static_site.py [--src analysis_results] [--out analysis_results/_site]
"""
import argparse
import html
import re
import shutil
from pathlib import Path

from markdown_it import MarkdownIt

DATE_RE = re.compile(r"_(\d{8})\.")
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

STYLE_CSS = """
:root {
  --bg: #21222c; --container-bg: #282a36; --fg: #f8f8f2; --muted: #6272a4; --border: #44475a; --th-bg: #44475a;
  --stripe-bg: #2d2f3f; --box-bg: #343649;
  --cyan: #8be9fd; --green: #50fa7b; --orange: #ffb86c; --pink: #ff79c6; --purple: #bd93f9; --yellow: #f1fa8c;
}
html[data-theme="catppuccin-mocha"] {
  --bg: #181825; --container-bg: #1e1e2e; --fg: #cdd6f4; --muted: #7f849c; --border: #45475a; --th-bg: #45475a;
  --stripe-bg: #313244; --box-bg: #313244;
  --cyan: #89dceb; --green: #a6e3a1; --orange: #fab387; --pink: #f5c2e7; --purple: #cba6f7; --yellow: #f9e2af;
}
html[data-theme="catppuccin-latte"] {
  --bg: #e6e9ef; --container-bg: #eff1f5; --fg: #4c4f69; --muted: #6c6f85; --border: #bcc0cc; --th-bg: #4c4f69;
  --stripe-bg: #e6e9ef; --box-bg: #ccd0da;
  --cyan: #04a5e5; --green: #40a02b; --orange: #fe640b; --pink: #ea76cb; --purple: #8839ef; --yellow: #df8e1d;
}
body { font-family: "Hiragino Sans", "Helvetica Neue", Arial, "Yu Gothic", sans-serif; line-height: 1.7; color: var(--fg); background: var(--bg); margin: 0; padding: 0; }
.container { max-width: 1000px; margin: 0 auto; padding: 30px 24px 80px; background: var(--container-bg); box-shadow: 0 0 24px rgba(0,0,0,0.5); }
.controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
.controls label { font-size: 0.85rem; color: var(--muted); }
.controls label[for="theme-select"] { margin-left: auto; }
.controls select { font: inherit; font-size: 0.85rem; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--border); background: var(--box-bg); color: var(--fg); }
h1 { font-size: 1.7em; color: var(--purple); border-bottom: 4px solid var(--purple); padding-bottom: 12px; margin-bottom: 16px; }
h2 { font-size: 1.25em; color: var(--container-bg); background: var(--purple); padding: 8px 14px; margin-top: 36px; border-radius: 4px; }
h3 { font-size: 1.05em; color: var(--cyan); border-left: 4px solid var(--pink); padding-left: 10px; margin-top: 24px; }
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; color: var(--pink); }
table { border-collapse: collapse; width: 100%; margin: 14px 0 24px; font-size: 0.92em; }
th, td { border: 1px solid var(--border); padding: 6px 10px; text-align: right; color: var(--fg); }
th { background: var(--th-bg); color: var(--yellow); text-align: center; }
td:first-child, th:first-child { text-align: left; }
tr:nth-child(even) td { background: var(--stripe-bg); }
.note { color: var(--muted); font-size: 0.88em; }
.file-list { list-style: none; padding: 0; }
.file-list li { background: var(--box-bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; margin: 8px 0; }
#company-table th { cursor: pointer; user-select: none; }
#company-table th:hover { color: var(--pink); }
#company-table th.sort-asc::after { content: " \\25B2"; }
#company-table th.sort-desc::after { content: " \\25BC"; }
.filter-box { width: 100%; box-sizing: border-box; padding: 10px 14px; margin: 12px 0 20px; font-size: 1em; background: var(--box-bg); color: var(--fg); border: 1px solid var(--border); border-radius: 6px; }
pre { background: var(--box-bg); border: 1px solid var(--border); border-radius: 6px; padding: 16px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; font-size: 0.88em; }
.md-content ul, .md-content ol { padding-left: 1.4em; }
.md-content strong { color: var(--yellow); }
footer { margin-top: 60px; color: var(--muted); font-size: 0.8em; text-align: center; }
""".strip()

SITE_JS = """
(function() {
  var KEY = "stock-analysis-theme";
  var select = document.getElementById("theme-select");
  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    if (select) select.value = theme;
  }
  apply(localStorage.getItem(KEY) || "dracula");
  if (select) {
    select.addEventListener("change", function() {
      localStorage.setItem(KEY, select.value);
      apply(select.value);
    });
  }
})();
""".strip()

THEME_CONTROLS = """<div class="controls">
<label for="theme-select">配色</label>
<select id="theme-select">
<option value="dracula">\U0001F9DB Dracula</option>
<option value="catppuccin-mocha">\U0001F431 Catppuccin Mocha</option>
<option value="catppuccin-latte">☕ Catppuccin Latte</option>
</select>
</div>"""


def render_markdown(text: str) -> str:
    # 日本語の句読点・括弧がURLに巻き込まれるlinkify-itの誤爆を避けるため、
    # 自前の正規表現でURL範囲を確定してから <URL> 形式のautolinkに変換する。
    text = URL_RE.sub(lambda m: f"<{m.group(0)}>", text)
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    return md.render(text)


def extract_date(filename: str) -> str:
    m = DATE_RE.search(filename)
    return m.group(1) if m else ""


def format_date(d: str) -> str:
    return f"{d[0:4]}/{d[4:6]}/{d[6:8]}" if len(d) == 8 else d


def page(title: str, breadcrumb: str, body: str, depth: int) -> str:
    prefix = "../" * depth
    return f"""<!DOCTYPE html>
<html lang="ja" data-theme="dracula">
<head>
<meta charset="UTF-8">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="{prefix}assets/style.css">
</head>
<body>
<div class="container">
{THEME_CONTROLS}
{breadcrumb}
{body}
</div>
<script src="{prefix}assets/site.js"></script>
</body>
</html>
"""


def company_breadcrumb(company_dirname: str, filename: str) -> str:
    return (
        f'<p class="note"><a href="../index.html">銘柄一覧</a> &raquo; '
        f'<a href="index.html">{html.escape(company_dirname)}</a> &raquo; {html.escape(filename)}</p>'
    )


def build(src: Path, out: Path) -> int:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    assets = out / "assets"
    assets.mkdir()
    (assets / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    (assets / "site.js").write_text(SITE_JS, encoding="utf-8")

    companies = []
    for company_dir in sorted(src.iterdir()):
        if not company_dir.is_dir() or company_dir.name.startswith("_"):
            continue
        m = re.match(r"^(.+?)_(.+)$", company_dir.name)
        code, name = (m.group(1), m.group(2)) if m else (company_dir.name, "")
        out_company = out / company_dir.name
        out_company.mkdir(parents=True, exist_ok=True)

        reports = sorted(company_dir.glob("*.html"), key=lambda p: extract_date(p.name), reverse=True)
        mds = sorted(company_dir.glob("*.md"), key=lambda p: extract_date(p.name), reverse=True)

        report_links = []
        for f in reports:
            shutil.copy2(f, out_company / f.name)
            report_links.append((f.name, format_date(extract_date(f.name))))

        md_links = []
        for f in mds:
            text = f.read_text(encoding="utf-8")
            rendered_name = f.stem + ".html"
            raw_name = f.stem + ".raw.html"
            rendered_body = (
                f'<h1>{html.escape(f.name)}</h1>'
                f'<p class="note"><a href="{raw_name}">生データ表示</a></p>'
                f'<div class="md-content">{render_markdown(text)}</div>'
            )
            (out_company / rendered_name).write_text(
                page(f.name, company_breadcrumb(company_dir.name, f.name), rendered_body, depth=1),
                encoding="utf-8",
            )
            raw_body = (
                f'<h1>{html.escape(f.name)}（生データ）</h1>'
                f'<p class="note"><a href="{rendered_name}">整形表示</a></p>'
                f'<pre>{html.escape(text)}</pre>'
            )
            (out_company / raw_name).write_text(
                page(f.name + "（生データ）", company_breadcrumb(company_dir.name, f.name), raw_body, depth=1),
                encoding="utf-8",
            )
            md_links.append((f.name, rendered_name, raw_name, format_date(extract_date(f.name))))

        body = f"<h1>{html.escape(name)}（{html.escape(code)}）</h1>"
        body += '<h2>分析レポート</h2><ul class="file-list">'
        for fname, d in report_links:
            body += f'<li><a href="{html.escape(fname)}">{html.escape(fname)}</a> <span class="note">{d}</span></li>'
        body += "</ul>"
        if md_links:
            body += '<h2>生データ (raw_data)</h2><ul class="file-list">'
            for fname, rendered_name, raw_name, d in md_links:
                body += (
                    f'<li>{html.escape(fname)} <span class="note">{d}</span> — '
                    f'<a href="{rendered_name}">整形表示</a> / <a href="{raw_name}">生データ</a></li>'
                )
            body += "</ul>"
        body += '<p><a href="../index.html">&larr; 銘柄一覧に戻る</a></p>'
        (out_company / "index.html").write_text(
            page(f"{name}（{code}）", "", body, depth=1), encoding="utf-8"
        )

        latest_date = report_links[0][1] if report_links else (md_links[0][3] if md_links else "")
        companies.append((code, name, company_dir.name, latest_date))

    companies.sort(key=lambda c: c[0])
    rows = "\n".join(
        f'<tr><td>{html.escape(code)}</td><td><a href="{html.escape(dirname)}/index.html">{html.escape(name)}</a></td>'
        f'<td>{d}</td></tr>'
        for code, name, dirname, d in companies
    )
    body = f"""<h1>日本株分析レポート一覧</h1>
<p class="note">{len(companies)}銘柄</p>
<input type="text" id="filter" class="filter-box" placeholder="銘柄コード・会社名で絞り込み" oninput="filterTable()">
<table id="company-table">
<thead><tr>
<th onclick="sortTable(0,'num')">コード</th>
<th onclick="sortTable(1,'str')">会社名</th>
<th onclick="sortTable(2,'str')">最新レポート日</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
<script>
function filterTable() {{
  const q = document.getElementById('filter').value.toLowerCase();
  document.querySelectorAll('#company-table tbody tr').forEach(function(tr) {{
    tr.style.display = tr.textContent.toLowerCase().includes(q) ? '' : 'none';
  }});
}}
let sortState = {{col: -1, asc: true}};
function sortTable(colIndex, type) {{
  const table = document.getElementById('company-table');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.rows);
  const asc = sortState.col === colIndex ? !sortState.asc : true;
  rows.sort(function(a, b) {{
    let va = a.cells[colIndex].textContent.trim();
    let vb = b.cells[colIndex].textContent.trim();
    if (type === 'num') {{ va = parseFloat(va) || 0; vb = parseFloat(vb) || 0; }}
    if (va < vb) return asc ? -1 : 1;
    if (va > vb) return asc ? 1 : -1;
    return 0;
  }});
  rows.forEach(function(r) {{ tbody.appendChild(r); }});
  sortState = {{col: colIndex, asc: asc}};
  table.querySelectorAll('th').forEach(function(th, i) {{
    th.classList.remove('sort-asc', 'sort-desc');
    if (i === colIndex) th.classList.add(asc ? 'sort-asc' : 'sort-desc');
  }});
}}
</script>
"""
    (out / "index.html").write_text(page("日本株分析レポート一覧", "", body, depth=0), encoding="utf-8")
    return len(companies)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default="analysis_results", help="入力ディレクトリ（既定: analysis_results）")
    parser.add_argument("--out", default="analysis_results/_site", help="出力ディレクトリ（既定: analysis_results/_site）")
    args = parser.parse_args()
    count = build(Path(args.src), Path(args.out))
    print(f"生成完了: {args.out}（{count}銘柄）")


if __name__ == "__main__":
    main()
