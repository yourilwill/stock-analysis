#!/usr/bin/env python3
"""IRBANKから指定銘柄の損益推移（通期実績・最新通期予想）を取得するCLIツール。

使い方:
    python3 irbank_pl.py <銘柄コード or Eコード>
    python3 irbank_pl.py 6817
    python3 irbank_pl.py E01971
"""
import argparse
import re
import sys
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_number(s: str):
    s = strip_tags(s).replace(",", "").replace("△", "-").replace("▲", "-")
    if s in ("", "-"):
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.1f}"


def fetch_pl(code: str):
    url = f"https://irbank.net/{code}/pl?tm=100"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    name_m = re.search(r'<meta property="og:title" content="([^（\(]+)', html)
    company = name_m.group(1).strip() if name_m else code

    table_m = re.search(r'<table class="cs">(.*?)</table>', html, re.S)
    if not table_m:
        raise ValueError(f"PLテーブルが見つかりません: {url}")
    table = table_m.group(1)

    thead_m = re.search(r"<thead>(.*?)</thead>", table, re.S)
    if not thead_m:
        raise ValueError("PLテーブルのヘッダーが見つかりませんでした")
    headers = [strip_tags(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", thead_m.group(), re.S)]
    # [年度, 四半期, (提出日), 売上収益, 営業利益, 経常利益, 当期利益, 当期包括利益]
    cols_per_row = len(headers) - 1  # 年度列を除く

    # ヘッダー名からrow内のインデックスを動的に決定（提出日列の有無に対応）
    def find_col(keywords: list) -> int:
        for i, h in enumerate(headers):
            if i == 0:
                continue  # 年度列はスキップ
            for kw in keywords:
                if kw in h:
                    return i - 1  # row内のインデックス（0=四半期セル）
        return None

    rev_idx = find_col(["売上収益", "収益", "売上高"])
    op_idx = find_col(["営業利益"])
    ord_idx = find_col(["経常利益"])
    net_idx = find_col(["当期利益", "当期純利益"])

    if any(x is None for x in [rev_idx, op_idx, ord_idx, net_idx]):
        raise ValueError(f"PLテーブルの列が見つかりません (headers={headers})")

    tbody_m = re.search(r"<tbody>(.*?)</tbody>", table, re.S)
    if not tbody_m:
        raise ValueError("PLテーブルのボディが見つかりませんでした")
    cells = re.findall(r"<td([^>]*)>(.*?)</td>", tbody_m.group(), re.S)

    results = []
    i = 0
    while i < len(cells):
        attrs, text = cells[i]
        year_m = re.search(r"(\d{4})年", text)
        rowspan_m = re.search(r'rowspan="(\d+)"', attrs)
        if not year_m or not rowspan_m:
            i += 1
            continue
        year = int(year_m.group(1))
        month_m = re.search(r"(\d{1,2})月期", text)
        month = int(month_m.group(1)) if month_m else 12
        rowspan = int(rowspan_m.group(1))
        i += 1

        for _ in range(rowspan):
            row = cells[i:i + cols_per_row]
            i += cols_per_row
            if len(row) < cols_per_row:
                break
            period_text = row[0][1]
            is_annual = "通期" in strip_tags(period_text)
            if not is_annual:
                continue
            is_actual = "co_red" in period_text
            is_forecast = "co_gr" in period_text
            results.append({
                "year": year,
                "month": month,
                "label": f"{year}/{month:02d}",
                "type": "実績" if is_actual else ("予想" if is_forecast else ""),
                "rev": parse_number(row[rev_idx][1]),
                "op": parse_number(row[op_idx][1]),
                "ord": parse_number(row[ord_idx][1]),
                "net": parse_number(row[net_idx][1]),
            })

    return company, results, url


def main():
    parser = argparse.ArgumentParser(description="IRBANKから損益推移(通期)を取得")
    parser.add_argument("code", help="銘柄コード(例: 6817) または Eコード(例: E01971)")
    args = parser.parse_args()

    try:
        company, rows, url = fetch_pl(args.code)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    # 通期実績の全件 + 実績未確定年の最新通期予想（直近1件のみ）
    actual_years = {r["year"] for r in rows if r["type"] == "実績"}
    display = [r for r in rows if r["type"] == "実績"]
    forecasts = [r for r in rows if r["type"] == "予想" and r["year"] not in actual_years]
    if forecasts:
        display.append(forecasts[-1])  # 最新の通期予想

    if not display:
        print(f"エラー: {company}({args.code}) の通期データが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    print(f"{company}({args.code}) 損益推移・百万円 (通期実績＋直近予想)")
    print(f"{'年度':<10} {'区分':4} {'収益(売上高)':>12} {'営業利益':>10} {'経常利益':>10} {'当期利益':>10} {'営業利益率':>10}")
    print("-" * 72)
    for r in display:
        op_rate = f"{r['op'] / r['rev'] * 100:.1f}%" if r["rev"] and r["op"] is not None else "-"
        print(f"{r['label']:<10} {r['type']:4} {fmt(r['rev']):>12} {fmt(r['op']):>10} {fmt(r['ord']):>10} {fmt(r['net']):>10} {op_rate:>10}")
    print(f"出典: {url}")


if __name__ == "__main__":
    main()
