"""Run with: python -m voice"""

import logging
import os
import sys

from aiohttp import web

from .app import ZoomBotApp
from .config import settings
from .speech import make_stt, make_tts


def use_certifi_if_needed():
    """python.org builds of Python on macOS ship without CA certificates; fall back to certifi's bundle."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


def main():
    use_certifi_if_needed()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("voice")
    if logging.getLogger().level > logging.DEBUG:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if not settings.api_token:
        log.error("API_TOKEN is not set. Put a long random value in .env (e.g. from `openssl rand -hex 24`); "
                  "the bridge and the OpenClaw skill use it too.")
        sys.exit(1)
    os.makedirs(settings.data_dir, exist_ok=True)

    log.info("AI brain: %s (model %s, %s)", settings.llm_url, settings.llm_model,
             "with API key" if settings.llm_api_key else "no API key")
    if settings.zoom_api_enabled:
        log.info("Zoom API: on (a new meeting for every call, no recording prompt)")
    elif settings.personal_meeting_url:
        log.info("Zoom API: off; calls use your personal meeting (PERSONAL_MEETING_URL)")
    else:
        log.warning("Neither PERSONAL_MEETING_URL nor the Zoom API settings are set: the AI can join "
                    "meetings you give it a link to, but can't call you.")
    stt, tts = make_stt(settings), make_tts(settings)
    app = ZoomBotApp(settings, stt, tts).make_app()
    log.info("Listening on %s:%s; waiting for the Zoom bridge to connect", settings.host, settings.port)
    web.run_app(app, host=settings.host, port=settings.port, print=None)


if __name__ == "__main__":
    main()
