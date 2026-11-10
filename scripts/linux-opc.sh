#!/bin/bash
# OPC UA gateway (Modbus/TCP -> OPC UA) for the sceptre app's `linux_fuxa` stack.
# Gateway code and unit come from overlays/linux-opc. Config is injected at runtime by the
# sceptre app: /etc/sceptre/opc_gateway.json. Licences: asyncua LGPL-3.0, pymodbus BSD-3-Clause.
set -ex

export DEBIAN_FRONTEND=noninteractive

ASYNCUA_VERSION=2.1.0
PYMODBUS_VERSION=3.16.2

apt-get install -y python3 python3-venv

install -d /etc/sceptre
python3 -m venv /opt/opcua-gateway/venv
/opt/opcua-gateway/venv/bin/pip install --no-cache-dir \
  "asyncua==${ASYNCUA_VERSION}" "pymodbus==${PYMODBUS_VERSION}"

# uaread / uals / uawrite for checking the gateway from a shell
for t in uaread uals uawrite; do
  ln -sf "/opt/opcua-gateway/venv/bin/$t" "/usr/local/bin/$t"
done

systemctl enable opcua-gateway.service
