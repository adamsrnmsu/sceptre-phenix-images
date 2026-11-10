#!/bin/bash
# InfluxDB 2 + Telegraf for the sceptre app's `linux_fuxa` historian role.
# Runtime injects from the sceptre app: /etc/telegraf/telegraf.conf and
# /etc/phenix/startup/influx-setup.sh (creates org/bucket/token, restarts telegraf).
# Licences: InfluxDB 2 (MIT), Telegraf (MIT).
set -ex

export DEBIAN_FRONTEND=noninteractive

INFLUXDB_VERSION=2.9.1-1
INFLUXDB_CLI_VERSION=2.7.5-3
TELEGRAF_VERSION=1.40.3-1
# https://docs.influxdata.com/influxdb/v2/install/ - archive key fingerprint
INFLUX_KEY_FPR=24C975CBA61A024EE1B631787C3D57159FC2F927

apt-get install -y curl gnupg

install -d -m 0755 /etc/apt/keyrings /etc/phenix/startup
curl -fsSL https://repos.influxdata.com/influxdata-archive.key -o /tmp/influxdata-archive.key
gpg --show-keys --with-colons /tmp/influxdata-archive.key | grep -q "^fpr:\+${INFLUX_KEY_FPR}:\$"
gpg --dearmor < /tmp/influxdata-archive.key > /etc/apt/keyrings/influxdata-archive.gpg
rm -f /tmp/influxdata-archive.key
echo 'deb [signed-by=/etc/apt/keyrings/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main' \
  > /etc/apt/sources.list.d/influxdata.list

apt-get update
apt-get install -y "influxdb2=${INFLUXDB_VERSION}" "influxdb2-cli=${INFLUXDB_CLI_VERSION}" "telegraf=${TELEGRAF_VERSION}"

# The telegraf deb installs its systemd unit from the postinst only when systemd is running, so in the
# build chroot it falls back to a SysV script. Install the unit by hand; the native unit takes precedence over the leftover SysV script.
install -m 0644 /usr/lib/telegraf/scripts/telegraf.service /etc/systemd/system/telegraf.service

systemctl enable influxdb.service telegraf.service
