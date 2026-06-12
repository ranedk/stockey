from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from advisory.sync_state import DEFAULT_REDIS_HOST, DEFAULT_REDIS_PORT
from utils.redis_utils import get_redis_client


DEFAULT_OUTPUT_DIR = Path("live_dashboard")
DEFAULT_CHANNEL_PATTERN = "stockey:continuous_watch:*"
DEFAULT_MAX_FEED_ITEMS = 200


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def format_operator_message(channel: str, payload: dict[str, Any]) -> str:
    channel = str(channel)
    published_at = str(payload.get("published_at") or payload.get("generated_at") or pd.Timestamp.utcnow().isoformat())
    if channel.endswith(":alerts"):
        count = int(payload.get("alert_count") or len(payload.get("alerts") or []))
        sample = payload.get("alerts") or []
        symbols = ", ".join(sorted({str(item.get("symbol") or "") for item in sample if str(item.get("symbol") or "").strip()}))
        detail = f" symbols={symbols}" if symbols else ""
        return f"{published_at} alerts count={count}{detail}"
    if channel.endswith(":router"):
        return (
            f"{published_at} router planned={int(payload.get('planned_actions') or 0)} "
            f"executed={int(payload.get('executed_actions') or 0)}"
        )
    if channel.endswith(":ohlcv"):
        return (
            f"{published_at} ohlcv monitored_symbols={int(payload.get('symbol_count') or 0)} "
            f"alerts={int(payload.get('alert_count') or 0)}"
        )
    if channel.endswith(":news"):
        return f"{published_at} news matched={int(payload.get('matched_event_count') or 0)}"
    if channel.endswith(":announcements"):
        return f"{published_at} announcements matched={int(payload.get('match_count') or 0)}"
    if channel.endswith(":dashboard"):
        return f"{published_at} dashboard refreshed"
    if channel.endswith(":summary"):
        cycles = payload.get("cycles") or {}
        return f"{published_at} cycle complete stages={','.join(sorted(cycles.keys()))}"
    return f"{published_at} {channel}"


def append_operator_event(
    *,
    output_dir: str | Path,
    channel: str,
    payload: dict[str, Any],
    max_items: int = DEFAULT_MAX_FEED_ITEMS,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    feed_json = output_path / "operator_feed.json"
    feed_jsonl = output_path / "operator_feed.jsonl"
    feed_txt = output_path / "operator_feed.txt"

    entry = {
        "received_at": pd.Timestamp.utcnow().isoformat(),
        "channel": str(channel),
        "message": format_operator_message(channel, payload),
        "payload": _json_ready(payload),
    }

    existing: list[dict[str, Any]] = []
    if feed_json.exists():
        try:
            existing = json.loads(feed_json.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception as exc:
            record_local_fallback_event(
                module="advisory.live_notifier",
                source=str(feed_json),
                fallback_type="live_notifier_feed_read_failed",
                severity="warn",
                reason="Existing operator feed could not be read; live notifier will rebuild the bounded feed from the current event.",
                error=exc,
                metadata={
                    "output_dir": str(output_path),
                    "feed_json": str(feed_json),
                    "channel": str(channel),
                },
            )
            existing = []
    existing.append(entry)
    existing = existing[-int(max_items) :]
    feed_json.write_text(json.dumps(existing, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    with feed_jsonl.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    feed_txt.write_text("\n".join(item["message"] for item in existing) + "\n", encoding="utf-8")
    return entry


def subscribe_and_run(
    *,
    output_dir: str | Path,
    redis_host: str = DEFAULT_REDIS_HOST,
    redis_port: int = DEFAULT_REDIS_PORT,
    channel_pattern: str = DEFAULT_CHANNEL_PATTERN,
    max_feed_items: int = DEFAULT_MAX_FEED_ITEMS,
    duration_seconds: int | None = None,
) -> dict[str, Any]:
    client = get_redis_client(host=redis_host, port=int(redis_port), decode_responses=True, fail_soft=False)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.psubscribe(str(channel_pattern))
    _emit(f"[advisory.live_notifier] subscribed pattern={channel_pattern} redis={redis_host}:{redis_port}")
    started = time.monotonic()
    count = 0
    last_entry: dict[str, Any] | None = None
    try:
        while True:
            if duration_seconds is not None and (time.monotonic() - started) >= int(duration_seconds):
                break
            message = pubsub.get_message(timeout=1.0)
            if not message:
                continue
            channel = str(message.get("channel") or "")
            raw = message.get("data")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except Exception as exc:
                record_local_fallback_event(
                    module="advisory.live_notifier",
                    source=str(channel),
                    fallback_type="live_notifier_payload_parse_failed",
                    severity="warn",
                    reason="Redis operator-feed payload was not valid JSON; live notifier will preserve the raw payload.",
                    error=exc,
                    metadata={
                        "channel": str(channel),
                        "payload_type": type(raw).__name__,
                        "payload_preview": str(raw)[:500],
                    },
                )
                payload = {"raw": raw}
            last_entry = append_operator_event(
                output_dir=output_dir,
                channel=channel,
                payload=payload,
                max_items=max_feed_items,
            )
            count += 1
            _emit(f"[advisory.live_notifier] event {count} channel={channel} message={last_entry['message']}")
    finally:
        try:
            pubsub.close()
        finally:
            client.close()
    return {
        "status": "ok",
        "event_count": count,
        "output_dir": str(output_dir),
        "last_entry": last_entry,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Subscribe to Stockey continuous-watch Redis channels and build a human-readable operator feed.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--redis-host", default=DEFAULT_REDIS_HOST)
    parser.add_argument("--redis-port", type=int, default=DEFAULT_REDIS_PORT)
    parser.add_argument("--channel-pattern", default=DEFAULT_CHANNEL_PATTERN)
    parser.add_argument("--max-feed-items", type=int, default=DEFAULT_MAX_FEED_ITEMS)
    parser.add_argument("--duration-seconds", type=int, help="Optional max runtime; default is to run forever")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = subscribe_and_run(
        output_dir=args.output_dir,
        redis_host=args.redis_host,
        redis_port=int(args.redis_port),
        channel_pattern=args.channel_pattern,
        max_feed_items=int(args.max_feed_items),
        duration_seconds=args.duration_seconds,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
