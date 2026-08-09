import logging
import os
import re
import urllib.parse
from typing import Optional

import requests
import urllib3
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================

APP_NAME = "TeraBox Resolver API"
APP_VERSION = "1.0.0"

BASE_URL = os.getenv("TERABOX_BASE_URL", "https://terabox.beer")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("terabox_api")

# Comma-separated Blogger domains.
# Example:
# ALLOWED_ORIGINS=https://example.blogspot.com,https://www.example.com
allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

# ============================================================
# RATE LIMITER
# ============================================================

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("Request %s %s", request.method, request.url.path)
    response = await call_next(request)
    logger.info("Response %s %s %s", request.method, request.url.path, response.status_code)
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": "Invalid request payload.", "details": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    logger.exception("Unhandled exception", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error."})

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# ============================================================
# REQUEST MODEL
# ============================================================

class ResolveRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="TeraBox share URL"
    )

# ============================================================
# TERA BOX DOWNLOADER
# Based on uploaded TeraboxCLI.py
# ============================================================

class TeraboxDownloader:

    def __init__(self):
        self.session = requests.Session()

        # Production: keep TLS verification enabled.
        self.session.verify = True

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/137.0.0.0 "
                "Mobile Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-MM,en-GB;q=0.9,en-US;q=0.8",
            "Sec-Ch-Ua": (
                '"Chromium";v="137", '
                '"Not/A)Brand";v="24"'
            ),
            "Sec-Ch-Ua-Mobile": "?1",
            "Sec-Ch-Ua-Platform": '"Android"',
        }

    # --------------------------------------------------------
    # Extract video ID
    # --------------------------------------------------------

    def extract_video_id(self, url: str) -> Optional[str]:

        patterns = [
            r"/s/([a-zA-Z0-9_-]+)",
            r"share\.com/s/([a-zA-Z0-9_-]+)",
            r"file\.com/s/([a-zA-Z0-9_-]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)

            if match:
                return match.group(1)

        return None

    # --------------------------------------------------------
    # Find M3U8
    # --------------------------------------------------------

    def extract_m3u8_url(self, text: str) -> Optional[str]:

        patterns = [
            r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)',
            r'(https?://[^\s"\'<>]+/playlist\.m3u8[^\s"\'<>]*)',
            r'(https?://[^\s"\'<>]+\.m3u8\?[^\s"\'<>]*)',
        ]

        for pattern in patterns:

            match = re.search(pattern, text)

            if match:
                return match.group(1)

        return None

    # --------------------------------------------------------
    # Follow redirects
    # --------------------------------------------------------

    def follow_redirects(
        self,
        url: str,
        max_redirects: int = 5
    ):

        current_url = url

        for _ in range(max_redirects):

            try:

                response = self.session.get(
                    current_url,
                    headers=self.headers | {
                        "Referer": BASE_URL + "/"
                    },
                    allow_redirects=False,
                    timeout=(10, 30),
                )

                if response.status_code in (
                    301,
                    302,
                    303,
                    307,
                    308,
                ):

                    location = response.headers.get("Location")

                    if location:

                        current_url = urllib.parse.urljoin(
                            current_url,
                            location
                        )

                        continue

                m3u8_url = self.extract_m3u8_url(
                    response.text or ""
                )

                return {
                    "final_url": current_url,
                    "m3u8_url": m3u8_url,
                    "response": response,
                }

            except requests.RequestException:

                return {
                    "final_url": current_url,
                    "m3u8_url": None,
                    "response": None,
                }

        return {
            "final_url": current_url,
            "m3u8_url": None,
            "response": None,
        }

    # --------------------------------------------------------
    # Main resolver
    # --------------------------------------------------------

    def process_terabox_link(self, terabox_url: str):

        video_id = self.extract_video_id(
            terabox_url
        )

        if not video_id:
            return {
                "error": "Could not extract video ID from the link."
            }

        try:

            # Initial connection
            self.session.get(
                BASE_URL,
                headers=self.headers | {
                    "Referer": "https://www.google.com/"
                },
                timeout=(10, 30),
            )

            # Watch page
            watch_url = f"{BASE_URL}/watch/{video_id}"

            self.session.get(
                watch_url,
                headers=self.headers | {
                    "Referer": BASE_URL + "/"
                },
                timeout=(10, 30),
            )

            # API
            encoded_url = urllib.parse.quote(
                terabox_url,
                safe=""
            )

            api_url = (
                f"{BASE_URL}/api/terabox-new"
                f"?link={encoded_url}"
            )

            response = self.session.get(
                api_url,
                headers=self.headers | {
                    "Referer": watch_url
                },
                timeout=(10, 30),
            )

            response.raise_for_status()

            try:
                api_result = response.json()

            except ValueError:
                return {
                    "error": "Failed to parse upstream API response."
                }

            if not isinstance(api_result, dict):
                return {
                    "error": "Invalid upstream API response."
                }

            if api_result.get("error") is not False:

                error_msg = (
                    api_result.get("message")
                    or api_result.get("error")
                    or "Unknown upstream error"
                )

                return {
                    "error": f"API request failed: {error_msg}"
                }

            # ------------------------------------------------
            # Find video URL
            # ------------------------------------------------

            possible_fields = [
                "stream_download_url",
                "download_link",
                "fallback_url",
                "proxy_url",
                "url",
                "video_url",
            ]

            video_url = None

            for field in possible_fields:

                value = api_result.get(field)

                if isinstance(value, str) and value.startswith(
                    ("http://", "https://")
                ):
                    video_url = value
                    break

            # Fallback: find any HTTP URL
            if not video_url:

                for key, value in api_result.items():

                    if isinstance(value, str) and value.startswith(
                        ("http://", "https://")
                    ):
                        video_url = value
                        break

            if not video_url:

                return {
                    "error": "No video URL found in upstream response."
                }

            # ------------------------------------------------
            # Follow redirects / detect M3U8
            # ------------------------------------------------

            redirect_result = self.follow_redirects(
                video_url
            )

            if redirect_result["m3u8_url"]:

                final_video_url = (
                    redirect_result["m3u8_url"]
                )

                stream_type = "m3u8"

            else:

                final_video_url = video_url
                stream_type = "direct"

            return {
                "success": True,
                "video_id": video_id,
                "video_url": final_video_url,
                "original_url": video_url,
                "watch_page": watch_url,
                "file_name": api_result.get(
                    "file_name",
                    "Unknown"
                ),
                "file_size": api_result.get(
                    "file_size",
                    "Unknown"
                ),
                "stream_type": stream_type,
            }

        except requests.Timeout:

            return {
                "error": "Upstream request timed out."
            }

        except requests.RequestException as exc:

            return {
                "error": f"Upstream request failed: {str(exc)}"
            }

        except Exception:

            return {
                "error": "Unexpected server error."
            }

# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
@limiter.limit("60/minute")
async def health(request: Request):

    return {
        "status": "ok",
        "service": APP_NAME,
        "version": APP_VERSION,
    }

# ============================================================
# ROOT
# ============================================================

@app.get("/")
@limiter.limit("60/minute")
async def root(request: Request):

    return {
        "service": APP_NAME,
        "status": "online",
        "health": "/health",
    }

# ============================================================
# RESOLVE API
# ============================================================

@app.post("/api/resolve")
@limiter.limit("10/minute")
async def resolve(
    payload: ResolveRequest,
    request: Request
):

    url = payload.url.strip()
    logger.info("Resolving URL: %s", url)

    if not url.startswith(("http://", "https://")):

        raise HTTPException(
            status_code=400,
            detail="Invalid URL."
        )

    downloader = TeraboxDownloader()

    result = downloader.process_terabox_link(url)

    if result.get("error"):

        raise HTTPException(
            status_code=422,
            detail=result["error"]
        )

    return result
