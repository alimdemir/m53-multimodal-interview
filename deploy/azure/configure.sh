#!/usr/bin/env bash
set -euo pipefail

domain="${M53_DOMAIN:?M53_DOMAIN ayarlayın, örn. m53.<bölge>.cloudapp.azure.com}"
public_ip="${M53_PUBLIC_IP:?M53_PUBLIC_IP ayarlayın}"
django_secret="$(openssl rand -hex 48)"
turn_password="$(openssl rand -hex 24)"
ice_json="[{\"urls\":[\"stun:${domain}:3478\",\"turn:${domain}:3478?transport=udp\",\"turn:${domain}:3478?transport=tcp\"],\"username\":\"mulakatos\",\"credential\":\"${turn_password}\"}]"

sed \
  -e "s|__DJANGO_SECRET_KEY__|${django_secret}|g" \
  -e "s|__ICE_SERVERS_JSON__|${ice_json}|g" \
  -e "s|__DOMAIN__|${domain}|g" \
  -e "s|__PUBLIC_IP__|${public_ip}|g" \
  /tmp/mulakatos.env.template > /tmp/mulakatos.env
sed -e "s|__TURN_PASSWORD__|${turn_password}|g" -e "s|__DOMAIN__|${domain}|g" -e "s|__PUBLIC_IP__|${public_ip}|g" \
  /tmp/turnserver.conf.template > /tmp/turnserver.conf

install -o root -g www-data -m 0640 /tmp/mulakatos.env /etc/mulakatos.env
install -o root -g root -m 0644 /tmp/turnserver.conf /etc/turnserver.conf
install -o root -g root -m 0644 /tmp/mulakatos.service /etc/systemd/system/mulakatos.service
install -o root -g root -m 0750 /tmp/deallocate-vm.py /usr/local/sbin/mulakatos-deallocate
install -o root -g root -m 0644 /tmp/mulakatos-deallocate.service /etc/systemd/system/mulakatos-deallocate.service
install -o root -g root -m 0644 /tmp/mulakatos-deallocate.timer /etc/systemd/system/mulakatos-deallocate.timer
sed -i "s|__DOMAIN__|${domain}|g" /tmp/Caddyfile
install -o root -g root -m 0644 /tmp/Caddyfile /etc/caddy/Caddyfile

rm -f /tmp/mulakatos.env /tmp/mulakatos.env.template /tmp/turnserver.conf \
  /tmp/turnserver.conf.template /tmp/mulakatos.service /tmp/Caddyfile \
  /tmp/deallocate-vm.py /tmp/mulakatos-deallocate.service /tmp/mulakatos-deallocate.timer

timedatectl set-timezone Europe/Istanbul
systemctl daemon-reload
systemctl enable redis-server mulakatos coturn caddy mulakatos-deallocate.timer
caddy validate --config /etc/caddy/Caddyfile
