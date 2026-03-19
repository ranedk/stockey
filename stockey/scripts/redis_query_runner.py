#!/usr/bin/env python3

import os
import json
import argparse
from typing import Any

import redis
from environs import Env

env = Env()
env.read_env()


def get_redis_client() -> redis.Redis:
    return redis.Redis(
        host=env("REDIS_HOST"),
        port=int(env("REDIS_PORT")),
        db=env.int("REDIS_DB"),
        decode_responses=True,
    )


def normalize(value: Any):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {normalize(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize(v) for v in value]
    return value


def read_key(r: redis.Redis, key: str, max_items: int = 100):
    key_type = r.type(key)

    result = {
        "key": key,
        "type": key_type,
        "ttl": r.ttl(key),
    }

    if key_type == "string":
        result["value"] = r.get(key)

    elif key_type == "set":
        members = list(r.smembers(key))
        result["count"] = len(members)
        result["members"] = members[:max_items]

    elif key_type == "hash":
        data = r.hgetall(key)
        items = list(data.items())[:max_items]
        result["count"] = len(data)
        result["value"] = dict(items)

    elif key_type == "list":
        values = r.lrange(key, 0, max_items - 1)
        result["count"] = r.llen(key)
        result["value"] = values

    elif key_type == "zset":
        values = r.zrange(key, 0, max_items - 1, withscores=True)
        result["count"] = r.zcard(key)
        result["value"] = [{"member": m, "score": s} for m, s in values]

    elif key_type == "stream":
        values = r.xrange(key, count=max_items)
        result["value"] = [{"id": msg_id, "fields": fields} for msg_id, fields in values]

    elif key_type == "none":
        result["value"] = None

    else:
        result["note"] = f"Unsupported or unknown Redis type: {key_type}"

    return normalize(result)


def scan_keys(r: redis.Redis, pattern: str = "*", cursor: int = 0, count: int = 50):
    next_cursor, keys = r.scan(cursor=cursor, match=pattern, count=count)
    return {
        "cursor": next_cursor,
        "keys": keys,
        "count": len(keys),
    }


def run_raw_command(r: redis.Redis, command_parts: list[str]):
    if not command_parts:
        raise ValueError("No command provided")

    result = r.execute_command(*command_parts)
    return normalize(result)


def main():
    parser = argparse.ArgumentParser(description="Redis inspector / command runner")
    sub = parser.add_subparsers(dest="action", required=True)

    scan_p = sub.add_parser("scan", help="Scan keys with pagination")
    scan_p.add_argument("--pattern", default="*")
    scan_p.add_argument("--cursor", type=int, default=0)
    scan_p.add_argument("--count", type=int, default=50)

    get_p = sub.add_parser("get", help="Get key details")
    get_p.add_argument("key")
    get_p.add_argument("--max-items", type=int, default=100)

    raw_p = sub.add_parser("raw", help="Run arbitrary Redis command")
    raw_p.add_argument("command", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    r = get_redis_client()

    try:
        if args.action == "scan":
            result = scan_keys(
                r,
                pattern=args.pattern,
                cursor=args.cursor,
                count=args.count,
            )

        elif args.action == "get":
            result = read_key(
                r,
                key=args.key,
                max_items=args.max_items,
            )

        elif args.action == "raw":
            if not args.command:
                raise ValueError("Provide a command after 'raw'")
            result = run_raw_command(r, args.command)

        else:
            raise ValueError(f"Unsupported action: {args.action}")

        print(json.dumps(result, indent=2, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({
            "status": "error",
            "error": str(e),
        }, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
