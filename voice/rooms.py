"""Jitsi room links."""

import re
from urllib.parse import urlparse


def jitsi_room(link, base=""):
    """A Jitsi room link (or a bare room name on `base`) as a normalized URL without the #fragment."""
    link = (link or "").strip()
    if not link:
        raise ValueError("no room given")
    if "://" not in link:
        if not base:
            raise ValueError(f"{link!r} isn't a link; set JITSI_URL so room names can be used")
        if not re.fullmatch(r"[^/\s?#]+", link):
            raise ValueError(f"{link!r} isn't a valid room name")
        link = f"{base.rstrip('/')}/{link}"
    url = urlparse(link)
    if url.scheme not in ("http", "https") or not url.netloc or url.path.strip("/") == "":
        raise ValueError(f"{link!r} isn't a Jitsi room link (like https://meet.example.com/RoomName)")
    # Jitsi room names aren't case-sensitive.
    return url._replace(netloc=url.netloc.lower(), path=url.path.lower(), fragment="").geturl()
