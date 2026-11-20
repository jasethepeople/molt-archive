#!/usr/bin/env bash
set -euo pipefail

docker compose -f spot-signal/docker-compose.yml down
printf '%s\n' 'Free GitHub personal-account quota: 120 Codespaces core-hours/month. Actual session usage is not measured here; check GitHub Settings → Billing.'
printf '%s\n' 'Stop the Codespace in the GitHub UI too. The Postgres volume is preserved.'
