#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${BOT_TOKEN:-}" ]]; then
  printf '%s\n' 'Set BOT_TOKEN in github.com/settings/codespaces → New secret → name BOT_TOKEN, then restart this Codespace.' >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  printf '%s\n' 'Docker-in-Docker is unavailable in this Codespace. Rebuild it with the repository devcontainer, then retry.' >&2
  exit 1
fi

node --input-type=module <<'NODE'
import { randomBytes } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { chmodSync } from "node:fs";

const templatePath = "spot-signal/.env.example";
const envPath = "spot-signal/.env";
const parse = text => Object.fromEntries(text.split(/\r?\n/)
  .filter(line => line && !line.startsWith("#") && line.includes("="))
  .map(line => {
    const index = line.indexOf("=");
    return [line.slice(0, index), line.slice(index + 1)];
  }));
const template = parse(await readFile(templatePath, "utf8"));
let values = {};
try {
  values = parse(await readFile(envPath, "utf8"));
} catch {}
values = { ...template, ...values };
const secret = key => values[key] || randomBytes(32).toString("hex");
values.POSTGRES_PASSWORD = secret("POSTGRES_PASSWORD");
values.APP_DB_PASSWORD = secret("APP_DB_PASSWORD");
values.APP_HMAC_SECRET = secret("APP_HMAC_SECRET");
values.BOT_INGEST_TOKEN = secret("BOT_INGEST_TOKEN");
values.MODERATION_TOKEN = secret("MODERATION_TOKEN");
values.TELEGRAM_BOT_TOKEN = process.env.BOT_TOKEN;
values.DATABASE_URL = `postgres://spotsignal_app:${values.APP_DB_PASSWORD}@127.0.0.1:5432/spotsignal`;
values.SECURE_COOKIES = "true";
values.HOST_PORT = "3000";
await writeFile(envPath, `${Object.entries(values).map(([key, value]) => `${key}=${value}`).join("\n")}\n`, { mode: 0o600 });
chmodSync(envPath, 0o600);
NODE

COMPOSE=(docker compose -f spot-signal/docker-compose.yml)
"${COMPOSE[@]}" up -d db redis

ready=0
for _ in $(seq 1 120); do
  if "${COMPOSE[@]}" exec -T db pg_isready -q -U postgres -d spotsignal >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  printf '%s\n' 'Postgres did not become ready within 120 seconds. Check docker compose logs db.' >&2
  exit 1
fi

corepack pnpm --dir spot-signal test
"${COMPOSE[@]}" up -d --build api bot web

ready=0
for _ in $(seq 1 120); do
  if curl -fsS --output /dev/null http://localhost:3000/healthz; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  printf '%s\n' 'SpotSignal API did not become healthy within 120 seconds. Check docker compose logs api bot web.' >&2
  exit 1
fi

if [[ -n "${CODESPACES:-}" ]]; then
  : "${CODESPACE_NAME:?CODESPACE_NAME is missing in this Codespace}"
  gh codespace ports visibility 3000:public 5173:public --codespace "$CODESPACE_NAME"
  API_URL="https://${CODESPACE_NAME}-3000.app.github.dev"
  PWA_URL="https://${CODESPACE_NAME}-5173.app.github.dev"
else
  API_URL="http://localhost:3000"
  PWA_URL="http://localhost:5173"
fi

printf '\n✅ API live at: %s\n' "$API_URL"
printf '✅ PWA live at: %s\n' "$PWA_URL"
printf '%s\n' '✅ Bot is long-polling — message it on Telegram to test.'
printf '%s\n' 'Test checklist: see docs/PHONE_DEPLOY.md §5'
printf '%s\n' 'Shut down to save hours: bash scripts/codespace-down.sh'
