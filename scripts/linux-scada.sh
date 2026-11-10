#!/bin/bash
# FUXA SCADA/HMI + Firefox kiosk + RTU terminals for the sceptre app's `linux_fuxa` stack.
# One image serves scada-server, hmi and engineer-workstation; the role is whatever the app injects:
#   /etc/sceptre/fuxa_project.json -> fuxa.service + fuxa-project.service run (scada-server)
#   /etc/sceptre/hmi_url           -> Firefox kiosk autostarts (hmi, scada-server)
#   /etc/sceptre/rtus.txt          -> one SSH terminal per RTU (engineer-workstation)
# Units, loader and autostarts come from overlays/linux-scada.
# Licences: FUXA MIT (@frangoteam/fuxa-min), Node.js MIT, Firefox MPL-2.0.
set -ex

export DEBIAN_FRONTEND=noninteractive

FUXA_VERSION=1.3.1
NODE_MAJOR=22

# mingui pulls in snapd; its apt post-invoke hook cannot talk to snapd in the build chroot and makes apt exit non-zero
grep -l 'advise-snap' /etc/apt/apt.conf.d/* | xargs -r rm -f

apt-get install -y ca-certificates curl gnupg jq wget xfce4-terminal sshpass openssh-client telnet

# Node (NodeSource; Ubuntu's nodejs is too old for FUXA). -min omits node-snap7 (S7 driver), so no compiler is needed.
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
apt-get install -y nodejs
node -v | grep -q "^v${NODE_MAJOR}\."
npm install --prefix /opt/fuxa --omit=dev --no-audit --no-fund "@frangoteam/fuxa-min@${FUXA_VERSION}"
install -d /var/lib/fuxa /etc/sceptre
chmod +x /usr/local/bin/fuxa-load-project /usr/local/bin/engineer-terminals
systemctl enable fuxa.service fuxa-project.service

# Firefox from Mozilla's apt repo (Ubuntu's firefox is a snap and does not work in a chroot)
install -d -m 0755 /etc/apt/keyrings /etc/firefox/policies
wget -qO /etc/apt/keyrings/packages.mozilla.org.asc https://packages.mozilla.org/apt/repo-signing-key.gpg
echo 'deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main' \
  > /etc/apt/sources.list.d/mozilla.list
printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
apt-get update
apt-get install -y --allow-downgrades firefox
# skip first-run pages so the kiosk lands on the URL
cat > /etc/firefox/policies/policies.json <<'POL'
{"policies":{"OverrideFirstRunPage":"","OverridePostUpdatePage":"","DontCheckDefaultBrowser":true,"DisableTelemetry":true,"DisableFirefoxStudies":true,"NoDefaultBookmarks":true,"DisableProfileImport":true,"Preferences":{"browser.aboutwelcome.enabled":false,"datareporting.policy.dataSubmissionPolicyBypassNotification":true}}}
POL

# The range display must never blank or lock: hide screensaver/power-manager autostarts and make their
# binaries unrunnable (nosaver.desktop runs `xset s off -dpms` at login).
for f in xfce4-screensaver xscreensaver xfce4-power-manager gnome-screensaver light-locker; do
  d=/etc/xdg/autostart/$f.desktop
  if [ -f "$d" ]; then
    sed -i '/^Hidden=/d' "$d"
    echo 'Hidden=true' >> "$d"
  fi
  for b in /usr/bin/$f /usr/libexec/$f /usr/lib/*/xfce4/"$f"; do
    if [ -f "$b" ]; then chmod -x "$b"; fi
  done
done

# no DHCP datasource to wait for: cloud-init / wait-online would hold graphical.target ~5 min
touch /etc/cloud/cloud-init.disabled
ln -sf /dev/null /etc/systemd/system/systemd-networkd-wait-online.service
