# redis_copy.py
import sys
import redis

SRC = redis.Redis(host="127.0.0.1",   port=6379, db=0)
DST = redis.Redis(host="172.26.39.7", port=6379, db=0)


def copy_key_by_type(key: str):
    t = SRC.type(key).decode()
    ttl = SRC.pttl(key)
    ttl = None if ttl < 0 else ttl / 1000.0  # seconds or None

    if t == "string":
        val = SRC.get(key)
        DST.set(key, val, ex=ttl)

    elif t == "hash":
        data = SRC.hgetall(key)
        if data:
            DST.hset(key, mapping=data)
            if ttl: DST.expire(key, ttl)

    elif t == "list":
        items = SRC.lrange(key, 0, -1)
        if items:
            # replace if it exists
            DST.delete(key)
            DST.rpush(key, *items)
            if ttl: DST.expire(key, ttl)

    elif t == "set":
        members = SRC.smembers(key)
        if members:
            DST.delete(key)
            DST.sadd(key, *members)
            if ttl: DST.expire(key, ttl)

    elif t == "zset":
        items = SRC.zrange(key, 0, -1, withscores=True)
        if items:
            DST.delete(key)
            DST.zadd(key, dict(items))
            if ttl: DST.expire(key, ttl)

    else:
        raise NotImplementedError(f"Unsupported type: {t}")


if len(sys.argv) <= 1:
    print("error: provide a key please e.g. wpi:downloaded ")
    exit()

rkey = sys.argv[1].strip()
print("Copying key: ", rkey)

copy_key_by_type(rkey)
