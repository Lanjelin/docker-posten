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
)
from gevent.pywsgi import WSGIServer
from werkzeug.middleware.proxy_fix import ProxyFix
from cachetools import cached, TTLCache
import datetime as dt
import base64
import time

def ttl_midnight():
  tomorrow = dt.date.today() + dt.timedelta(1)
  midnight = dt.datetime.combine(tomorrow, dt.time())
  return (midnight - dt.datetime.now()).seconds

@cached(cache=TTLCache(maxsize=5096, ttl=ttl_midnight()))
def Posten(postCode):
  s = session()
  s.headers["User-Agent"] = "Mozilla/5.0"
  token_url = "https://www.posten.no/levering-av-post"
  service_url = "https://www.posten.no/levering-av-post/_/service/no.posten.website/delivery-days"
  api_token = ""

  # Getting token from posten - ready to use as a fallback
  def get_token():
    try:
      response = s.get(token_url)
      soup = BeautifulSoup(response.text, "html.parser")
      delivery_script = soup.find("script", {"data-react4xp-ref":"parts_mailbox-delivery__main_1_leftRegion_11"})
      delivery_json = json.loads(delivery_script.contents[0])
      api_token = delivery_json["props"]["apiKey"]
      service_url = delivery_json["props"]["serviceUrl"]
    except RequestException as e:
      return(False, e)

  #Generate token - thanks BobTheShoplifter@github
  api_token = (base64.b64encode(bytes(base64.b64decode("pils")) + bytes(str(int(time.time())), "utf8")).decode().replace("=", ""))


  # Getting dates
  try:
    s.headers["kp-api-token"] = api_token
    response = s.get(service_url + "?postalCode=" + str(postCode))
    if not response.status_code == 200:
      return(False, response.status_code)
    return(True, response.content)
  except RequestException as e:
    return(False, e)

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
    f"<br><br><a href='https://github.com/Lanjelin/docker-posten/'>GitHub</a>"
  )


@app.route("/raw/<int:postCode>", methods=["GET"])
@app.route("/raw/<int:postCode>.json", methods=["GET"])
@cross_origin()
def delivery_raw(postCode):
  if not request.method == "GET":
    return 404
  delivery_dates = Posten(postCode)
  if delivery_dates[0]:
    return jsonify(json.loads(delivery_dates[1]))
  else:
    return jsonify({"Error": delivery_dates[1]})

@app.route("/text/<int:postCode>", methods=["GET"])
@app.route("/text/<int:postCode>.json", methods=["GET"])
@cross_origin()
def deilvery_days(postCode):
  if not request.method == "GET":
    return 404
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
  app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
  http_server = WSGIServer(("0.0.0.0", 5000), app)
  http_server.serve_forever()
