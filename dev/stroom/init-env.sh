#!/usr/bin/env bash
# Writes .env with a random insecure test credential for the local Stroom stack, once.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "STROOM_TEST_CREDENTIAL=local-$(python -c 'import secrets; print(secrets.token_urlsafe(24))')" > .env
  echo "Wrote dev/stroom/.env"
else
  echo "dev/stroom/.env already exists"
fi
