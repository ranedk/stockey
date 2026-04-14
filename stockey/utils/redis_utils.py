from __future__ import annotations

import sys
import time
from typing import Any

import redis
from environs import Env


env = Env()
env.read_env()

REDIS_OPERATION_ATTEMPTS = max(env.int("REDIS_OPERATION_ATTEMPTS", 3), 1)
REDIS_RETRY_SLEEP_SECONDS = env.float("REDIS_RETRY_SLEEP_SECONDS", 1.0)
REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS = env.float("REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS", 2.0)
REDIS_SOCKET_TIMEOUT_SECONDS = env.float("REDIS_SOCKET_TIMEOUT_SECONDS", 5.0)
REDIS_FAIL_SOFT = env.bool("REDIS_FAIL_SOFT", True)
REDIS_RECONNECT_COOLDOWN_SECONDS = max(env.float("REDIS_RECONNECT_COOLDOWN_SECONDS", 30.0), 0.0)
_ORIGINAL_REDIS_CLASS = redis.Redis


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _default_for(command_name: str) -> Any:
    if command_name in {"smembers", "keys"}:
        return set()
    if command_name in {"sismember", "exists", "publish"}:
        return False
    if command_name in {"get", "hget", "lpop", "rpop"}:
        return None
    if command_name in {"sadd", "set", "hset", "delete", "expire"}:
        return 0
    if command_name == "scan":
        return 0, []
    return None


class ResilientRedis:
    def __init__(self, *args: Any, fail_soft: bool | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("socket_connect_timeout", REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS)
        kwargs.setdefault("socket_timeout", REDIS_SOCKET_TIMEOUT_SECONDS)
        kwargs.setdefault("retry_on_timeout", True)
        self._args = args
        self._kwargs = kwargs
        self._fail_soft = REDIS_FAIL_SOFT if fail_soft is None else bool(fail_soft)
        self._client: redis.Redis | None = None
        self._cooldown_until = 0.0
        self._cooldown_logged = False

    def _connect(self) -> redis.Redis:
        self._client = _ORIGINAL_REDIS_CLASS(*self._args, **self._kwargs)
        return self._client

    def _call(self, command_name: str, *args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        now = time.time()
        if self._fail_soft and now < self._cooldown_until:
            if not self._cooldown_logged:
                remaining = max(self._cooldown_until - now, 0.0)
                _emit(
                    f"[utils.redis] redis unavailable; suppressing reconnect attempts for "
                    f"{remaining:.1f}s command={command_name}"
                )
                self._cooldown_logged = True
            return _default_for(command_name)
        self._cooldown_logged = False
        for attempt in range(1, REDIS_OPERATION_ATTEMPTS + 1):
            try:
                client = self._client or self._connect()
                return getattr(client, command_name)(*args, **kwargs)
            except (redis.ConnectionError, redis.TimeoutError, OSError) as exc:
                last_exc = exc
                self._client = None
                if attempt >= REDIS_OPERATION_ATTEMPTS:
                    break
                _emit(
                    f"[utils.redis] redis unavailable; retrying command={command_name} "
                    f"attempt={attempt + 1}/{REDIS_OPERATION_ATTEMPTS} error={exc.__class__.__name__}: {exc}"
                )
                time.sleep(REDIS_RETRY_SLEEP_SECONDS * attempt)
        if self._fail_soft and REDIS_RECONNECT_COOLDOWN_SECONDS > 0:
            self._cooldown_until = time.time() + REDIS_RECONNECT_COOLDOWN_SECONDS
        if self._fail_soft:
            _emit(
                f"[utils.redis] redis unavailable; continuing without redis state command={command_name} "
                f"error={last_exc.__class__.__name__ if last_exc else 'unknown'}: {last_exc}"
            )
            return _default_for(command_name)
        if last_exc is not None:
            raise last_exc
        return _default_for(command_name)

    def __getattr__(self, command_name: str):
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return self._call(command_name, *args, **kwargs)

        return _wrapped

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:
            pass
        finally:
            self._client = None


def get_redis_client(host: str, port: int | str, *, decode_responses: bool = True, fail_soft: bool | None = None) -> ResilientRedis:
    return ResilientRedis(host=host, port=int(port), decode_responses=decode_responses, fail_soft=fail_soft)


def install_resilient_redis() -> None:
    """Patch redis.Redis so legacy ingestion modules get resilient behavior."""
    if getattr(redis.Redis, "__name__", "") == "ResilientRedis":
        return
    redis.Redis = ResilientRedis  # type: ignore[assignment]
