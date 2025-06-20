import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import ua_generator


def get_dynamic_headers():
    """Dynamically generate headers for requests."""
    ua = ua_generator.generate(
        browser=("chrome", "firefox"),
        device=("desktop",),
        platform=("windows", "macos"),
    )
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9,uz;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "connection": "keep-alive",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }
    headers.update(ua.headers.get())
    return headers


def get_with_retries(url, headers=None, timeout=10, retries=5, backoff_factor=0.3):
    """
    Perform a GET request with retry logic and custom headers.

    Args:
        url (str): URL to fetch.
        headers (dict): Optional HTTP headers.
        timeout (int): Timeout in seconds for the request.
        retries (int): Number of total retries.
        backoff_factor (float): Delay multiplier between retries.

    Returns:
        requests.Response: The HTTP response object.

    Raises:
        requests.RequestException: If the request fails after retries.
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response
    except requests.RequestException as e:
        print(f"Request failed: {e}")
        raise
