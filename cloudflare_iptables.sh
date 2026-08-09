#!/usr/bin/env bash
# Ограничивает вход на публичный TLS-порт панели (см. README "Публикация
# панели через Cloudflare") только
# IP-диапазонами самого Cloudflare -- порт не должен быть напрямую
# доступен всему интернету, только через прокси Cloudflare. Отдельная
# история от NFQUEUE-правил (mangle) в sandbox/setup_sandbox.sh -- это
# обычный filter/INPUT ACL по порту, не пересекается с ними ни таблицей,
# ни цепочкой.
#
# Идемпотентен -- пересоздаёт ipset-множества с нуля при каждом запуске
# (Cloudflare изредка меняет диапазоны, см. cron ниже).
#
#   sudo ./cloudflare_iptables.sh <PANEL_PUBLIC_PORT>
#
# В cron (обновлять диапазоны раз в сутки):
#   0 4 * * * root /opt/z2r_autobench/z0r-panel/cloudflare_iptables.sh <PORT> >> /var/log/cf-iptables.log 2>&1
set -euo pipefail

PORT="${1:?Использование: $0 <PANEL_PUBLIC_PORT>}"

if ! command -v ipset >/dev/null; then
    echo "ipset не установлен: sudo apt-get install -y ipset" >&2
    exit 1
fi

ipset create cf-ipv4 hash:net family inet -exist
ipset create cf-ipv6 hash:net family inet6 -exist
ipset flush cf-ipv4
ipset flush cf-ipv6

curl -fsSL https://www.cloudflare.com/ips-v4 | while read -r net; do
    [ -n "$net" ] && ipset add cf-ipv4 "$net"
done
curl -fsSL https://www.cloudflare.com/ips-v6 | while read -r net; do
    [ -n "$net" ] && ipset add cf-ipv6 "$net"
done

# Убираем прошлые правила этого скрипта перед тем как добавить свежие --
# иначе повторный запуск плодит дубликаты (тот же класс бага, что уже
# был со stale NFQUEUE-правилами в z2r_autobench/Zenith -- см. их
# CLAUDE.md/README).
iptables -D INPUT -p tcp --dport "$PORT" -m set --match-set cf-ipv4 src -j ACCEPT 2>/dev/null || true
iptables -D INPUT -p tcp --dport "$PORT" -j DROP 2>/dev/null || true
ip6tables -D INPUT -p tcp --dport "$PORT" -m set --match-set cf-ipv6 src -j ACCEPT 2>/dev/null || true
ip6tables -D INPUT -p tcp --dport "$PORT" -j DROP 2>/dev/null || true

iptables -I INPUT -p tcp --dport "$PORT" -m set --match-set cf-ipv4 src -j ACCEPT
iptables -A INPUT -p tcp --dport "$PORT" -j DROP
ip6tables -I INPUT -p tcp --dport "$PORT" -m set --match-set cf-ipv6 src -j ACCEPT
ip6tables -A INPUT -p tcp --dport "$PORT" -j DROP

echo "Готово: порт $PORT открыт только для IP-диапазонов Cloudflare ($(ipset list cf-ipv4 | grep -c '^[0-9]') v4 + $(ipset list cf-ipv6 | grep -c '^[0-9a-f]') v6 сетей)."
echo "Не забудь сохранить правила (iptables-persistent/netfilter-persistent), иначе слетят при ребуте:"
echo "  sudo netfilter-persistent save   # если установлен iptables-persistent"
