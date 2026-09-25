FROM docker.io/python:3.13-slim

WORKDIR /app

# git is needed by pip to install pytincture from its repository; gcc and
# libffi cover bcrypt/cryptography on architectures without wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libffi-dev git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# wapyt is installed non-editable so pytincture's widgetset discovery can read
# __widgetset__ from the distribution; an editable install silently yields an
# app with no widgets.
COPY vendor-wheels/ /tmp/vendor-wheels/
RUN pip install --no-cache-dir /tmp/vendor-wheels/wapyt-*.whl && rm -rf /tmp/vendor-wheels

COPY service.py .
COPY appcode/ appcode/

# SQLite, secret.key (encrypts stored MongoDB passwords) and session.key live
# here. Mount a named volume: losing secret.key loses every stored password.
RUN mkdir -p /data
VOLUME /data

EXPOSE 8766

ENV MONGUANA_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8766

# Never as root: the database holds every user's server credentials.
RUN useradd --system --uid 10001 --home /app monguana && chown -R monguana /app /data
USER monguana

CMD ["python", "service.py"]
