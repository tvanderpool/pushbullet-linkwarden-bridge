#!/usr/bin/env bash
cd $(dirname "$0") || exit 1
docker run -d --restart unless-stopped \
    --name pushbullet-linkwarden-bridge \
    -v "$(pwd):/app" -w /app \
    --entrypoint /app/docker-entrypoint.sh \
    --add-host=host.docker.internal:host-gateway \
    python:3.12-slim
