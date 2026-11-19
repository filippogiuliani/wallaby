#!/bin/sh
# Rebuild and restart Wallaby. Data stays in ./Wallaby_data (mounted on /app/data).
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo "The .env file is missing: copy .env.example to .env and configure it." >&2
  exit 1
fi
sudo docker build -t wallaby .
sudo docker rm -f wallaby 2>/dev/null || true
sudo docker run -d --restart unless-stopped -p 3000:3000 --env-file .env \
  -v "$(pwd)/Wallaby_data:/app/data" --name wallaby wallaby
echo "Wallaby started on http://localhost:3000"
