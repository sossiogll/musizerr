#!/bin/sh
# The official image's own 10-listen-on-ipv6-by-default.sh edits /etc/nginx/conf.d/default.conf, which here does not
# exist yet when it runs (the template is rendered by 20-envsubst-on-templates.sh, after it). Same thing, afterwards:
# listen on IPv6 too when the container has it.
set -eu

conf=/etc/nginx/conf.d/default.conf

[ -f "$conf" ] || exit 0
[ -f /proc/net/if_inet6 ] || exit 0
grep -q 'listen \[::\]:80;' "$conf" && exit 0
grep -q 'listen 80;' "$conf" || exit 0

sed -i 's/^\( *\)listen 80;/\1listen 80;\n\1listen [::]:80;/' "$conf"
echo "25-listen-on-ipv6.sh: enabled listening on IPv6"
