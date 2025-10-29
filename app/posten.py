#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import base64
import logging
import datetime as dt

from flask import (
    Flask,
    request,
    make_response,
    jsonify,
    send_from_directory,
    Response,
)
from flask_cors import CORS, cross_origin
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
LAST_UPSTREAM_STATUS = {"status": None, "when": None, "note": None}


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


def _fetch_dates(s: Session, token: str, post_code: str):
    """Call upstream with a token and parse the response defensively."""
    url = f"{SERVICE_URL}?postalCode={post_code}"
    try:
        headers = {"kp-api-token": token}
        r = s.get(url, headers=headers, timeout=REQ_TIMEOUT)
    except RequestException as e:
        return (False, {"where": "fetch_dates", "status": None, "body": None, "error": str(e)}, 502)

    status = r.status_code
    body_text = r.text if r.text is not None else ""

    # record for healthz
    LAST_UPSTREAM_STATUS["status"] = status
    LAST_UPSTREAM_STATUS["when"] = dt.datetime.now().isoformat(timespec="seconds")
    LAST_UPSTREAM_STATUS["note"] = "generated_token" if len(token) < 64 else "scraped_token"

    if status != 200:
        # Bubble up upstream status with body for insight
        return (False, {"where": "fetch_dates", "status": status, "body": body_text, "error": "upstream non-200"},
                status if 400 <= status < 600 else 502)

    # 200 OK but shape may be [] / {}
    try:
        data = r.json()
    except ValueError:
        return (False, {"where": "fetch_dates", "status": 200, "body": body_text[:500], "error": "invalid JSON"}, 502)

    if not isinstance(data, dict) or "delivery_dates" not in data:
        return (False, {"where": "fetch_dates", "status": 200, "body": data, "error": "missing 'delivery_dates'"}, 502)

    # Happy path
    return (True, data, 200)


# ---------------------------------
# Day-scoped cache key (Option A)
# ---------------------------------

# Bounded cache for today only
_DAILY_CACHE = LRUCache(maxsize=5096)
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
        return _DAILY_CACHE[key]

    s = _new_session()

    # 1) try generated token
    gen_token = _gen_token()
    ok, payload, code = _fetch_dates(s, gen_token, postCode)
    if ok:
        _DAILY_CACHE[key] = (True, payload)
        return (True, payload)

    logging.warning(
        f"Generated token failed for {postCode}: status={payload.get('status')} err={payload.get('error')}"
    )

    # 2) try scraped token
    tok_ok, tok = _scrape_token(s)
    if not tok_ok:
        err = {"source": "token_scrape", **tok}
        logging.error(f"Token scrape failed for {postCode}: {err}")
        return (False, err, 502)

    ok2, payload2, code2 = _fetch_dates(s, tok, postCode)
    if ok2:
        _DAILY_CACHE[key] = (True, payload2)
        return (True, payload2)

    err = {"source": "fetch_with_scraped_token", **payload2}
    logging.error(
        f"Scraped token fetch failed for {postCode}: status={payload2.get('status')} err={payload2.get('error')}"
    )
    return (False, err, code2 if code2 else 502)


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
        mimetype="image/vnd.microsoft.icon",
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
    """Simple health endpoint with last upstream status hint."""
    return jsonify({
        "ok": True,
        "last_upstream": LAST_UPSTREAM_STATUS,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
    }), 200


@app.route("/raw/<int:postCode>", methods=["GET"])
@app.route("/raw/<int:postCode>.json", methods=["GET"])
@cross_origin()
def delivery_raw(postCode):
    postCode = str(postCode).zfill(4)
    if request.method != "GET":
        return Response(status=405)
    if len(postCode) != 4:
        return Response(status=404)

    res = Posten(postCode)
    if res[0]:
        return jsonify(res[1]), 200
    else:
        err = res[1]
        status = res[2] if len(res) > 2 else 502
        return jsonify({"ok": False, "postcode": postCode, "upstream": err}), status


@app.route("/text/<int:postCode>", methods=["GET"])
@app.route("/text/<int:postCode>.json", methods=["GET"])
@cross_origin()
def delivery_days(postCode):
    postCode = str(postCode).zfill(4)
    if request.method != "GET":
        return Response(status=405)
    if len(postCode) != 4:
        return Response(status=404)

    res = Posten(postCode)
    if res[0]:
        weekdays = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
        months = ["Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Desember"]
        out = []
        for d in res[1]["delivery_dates"]:
            date = dt.datetime.strptime(d, "%Y-%m-%d")
            out.append(f"{weekdays[date.weekday()]} {date.day}. {months[date.month-1]}")
        return jsonify({"delivery_dates": out}), 200
    else:
        err = res[1]
        status = res[2] if len(res) > 2 else 502
        return jsonify({"ok": False, "postcode": postCode, "upstream": err}), status


@app.route("/next/<int:postCode>", methods=["GET"])
@app.route("/next/<int:postCode>.json", methods=["GET"])
@cross_origin()
def delivery_next(postCode):
    postCode = str(postCode).zfill(4)
    if request.method != "GET":
        return Response(status=405)
    if len(postCode) != 4:
        return Response(status=404)

    res = Posten(postCode)
    if res[0]:
        nextDatesStrings = ["i dag", "i morgen", "i overmorgen"]
        out = []
        for d in res[1]["delivery_dates"]:
            deliveryDate = dt.datetime.strptime(d, "%Y-%m-%d").date()
            delta = (deliveryDate - dt.datetime.now().date()).days
            out.append(nextDatesStrings[delta] if 0 <= delta <= 2 else f"om {delta} dager")
        return jsonify({"delivery_dates": out}), 200
    else:
        err = res[1]
        status = res[2] if len(res) > 2 else 502
        return jsonify({"ok": False, "postcode": postCode, "upstream": err}), status


if __name__ == "__main__":
    # Ensure logs directory exists
    logs_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    logging.basicConfig(
        filename=os.path.join(logs_dir, 'posten.log'),
        format='%(asctime)s %(levelname)s:%(message)s',
        datefmt='%d/%m/%Y %H:%M:%S',
        level=logging.INFO
    )

    # Proxy headers (X-Forwarded-Proto/Host)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Run WSGI server
    http_server = WSGIServer(("0.0.0.0", 5000), app)
    http_server.serve_forever()

