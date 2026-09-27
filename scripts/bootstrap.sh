#!/bin/sh
set -eu
new_secret() {
  openssl rand -hex 32
}

if [ ! -f .env ]; then
  cp .env.example .env
  for name in MYSQL_PASSWORD MYSQL_ROOT_PASSWORD RABBITMQ_PASSWORD GRAFANA_PASSWORD API_KEY READ_API_KEY GRAFANA_DB_PASSWORD UI_ADMIN_PASSWORD; do
    value=$(new_secret)
    sed -i "s/^$name=.*/$name=$value/" .env
  done
  chmod 600 .env
fi
if ! grep -q '^UI_ADMIN_USERNAME=' .env; then
  printf '%s\n' 'UI_ADMIN_USERNAME=admin' >> .env
fi
if ! grep -q '^UI_ADMIN_PASSWORD=' .env; then
  printf 'UI_ADMIN_PASSWORD=%s\n' "$(new_secret)" >> .env
fi
docker compose up -d --build --wait --wait-timeout 180
