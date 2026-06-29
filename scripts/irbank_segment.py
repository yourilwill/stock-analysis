#!/usr/bin/env python3
"""IRBANKから指定銘柄のセグメント別売上高・営業利益・利益率を取得するCLIツール。

ステップ2（業容とビジネスモデル）に使用。WebFetch不要化。

使い方:
    python3 irbank_segment.py <銘柄コード or Eコード>
    python3 irbank_segment.py 4627
    python3 irbank_segment.py E00915
    python3 irbank_segment.py E00915 --years 3  # 直近3期のみ
"""
import argparse
import re
import sys

from irbank_utils import fetch_with_retry

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_oku(cell_html: str):
    """「54.9億」→5490、「489百万」→489、「11.6億」→1160 に変換（百万円単位）。"""
    text = cell_html.split("<br>")[0]
    text = strip_tags(text).strip()
    if not text or text == "-":
        return None
    m = re.match(r"([\d.]+)億", text)
    if m:
        return round(float(m.group(1)) * 100)
    m = re.match(r"([\d.]+)百万", text)
    if m:
        return round(float(m.group(1)))
    m = re.match(r"([\d.]+)千万", text)
    if m:
        return round(float(m.group(1)) * 10)
    return None


def resolve_ecode(code: str) -> str:
    if re.match(r"^E\d+$", code, re.I):
        return code.upper()
    resp = fetch_with_retry(f"https://irbank.net/{code}", allow_redirects=True)
    m = re.search(r'href="/(E\d{5})', resp.text)
    if m:
        return m.group(1)
    raise ValueError(f"Eコードが解決できませんでした（コード: {code}）")


def fetch_segment(ecode: str):
    url = f"https://irbank.net/{ecode}/segment?tm=100"
    resp = fetch_with_retry(url)
    html = resp.text

    name_m = re.search(r'<meta property="og:title" content="([^（\(]+)', html)
    company = name_m.group(1).strip() if name_m else ecode

    table_m = re.search(r'<table class="bar bs">(.*?)</table>', html, re.S)
    if not table_m:
        raise ValueError(f"セグメントテーブルが見つかりません: {url}")
    table = table_m.group(1)

    # セグメント名（ヘッダー行の3列目以降）
    thead_m = re.search(r"<thead[^>]*>.*?<tr[^>]*>(.*?)</tr>", table, re.S)
    if not thead_m:
        raise ValueError("ヘッダー行が見つかりません")
    ths = re.findall(r"<th[^>]*>(.*?)</th>", thead_m.group(1), re.S)
    segments = [strip_tags(th) for th in ths[2:]]  # 「科目」「年度」を除く
    if not segments:
        raise ValueError("セグメント名が取得できませんでした")

    seg_count = len(segments)
    # {科目: {年度: {セグメント名: 値(百万円)}}}
    data: dict[str, dict[str, dict[str, int | None]]] = {}
    current_subject: str | None = None
    years_order: list[str] = []

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
    for row in rows:
        td_list = re.findall(r"<t[dh]([^>]*)>(.*?)</t[dh]>", row, re.S)
        if not td_list:
            continue

        first_attrs, first_content = td_list[0]
        first_text = strip_tags(first_content)

        # ヘッダー行（「科目」セルを含む）はスキップ
        if "ct" in first_attrs:
            continue

        if "rowspan" in first_attrs:
            # 科目行の1行目: 科目セル + 年度セル + 値×N
            subject_span = re.search(r"<span[^>]*>(.*?)</span>", first_content, re.S)
            current_subject = strip_tags(subject_span.group(1)) if subject_span else first_text
            data.setdefault(current_subject, {})
            rest = td_list[1:]
            if len(rest) >= 1 + seg_count:
                year = strip_tags(rest[0][1])
                if re.match(r"\d{4}/\d{2}", year):
                    data[current_subject][year] = {
                        seg: parse_oku(rest[1 + i][1]) for i, seg in enumerate(segments)
                    }
                    if year not in years_order:
                        years_order.append(year)
        elif current_subject:
            # 同科目の後続行: 年度セル + 値×N
            if len(td_list) >= 1 + seg_count:
                year = strip_tags(td_list[0][1])
                if re.match(r"\d{4}/\d{2}", year):
                    data.setdefault(current_subject, {})[year] = {
                        seg: parse_oku(td_list[1 + i][1]) for i, seg in enumerate(segments)
                    }
                    if year not in years_order:
                        years_order.append(year)

    return company, segments, data, years_order, url


def fmt(v, width: int = 8) -> str:
    if v is None:
        return "-".rjust(width)
    return f"{v:,}".rjust(width)


def pct(v, total) -> str:
    if v is None or not total:
        return "-".rjust(7)
    return f"{v / total * 100:.1f}%".rjust(7)


def main():
    parser = argparse.ArgumentParser(description="IRBANKからセグメント別財務データを取得（ステップ2用）")
    parser.add_argument("code", help="銘柄コード(例: 4627) または Eコード(例: E00915)")
    parser.add_argument("--years", type=int, default=None, help="直近N期のみ表示 (省略時: 全件)")
    args = parser.parse_args()

    try:
        ecode = resolve_ecode(args.code)
        company, segments, data, years_order, url = fetch_segment(ecode)
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if not years_order:
        print("エラー: セグメントデータが取得できませんでした", file=sys.stderr)
        sys.exit(1)

    display_years = years_order[-args.years:] if args.years else years_order

    print(f"{company}({args.code}) セグメント別財務データ（百万円）")
    print(f"出典: {url}")

    subjects = [s for s in ("売上高", "営業利益") if s in data]
    if not subjects:
        subjects = list(data.keys())

    # 最新期のサマリーテーブル
    latest_year = display_years[-1]
    rev_data = data.get("売上高", {}).get(latest_year, {})
    op_data = data.get("営業利益", {}).get(latest_year, {})

    total_rev = sum(v for v in rev_data.values() if v is not None)
    total_op = sum(v for v in op_data.values() if v is not None)

    print()
    print(f"【{latest_year}（最新期）セグメント別】")
    seg_width = max(len(s) for s in segments) + 2
    header = f"{'セグメント':<{seg_width}} {'売上高':>8} {'売上構成比':>10} {'営業利益':>9} {'利益構成比':>10} {'利益率':>7}"
    print(header)
    print("-" * len(header))
    for seg in segments:
        rev = rev_data.get(seg)
        op = op_data.get(seg)
        rate = f"{op / rev * 100:.1f}%" if rev and op is not None else "-"
        print(f"{seg:<{seg_width}} {fmt(rev)} {pct(rev, total_rev)} {fmt(op, 9)} {pct(op, total_op)} {rate:>7}")
    print("-" * len(header))
    print(f"{'セグメント計':<{seg_width}} {fmt(total_rev)} {'100.0%':>10} {fmt(total_op, 9)} {'100.0%':>10} {f'{total_op / total_rev * 100:.1f}%' if total_rev else '-':>7}")

    # 全期間の時系列
    if len(display_years) > 1:
        print()
        print("【売上高推移】")
        seg_cols = "  ".join(f"{s:>{max(8, len(s))}}" for s in segments)
        print(f"{'年度':<10}  {seg_cols}")
        print("-" * (10 + 2 + sum(max(8, len(s)) + 2 for s in segments)))
        for year in display_years:
            yr_data = data.get("売上高", {}).get(year, {})
            cols = "  ".join(fmt(yr_data.get(s), max(8, len(s))) for s in segments)
            print(f"{year:<10}  {cols}")

        print()
        print("【営業利益推移】")
        print(f"{'年度':<10}  {seg_cols}")
        print("-" * (10 + 2 + sum(max(8, len(s)) + 2 for s in segments)))
        for year in display_years:
            yr_data = data.get("営業利益", {}).get(year, {})
            cols = "  ".join(fmt(yr_data.get(s), max(8, len(s))) for s in segments)
            print(f"{year:<10}  {cols}")


if __name__ == "__main__":
    main()
