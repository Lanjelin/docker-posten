FROM python:3.12.0a7-alpine3.17

COPY app/ /app

RUN \
  cd app && \
  python -m pip install -r requirements.txt

EXPOSE 5000

ENTRYPOINT ["python", "/app/posten.py"]
