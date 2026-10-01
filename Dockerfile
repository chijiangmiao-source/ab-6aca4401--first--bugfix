FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    QUIET=1

WORKDIR /srv

COPY app ./app
COPY tests ./tests
COPY verify.py ./verify.py

EXPOSE 8080

HEALTHCHECK --interval=3s --timeout=3s --start-period=3s --retries=15 \
    CMD python3 -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0 if r.status==200 else 1)"

CMD ["python3", "-m", "app.server"]
