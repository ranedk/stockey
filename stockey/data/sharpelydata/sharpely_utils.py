import base64
import os

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from utils.http import get_dynamic_headers, get_with_retries

# The sharpely web app AES-256-CBC-encrypts its /api/v2/core responses. The key is a
# UTF-8 secret from the JS bundle, zero-padded to 32 bytes (recovered from the client
# key-derivation: sigBytes<32 -> right-pad with zeros). Env-overridable in case it rotates.
SHARPELY_V2_AES_KEY = os.getenv("SHARPELY_V2_AES_KEY", "p10tibarp999sha11lynt")


def _derive_key(secret: str) -> bytes:
    raw = secret.encode("utf-8")
    return raw + b"\x00" * (32 - len(raw)) if len(raw) < 32 else raw[:32]


def decrypt_sharpely_v2(payload: str) -> str:
    """Decrypt a 'base64(iv):base64(ciphertext)' sharpely v2 response body to plaintext JSON."""
    if not payload or ":" not in payload:
        raise ValueError("sharpely v2 payload is not in iv:ciphertext form")
    iv_b64, ct_b64 = payload.split(":", 1)
    cipher = AES.new(_derive_key(SHARPELY_V2_AES_KEY), AES.MODE_CBC, base64.b64decode(iv_b64))
    return unpad(cipher.decrypt(base64.b64decode(ct_b64)), AES.block_size).decode("utf-8")


def get_access_token():
    CLIENT_ID = "Li2L9VO1eawEbsgLrHdpZjhmUdW6N8Cm"
    resp = get_with_retries(
        f"https://api.mintbox.ai/api/Auth/getToken?clientId={CLIENT_ID}"
    ).json()
    return resp["response"]["accessToken"]


def get_sharpely_headers():
    ACCESS_TOKEN = get_access_token()
    headers = get_dynamic_headers()
    headers.update(
        {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Origin": "https://sharpely.in",
            "Referer": "https://sharpely.in/",
        }
    )
    return headers


def get_v2_access_token() -> str:
    """The /api/v2/core endpoints authenticate with a distinct app token from
    pyapiv2 (not the legacy Auth/getToken flow the other endpoints use)."""
    base = get_dynamic_headers()
    base.update({"Origin": "https://sharpely.in", "Referer": "https://sharpely.in/"})
    resp = get_with_retries(
        "https://pyapiv2.mintbox.ai/api/core/getAccessToken",
        headers=base,
        method="POST",
        json_data={"id": "sharpely-app"},
    ).json()
    return resp["access_token"] if isinstance(resp, dict) else str(resp)


# Cache the v2 token across a batch: fetching a fresh token per symbol both wastes the
# token endpoint's quota and trips its rate limit (observed 403s mid-batch).
_V2_TOKEN_CACHE: dict[str, str] = {}


def get_sharpely_v2_headers(*, force_refresh: bool = False):
    if force_refresh or "token" not in _V2_TOKEN_CACHE:
        _V2_TOKEN_CACHE["token"] = get_v2_access_token()
    headers = get_dynamic_headers()
    headers.update(
        {
            "Authorization": f"Bearer {_V2_TOKEN_CACHE['token']}",
            "Origin": "https://sharpely.in",
            "Referer": "https://sharpely.in/",
        }
    )
    return headers
