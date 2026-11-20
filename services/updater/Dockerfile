FROM docker:27-cli

ARG COMPOSE_VERSION=v2.39.2
RUN apk add --no-cache python3 git bash curl openssl \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
       -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

WORKDIR /app
COPY updater.py .
RUN mkdir -p /app/state
EXPOSE 8092
CMD ["python3", "/app/updater.py"]
