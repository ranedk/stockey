#!/usr/bin/env python3

import argparse
import json
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError

from environs import Env
from utils.store import _get_client

env = Env()
env.read_env()

AWS_BUCKET_NAME: str = env.str("AWS_BUCKET_NAME", "stockeydata")


def normalize_dt(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def list_objects(
    s3,
    bucket: str,
    prefix: str = "",
    max_keys: int = 100,
    continuation_token: str | None = None,
    delimiter: str | None = None,
):
    kwargs = {
        "Bucket": bucket,
        "Prefix": prefix,
        "MaxKeys": max_keys,
    }
    if continuation_token:
        kwargs["ContinuationToken"] = continuation_token
    if delimiter:
        kwargs["Delimiter"] = delimiter

    resp = s3.list_objects_v2(**kwargs)

    items = []
    for obj in resp.get("Contents", []):
        items.append(
            {
                "key": obj["Key"],
                "size": obj["Size"],
                "last_modified": normalize_dt(obj.get("LastModified")),
                "etag": obj.get("ETag"),
                "storage_class": obj.get("StorageClass"),
            }
        )

    prefixes = [
        entry["Prefix"] for entry in resp.get("CommonPrefixes", [])
    ]

    return {
        "bucket": bucket,
        "prefix": prefix,
        "count": len(items),
        "items": items,
        "common_prefixes": prefixes,
        "is_truncated": resp.get("IsTruncated", False),
        "next_continuation_token": resp.get("NextContinuationToken"),
    }


def head_object(s3, bucket: str, key: str):
    resp = s3.head_object(Bucket=bucket, Key=key)
    return {
        "bucket": bucket,
        "key": key,
        "content_length": resp.get("ContentLength"),
        "content_type": resp.get("ContentType"),
        "etag": resp.get("ETag"),
        "last_modified": normalize_dt(resp.get("LastModified")),
        "metadata": resp.get("Metadata", {}),
    }


def main():
    parser = argparse.ArgumentParser(description="Inspect S3 objects as JSON")
    sub = parser.add_subparsers(dest="action", required=True)

    list_p = sub.add_parser("list", help="List files in a bucket")
    list_p.add_argument("--bucket", default=AWS_BUCKET_NAME)
    list_p.add_argument("--prefix", default="")
    list_p.add_argument("--max-keys", type=int, default=100)
    list_p.add_argument("--continuation-token")
    list_p.add_argument(
        "--delimiter",
        default=None,
        help="Optional delimiter, usually '/' for prefix-style listings",
    )

    head_p = sub.add_parser("head", help="Get metadata for one object")
    head_p.add_argument("--bucket", default=AWS_BUCKET_NAME)
    head_p.add_argument("--key", required=True)

    args = parser.parse_args()
    s3 = _get_client()

    try:
        if args.action == "list":
            result = list_objects(
                s3=s3,
                bucket=args.bucket,
                prefix=args.prefix,
                max_keys=args.max_keys,
                continuation_token=args.continuation_token,
                delimiter=args.delimiter,
            )
        elif args.action == "head":
            result = head_object(
                s3=s3,
                bucket=args.bucket,
                key=args.key,
            )
        else:
            raise ValueError(f"Unsupported action: {args.action}")

        print(json.dumps({"status": "ok", **result}, indent=2, ensure_ascii=False))
    except (BotoCoreError, ClientError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
