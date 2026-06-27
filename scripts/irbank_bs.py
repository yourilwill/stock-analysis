#!/usr/bin/env python3
"""IRBANKから指定銘柄の財務推移（BS・自己資本比率等）を取得するCLIツール。

使い方:
    python3 irbank_bs.py <銘柄コード or Eコード>
    python3 irbank_bs.py 6817
    python3 irbank_bs.py E01971
    python3 irbank_bs.py 6817 --years 5
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


def parse_percent(s: str):
    s = strip_tags(s).replace(",", "").rstrip("%")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.1f}"


def fetch_bs(code: str):
    url = f"https://irbank.net/{code}/bs"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    name_m = re.search(r'<meta property="og:title" content="([^（\(]+)', html)
    company = name_m.group(1).strip() if name_m else code

    table_m = re.search(r'<table class="cs">(.*?)</table>', html, re.S)
    if not table_m:
        raise ValueError(f"BSテーブルが見つかりません: {url}")
    table = table_m.group(1)

    thead_m = re.search(r"<thead>(.*?)</thead>", table, re.S)
    if not thead_m:
        raise ValueError("BSテーブルのヘッダーが見つかりませんでした")
    headers = [strip_tags(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", thead_m.group(), re.S)]
    # [年度, 総資産, 株主資本, 利益剰余金, 有利子負債, 現金等, 株主資本比率, 有利子負債比率, のれん]
    n_cols = len(headers)

    tbody_m = re.search(r"<tbody>(.*?)</tbody>", table, re.S)
    if not tbody_m:
        raise ValueError("BSテーブルのボディが見つかりませんでした")
    rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_m.group(), re.S)

    all_rows = []
    for row_html in rows_html:
        cells_raw = re.findall(r"<td([^>]*)>(.*?)</td>", row_html, re.S)
        if len(cells_raw) < n_cols:
            continue
        year_str = strip_tags(cells_raw[0][1])
        m = re.match(r"(\d{4})/(\d{1,2})", year_str)
        if not m:
            continue

        def get(idx):
            return cells_raw[idx][1] if idx < len(cells_raw) else ""

        all_rows.append({
            "label": year_str,
            "year": int(m.group(1)),
            "month": int(m.group(2)),
            "total_assets": parse_number(get(1)),
            "equity": parse_number(get(2)),
            "retained": parse_number(get(3)),
            "debt": parse_number(get(4)),
            "cash": parse_number(get(5)),
            "equity_ratio": parse_percent(get(6)),
            "debt_ratio": parse_percent(get(7)),
        })

    # 決算月を自動検出: 最頻出の月 = 本決算月
    if all_rows:
        from collections import Counter
        fiscal_month = Counter(r["month"] for r in all_rows).most_common(1)[0][0]
        results = [r for r in all_rows if r["month"] == fiscal_month]
    else:
        results = []

    return company, results, url


def main():
    parser = argparse.ArgumentParser(description="IRBANKから財務推移(BS)を取得")
    parser.add_argument("code", help="銘柄コード(例: 6817) または Eコード(例: E01971)")
    parser.add_argument("--years", type=int, default=None, help="直近N期分のみ表示 (省略時: 全件)")
    args = parser.parse_args()

    try:
        company, rows, url = fetch_bs(args.code)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print(f"エラー: {company}({args.code}) の財務データが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    display = rows[-args.years:] if args.years else rows

    print(f"{company}({args.code}) 財務推移・百万円")
    print(f"{'年度':<10} {'総資産':>10} {'株主資本':>10} {'利益剰余金':>10} {'有利子負債':>10} {'現金等':>10} {'自己資本比率':>12} {'有利子負債比率':>14}")
    print("-" * 94)
    for r in display:
        eq_r = f"{r['equity_ratio']:.1f}%" if r["equity_ratio"] is not None else "-"
        debt_r = f"{r['debt_ratio']:.1f}%" if r["debt_ratio"] is not None else "-"
        print(
            f"{r['label']:<10} {fmt(r['total_assets']):>10} {fmt(r['equity']):>10} "
            f"{fmt(r['retained']):>10} {fmt(r['debt']):>10} {fmt(r['cash']):>10} "
            f"{eq_r:>12} {debt_r:>14}"
        )
    print(f"出典: {url}")


if __name__ == "__main__":
    main()
