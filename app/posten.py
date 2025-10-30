#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from requests import session
from requests.exceptions import RequestException
from bs4 import BeautifulSoup
import json
import os
from flask_cors import CORS, cross_origin
from flask import (
  Flask,
  request,
  make_response,
  jsonify,
  send_from_directory,
  Response,
)
from gevent.pywsgi import WSGIServer
from werkzeug.middleware.proxy_fix import ProxyFix
from cachetools import LRUCache
import datetime as dt
import base64
import time
import logging
import re
import sys

# -------- Cache (simple, day-scoped, success-only) --------
_DAILY_CACHE = LRUCache(maxsize=5096)
_CURRENT_DAY = dt.date.today().isoformat()

def _roll_cache_if_new_day():
  global _CURRENT_DAY
  today = dt.date.today().isoformat()
  if today != _CURRENT_DAY:
    _DAILY_CACHE.clear()
    _CURRENT_DAY = today
  return today  # return for use in cache key

# -------- Minimal health info --------
LAST_UPSTREAM = {
  "status": None,      # raw HTTP status from upstream (or None on transport error)
  "note": None,        # "generated ok", "generated failed, scraped ok", etc.
  "when": None,        # ISO timestamp of last upstream call
  "postcode": None,    # last requested postal code
}

def _health(status, note, postcode):
  LAST_UPSTREAM["status"] = status
  LAST_UPSTREAM["note"] = note
  LAST_UPSTREAM["when"] = dt.datetime.now().isoformat(timespec="seconds")
  LAST_UPSTREAM["postcode"] = postcode

# -------- Core fetcher --------
def Posten(postCode):
  # day-scoped cache
  postCode = str(postCode).zfill(4)
  today = _roll_cache_if_new_day()
  key = (today, postCode)

  if key in _DAILY_CACHE:
    _health(200, "cache hit", postCode)
    return _DAILY_CACHE[key]

  logging.info(f"Served {postCode}")
  s = session()
  s.headers["User-Agent"] = "Mozilla/5.0"

  token_url = "https://www.posten.no/levering-av-post"
  service_url = "https://www.posten.no/levering-av-post/_/service/no.posten.website/delivery-days"

  def get_token():
    try:
      response = s.get(token_url)
      soup = BeautifulSoup(response.text, "html.parser")
      delivery_script = soup.find("script", {
        "data-react4xp-ref": re.compile(r"^parts_mailbox-delivery__main_1_leftRegion_\d+$")
      })
      if not delivery_script or not delivery_script.contents:
        return (False, "delivery script not found")
      delivery_json = json.loads(delivery_script.contents[0])
      api_token = delivery_json["props"]["apiKey"]
      return (True, api_token)
    except RequestException as e:
      return (False, str(e))
    except Exception as e:
      return (False, f"parse failed: {e}")

  def get_dates(token, note_label):
    try:
      s.headers["kp-api-token"] = token
      response = s.get(service_url + "?postalCode=" + str(postCode))
      _health(response.status_code, note_label, postCode)
      if response.status_code != 200:
        return (False, response.status_code)
      data = response.json()
      if not isinstance(data, dict) or "delivery_dates" not in data:
        return (False, "missing 'delivery_dates'")
      if not isinstance(data["delivery_dates"], list) or len(data["delivery_dates"]) == 0:
        return (False, "empty 'delivery_dates'")
      return (True, json.dumps(data))
    except RequestException as e:
      _health(None, f"{note_label} transport error", postCode)
      return (False, str(e))
    except Exception as e:
      _health(200, f"{note_label} invalid JSON", postCode)
      return (False, f"invalid JSON: {e}")

  api_token = (
    base64.b64encode(
      bytes(base64.b64decode("pils")) + bytes(str(int(time.time())), "utf8")
    ).decode().replace("=", "")
  )

  # 1) Try generated token
  ok, data_or_err = get_dates(api_token, "generated token")
  if ok:
    _DAILY_CACHE[key] = (True, data_or_err)
    LAST_UPSTREAM["note"] = "generated ok"
    return _DAILY_CACHE[key]
  logging.warning(f"Generated token failed for {postCode}: {data_or_err}")

  # 2) Fallback: scrape token then fetch
  tok_ok, token_or_err = get_token()
  if not tok_ok:
    LAST_UPSTREAM["note"] = "generated failed, scraped failed"
    _health(None, "generated failed, scraped failed", postCode)
    return (False, token_or_err)

  ok2, data_or_err2 = get_dates(token_or_err, "scraped token")
  if ok2:
    _DAILY_CACHE[key] = (True, data_or_err2)
    LAST_UPSTREAM["note"] = "generated failed, scraped ok"
    return _DAILY_CACHE[key]

  LAST_UPSTREAM["note"] = "generated failed, scraped failed"
  _health(None, "generated failed, scraped failed", postCode)
  return (False, data_or_err2)

# -------- Flask app --------
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
  return jsonify({
    "ok": True,
    "last_upstream": LAST_UPSTREAM,
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
  delivery_dates = Posten(postCode)
  if delivery_dates[0]:
    return jsonify(json.loads(delivery_dates[1]))
  else:
    return jsonify({"Error": delivery_dates[1]})

@app.route("/text/<int:postCode>", methods=["GET"])
@app.route("/text/<int:postCode>.json", methods=["GET"])
@cross_origin()
def deilvery_days(postCode):
  postCode = str(postCode).zfill(4)
  if request.method != "GET":
    return Response(status=405)
  if len(postCode) != 4:
    return Response(status=404)
  delivery_dates = Posten(postCode)
  if delivery_dates[0]:
    weekdays = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
    months = ["Januar", "Februar", "Mars", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Desember"]
    text_dates = []
    for date in json.loads(delivery_dates[1])['delivery_dates']:
      date = dt.datetime.strptime(date, "%Y-%m-%d")
      text_dates.append(f"{weekdays[date.weekday()]} {date.day}. {months[date.month-1]}")
    return jsonify({"delivery_dates": text_dates})
  else:
    return jsonify({"Error": delivery_dates[1]})

@app.route("/next/<int:postCode>", methods=["GET"])
@app.route("/next/<int:postCode>.json", methods=["GET"])
@cross_origin()
def delivery_next(postCode):
  postCode = str(postCode).zfill(4)
  if request.method != "GET":
    return Response(status=405)
  if len(postCode) != 4:
    return Response(status=404)
  delivery_dates = Posten(postCode)
  if delivery_dates[0]:
    nextDatesStrings = ["i dag", "i morgen", "i overmorgen"]
    next_dates = []
    for date in json.loads(delivery_dates[1])['delivery_dates']:
      deliveryDate = dt.datetime.strptime(date, "%Y-%m-%d").date()
      nextDate = (deliveryDate - dt.datetime.now().date()).days
      if nextDate > 2:
        next_dates.append(f"om {nextDate} dager")
      else:
        next_dates.append(nextDatesStrings[nextDate])
    return jsonify({"delivery_dates": next_dates})
  else:
    return jsonify({"Error": delivery_dates[1]})

if __name__ == "__main__":
  logs_dir = os.path.join(app.root_path, 'logs')
  os.makedirs(logs_dir, exist_ok=True)
  handlers = [
    logging.FileHandler(os.path.join(logs_dir, 'posten.log')),
    logging.StreamHandler(sys.stdout)
  ]
  logging.basicConfig(handlers=handlers,
                      format='%(asctime)s %(levelname)s:%(message)s',
                      datefmt='%d/%m/%Y %H:%M:%S',
                      level=logging.INFO)
  app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
  http_server = WSGIServer(("0.0.0.0", 5000), app)
  http_server.serve_forever()

