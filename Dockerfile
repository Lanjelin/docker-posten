FROM python:3.9.16-alpine3.17

COPY app/ /app

RUN \
  echo "**** install packages ****" && \
  apk update && \
  apk add --update --no-cache --virtual .build-deps \
    gcc \
    libc-dev \
    libffi-dev && \
  cd app && \
  python -m pip install -r requirements.txt && \
  echo "**** cleanup ****" && \
  apk del .build-deps && \
  rm -rf \
    /tmp/* \
    /var/tmp/*

EXPOSE 5000

ENTRYPOINT ["python", "/app/posten.py"]
