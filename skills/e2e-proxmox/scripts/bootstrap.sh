#!/usr/bin/env bash
# Runs as root INSIDE a fresh Debian 13 test LXC, never on the Proxmox node.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if [[ -e /opt/nanoclaw || -L /opt/nanoclaw || -e /home/nanoclaw || -L /home/nanoclaw ]] || id nanoclaw >/dev/null 2>&1; then
  echo 'NanoClaw paths or account already exist; use a fresh container.' >&2
  exit 65
fi

# pct start returns before DHCP/DNS is necessarily ready. Do not install
# packages using the template's stale indexes after a partial apt update.
timeout 120 bash -c '
  until getent ahostsv4 deb.debian.org >/dev/null &&
        getent ahostsv4 security.debian.org >/dev/null; do sleep 2; done
'
apt-get -o Acquire::Retries=3 update --error-on=any -qq
apt-get -o Acquire::Retries=3 install -y -qq \
  ca-certificates curl git python3 sudo build-essential \
  dbus-user-session libpam-systemd systemd-container

useradd --create-home --shell /bin/bash nanoclaw
# The user manager caches supplementary groups at startup. Establish Docker
# membership now; adding it later only fixes the installer shell, not services.
groupadd --system --force docker
usermod -aG docker nanoclaw
printf 'nanoclaw ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/nanoclaw-e2e
chmod 440 /etc/sudoers.d/nanoclaw-e2e
visudo -cf /etc/sudoers.d/nanoclaw-e2e
loginctl enable-linger nanoclaw
systemctl start "user@$(id -u nanoclaw).service"
install -d -m 0755 -o nanoclaw -g nanoclaw /opt/nanoclaw
install -d -m 0700 -o nanoclaw -g nanoclaw /home/nanoclaw/.nanoclaw-e2e
