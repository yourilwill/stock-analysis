#!/usr/bin/env python3
"""銘柄コードと年度を指定して、IRBANKから1株当たり年間配当金(円)と配当利回り(%)を取得するCLIツール。

使い方:
    python3 irbank_dividend.py <銘柄コード> <年度>
    python3 irbank_dividend.py 1605 2025
    python3 irbank_dividend.py 1419 2026 --adjusted   # 株式分割調整後の値を使う

「<年度>」は IRBANK の表に書かれている「YYYY年X月期」のYYYY部分(決算月は問わない)に対応する。
同一年度に複数行(予想/修正/実績)がある場合は、表の中で最後に記載された行(最新の値)を採用する。
"""
import argparse
import re
import sys
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_number(s: str):
    s = strip_tags(s).replace(",", "")
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


def fetch_dividend_table(code: str):
    url = f"https://irbank.net/{code}/dividend"
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    name_match = re.search(r'<meta property="og:title" content="([^（]+)', html)
    company_name = name_match.group(1).strip() if name_match else None

    table_match = re.search(r'<table class="cs">.*?</table>', html, re.S)
    if not table_match or not company_name:
        raise ValueError(f"銘柄コード '{code}' のデータが見つかりませんでした (URL: {url})")

    table_html = table_match.group()

    thead_match = re.search(r"<thead>.*?</thead>", table_html, re.S)
    if not thead_match:
        raise ValueError("配当テーブルのヘッダーが見つかりませんでした")
    headers = [strip_tags(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", thead_match.group(), re.S)]
    if not headers or headers[0] != "年度":
        raise ValueError("配当テーブルのヘッダー構造が想定と異なります")

    # 年度列を除いた、各サブ行内でのカラム位置
    sub_index = {name: idx - 1 for idx, name in enumerate(headers) if idx > 0}
    if "区分" not in sub_index or "合計" not in sub_index:
        raise ValueError("配当テーブルに想定する列(区分/合計)が見つかりませんでした")
    cols_per_row = len(headers) - 1

    tbody_match = re.search(r"<tbody>.*?</tbody>", table_html, re.S)
    cells = re.findall(r"<td([^>]*)>(.*?)</td>", tbody_match.group(), re.S)

    groups = []
    i = 0
    while i < len(cells):
        attrs, text = cells[i]
        year_match = re.search(r"(\d{4})年", text)
        rowspan_match = re.search(r'rowspan="(\d+)"', attrs)
        if not year_match or not rowspan_match:
            raise ValueError("配当テーブルの解析に失敗しました(IRBANKのページ構造が変更された可能性があります)")
        year = int(year_match.group(1))
        rowspan = int(rowspan_match.group(1))
        i += 1

        rows = []
        for _ in range(rowspan):
            row_cells = cells[i:i + cols_per_row]
            i += cols_per_row
            status = strip_tags(row_cells[sub_index["区分"]][1])
            total = parse_number(row_cells[sub_index["合計"]][1])
            adjusted = (
                parse_number(row_cells[sub_index["分割調整"]][1])
                if "分割調整" in sub_index else total
            )
            dividend_yield = (
                parse_percent(row_cells[sub_index["配当利回り"]][1])
                if "配当利回り" in sub_index else None
            )
            rows.append({"status": status, "total": total, "adjusted": adjusted, "yield": dividend_yield})
        groups.append({"year": year, "rows": rows})

    return company_name, groups, url


def get_dividend(code: str, year: int, use_adjusted: bool = False):
    company_name, groups, url = fetch_dividend_table(code)
    matches = [g for g in groups if g["year"] == year]
    if not matches:
        raise ValueError(f"{company_name}({code}) の {year}年度のデータは見つかりませんでした")

    key = "adjusted" if use_adjusted else "total"
    # 表内の最後の行から遡り、値が入っている最初の行(=最新の予想/修正/実績)を採用する。
    # 「実績」行が確定前の空欄(プレースホルダー)であることがあるため。
    latest_row = None
    for row in reversed(matches[-1]["rows"]):
        if row[key] is not None:
            latest_row = row
            break
    if latest_row is None:
        latest_row = matches[-1]["rows"][-1]

    return {
        "code": code,
        "company_name": company_name,
        "year": year,
        "dividend": latest_row[key],
        "dividend_yield": latest_row["yield"],
        "status": latest_row["status"],
        "source_url": url,
    }


def main():
    parser = argparse.ArgumentParser(description="IRBANKから指定銘柄・年度の配当金を取得する")
    parser.add_argument("code", help="銘柄コード (例: 1605)")
    parser.add_argument("year", type=int, help="年度 (例: 2025)")
    parser.add_argument("--adjusted", action="store_true", help="株式分割調整後の値を使う")
    args = parser.parse_args()

    try:
        result = get_dividend(args.code, args.year, args.adjusted)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    dividend = result["dividend"]
    dividend_str = "未発表" if dividend is None else f"{dividend}円"
    yield_value = result["dividend_yield"]
    yield_str = "不明" if yield_value is None else f"{yield_value}%"
    print(f"{result['company_name']}({result['code']}) {result['year']}年度の配当: "
          f"{dividend_str}（配当利回り: {yield_str}） [{result['status']}]")
    print(f"出典: {result['source_url']}")


if __name__ == "__main__":
    main()
