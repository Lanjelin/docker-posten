FROM python:3.12.0a7-alpine3.17

COPY app/ /app

RUN \
  echo "**** install packages ****" && \
  apk update && \
  apk add --no-cache \
    gcc && \
  cd app && \
  python -m pip install -r requirements.txt && \
  echo "**** cleanup ****" && \
  rm -rf \
    /tmp/* \
    /var/tmp/*

EXPOSE 5000

ENTRYPOINT ["python", "/app/posten.py"]
