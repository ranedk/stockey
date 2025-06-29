import redis
import json
import argparse
import sys


def redis_backup(
    host="localhost", port=6379, db=0, password=None, output_file="redis_global_backup.json"
):
    r = redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)
    keys = r.keys("*")
    print(f"Found {len(keys)} keys in Redis.")

    backup = {}

    for key in keys:
        key_type = r.type(key)

        try:
            if key_type == "string":
                backup[key] = {"type": "string", "value": r.get(key)}

            elif key_type == "list":
                backup[key] = {"type": "list", "value": r.lrange(key, 0, -1)}

            elif key_type == "set":
                backup[key] = {"type": "set", "value": list(r.smembers(key))}

            elif key_type == "zset":
                backup[key] = {"type": "zset", "value": r.zrange(key, 0, -1, withscores=True)}

            elif key_type == "hash":
                backup[key] = {"type": "hash", "value": r.hgetall(key)}

            else:
                print(f"Unknown type for key: {key}")

        except Exception as e:
            print(f"Error fetching key {key}: {e}")

    with open(output_file, "w") as f:
        json.dump(backup, f, indent=2)

    print(f"Backup completed. Saved to {output_file}")


def redis_restore(
    host="localhost", port=6379, db=0, password=None, input_file="redis_global_backup.json"
):
    r = redis.Redis(host=host, port=port, db=db, password=password, decode_responses=True)

    try:
        with open(input_file, "r") as f:
            backup = json.load(f)
    except Exception as e:
        print(f"Failed to read backup file: {e}")
        sys.exit(1)

    for key, item in backup.items():
        key_type = item.get("type")
        value = item.get("value")

        try:
            if key_type == "string":
                r.set(key, value)

            elif key_type == "list":
                r.delete(key)
                r.rpush(key, *value)

            elif key_type == "set":
                r.delete(key)
                r.sadd(key, *value)

            elif key_type == "zset":
                r.delete(key)
                r.zadd(key, dict(value))

            elif key_type == "hash":
                r.delete(key)
                r.hset(key, mapping=value)

            else:
                print(f"Unknown type for key: {key}")
        except Exception as e:
            print(f"Error restoring key {key}: {e}")

    print(f"Restore completed from {input_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Redis Backup and Restore Script")
    parser.add_argument("action", choices=["backup", "restore"], help="Action to perform")
    parser.add_argument("--host", default="localhost", help="Redis host")
    parser.add_argument("--port", type=int, default=6379, help="Redis port")
    parser.add_argument("--db", type=int, default=0, help="Redis database number")
    parser.add_argument("--password", default=None, help="Redis password if any")
    parser.add_argument(
        "--file", default="redis_global_backup.json", help="Backup file path"
    )

    args = parser.parse_args()

    if args.action == "backup":
        redis_backup(
            host=args.host,
            port=args.port,
            db=args.db,
            password=args.password,
            output_file=args.file,
        )
    elif args.action == "restore":
        redis_restore(
            host=args.host,
            port=args.port,
            db=args.db,
            password=args.password,
            input_file=args.file,
        )
