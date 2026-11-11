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
from datetime import date, datetime
from pathlib import Path

import requests

from holdings_loader import HOLDINGS_RAW_DIR, load_held_positions
from irbank_utils import UA
from yield_targets import analyzed_stocks, load_targets

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "market_watch" / "alert_state.json"
# 全銘柄の取得結果(日付ごと)。build_static_site.py がダッシュボードページの生成に使う。
QUOTES_DIR = REPO_ROOT / "market_watch" / "quotes"
# 売買が成立しなかった日は株価が「---」になる。前日終値の日付がこの日数以内なら
# 前日終値で判定し、それより古ければ売買停止・上場廃止とみなす。
NO_TRADE_MAX_DAYS = 10
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
# その日に売買が成立しなかった・売買停止・上場廃止予定などで株価が「---」表示になっている
_NO_PRICE_RE = re.compile(r"ポートフォリオに追加 (?:\S+ ){0,3}?--- 前日比 ---")
_PREV_CLOSE_RE = re.compile(r"前日終値 用語 ([\d,]+(?:\.\d+)?) \( (\d{1,2})/(\d{1,2}) \)")
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
        return _no_trade_quote(symbol, text)
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


def _no_trade_quote(symbol: str, text: str) -> dict:
    """株価が「---」のとき、前日終値が直近なら「当日売買なし」として前日終値で返す。

    出来高の少ない銘柄は売買が1株も成立しない日がある(2026-11-10のゼネラルパッカー)。
    前日終値の日付が古い場合は、売買停止・上場廃止(例: イーグル工業は9/28で止まっていた)。
    """
    pm = _PREV_CLOSE_RE.search(text)
    if not pm:
        raise ValueError(f"{symbol}: 株価が表示されていません(売買停止・上場廃止予定の可能性)")
    today = date.today()
    month, day = int(pm.group(2)), int(pm.group(3))
    prev = date(today.year, month, day)
    if prev > today:  # 年をまたいだ場合(1月に見た12/30など)
        prev = date(today.year - 1, month, day)
    if (today - prev).days > NO_TRADE_MAX_DAYS:
        raise ValueError(f"{symbol}: 株価が{prev.isoformat()}から更新されていません(売買停止・上場廃止の可能性)")
    dps_m = _DPS_RE.search(text)
    return {
        "price": _num(pm.group(1)),
        "change": None,
        "change_pct": None,
        "as_of": f"{month}/{day}",
        "dps": _num(dps_m.group(1)) if dps_m else None,
        "no_trade": True,
    }


def _progress(msg: str) -> None:
    """進捗を標準エラー出力へ(標準出力はSlack本文/JSON専用)。"""
    print(msg, file=sys.stderr, flush=True)


def _base_row(code: str, name: str, positions: dict, analyzed: dict, t: dict, error=None) -> dict:
    """ダッシュボード用の1銘柄分の行。株価が取れなくても、銘柄情報と保有情報は埋める。"""
    row = {
        "code": code,
        "name": name,
        "held": code in positions,
        # analysis_results/ のディレクトリ名(= 株分析サイトの銘柄ページのパス)
        "analysis_dir": f"{code}_{analyzed[code][0]}" if code in analyzed else None,
        "target_yield": t.get("target_yield"),
        "target_source": t.get("source"),
        "target_note": t.get("note"),
    }
    if code in positions:
        p = positions[code]
        row.update({"quantity": p["quantity"], "avg_cost": round(p["avg_cost"], 1)})
    if error:
        row["error"] = error
    return row


def save_snapshot(today: date, indices: list, rows: list, holdings_date, errors: list, skipped: list) -> None:
    """全銘柄の取得結果を market_watch/quotes/YYYY-MM-DD.json に保存する。

    watch_and_build.sh がこのディレクトリを監視しており、書き込まれると株分析サイトが
    再ビルドされてダッシュボードページが更新される。一時ファイル経由で書くのは、
    書きかけのJSONをビルドに読まれないようにするため。
    """
    QUOTES_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "holdings_file_date": holdings_date,
        "indices": indices,
        "stocks": rows,
        "errors": errors,
        "skipped": skipped,
    }
    path = QUOTES_DIR / f"{today.isoformat()}.json"
    tmp = QUOTES_DIR / f".{path.name}.part"
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


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

    rows, stocks, failed_codes, skipped = [], [], set(), []
    consecutive_blocks = 0
    # yield_targets.json には保有をやめた(未分析の)銘柄のエントリも残るため、
    # 今の保有銘柄＋分析済み銘柄に絞る(売却済みの銘柄を「新規候補」として出さない)。
    # 目安利回りが無い銘柄(無配・優先株など)も、ダッシュボード用に株価は取る。
    analyzed = analyzed_stocks()
    candidates = sorted(set(positions) | set(analyzed))

    def name_of(code: str) -> str:
        if targets.get(code, {}).get("name"):
            return targets[code]["name"]
        return positions[code]["name"] if code in positions else analyzed[code][0]

    for i, code in enumerate(candidates):
        t = targets.get(code, {})
        name = name_of(code)
        if i and i % BATCH_SIZE == 0:
            _progress(f"{i}/{len(candidates)}件取得、ブロック回避のため{BATCH_PAUSE_SEC}秒休憩")
            time.sleep(BATCH_PAUSE_SEC)
        _progress(f"[{i + 1}/{len(candidates)}] {code} {name}")
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            skipped = [f"{c} {name_of(c)}" for c in candidates[i:]]
            failed_codes.update(candidates[i:])
            for c in candidates[i:]:
                rows.append(_base_row(c, name_of(c), positions, analyzed, t=targets.get(c, {}),
                                      error="Yahoo!ファイナンスの一時ブロックにより未取得"))
            break
        row = _base_row(code, name, positions, analyzed, t)
        rows.append(row)
        try:
            q = fetch_quote(f"{code}.T")
            consecutive_blocks = 0
        except (ValueError, requests.RequestException, QuoteBlocked) as e:
            consecutive_blocks = consecutive_blocks + 1 if isinstance(e, QuoteBlocked) else 0
            errors.append(f"{code} {name}: {e}")
            row["error"] = str(e)
            _progress(f"  失敗: {e}")
            failed_codes.add(code)
            continue
        finally:
            time.sleep(REQUEST_INTERVAL_SEC)
        row.update({
            "price": q["price"],
            "change": q["change"],
            "change_pct": q["change_pct"],
            "as_of": q["as_of"],
            "no_trade": q.get("no_trade", False),
            "dps": q["dps"],
        })
        if q["price"] and q["dps"]:
            row["yield"] = round(q["dps"] / q["price"] * 100, 2)
        if row["held"] and q["price"]:
            p = positions[code]
            row.update({
                "market_value": round(q["price"] * p["quantity"]),
                "unrealized_pl": round((q["price"] - p["avg_cost"]) * p["quantity"]),
                "unrealized_pl_pct": round((q["price"] / p["avg_cost"] - 1) * 100, 2),
            })
            if q["dps"]:
                new_qty = p["quantity"] + ADD_ON_SHARES
                new_cost = (p["quantity"] * p["avg_cost"] + ADD_ON_SHARES * q["price"]) / new_qty
                row.update({
                    "annual_dividend": round(q["dps"] * p["quantity"]),
                    "book_yield": round(q["dps"] / p["avg_cost"] * 100, 2),
                    "add_on_avg_cost": round(new_cost, 1),
                    "add_on_book_yield": round(q["dps"] / new_cost * 100, 2),
                })
        if t.get("target_yield") is None or not q["price"] or not q["dps"]:
            continue  # 目安なし(無配・優先株など)・予想未定は到達判定しない
        cur_yield = q["dps"] / q["price"] * 100
        row.update({
            "reached": cur_yield >= t["target_yield"],
            # 目安利回りに届く株価(これ以下なら到達)
            "trigger_price": round(q["dps"] / t["target_yield"] * 100),
        })
        stocks.append(row)

    state = load_state()
    reached = [s for s in stocks if s["reached"]]
    for s in reached:
        s["first_reached"] = state.get(s["code"], today.isoformat())
        s["new"] = s["code"] not in state
    if not dry_run:
        save_snapshot(today, indices, rows, holdings_date, errors, skipped)
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
