#!/usr/bin/env python3
"""当日の市場概況と、目安の配当利回り(過去5年平均)に到達した銘柄をまとめるCLIツール。

株価・会社予想配当・指数は Yahoo!ファイナンス(finance.yahoo.co.jp)の銘柄ページから取得する。
IRBANKは当日終値の反映が翌日以降になることがあるため、引け後のジョブには使わない。
目安利回りは scripts/yield_targets.py が作る market_watch/yield_targets.json を使う。

到達判定: 予想配当利回り(会社予想1株配当 ÷ 終値) >= 目安利回り。無配(予想配当0・不明)は対象外。
保有銘柄は「買い増し候補」として、簿価利回り(予想配当 ÷ 平均取得価額)と、
100株買い増した場合の平均取得価額・簿価利回りも出す。未保有は「新規候補」。

前回までに到達済みだった銘柄は market_watch/alert_state.json に記録し、
新たに到達した銘柄だけを目立たせる(--dry-run では状態を更新しない)。

使い方:
    python3 scripts/market_digest.py            # Slack向けテキストを出力し、状態を更新
    python3 scripts/market_digest.py --json     # JSONで出力
    python3 scripts/market_digest.py --dry-run  # 状態ファイルを更新しない
"""
import argparse
import html
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests

from holdings_loader import HOLDINGS_RAW_DIR, load_held_positions
from irbank_utils import UA
from yield_targets import load_targets

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "market_watch" / "alert_state.json"
QUOTE_URL = "https://finance.yahoo.co.jp/quote/{symbol}"
# 1.5秒間隔では約90ページ目からブロックされた(2026-11-07)。終値ベースの判定で急ぐ必要は
# ないため、1回の実行に15分程度かかってもよい前提で間隔を大きく取る。
REQUEST_INTERVAL_SEC = 6.0
BLOCK_RETRIES = 3
BLOCK_BACKOFF_SEC = 60
# この件数だけ連続でブロックされたら、残りは取得せず打ち切る(ブロックの長期化を避ける)。
MAX_CONSECUTIVE_BLOCKS = 2
# 6秒間隔でも約100ページ目でブロックされた(2026-11-08)。件数で制限されている可能性があるため、
# この件数ごとに長めの休憩を挟む。
BATCH_SIZE = 40
BATCH_PAUSE_SEC = 600
# 株価欄の抽出に失敗したページの保存先(正規表現の調整用)
DEBUG_DIR = REPO_ROOT / "market_watch" / "debug"
ADD_ON_SHARES = 100

# (表示名, Yahoo!ファイナンスのシンボル)
INDICES = [
    ("日経平均", "998407.O"),
    ("TOPIX", "998405.T"),
    ("NYダウ", "^DJI"),
    ("S&P500", "^GSPC"),
    ("ナスダック", "^IXIC"),
]

# 価格の前に「決算発表予定日： 2026/11/9」、前日比の後に「年初来安値」等のバッジが入ることがある。
_PRICE_RE = re.compile(
    r"ポートフォリオに追加 (?:\S+ ){0,3}?([\d,]+(?:\.\d+)?) 前日比 ([+\-]?[\d,]+(?:\.\d+)?) "
    r"\( ([+\-]?[\d.]+) % \) (?:\S+ ){1,3}?(\d{1,2}/\d{1,2}|\d{1,2}:\d{2})(?: |$)"
)
# 売買停止・上場廃止予定などで株価が「---」表示になっている
_NO_PRICE_RE = re.compile(r"ポートフォリオに追加 (?:\S+ ){0,3}?--- 前日比 ---")
_DPS_RE = re.compile(r"1株配当 （会社予想） 用語 ([\d,.]+|---) 円")


def _num(s: str):
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


class QuoteBlocked(Exception):
    """Yahoo!ファイナンスが一時エラーページ(HTTP 5xx・「現在表示できません」)を返し続けた。"""


def _fetch_page(symbol: str) -> str:
    # 短時間に約120ページ取得すると、Yahoo!側が数十分単位で5xx(エラーページ)を返すようになる。
    # irbank_utils.fetch_with_retry の1〜4秒の待ちでは回復しないため、長めに待って再試行する。
    for attempt in range(BLOCK_RETRIES):
        resp = requests.get(QUOTE_URL.format(symbol=symbol), headers={"User-Agent": UA}, timeout=30)
        if resp.status_code < 500 and "現在表示できません" not in resp.text:
            resp.raise_for_status()
            return resp.text
        if attempt < BLOCK_RETRIES - 1:
            time.sleep(BLOCK_BACKOFF_SEC * (attempt + 1))
    raise QuoteBlocked(f"{symbol}: Yahoo!ファイナンスが一時エラーを返し続けました(HTTP {resp.status_code})")


def fetch_quote(symbol: str) -> dict:
    """{"price", "change", "change_pct", "as_of", "dps"} を返す。取れない項目はNone。"""
    page = _fetch_page(symbol)
    s = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s)))
    m = _PRICE_RE.search(text)
    if not m and _NO_PRICE_RE.search(text):
        raise ValueError(f"{symbol}: 株価が表示されていません(売買停止・上場廃止予定の可能性)")
    if not m:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{symbol}.html").write_text(page, encoding="utf-8")
        raise ValueError(f"{symbol}: 株価欄が見つかりません(ページを{DEBUG_DIR.name}/{symbol}.htmlに保存)")
    dps_m = _DPS_RE.search(text)
    return {
        "price": _num(m.group(1)),
        "change": _num(m.group(2)),
        "change_pct": _num(m.group(3)),
        # 日付(11/6)か、当日更新分は日本時間の時刻(15:30。米国指数は引け後の早朝6:37等)
        "as_of": m.group(4),
        "dps": _num(dps_m.group(1)) if dps_m else None,
    }


def _progress(msg: str) -> None:
    """進捗を標準エラー出力へ(標準出力はSlack本文/JSON専用)。"""
    print(msg, file=sys.stderr, flush=True)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect(dry_run: bool) -> dict:
    today = date.today()
    targets = load_targets()
    positions = load_held_positions()
    holdings_files = sorted(HOLDINGS_RAW_DIR.glob("*.csv")) if HOLDINGS_RAW_DIR.is_dir() else []
    holdings_date = None
    if holdings_files:
        m = re.search(r"_(\d{8})_", holdings_files[-1].name)
        holdings_date = m.group(1) if m else None

    indices, errors = [], []
    for label, symbol in INDICES:
        try:
            q = fetch_quote(symbol)
            indices.append({"name": label, **q})
        except (ValueError, requests.RequestException, QuoteBlocked) as e:
            errors.append(f"{label}: {e}")
        time.sleep(REQUEST_INTERVAL_SEC)

    stocks, failed_codes, skipped = [], set(), []
    consecutive_blocks = 0
    candidates = [(c, t) for c, t in targets.items() if t.get("target_yield") is not None]
    for i, (code, t) in enumerate(candidates):
        if i and i % BATCH_SIZE == 0:
            _progress(f"{i}/{len(candidates)}件取得、ブロック回避のため{BATCH_PAUSE_SEC}秒休憩")
            time.sleep(BATCH_PAUSE_SEC)
        _progress(f"[{i + 1}/{len(candidates)}] {code} {t.get('name', '')}")
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            skipped = [f"{c} {tt.get('name', '')}" for c, tt in candidates[i:]]
            failed_codes.update(c for c, _ in candidates[i:])
            break
        try:
            q = fetch_quote(f"{code}.T")
            consecutive_blocks = 0
        except (ValueError, requests.RequestException, QuoteBlocked) as e:
            consecutive_blocks = consecutive_blocks + 1 if isinstance(e, QuoteBlocked) else 0
            errors.append(f"{code} {t.get('name', '')}: {e}")
            _progress(f"  失敗: {e}")
            failed_codes.add(code)
            continue
        finally:
            time.sleep(REQUEST_INTERVAL_SEC)
        if not q["price"] or not q["dps"]:
            continue  # 無配・予想未定
        cur_yield = q["dps"] / q["price"] * 100
        item = {
            "code": code,
            "name": t.get("name", ""),
            "price": q["price"],
            "change_pct": q["change_pct"],
            "as_of": q["as_of"],
            "dps": q["dps"],
            "yield": round(cur_yield, 2),
            "target_yield": t["target_yield"],
            "target_note": t.get("note"),
            "reached": cur_yield >= t["target_yield"],
            # 目安利回りに届く株価(これ以下なら到達)
            "trigger_price": round(q["dps"] / t["target_yield"] * 100),
            "held": code in positions,
        }
        if item["held"]:
            p = positions[code]
            new_qty = p["quantity"] + ADD_ON_SHARES
            new_cost = (p["quantity"] * p["avg_cost"] + ADD_ON_SHARES * q["price"]) / new_qty
            item.update({
                "quantity": p["quantity"],
                "avg_cost": round(p["avg_cost"], 1),
                "book_yield": round(q["dps"] / p["avg_cost"] * 100, 2),
                "add_on_avg_cost": round(new_cost, 1),
                "add_on_book_yield": round(q["dps"] / new_cost * 100, 2),
            })
        stocks.append(item)

    state = load_state()
    reached = [s for s in stocks if s["reached"]]
    for s in reached:
        s["first_reached"] = state.get(s["code"], today.isoformat())
        s["new"] = s["code"] not in state
    if not dry_run:
        # 到達中の銘柄だけを残す(一度外れて再到達したら、また「新規」として通知する)。
        # 取得に失敗した銘柄は判定できないので前回の状態を引き継ぐ(翌日に「新規」と誤報しないため)。
        new_state = {c: d for c, d in state.items() if c in failed_codes}
        new_state.update({s["code"]: s["first_reached"] for s in reached})
        save_state(new_state)

    return {
        "date": today.isoformat(),
        "indices": indices,
        "reached": sorted(reached, key=lambda s: s["yield"] - s["target_yield"], reverse=True),
        "checked_count": len(stocks),
        "holdings_file_date": holdings_date,
        "errors": errors,
        "skipped": skipped,
    }


def _fmt_pct(v) -> str:
    return "---" if v is None else f"{v:+.2f}%"


def _stock_line(s: dict) -> str:
    line = (f"• {s['code']} {s['name']}  {s['price']:,.0f}円({_fmt_pct(s['change_pct'])})  "
            f"利回り *{s['yield']:.2f}%* (目安{s['target_yield']:.2f}%、{s['trigger_price']:,}円以下)")
    if s["held"]:
        line += (f"\n    簿価利回り{s['book_yield']:.2f}%(取得単価{s['avg_cost']:,.0f}円×{s['quantity']:.0f}株)"
                 f" → 100株買い増しで取得単価{s['add_on_avg_cost']:,.0f}円・簿価利回り{s['add_on_book_yield']:.2f}%")
    if s.get("target_note"):
        line += f"\n    ⚠ 目安値メモ: {s['target_note']}"
    return line


def format_slack(d: dict) -> str:
    lines = [f"📊 *市場概況 {d['date']}*"]
    for i in d["indices"]:
        lines.append(f"• {i['name']} {i['price']:,.2f}({_fmt_pct(i['change_pct'])}, {i['as_of']})")

    new = [s for s in d["reached"] if s["new"]]
    cont = [s for s in d["reached"] if not s["new"]]
    lines.append("")
    if new:
        lines.append(f"🔔 *新たに目安利回り(過去5年平均)に到達: {len(new)}件*")
        held_new = [s for s in new if s["held"]]
        fresh_new = [s for s in new if not s["held"]]
        if held_new:
            lines.append("*【保有・買い増し候補】*")
            lines += [_stock_line(s) for s in held_new]
        if fresh_new:
            lines.append("*【未保有・新規候補】*")
            lines += [_stock_line(s) for s in fresh_new]
    else:
        lines.append("🔔 新たに目安利回りに到達した銘柄はありません")
    if cont:
        lines.append("")
        lines.append(f"📌 到達中(継続): {len(cont)}件")
        lines += [f"• {s['code']} {s['name']} {s['yield']:.2f}%(目安{s['target_yield']:.2f}%"
                  f"{'・保有' if s['held'] else ''}、{s['first_reached']}〜)" for s in cont]

    lines.append("")
    lines.append(f"_対象{d['checked_count']}銘柄(分析済み＋保有、無配除く)。"
                 f"保有データ: {d['holdings_file_date'] or 'なし'}時点_")
    if d["errors"]:
        lines.append(f"_取得失敗 {len(d['errors'])}件:_")
    for e in d["errors"]:
        lines.append(f"_  {e}_")
    if d.get("skipped"):
        lines.append(f"_Yahoo!ファイナンスに一時ブロックされたため残り{len(d['skipped'])}銘柄は未チェック_")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="市場概況と目安利回り到達銘柄をまとめる")
    parser.add_argument("--json", action="store_true", help="JSONで出力する")
    parser.add_argument("--dry-run", action="store_true", help="到達状態ファイルを更新しない")
    args = parser.parse_args()

    d = collect(args.dry_run)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(format_slack(d))


if __name__ == "__main__":
    sys.exit(main())
