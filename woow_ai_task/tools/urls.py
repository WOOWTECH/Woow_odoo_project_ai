# -*- coding: utf-8 -*-
"""One rule for every URL whose host the module checks: links in replies
(markdown.safe_url and the reply's link hosts), reply files it downloads
(media.url_allowed, at every redirect hop) and the connection's endpoint
(ai_connection.endpoint_url_allowed / endpoint_origin).

The host the module checks must be the host the HTTP client (requests, so
urllib3) or the browser goes to. A URL is refused when they could disagree:

* a backslash anywhere: a path separator for browsers and urllib3, not for
  ``urlsplit`` (``https://evil.example\\@files.example.com/`` is
  ``files.example.com`` for urlsplit, ``evil.example`` for requests);
* userinfo, an ``@`` in the host part (``https://good.example@evil.example/``);
* a control character (a tab or a line break too);
* any other URL whose host urlsplit and urllib3 read differently, or that one
  of them cannot parse (a port out of range...)."""

from urllib.parse import urlsplit

try:
    from urllib3.exceptions import LocationParseError
    from urllib3.util import parse_url
except ImportError:  # pragma: no cover - requests always brings urllib3
    parse_url = None
    LocationParseError = ValueError


def _host_key(host):
    """A host as both parsers may spell it: lower case, IPv6 without its
    brackets, a non-ASCII name in its IDNA (``xn--``) form."""
    host = (host or '').strip('[]').lower()
    if host and not host.isascii():
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            return None
    return host


def split_url(url):
    """``urlsplit(url)`` when the URL's host is certain, else None (see the
    module docstring). A URL without a host part (``mailto:``...) is
    returned as split: there is no host to disagree on."""
    if not isinstance(url, str) or '\\' in url or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a port out of range
    except ValueError:
        return None
    if '@' in parts.netloc:
        return None
    if not parts.netloc:
        return parts
    if parse_url is None:
        return None
    try:
        client_host = parse_url(url).host
    except (LocationParseError, ValueError):
        return None
    mine, theirs = _host_key(hostname), _host_key(client_host)
    if mine is None or theirs is None or mine != theirs:
        return None
    return parts
