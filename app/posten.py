#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import base64
import logging
import datetime as dt
import sys

from flask import (
    Flask,
    request,
    make_response,
    jsonify,
    send_from_directory,
)
from flask_cors import CORS
from gevent.pywsgi import WSGIServer
from werkzeug.middleware.proxy_fix import ProxyFix

import requests
from requests import Session
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bs4 import BeautifulSoup
from cachetools import LRUCache

# --------------------------
# Config & constants
# --------------------------

POSTEN_ORIGIN  = "https://www.posten.no"
POSTEN_REFERER = "https://www.posten.no/levering-av-post"
SERVICE_URL    = f"{POSTEN_REFERER}/_/service/no.posten.website/delivery-days"
TOKEN_SEED_B64 = "pils"

# Localized labels for /text and /next
WEEKDAYS_NO = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
MONTHS_NO   = ["Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August",
               "September", "Oktober", "November", "Desember"]
NEXT_STRINGS = ["i dag", "i morgen", "i overmorgen"]

# (connect, read) timeouts
REQ_TIMEOUT = (3.05, 8.0)

RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
    raise_on_status=False,
)

# Global health info (best-effort)
LAST_UPSTREAM_STATUS = {
    "status": None,               # raw upstream HTTP status
    "interpreted_status": None,   # what our app concluded (e.g., 502 on empty data)
    "error": None,                # short reason when we treat as error
    "when": None,
    "note": None,                 # e.g., "generated ok", "generated failed, scraped ok (cached)"
    "body_preview": None,         # short preview of upstream body
}

# --------------------------
# HTTP helpers
# --------------------------

def _new_session() -> Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, */*;q=0.1",
        "Origin": POSTEN_ORIGIN,
        "Referer": POSTEN_REFERER,
    })
    adapter = HTTPAdapter(max_retries=RETRY, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def _gen_token() -> str:
    # kp-api-token = base64( base64("pils") + str(int(time.time())) ).replace("=", "")
    return base64.b64encode(
        base64.b64decode(TOKEN_SEED_B64) + str(int(time.time())).encode("utf-8")
    ).decode().replace("=", "")


def _scrape_token(s: Session):
    """Fallback: fetch token by scraping the referer page."""
    try:
        r = s.get(POSTEN_REFERER, timeout=REQ_TIMEOUT)
        r.raise_for_status()
    except RequestException as e:
        return (False, {"where": "scrape_token", "error": str(e)})

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", {"data-react4xp-ref": "parts_mailbox-delivery__main_1_leftRegion_11"})
        if not script:
            return (False, {"where": "scrape_token", "error": "delivery script not found"})
        delivery_json = json.loads(script.contents[0])
        api_token = delivery_json["props"]["apiKey"]
        return (True, api_token)
    except Exception as e:
        return (False, {"where": "scrape_token", "error": f"parse failed: {e}"})


def _record_health(raw_status, note, body_preview, interpreted_status=None, error=None):
    LAST_UPSTREAM_STATUS.update({
        "status": raw_status,
        "interpreted_status": interpreted_status,
        "error": error,
        "when": dt.datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "body_preview": (body_preview or "")[:200] if body_preview is not None else None,
    })


def _fetch_dates(s: Session, token: str, post_code: str):
    """Call upstream with a token and parse the response defensively."""
    url = f"{SERVICE_URL}?postalCode={post_code}"
    # Basic note (refined later by Posten() with "generated failed, scraped ok/failed")
    note = "generated token" if len(token) < 64 else "scraped token"

    try:
        headers = {"kp-api-token": token}
        r = s.get(url, headers=headers, timeout=REQ_TIMEOUT)
    except RequestException as e:
        _record_health(None, note, None, interpreted_status=502, error=f"transport: {e}")
        return (False, {"where": "fetch_dates", "status": None, "body": None, "error": str(e)}, 502)

    status = r.status_code
    body_text = r.text if r.text is not None else ""
    _record_health(status, note, body_text)

    if status != 200:
        interpreted = status if 400 <= status < 600 else 502
        _record_health(status, note, body_text, interpreted_status=interpreted, error="upstream non-200")
        return (False, {"where": "fetch_dates", "status": status, "body": body_text, "error": "upstream non-200"}, interpreted)

    # 200 OK but shape may be [] / {}
    try:
        data = r.json()
    except ValueError:
        _record_health(status, note, body_text, interpreted_status=502, error="invalid JSON")
        return (False, {"where": "fetch_dates", "status": 200, "body": body_text[:500], "error": "invalid JSON"}, 502)

    if not isinstance(data, dict) or "delivery_dates" not in data:
        _record_health(status, note, body_text, interpreted_status=502, error="missing 'delivery_dates'")
        return (False, {"where": "fetch_dates", "status": 200, "body": data, "error": "missing 'delivery_dates'"}, 502)

    dates = data.get("delivery_dates")
    if not isinstance(dates, list):
        _record_health(status, note, body_text, interpreted_status=502, error="'delivery_dates' not a list")
        return (False, {"where": "fetch_dates", "status": 200, "body": data, "error": "'delivery_dates' not a list"}, 502)
    if len(dates) == 0:
        _record_health(status, note, body_text, interpreted_status=502, error="empty 'delivery_dates'")
        return (False, {"where": "fetch_dates", "status": 200, "body": data, "error": "empty 'delivery_dates'"}, 502)

    # Happy path
    _record_health(status, note, body_text, interpreted_status=200, error=None)
    return (True, data, 200)

# --------------------------
# Daily success-only cache
# --------------------------

_DAILY_CACHE = LRUCache(maxsize=5096)  # only successful responses cached
_CURRENT_DAY = dt.date.today().isoformat()

def Posten(postCode: str):
    """Fetch delivery dates from Posten with graceful fallbacks (day-scoped cache)."""
    postCode = str(postCode).zfill(4)
    logging.info(f"Posten() request for {postCode}")

    # hard rollover at midnight
    global _CURRENT_DAY
    today = dt.date.today().isoformat()
    if today != _CURRENT_DAY:
        _DAILY_CACHE.clear()
        _CURRENT_DAY = today

    key = (today, postCode)
    # return cached success if available
    if key in _DAILY_CACHE:
        cached = _DAILY_CACHE[key]  # (True, {"delivery_dates": [...], "note": "..."})
        base_note = cached[1].get("note", "pulled from cache")
        note_cached = f"{base_note} (cached)"
        # reflect cached-serve in health
        _record_health(200, note_cached, body_preview=None, interpreted_status=200, error=None)
        # return a copy with "(cached)" in note (without mutating stored cache)
        return (True, {"delivery_dates": cached[1]["delivery_dates"], "note": note_cached})

    s = _new_session()

    # 1) try generated token
    gen_token = _gen_token()
    ok, payload, code = _fetch_dates(s, gen_token, postCode)
    if ok:
        LAST_UPSTREAM_STATUS["note"] = "generated ok"
        # store original note (no "(cached)" suffix)
        _DAILY_CACHE[key] = (True, {"delivery_dates": payload["delivery_dates"], "note": LAST_UPSTREAM_STATUS["note"]})
        # return exactly what's cached to keep parity
        return _DAILY_CACHE[key]

    logging.warning(f"Generated token failed for {postCode}: {payload.get('error')}")

    # 2) try scraped token
    tok_ok, tok = _scrape_token(s)
    if not tok_ok:
        LAST_UPSTREAM_STATUS["note"] = "generated failed, scraped failed"
        err = {"source": "token_scrape", **tok}
        logging.error(f"Token scrape failed for {postCode}: {err}")
        return (False, err, 502)

    ok2, payload2, code2 = _fetch_dates(s, tok, postCode)
    if ok2:
        LAST_UPSTREAM_STATUS["note"] = "generated failed, scraped ok"
        _DAILY_CACHE[key] = (True, {"delivery_dates": payload2["delivery_dates"], "note": LAST_UPSTREAM_STATUS["note"]})
        return _DAILY_CACHE[key]

    LAST_UPSTREAM_STATUS["note"] = "generated failed, scraped failed"
    err = {"source": "fetch_with_scraped_token", **payload2}
    logging.error(
        f"Scraped token fetch failed for {postCode}: status={payload2.get('status')} err={payload2.get('error')}"
    )
    return (False, err, code2 if code2 else 502)

# --------------------------
# Response shaping helpers
# --------------------------

def _compose_payload_from_success(data: dict) -> dict:
    """Ensure consistent payload (adds metadata)."""
    return {
        "delivery_dates": data.get("delivery_dates", []),
        "status": 200,
        "interpreted_status": 200,
        "error": None,
        "note": data.get("note"),  # includes "(cached)" when served from cache
    }

def _compose_payload_from_error(err: dict) -> dict:
    """Return empty dates + metadata for errors; mirrors healthz info."""
    return {
        "delivery_dates": [],
        "status": LAST_UPSTREAM_STATUS.get("status"),
        "interpreted_status": LAST_UPSTREAM_STATUS.get("interpreted_status", 502),
        "error": err.get("error") if isinstance(err, dict) else str(err),
        "note": LAST_UPSTREAM_STATUS.get("note"),
        "last_checked": LAST_UPSTREAM_STATUS.get("when"),
        "upstream_body_preview": LAST_UPSTREAM_STATUS.get("body_preview"),
    }

# --------------------------
# Flask app
# --------------------------

app = Flask(__name__)
CORS(app)
app.config["JSON_AS_ASCII"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = True

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "posten-logo-ud.png",
        mimetype="image/png",   # file is PNG, so serve proper MIME
    )

@app.route("/")
def hello():
    return make_response(
        f"Usage: <br>"
        f"&emsp; <a href='{request.url_root}raw/4321.json'>{request.url_root}raw/4321.json</a> for raw data.<br>"
        f"&emsp; <a href='{request.url_root}text/4321.json'>{request.url_root}text/4321.json</a> for formatted text dates.<br>"
        f"&emsp; <a href='{request.url_root}next/4321.json'>{request.url_root}next/4321.json</a> for days until next delivery.<br>"
        f"<br><br>Source on <a href='https://github.com/Lanjelin/docker-posten/'>GitHub</a>"
    )

@app.route("/healthz", methods=["GET"])
def healthz():
    """Slim health endpoint including interpreted status, reason, and body preview."""
    return jsonify({
        "ok": True,
        "last_upstream": LAST_UPSTREAM_STATUS,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
    }), 200

# --------------------------
# Endpoints
# --------------------------

@app.route("/raw/<int:postCode>.json", methods=["GET"])
@app.route("/raw/<int:postCode>", methods=["GET"])
def delivery_raw(postCode):
    postCode = str(postCode).zfill(4)

    res = Posten(postCode)
    if res[0]:
        payload = _compose_payload_from_success(res[1])
        return jsonify(payload), 200
    else:
        err = res[1]
        payload = _compose_payload_from_error(err)
        return jsonify(payload), 200  # stable 200 with empty dates + metadata

@app.route("/text/<int:postCode>.json", methods=["GET"])
@app.route("/text/<int:postCode>", methods=["GET"])
def delivery_days(postCode):
    postCode = str(postCode).zfill(4)

    res = Posten(postCode)
    if res[0]:
        out = []
        for d in res[1]["delivery_dates"]:
            date = dt.datetime.strptime(d, "%Y-%m-%d")
            out.append(f"{WEEKDAYS_NO[date.weekday()]} {date.day}. {MONTHS_NO[date.month-1]}")
        payload = {
            "delivery_dates": out,
            "status": 200,
            "interpreted_status": 200,
            "error": None,
            "note": res[1].get("note"),  # includes "(cached)" when served from cache
        }
        return jsonify(payload), 200
    else:
        err = res[1]
        payload = _compose_payload_from_error(err)
        return jsonify(payload), 200

@app.route("/next/<int:postCode>.json", methods=["GET"])
@app.route("/next/<int:postCode>", methods=["GET"])
def delivery_next(postCode):
    postCode = str(postCode).zfill(4)

    res = Posten(postCode)
    if res[0]:
        out = []
        today = dt.datetime.now().date()
        for d in res[1]["delivery_dates"]:
            deliveryDate = dt.datetime.strptime(d, "%Y-%m-%d").date()
            delta = (deliveryDate - today).days
            out.append(NEXT_STRINGS[delta] if 0 <= delta <= 2 else f"om {delta} dager")
        payload = {
            "delivery_dates": out,
            "status": 200,
            "interpreted_status": 200,
            "error": None,
            "note": res[1].get("note"),  # includes "(cached)" when served from cache
        }
        return jsonify(payload), 200
    else:
        err = res[1]
        payload = _compose_payload_from_error(err)
        return jsonify(payload), 200

# --------------------------
# Main
# --------------------------

if __name__ == "__main__":
    # Ensure logs directory exists
    logs_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    # Log to both file and stdout (Docker-friendly)
    handlers = [
        logging.FileHandler(os.path.join(logs_dir, 'posten.log')),
        logging.StreamHandler(sys.stdout)
    ]
    logging.basicConfig(
        handlers=handlers,
        format='%(asctime)s %(levelname)s:%(message)s',
        datefmt='%d/%m/%Y %H:%M:%S',
        level=logging.INFO
    )

    # Proxy headers (X-Forwarded-Proto/Host)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Run WSGI server
    http_server = WSGIServer(("0.0.0.0", 5000), app)
    http_server.serve_forever()

