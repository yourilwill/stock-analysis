#!/usr/bin/env python3
"""指定日にTDnetで決算短信を発表した銘柄と、analysis_results/内の分析済み銘柄・
holdings_raw/内の保有銘柄を突き合わせるCLIツール。

TDnet（適時開示情報閲覧サービス）は素のUser-Agentだと403を返すため、
irbank_utils.fetch_with_retry（ブラウザ相当のUA付き）で取得する。

使い方:
    python3 tdnet_earnings_check.py <YYYYMMDD or YYYY-MM-DD>
    python3 tdnet_earnings_check.py 20260730
    python3 tdnet_earnings_check.py 2026-07-30
"""
import argparse
import json
import re
import sys
from pathlib import Path

import requests

from holdings_loader import load_held_stocks
from irbank_utils import fetch_with_retry

TDNET_LIST_URL = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date}.html"
ANALYSIS_RESULTS_DIR = Path(__file__).resolve().parent.parent / "analysis_results"


def normalize_date(s: str) -> str:
    s = s.replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{8}", s):
        raise ValueError(f"日付はYYYYMMDDまたはYYYY-MM-DD形式で指定してください: {s}")
    return s


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return s.replace("　", " ").strip()


def fetch_page(date: str, page: int) -> str:
    url = TDNET_LIST_URL.format(page=page, date=date)
    resp = fetch_with_retry(url)
    resp.encoding = "utf-8"  # サーバーがcharsetを正しく返さず文字化けするため明示指定
    return resp.text


def parse_rows(html: str):
    """開示情報テーブルから(時刻, コード, 会社名, タイトル)のタプル列を抽出。"""
    start = html.find('id="main-list-table"')
    end = html.find('id="kaiji-info-box-bottom"')
    if start == -1 or end == -1:
        return []
    snippet = html[start:end]
    cells = [strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", snippet, re.S)]

    rows = []
    i = 0
    n = len(cells)
    while i < n:
        if re.fullmatch(r"\d{1,2}:\d{2}", cells[i]):
            time_, code, name, title = (cells[i:i + 4] + ["", "", "", ""])[:4]
            rows.append((time_, code, name, title))
            i += 4
        else:
            i += 1
    return rows


def total_count(html: str) -> int:
    m = re.search(r"全(\d+)件", html)
    return int(m.group(1)) if m else 0


def fetch_all_rows(date: str):
    first_html = fetch_page(date, 1)
    total = total_count(first_html)
    rows = parse_rows(first_html)
    pages = (total + 99) // 100 if total else 1
    for page in range(2, pages + 1):
        html = fetch_page(date, page)
        rows.extend(parse_rows(html))
    return rows, total


# 全角数字→半角の変換テーブル（決算短信タイトルの「第１四半期」等に対応）
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def classify_kessan(title: str) -> str:
    """決算短信タイトルから決算区分（第1〜3四半期／中間／本決算）を判定。"""
    t = title.translate(_ZEN2HAN)
    m = re.search(r"第(\d)四半期", t)
    if m:
        return f"第{m.group(1)}四半期"
    if "中間" in t or "半期" in t:
        return "中間期"
    return "本決算"


def load_analyzed_companies():
    """analysis_results/配下のディレクトリから {証券コード: (会社名, 分析日, HTMLパス)} を作る。"""
    companies = {}
    if not ANALYSIS_RESULTS_DIR.is_dir():
        return companies
    for d in sorted(ANALYSIS_RESULTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        m = re.match(r"^([0-9A-Za-z]+)_(.+)$", d.name)
        if not m:
            continue
        code, name = m.group(1), m.group(2)
        html_files = sorted(d.glob("*.html"))
        if not html_files:
            continue
        html_path = html_files[-1]
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        date_m = re.search(r"分析日[:：]\s*([0-9年/月日\-]+)", html)
        analyzed_at = date_m.group(1).strip() if date_m else "不明"
        companies[code] = (name, analyzed_at, html_path, html)
    return companies


def is_reflected(html: str, date: str) -> bool:
    """レポートHTML内に該当日付の追記があるか判定。
    「YYYY年M月D日」（漢字）表記に加え、「YYYY/M/D」「YYYY/MM/DD」（スラッシュ）
    「YYYY-MM-DD」（ハイフン）表記でも追記されるケースがあるため、いずれかの
    表記が見つかれば反映済みとみなす。
    """
    y, m, d = int(date[:4]), int(date[4:6]), int(date[6:8])
    candidates = [
        f"{y}年{m}月{d}日",
        f"{y}/{m}/{d}",
        f"{y}/{m:02d}/{d:02d}",
        f"{y}-{m:02d}-{d:02d}",
    ]
    return any(c in html for c in candidates)


def main():
    parser = argparse.ArgumentParser(description="TDnetの決算短信発表銘柄と分析済み銘柄(analysis_results/)を突き合わせる")
    parser.add_argument("date", help="対象日（YYYYMMDD または YYYY-MM-DD）")
    parser.add_argument("--json", action="store_true", help="人間向けテキストの代わりに構造化JSONを標準出力へ出す(他リポジトリからの呼び出し向け)")
    args = parser.parse_args()

    try:
        date = normalize_date(args.date)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        rows, total = fetch_all_rows(date)
    except (ValueError, requests.RequestException) as e:
        print(f"エラー: TDnetの取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    kessan_rows = [r for r in rows if "決算短信" in r[3]]
    analyzed = load_analyzed_companies()
    held = load_held_stocks()

    # 保有していてかつ分析済みの銘柄は、情報として上位互換のanalyzed_matches側にのみ
    # 載せる(held_matches側からは除外、design_doc/stock-earnings-notify-holdings.md参照)。
    analyzed_matches = []
    held_matches = []
    for time_, code, name, title in kessan_rows:
        code4 = code[:4]
        if code4 in analyzed:
            a_name, analyzed_at, html_path, html = analyzed[code4]
            analyzed_matches.append({
                "code": code4,
                "name": name,
                "kubun": classify_kessan(title),
                "analyzed_at": analyzed_at,
                "reflected": is_reflected(html, date),
            })
        elif code4 in held:
            held_matches.append({
                "code": code4,
                "name": name,
                "kubun": classify_kessan(title),
            })

    if args.json:
        print(json.dumps({
            "date": date,
            "total": total,
            "kessan_count": len(kessan_rows),
            "analyzed_matches": analyzed_matches,
            "held_matches": held_matches,
        }, ensure_ascii=False))
        return

    y, m, d = date[:4], date[4:6], date[6:8]
    print(f"{y}年{int(m)}月{int(d)}日 TDnet開示件数: 全{total}件（うち決算短信 {len(kessan_rows)}件）")
    print()
    if not analyzed_matches:
        print("分析済み銘柄との一致はありませんでした。")
        return

    print(f"{'コード':<6} {'銘柄名':<14} {'決算区分':<8} {'分析日':<14} {'反映状況'}")
    print("-" * 70)
    for r in analyzed_matches:
        reflected = "反映済み" if r["reflected"] else "未反映"
        print(f"{r['code']:<6} {r['name']:<14} {r['kubun']:<8} {r['analyzed_at']:<14} {reflected}")


if __name__ == "__main__":
    main()
