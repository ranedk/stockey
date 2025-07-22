from utils.http import get_dynamic_headers, get_with_retries


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
