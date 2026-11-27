FROM python:3.14-slim
WORKDIR /app
COPY *.py /app/planetarium/
COPY static /app/planetarium/static
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3","-m","planetarium.server","--bind","0.0.0.0","--config","/data/sources.json","--db","/data/events.sqlite"]
