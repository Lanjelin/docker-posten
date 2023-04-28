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
    render_template,
)
from cachetools import cached, TTLCache

@cached(cache=TTLCache(maxsize=1024, ttl=60*30))
def Posten(postCode):
    s = session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    token_url = "https://www.posten.no/levering-av-post"
    service_url = ""
    api_token = ""

    # Getting token
    try:
        response = s.get(token_url)
        soup = BeautifulSoup(response.text, "html.parser")
        delivery_script = soup.find("script", {"data-react4xp-ref":"parts_mailbox-delivery__main_1_leftRegion_11"})
        delivery_json = json.loads(delivery_script.contents[0])
        api_token = delivery_json["props"]["apiKey"]
        service_url = delivery_json["props"]["serviceUrl"]
    except RequestException as e:
        return(False, e)

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
    return f"Usage: {request.url_root}PostalCode.json"

@app.route("/<int:postCode>", methods=["GET"])
@app.route("/<int:postCode>.json", methods=["GET"])
@cross_origin()
def serveDelivery(postCode):
    if not request.method == "GET":
        return 404
    delivery_dates = Posten(postCode)
    if delivery_dates[0]:
        return jsonify(json.loads(delivery_dates[1]))
    else:
        return jsonify({"Error": delivery_dates[1]})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
