#!/usr/bin/env bash
# Provision a 6 Stars Clips server (Ubuntu 24.04). Run once as root:
#   ssh root@<ip> 'bash -s' < scripts/provision_server.sh
# Idempotent: safe to run again. Takes no secrets.
set -euo pipefail

DEPLOY_USER="deploy"
APP_DIR="/opt/6stars"
SWAP_FILE="/swapfile"
SWAP_SIZE_GB=4

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

echo "== apt update/upgrade"
apt-get update -q
apt-get "${APT_OPTS[@]}" upgrade -q
apt-get "${APT_OPTS[@]}" install -q ca-certificates curl gnupg ufw

echo "== timezone UTC"
timedatectl set-timezone UTC

echo "== Docker Engine + compose plugin (official apt repo)"
install -m 0755 -d /etc/apt/keyrings
if [ ! -s /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
# shellcheck disable=SC1091
CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
REPO_LINE="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable"
if [ "$(cat /etc/apt/sources.list.d/docker.list 2>/dev/null || true)" != "$REPO_LINE" ]; then
  echo "$REPO_LINE" > /etc/apt/sources.list.d/docker.list
  apt-get update -q
fi
apt-get "${APT_OPTS[@]}" install -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

echo "== user ${DEPLOY_USER}"
if ! id -u "$DEPLOY_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"
DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_HOME/.ssh"
touch "$DEPLOY_HOME/.ssh/authorized_keys"
if [ -s /root/.ssh/authorized_keys ]; then
  # Copy root's authorized keys (the key the server was created with), no duplicates.
  while IFS= read -r key; do
    [ -z "$key" ] && continue
    grep -qxF "$key" "$DEPLOY_HOME/.ssh/authorized_keys" || echo "$key" >> "$DEPLOY_HOME/.ssh/authorized_keys"
  done < /root/.ssh/authorized_keys
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_HOME/.ssh/authorized_keys"
chmod 600 "$DEPLOY_HOME/.ssh/authorized_keys"

echo "== app directories"
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$APP_DIR/data"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR/secrets"

echo "== firewall"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== ssh: key login only"
cat > /etc/ssh/sshd_config.d/00-6stars.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSHD
chmod 644 /etc/ssh/sshd_config.d/00-6stars.conf
# Ubuntu 24.04 uses socket activation, so /run/sshd may not exist yet.
install -d -m 755 /run/sshd
sshd -t
systemctl reload ssh

echo "== swap ${SWAP_SIZE_GB}G"
if ! swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
  if [ ! -f "$SWAP_FILE" ]; then
    fallocate -l "${SWAP_SIZE_GB}G" "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
  fi
  swapon "$SWAP_FILE"
fi
grep -qE "^${SWAP_FILE}[[:space:]]" /etc/fstab || echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab

echo "== done"
docker --version
docker compose version
ufw status
swapon --show
timedatectl show -p Timezone --value
[ -f /var/run/reboot-required ] && echo "NOTE: reboot required to finish kernel/library upgrades" || true
