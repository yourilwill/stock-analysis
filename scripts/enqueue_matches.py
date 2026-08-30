#!/usr/bin/env python3
"""tdnet_earnings_check.py --json の出力(標準入力経由)を受け取り、
analyzed_matches(分析済み銘柄)を夜間バッチの積み残しキュー(analysis_results/queue_state.json)へ
追記するCLIツール。held_matches(保有のみで未分析)は対象外
(docs/design_doc/stock-analysis/nightly-batch.md参照)。

使い方:
    python3 tdnet_earnings_check.py <date> --json | python3 enqueue_matches.py
"""
import json
import sys
from pathlib import Path

QUEUE_STATE_PATH = Path(__file__).resolve().parent.parent / "analysis_results" / "queue_state.json"


def load_queue_state() -> list:
    if not QUEUE_STATE_PATH.exists():
        return []
    return json.loads(QUEUE_STATE_PATH.read_text(encoding="utf-8"))


def save_queue_state(queue: list) -> None:
    QUEUE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = QUEUE_STATE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(QUEUE_STATE_PATH)


def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"エラー: 標準入力がJSONとして読み取れませんでした: {e}", file=sys.stderr)
        sys.exit(1)

    detected_date = payload.get("date")
    analyzed_matches = payload.get("analyzed_matches")
    if not detected_date or analyzed_matches is None:
        print("エラー: 標準入力のJSONに date / analyzed_matches フィールドがありません", file=sys.stderr)
        sys.exit(1)

    queue = load_queue_state()
    existing_codes = {item["code"] for item in queue}

    added = []
    for match in analyzed_matches:
        code = match["code"]
        if code in existing_codes:
            # 既にキュー内(pending/in_progress/done/failed_needs_attentionいずれの状態でも)
            # 同一銘柄の重複投入はしない。
            continue
        queue.append({
            "code": code,
            "name": match["name"],
            "kubun": match["kubun"],
            "detected_date": detected_date,
            "status": "pending",
            "attempts": 0,
        })
        existing_codes.add(code)
        added.append(code)

    save_queue_state(queue)
    print(f"{len(added)}件をキューに追加しました(検知日: {detected_date})。" + (f" コード: {', '.join(added)}" if added else ""))


if __name__ == "__main__":
    main()
