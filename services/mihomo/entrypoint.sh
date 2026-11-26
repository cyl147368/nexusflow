#!/bin/sh
set -eu

: "${AIRPORT_SUBSCRIPTION_URL:?AIRPORT_SUBSCRIPTION_URL is required}"

CONFIG_DIR="${MIHOMO_HOME:-/root/.config/mihomo}"
mkdir -p "$CONFIG_DIR/providers"
umask 077

cat > "$CONFIG_DIR/config.yaml" <<EOF
mixed-port: 0
allow-lan: true
bind-address: "*"
mode: rule
log-level: warning
ipv6: false
unified-delay: true
tcp-concurrent: true
external-controller: 0.0.0.0:9090
secret: "${MIHOMO_CONTROLLER_SECRET:-}"

profile:
  store-selected: true

dns:
  enable: true
  ipv6: false
  nameserver:
    - 1.1.1.1
    - 8.8.8.8

proxy-providers:
  airport:
    type: http
    url: "${AIRPORT_SUBSCRIPTION_URL}"
    path: ./providers/airport.yaml
    interval: 21600
    header:
      User-Agent:
        - "Clash.Meta/1.19.0"
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      timeout: 5000
      lazy: false
      expected-status: 204

proxy-groups:
  - name: REG-US
    type: select
    use: [airport]
    filter: "(?i)(美国|🇺🇸|United States)"
    exclude-filter: "(?i)(香港|🇭🇰|Hong Kong)"

  - name: REG-JP
    type: select
    use: [airport]
    filter: "(?i)(日本|🇯🇵|Japan)"
    exclude-filter: "(?i)(香港|🇭🇰|Hong Kong)"

  - name: REG-SG
    type: select
    use: [airport]
    filter: "(?i)(新加坡|🇸🇬|Singapore)"
    exclude-filter: "(?i)(香港|🇭🇰|Hong Kong)"

  - name: REG-TW
    type: select
    use: [airport]
    filter: "(?i)(台湾|臺灣|🇹🇼|Taiwan)"
    exclude-filter: "(?i)(香港|🇭🇰|Hong Kong)"

  - name: REG-AUTO
    type: url-test
    use: [airport]
    filter: "(?i)(美国|🇺🇸|日本|🇯🇵|新加坡|🇸🇬|台湾|臺灣|🇹🇼|United States|Japan|Singapore|Taiwan)"
    exclude-filter: "(?i)(香港|🇭🇰|Hong Kong)"
    url: https://www.gstatic.com/generate_204
    interval: 300
    timeout: 5000
    tolerance: 50

listeners:
  - name: reg-auto
    type: mixed
    port: 17890
    listen: 0.0.0.0
    proxy: REG-AUTO
    udp: false

  - name: reg-us
    type: mixed
    port: 17891
    listen: 0.0.0.0
    proxy: REG-US
    udp: false

  - name: reg-jp
    type: mixed
    port: 17892
    listen: 0.0.0.0
    proxy: REG-JP
    udp: false

  - name: reg-sg
    type: mixed
    port: 17893
    listen: 0.0.0.0
    proxy: REG-SG
    udp: false

  - name: reg-tw
    type: mixed
    port: 17894
    listen: 0.0.0.0
    proxy: REG-TW
    udp: false

rules:
  - MATCH,DIRECT
EOF

exec /mihomo -d "$CONFIG_DIR" -f "$CONFIG_DIR/config.yaml"
