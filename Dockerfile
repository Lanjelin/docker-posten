FROM python:3.12-slim

WORKDIR /app
COPY app/ /app

RUN \
  echo "**** install packages ****" && \
  python -m pip install -r requirements.txt

EXPOSE 5000

ENTRYPOINT ["python", "/app/posten.py"]
