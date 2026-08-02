# Pinned by tag AND digest. Upstream blinkbridge uses a bare `python:alpine`
# with unpinned pip installs (bug B-21), so two builds a week apart can differ
# in Python minor version and in blinkpy major version -- and blinkpy's auth
# flow has broken downstream bridges twice in the past year.
FROM python:3.12-alpine AS base

RUN apk add --no-cache ffmpeg tini

# Dependencies first, so source edits do not invalidate the layer.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY lostblink ./lostblink
RUN pip install --no-cache-dir --no-deps -e .

# Run unprivileged. The credentials file holds a full-access OAuth refresh
# token; there is no reason for this process to be root.
RUN adduser -D -u 1000 lostblink \
    && mkdir -p /config /working \
    && chown -R lostblink:lostblink /config /working /app
USER lostblink

ENV LOSTBLINK_CONFIG=/config/config.json \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/working"]

# tini reaps the ffmpeg children we spawn; without it they accumulate as
# zombies over a long-running container's lifetime.
ENTRYPOINT ["/sbin/tini", "--", "python", "-m", "lostblink"]
CMD ["run"]
