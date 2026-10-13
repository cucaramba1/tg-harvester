#!/bin/zsh
# TG Harvester: автозапуск трёх процессов.
#   scripts/setup.sh install | bot-only | restart | stop | status | logs | night-now | uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
DOMAIN="gui/$(id -u)"
BASE="com.cucaramba.tg-harvester"
LABELS=("$BASE" "$BASE.night" "$BASE.audit")

plist_path() { echo "$HOME/Library/LaunchAgents/$1.plist"; }

write_plist() {  # $1 label, $2 команда, $3 xml расписания
  mkdir -p "$ROOT/logs"
  cat > "$(plist_path "$1")" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-m</string><string>harvester</string><string>$2</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  $3
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd-$2.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd-$2.err.log</string>
</dict>
</plist>
EOF
}

case "${1:-install}" in
  install)
    cd "$ROOT"
    [[ -f .env ]] || { cp .env.example .env; echo "Заполни BOT_TOKEN, TG_API_ID, TG_API_HASH в $ROOT/.env и запусти install ещё раз"; exit 1; }
    [[ -f data/harvester.session ]] || { echo "Сначала: uv run python -m harvester login"; exit 1; }
    [[ -x "$PY" ]] || uv sync
    write_plist "$BASE" live "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>"
    write_plist "$BASE.night" night "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>"
    write_plist "$BASE.audit" audit "<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>"
    for l in $LABELS; do
      launchctl bootout "$DOMAIN/$l" 2>/dev/null || true
      launchctl bootstrap "$DOMAIN" "$(plist_path "$l")"
    done
    echo "✅ TG Harvester установлен: live работает, ночь в 03:00, аудит по воскресеньям в 04:00"
    ;;
  bot-only)  # только бот ревью, без сбора и ночи: сервис не растит диск
    cd "$ROOT"
    write_plist "$BASE" bot "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer>"
    launchctl bootout "$DOMAIN/$BASE" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$(plist_path "$BASE")"
    echo "🤖 запущен только бот (сбор и ночь выключены). Полный запуск: scripts/setup.sh install"
    ;;
  restart) launchctl kickstart -k "$DOMAIN/$BASE" && echo "🔄 live перезапущен" ;;
  stop) for l in $LABELS; do launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; done; echo "⏹ остановлен" ;;
  status) for l in $LABELS; do echo "== $l"; launchctl print "$DOMAIN/$l" 2>/dev/null | grep -E "state|pid|last exit" || echo "не загружен"; done ;;
  logs) tail -n 50 -f "$ROOT/logs/harvester-live.log" ;;
  night-now) launchctl kickstart "$DOMAIN/$BASE.night" && echo "🌙 ночной разбор запущен, лог: logs/harvester-night.log" ;;
  uninstall)
    for l in $LABELS; do launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; rm -f "$(plist_path "$l")"; done
    echo "🗑 автозапуск удалён, data/ не тронута"
    ;;
  *) echo "usage: $0 install|bot-only|restart|stop|status|logs|night-now|uninstall"; exit 1 ;;
esac
