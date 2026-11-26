# Mihomo airport gateway

This optional service converts the provider's Clash subscription into four
internal HTTP/SOCKS5 endpoints:

- `http://mihomo:17890` / `socks5://mihomo:17890`: US, Japan or Singapore
- `http://mihomo:17891` / `socks5://mihomo:17891`: United States
- `http://mihomo:17892` / `socks5://mihomo:17892`: Japan
- `http://mihomo:17893` / `socks5://mihomo:17893`: Singapore

Hong Kong nodes are excluded from every group. The subscription URL is read
from the server-only `AIRPORT_SUBSCRIPTION_URL` environment variable and is
not stored in the repository.

Start it on a configured server with:

```sh
docker compose --profile airport up -d mihomo
```
