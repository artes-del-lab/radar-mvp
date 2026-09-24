#!/usr/bin/env bash
# Установка РАДАРа на чистый сервер Ubuntu 24.04 одной командой.
#
# Что делает:
#   1. ставит Python, git и Caddy (веб-сервер с автоматическим HTTPS);
#   2. скачивает РАДАР в /opt/radar и ставит зависимости;
#   3. спрашивает ключи и создаёт .env с паролем для входа;
#   4. запускает РАДАР как службу (перезапуск при сбоях и после перезагрузки);
#   5. включает HTTPS на адресе вида 1-2-3-4.sslip.io (домен не нужен);
#   6. включает оценку новых тендеров каждое утро.
#
# Повторный запуск безопасен: обновит код, а .env и накопленные оценки оставит.
#
# Запуск (от root, в веб-консоли сервера):
#   curl -fsSL https://raw.githubusercontent.com/artes-del-lab/radar-mvp/claude/radar-tender-mvp-jh8u0b/deploy/install.sh | bash
# Если репозиторий станет закрытым — перед этим: export GH_TOKEN=токен_GitHub
# и добавить к curl: -H "Authorization: token $GH_TOKEN"

set -euo pipefail

REPO="artes-del-lab/radar-mvp"
BRANCH="${RADAR_BRANCH:-claude/radar-tender-mvp-jh8u0b}"
APP_DIR=/opt/radar
APP_USER=radar
TOKEN_FILE=/root/.radar-github-token

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
ask() {  # ask "Вопрос" имя_переменной — читает с клавиатуры, даже если скрипт пришёл через curl | bash
  local answer
  read -r -p "$1: " answer </dev/tty
  printf -v "$2" '%s' "$answer"
}

[ "$(id -u)" -eq 0 ] || { echo "Запустите от root (или через sudo)"; exit 1; }

# --- токен GitHub нужен, только если репозиторий закрытый ---------------------
if [ -n "${GH_TOKEN:-}" ]; then
  printf '%s' "$GH_TOKEN" >"$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi
GH_TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null || true)"
if [ -n "$GH_TOKEN" ]; then
  DEFAULT_REPO_URL="https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git"
else
  DEFAULT_REPO_URL="https://github.com/${REPO}.git"
fi
REPO_URL="${RADAR_REPO_URL:-$DEFAULT_REPO_URL}"  # RADAR_REPO_URL — для проверки скрипта

say "Ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
  # В образе нет caddy — ставим из официального репозитория Caddy.
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt >/etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi

id "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

say "Скачиваю РАДАР (ветка $BRANCH)"
# Папка принадлежит пользователю radar, а git запускается от root.
git config --global --get-all safe.directory | grep -qx "$APP_DIR" || git config --global --add safe.directory "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q "$REPO_URL" "$BRANCH"
  git -C "$APP_DIR" reset -q --hard FETCH_HEAD
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  # Токен не храним в настройках репозитория — только в $TOKEN_FILE.
  git -C "$APP_DIR" remote set-url origin "https://github.com/${REPO}.git"
fi

say "Ставлю зависимости Python"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"

# --- .env ---------------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
  say "Ключи (вставьте и нажмите Enter)"
  # Ключи можно передать заранее: export RADAR_ANTHROPIC_TOKEN=… RADAR_TENDERPLAN_TOKEN=…
  ANTHROPIC_TOKEN="${RADAR_ANTHROPIC_TOKEN:-}"
  TENDERPLAN_TOKEN="${RADAR_TENDERPLAN_TOKEN:-}"
  [ -n "$ANTHROPIC_TOKEN" ] || ask "Токен шлюза baza-ai (ANTHROPIC_AUTH_TOKEN)" ANTHROPIC_TOKEN
  [ -n "$TENDERPLAN_TOKEN" ] || ask "Токен Тендерплана" TENDERPLAN_TOKEN
  PASSWORD="$(python3 -c "import secrets; print(secrets.token_urlsafe(10))")"
  cat >"$APP_DIR/.env" <<EOF
TENDERPLAN_API_KEY=$TENDERPLAN_TOKEN
TENDERPLAN_SEARCH_KEY_ID=
DATA_SOURCE=auto

ANTHROPIC_API_KEY=
ANTHROPIC_BASE_URL=https://api.baza-ai.org
ANTHROPIC_AUTH_TOKEN=$ANTHROPIC_TOKEN
ANTHROPIC_MODEL=claude-opus-5

RADAR_USER=radar
RADAR_PASSWORD=$PASSWORD
EOF
fi
chmod 600 "$APP_DIR/.env"
mkdir -p "$APP_DIR/data/cache"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "Запускаю РАДАР как службу"
cat >/etc/systemd/system/radar.service <<EOF
[Unit]
Description=РАДАР — дашборд тендеров
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR/backend
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Утренняя оценка: берёт свежие тендеры и оценивает новые. Второй запуск через
# полчаса добирает то, что не прошло из-за сбоев шлюза (готовые оценки — из кэша).
cat >/etc/systemd/system/radar-scoring.service <<EOF
[Unit]
Description=РАДАР — оценка новых тендеров

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR/backend
ExecStart=$APP_DIR/.venv/bin/python -m app.scoring
EOF
cat >/etc/systemd/system/radar-scoring.timer <<'EOF'
[Unit]
Description=РАДАР — оценка новых тендеров по утрам

[Timer]
OnCalendar=*-*-* 06:30:00 Europe/Moscow
OnCalendar=*-*-* 07:00:00 Europe/Moscow
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable -q radar.service radar-scoring.timer
systemctl restart radar.service
systemctl start radar-scoring.timer

say "Включаю HTTPS"
IP="$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')"
HOST="${RADAR_DOMAIN:-${IP//./-}.sslip.io}"
cat >/etc/caddy/Caddyfile <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
systemctl enable -q caddy
systemctl restart caddy
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

sleep 3
if systemctl is-active -q radar.service; then
  PASSWORD_NOW="$(grep '^RADAR_PASSWORD=' "$APP_DIR/.env" | cut -d= -f2-)"
  say "Готово"
  echo "  Адрес:  https://$HOST"
  echo "  Логин:  radar"
  echo "  Пароль: $PASSWORD_NOW"
  echo
  echo "  Первое открытие — до минуты: РАДАР загружает карточки тендеров."
  echo "  Сертификат HTTPS выпускается автоматически в первые минуты."
else
  say "РАДАР не запустился. Последние строки журнала:"
  journalctl -u radar.service -n 30 --no-pager
  exit 1
fi
