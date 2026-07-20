"""Helpers for Lemon Squeezy checkout URLs (discount codes, custom fields)."""
from urllib.parse import quote, urlencode, urlparse, urlunparse, parse_qsl


def with_query(url: str, **params) -> str:
    """Append query params to a checkout URL, preserving existing ones."""
    if not url:
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None or value == "":
            continue
        query[key] = value
    return urlunparse(parts._replace(query=urlencode(query, quote_via=quote)))


def with_discount(url: str, code: str | None) -> str:
    """Attach ``checkout[discount_code]`` when a code is provided."""
    code = (code or "").strip()
    if not code:
        return url
    return with_query(url, **{"checkout[discount_code]": code})


def with_custom(url: str, **custom) -> str:
    """Attach Lemon ``checkout[custom][key]=value`` fields."""
    params = {f"checkout[custom][{k}]": v for k, v in custom.items() if v}
    return with_query(url, **params) if params else url


def with_success_redirect(url: str, success_url: str | None) -> str:
    """Send buyers back to My space after Lemon confirms payment."""
    success_url = (success_url or "").strip()
    if not url or not success_url:
        return url
    return with_query(url, **{"checkout[redirect_url]": success_url})
