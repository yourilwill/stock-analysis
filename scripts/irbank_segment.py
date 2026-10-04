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


UNIT_TO_MILLION = {"兆": 1_000_000, "億": 100, "千万": 10, "百万": 1}

# 売上・利益の科目名は業種や会計基準で異なる（不動産=営業収益、IFRS=売上収益 等）。先に見つかったものを使う。
REVENUE_SUBJECTS = ("売上高", "営業収益", "売上収益", "営業収入", "経常収益", "収益")
PROFIT_SUBJECTS = ("営業利益", "セグメント利益", "事業利益", "経常利益", "当期利益", "当期純利益")

# IRBANKのセグメント列には調整額（全社費用・セグメント間消去）が並ぶことがあるため、合計・構成比から除外する
ADJUSTMENT_PATTERN = re.compile(r"調整|消去|全社")


def parse_oku(cell_html: str):
    """「54.9億」→5490、「489百万」→489、「-16.6億」→-1660 に変換（百万円単位）。"""
    text = cell_html.split("<br>")[0]
    text = strip_tags(text).strip().replace(",", "")
    if not text or text == "-":
        return None
    m = re.match(r"(-?[\d.]+)(兆|億|千万|百万)", text)
    if m:
        value = float(m.group(1)) * UNIT_TO_MILLION[m.group(2)]
        # 1百万円未満は整数丸めで0（符号も消える）になるため小数1桁を残す
        return round(value) if abs(value) >= 1 else round(value, 1)
    return None


def pick_subject(data: dict, candidates: tuple[str, ...], year: str) -> str | None:
    """候補のうち指定年度に値がある科目を優先し、無ければ存在する最初の科目を返す。

    科目名は年度途中で変わることがある（例: 営業利益→セグメント利益）ため、年度ごとに選ぶ。
    """
    present = [s for s in candidates if s in data]
    with_value = [s for s in present if any(v is not None for v in data[s].get(year, {}).values())]
    return (with_value or present or [None])[0]


def subject_row(data: dict, candidates: tuple[str, ...], year: str) -> tuple[str | None, dict]:
    subject = pick_subject(data, candidates, year)
    return subject, data.get(subject, {}).get(year, {}) if subject else {}


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

    latest_year = display_years[-1]
    rev_subject, rev_data = subject_row(data, REVENUE_SUBJECTS, latest_year)
    op_subject, op_data = subject_row(data, PROFIT_SUBJECTS, latest_year)
    if not rev_subject or not op_subject:
        print(f"警告: 売上/利益の科目が特定できません（取得できた科目: {', '.join(data.keys())}）", file=sys.stderr)
    rev_label = rev_subject or "売上高"
    op_label = op_subject or "営業利益"

    # 最新期のサマリーテーブル

    business_segments = [s for s in segments if not ADJUSTMENT_PATTERN.search(s)]
    adjustment_segments = [s for s in segments if ADJUSTMENT_PATTERN.search(s)]
    rev_values = [rev_data.get(s) for s in business_segments if rev_data.get(s) is not None]
    op_values = [op_data.get(s) for s in business_segments if op_data.get(s) is not None]

    total_rev = sum(rev_values) if rev_values else None
    total_op = sum(op_values) if op_values else None
    # 赤字セグメントがあると合計比の構成比が100%を超えたり負になるため、利益構成比は黒字セグメント合計を分母にする
    total_op_positive = sum(v for v in op_values if v > 0)
    has_loss = any(v < 0 for v in op_values)

    print()
    print(f"【{latest_year}（最新期）セグメント別】")
    seg_width = max(len(s) for s in segments) + 2
    header = f"{'セグメント':<{seg_width}} {rev_label:>8} {'売上構成比':>10} {op_label:>9} {'利益構成比':>10} {'利益率':>7}"
    print(header)
    print("-" * len(header))
    for seg in business_segments:
        rev = rev_data.get(seg)
        op = op_data.get(seg)
        rate = f"{op / rev * 100:.1f}%" if rev and op is not None else "-"
        print(f"{seg:<{seg_width}} {fmt(rev)} {pct(rev, total_rev)} {fmt(op, 9)} {'赤字'.rjust(8) if op is not None and op < 0 else pct(op, total_op_positive)} {rate:>7}")
    print("-" * len(header))
    print(f"{'セグメント計':<{seg_width}} {fmt(total_rev)} {'100.0%' if total_rev else '-':>10} {fmt(total_op, 9)} {'100.0%' if total_op_positive else '-':>10} {f'{total_op / total_rev * 100:.1f}%' if total_rev and total_op is not None else '-':>7}")
    for seg in adjustment_segments:
        print(f"{seg:<{seg_width}} {fmt(rev_data.get(seg))} {'':>10} {fmt(op_data.get(seg), 9)}")
    if has_loss:
        print("※利益構成比は黒字セグメントの合計を分母に算出（赤字セグメントは除外）。セグメント計の利益は赤字を含む合算。")
    if adjustment_segments:
        print(f"※「{'」「'.join(adjustment_segments)}」列は調整額のためセグメント計・構成比から除外。")
    print("※「その他」等の列が実は調整額の場合もあるため、セグメント計＋調整額を決算短信の連結実績と照合すること。")

    # 全期間の時系列（科目名が年度で変わる場合は、その年度に値がある科目を使い末尾に注記する）
    if len(display_years) > 1:
        seg_cols = "  ".join(f"{s:>{max(8, len(s))}}" for s in segments)
        rule = "-" * (10 + 2 + sum(max(8, len(s)) + 2 for s in segments))
        for candidates, label in ((REVENUE_SUBJECTS, rev_label), (PROFIT_SUBJECTS, op_label)):
            print()
            print(f"【{label}推移】")
            print(f"{'年度':<10}  {seg_cols}")
            print(rule)
            for year in display_years:
                subject, yr_data = subject_row(data, candidates, year)
                cols = "  ".join(fmt(yr_data.get(s), max(8, len(s))) for s in segments)
                note = f"  ({subject})" if subject and subject != label and yr_data else ""
                print(f"{year:<10}  {cols}{note}")

if __name__ == "__main__":
    main()
