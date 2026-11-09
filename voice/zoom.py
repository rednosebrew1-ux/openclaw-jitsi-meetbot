"""Zoom meeting links and the (optional) Zoom REST API."""

import base64
import hashlib
import hmac
import logging
import re
import time
from urllib.parse import parse_qs, urlparse

import aiohttp

log = logging.getLogger(__name__)


class ZoomError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def parse_meeting(link):
    """Turn a join link (or a bare meeting number) into (meeting_number, passcode).

    Accepts https://zoom.us/j/123?pwd=..., regional and company subdomains,
    zoommtg://...join?confno=123&pwd=..., /wc/join/ links and "123 4567 8901".
    Personal links like zoom.us/my/name can't be resolved without logging in to Zoom.
    """
    link = (link or "").strip()
    if not link:
        raise ValueError("no meeting link given")
    digits = re.sub(r"[\s-]", "", link)
    if digits.isdigit():
        if not 9 <= len(digits) <= 12:
            raise ValueError(f"{digits!r} doesn't look like a Zoom meeting number")
        return digits, ""
    url = urlparse(link if "://" in link else "https://" + link)
    query = parse_qs(url.query)
    passcode = (query.get("pwd") or [""])[0]
    if "confno" in query:
        number = query["confno"][0]
    else:
        m = re.search(r"/(?:j|s|w|wc(?:/join)?|wc/\d+/join|join)/(\d{9,12})", url.path)
        if not m:
            if "/my/" in url.path:
                raise ValueError("personal links like zoom.us/my/name don't include the meeting number; "
                                 "use the link with /j/ and the number instead")
            raise ValueError(f"can't find a meeting number in {link!r}")
        number = m.group(1)
    number = re.sub(r"\D", "", number)
    if not 9 <= len(number) <= 12:
        raise ValueError(f"{number!r} doesn't look like a Zoom meeting number")
    return number, passcode


def verify_webhook(secret, timestamp, body, signature, max_age=300):
    """Check Zoom's x-zm-signature header (v0=HMAC-SHA256 of "v0:{timestamp}:{body}")."""
    if not (secret and timestamp and signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > max_age:
            return False
    except ValueError:
        return False
    message = f"v0:{timestamp}:{body.decode('utf-8', 'replace')}".encode()
    expected = "v0=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def url_validation_response(secret, plain_token):
    """The reply Zoom expects when it validates a webhook URL."""
    digest = hmac.new(secret.encode(), plain_token.encode(), hashlib.sha256).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": digest}


class ZoomAPI:
    """A Server-to-Server OAuth app on your own Zoom account."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._token, self._token_expires = None, 0.0

    async def _access_token(self, session):
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.cfg.zoom_client_id}:{self.cfg.zoom_client_secret}".encode()).decode()
        params = {"grant_type": "account_credentials", "account_id": self.cfg.zoom_account_id}
        async with session.post(self.cfg.zoom_oauth_url, params=params,
                                headers={"Authorization": f"Basic {basic}"}) as r:
            data = await r.json(content_type=None)
            if r.status >= 400 or "access_token" not in data:
                raise ZoomError(f"Zoom login failed ({r.status}): {data.get('reason') or data}", r.status)
        self._token = data["access_token"]
        self._token_expires = time.time() + float(data.get("expires_in", 3600))
        return self._token

    async def _request(self, method, path, **kwargs):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            token = await self._access_token(s)
            async with s.request(method, self.cfg.zoom_api_url + path,
                                 headers={"Authorization": f"Bearer {token}"}, **kwargs) as r:
                data = await r.json(content_type=None) if r.content_length != 0 else {}
                if r.status >= 400:
                    message = data.get("message") if isinstance(data, dict) else data
                    raise ZoomError(f"Zoom API {method} {path} failed ({r.status}): {message}", r.status)
                return data

    async def create_meeting(self, topic):
        """An instant meeting the bot can join before you do. Returns the API's meeting object."""
        body = {
            "topic": topic[:200],
            "type": 1,  # instant
            "settings": {"join_before_host": True, "jbh_time": 0, "waiting_room": False},
        }
        return await self._request("POST", "/users/me/meetings", json=body)

    async def get_meeting(self, number):
        return await self._request("GET", f"/meetings/{number}")

    async def recording_token(self, number):
        """A token that lets the bot listen without asking the host. None if Zoom says no."""
        try:
            data = await self._request("GET", f"/meetings/{number}/jointoken/local_recording")
        except (ZoomError, aiohttp.ClientError) as e:
            log.warning("couldn't get a recording token for meeting %s (the host will be asked "
                        "to allow recording instead): %s", number, e)
            return None
        return data.get("token")
