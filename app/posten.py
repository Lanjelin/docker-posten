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
from cachetools import cached, TTLCache
import datetime as dt
import base64
import time
import logging


def ttl_midnight():
  tomorrow = dt.date.today() + dt.timedelta(1)
  midnight = dt.datetime.combine(tomorrow, dt.time())
  return (midnight - dt.datetime.now()).seconds


@cached(cache=TTLCache(maxsize=5096, ttl=ttl_midnight()))
def Posten(postCode):
  logging.info(f"Served {postCode}")
  s = session()
  s.headers["User-Agent"] = "Mozilla/5.0"
  token_url = "https://www.posten.no/levering-av-post"
  service_url = "https://www.posten.no/levering-av-post/_/service/no.posten.website/delivery-days"

  # Getting token from posten - using as fallback
  def get_token():
    try:
      response = s.get(token_url)
      soup = BeautifulSoup(response.text, "html.parser")
      delivery_script = soup.find("script", {"data-react4xp-ref":"parts_mailbox-delivery__main_1_leftRegion_11"})
      delivery_json = json.loads(delivery_script.contents[0])
      api_token = delivery_json["props"]["apiKey"]
      # service_url = delivery_json["props"]["serviceUrl"]
    except RequestException as e:
      return(False, e)
    else:
      return(True, api_token)

  # Getting dates
  def get_dates(token):
    try:
      s.headers["kp-api-token"] = token
      response = s.get(service_url + "?postalCode=" + str(postCode))
      if not response.status_code == 200:
        return(False, response.status_code)
    except RequestException as e:
      return(False, e)
    else:
      return(True, response.content)

  # Generate token - thanks BobTheShoplifter@github
  api_token = (base64.b64encode(bytes(base64.b64decode("pils")) + bytes(str(int(time.time())), "utf8")).decode().replace("=", ""))

  # Try generated, else curl for a token
  if (data := get_dates(api_token))[0]:
    return data
  logging.warning(f"Generated token failed {data[1]} - {api_token}")
  if not (crawl_token := get_token())[0]:
    return crawl_token
  return(get_dates(crawl_token[1]))


app = Flask(__name__)
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
    f"Usage: <br>" \
    f"&emsp; <a href='{request.url_root}raw/4321.json'>{request.url_root}raw/4321.json</a> for raw data.<br>" \
    f"&emsp; <a href='{request.url_root}text/4321.json'>{request.url_root}text/4321.json</a> for formatted text dates.<br>" \
    f"<br><br>Source on <a href='https://github.com/Lanjelin/docker-posten/'>GitHub</a>"
  )


@app.route("/raw/<int:postCode>", methods=["GET"])
@app.route("/raw/<int:postCode>.json", methods=["GET"])
@cross_origin()
def delivery_raw(postCode):
  postCode = str(postCode).zfill(4)
  if not request.method == "GET":
    return Response(status=405)
  if not (len(str(postCode)) == 4):
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
  if not request.method == "GET":
    return Response(status=405)
  if not (len(str(postCode)) == 4):
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


if __name__ == "__main__":
  logging.basicConfig(filename=os.path.join(app.root_path, 'logs/posten.log'), format='%(asctime)s %(levelname)s:%(message)s', datefmt='%d/%m/%Y %H:%M:%S', level=logging.INFO)
  app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
  http_server = WSGIServer(("0.0.0.0", 5000), app)
  http_server.serve_forever()
