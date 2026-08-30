#!/usr/bin/env python3
"""積み残しキュー(analysis_results/queue_state.json)の現在の状況を1行JSONで出力する
読み取り専用CLI。personal_memo側の朝サマリー投稿(stock_analysis_queue_summaryロール)から
呼び出される。docs/design_doc/stock-analysis/nightly-batch.md参照。

使い方:
    python3 queue_status.py --json
"""
import argparse
import json
from pathlib import Path

QUEUE_STATE_PATH = Path(__file__).resolve().parent.parent / "analysis_results" / "queue_state.json"


def build_status() -> dict:
    if not QUEUE_STATE_PATH.exists():
        return {
            "pending_count": 0,
            "oldest_pending_detected_date": None,
            "last_batch_date": None,
            "last_batch_processed": [],
        }

    queue = json.loads(QUEUE_STATE_PATH.read_text(encoding="utf-8"))

    pending = [item for item in queue if item["status"] == "pending"]
    oldest_pending_detected_date = min((item["detected_date"] for item in pending), default=None)

    processed_dates = [item["processed_date"] for item in queue if item.get("processed_date")]
    last_batch_date = max(processed_dates, default=None)

    last_batch_processed = []
    if last_batch_date:
        last_batch_processed = [
            {
                "code": item["code"],
                "name": item["name"],
                "kubun": item["kubun"],
                "status": item["status"],
                "summary": item.get("summary", ""),
            }
            for item in queue
            if item.get("processed_date") == last_batch_date
        ]

    return {
        "pending_count": len(pending),
        "oldest_pending_detected_date": oldest_pending_detected_date,
        "last_batch_date": last_batch_date,
        "last_batch_processed": last_batch_processed,
    }


def main():
    parser = argparse.ArgumentParser(description="積み残しキューの現在の状況を出力する")
    parser.add_argument("--json", action="store_true", help="構造化JSONを標準出力へ出す(既定の唯一の出力形式)")
    args = parser.parse_args()

    status = build_status()

    if args.json:
        print(json.dumps(status, ensure_ascii=False))
        return

    print(f"pending: {status['pending_count']}件 (最古の検知日: {status['oldest_pending_detected_date'] or 'なし'})")
    print(f"前回バッチ({status['last_batch_date'] or 'なし'}):")
    for item in status["last_batch_processed"]:
        print(f"  {item['code']} {item['name']} [{item['status']}] {item['summary']}")


if __name__ == "__main__":
    main()
