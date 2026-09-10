#!/usr/bin/env bash
#
# setup_nginx_proxy.sh -- put the screener behind nginx with HTTP basic auth.
#
#   sudo ./deploy/setup_nginx_proxy.sh <username>                    # Cloudflare in front (default)
#   sudo ./deploy/setup_nginx_proxy.sh <username> --static-root /var/www/screener
#                                                                    # production build: nginx
#                                                                    # serves the hashed assets
#   sudo ./deploy/setup_nginx_proxy.sh <username> --direct           # no CDN, open :80 to the world
#   sudo ./deploy/setup_nginx_proxy.sh <username> \
#        --origin-cert /path/cert.pem --origin-key /path/key.pem     # + serve :443 (Full (strict))
#
# Prompts for the password. Never pass it on the command line -- it would land in your
# shell history and in /proc/<pid>/cmdline while the script runs.
#
# Idempotent. Re-running with the same username changes that password; a different
# username adds a user.
#
# CLOUDFLARE MODE (the default, and what stockey.japlin.com actually uses)
#   The hostname resolves to Cloudflare (2606:4700:...), so the browser's TLS terminates
#   at Cloudflare's edge, NOT here. Two consequences the plain setup gets wrong:
#
#     * Every request arrives from a Cloudflare address, so nginx logs, and anything
#       built on $remote_addr, would record the CDN instead of the visitor. Fixed with
#       real_ip over Cloudflare's published ranges + CF-Connecting-IP.
#     * Opening :80 to the whole internet leaves the origin directly reachable at its own
#       IP, letting anyone skip Cloudflare entirely. So ufw allows :80 ONLY from
#       Cloudflare's ranges, fetched live rather than hardcoded (they change).
#
# WHAT IT DOES NOT DO
#   It never exposes 3000/8000/8090. Everything is reached through this app's own
#   same-origin route rules, so the basic-auth gate cannot be sidestepped by hitting a
#   backend port directly.

set -euo pipefail

USERNAME=""; MODE="cloudflare"; ORIGIN_CERT=""; ORIGIN_KEY=""; STATIC_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --direct) MODE="direct" ;;
    --static-root) STATIC_ROOT="${2:-}"; shift ;;
    --origin-cert) ORIGIN_CERT="${2:-}"; shift ;;
    --origin-key)  ORIGIN_KEY="${2:-}"; shift ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) USERNAME="$1" ;;
  esac
  shift
done
[ -n "$USERNAME" ] || { echo "Usage: sudo $0 <username> [--static-root DIR] [--direct] [--origin-cert C --origin-key K]" >&2; exit 2; }
# Checked after parsing so --help is usable without sudo.
[ "$(id -u)" -eq 0 ] || { echo "This needs root: sudo $0 $USERNAME" >&2; exit 1; }
if [ -n "$ORIGIN_CERT" ] || [ -n "$ORIGIN_KEY" ]; then
  [ -r "$ORIGIN_CERT" ] && [ -r "$ORIGIN_KEY" ] || { echo "origin cert/key not readable" >&2; exit 2; }
fi
if [ -n "$STATIC_ROOT" ]; then
  [ -d "$STATIC_ROOT/_nuxt" ] || {
    echo "!! ${STATIC_ROOT}/_nuxt does not exist. Run ./deploy/build_and_deploy.sh first," >&2
    echo "   or leave --static-root off to proxy everything to the dev server." >&2
    exit 2
  }
fi

SITE_NAME="screener"
SITE_FILE="/etc/nginx/sites-available/${SITE_NAME}"
HTPASSWD="/etc/nginx/.htpasswd-${SITE_NAME}"
REALIP_FILE="/etc/nginx/conf.d/cloudflare-realip.conf"
# Pinned to the literal IPv4 address, not "localhost": the Nuxt dev server binds ONLY
# ::1 unless started with --host, and `localhost` resolves at config-parse time to
# whichever family comes first -- a proxy that works or 502s depending on /etc/hosts.
APP_UPSTREAM="127.0.0.1:3000"

command -v nginx >/dev/null || { echo "nginx is not installed" >&2; exit 1; }
command -v htpasswd >/dev/null 2>&1 || { echo "==> installing apache2-utils"; apt-get update -qq && apt-get install -y -qq apache2-utils; }

echo "==> basic-auth credentials for '${USERNAME}'"
if [ -f "$HTPASSWD" ]; then htpasswd -B "$HTPASSWD" "$USERNAME"; else htpasswd -c -B "$HTPASSWD" "$USERNAME"; fi
chown root:www-data "$HTPASSWD"; chmod 640 "$HTPASSWD"

# ---------------------------------------------------------------- real client IPs
if [ "$MODE" = "cloudflare" ]; then
  echo "==> fetching Cloudflare IP ranges (live, not hardcoded -- they change)"
  CF_V4="$(curl -fsS --max-time 20 https://www.cloudflare.com/ips-v4)" || { echo "could not fetch ips-v4" >&2; exit 1; }
  CF_V6="$(curl -fsS --max-time 20 https://www.cloudflare.com/ips-v6)" || { echo "could not fetch ips-v6" >&2; exit 1; }
  {
    echo "# Managed by screener/deploy/setup_nginx_proxy.sh -- regenerated on each run."
    echo "# Without this, \$remote_addr is a Cloudflare address on every request and the"
    echo "# access log records the CDN rather than the visitor."
    while read -r r; do [ -n "$r" ] && echo "set_real_ip_from ${r};"; done <<< "$CF_V4"
    while read -r r; do [ -n "$r" ] && echo "set_real_ip_from ${r};"; done <<< "$CF_V6"
    echo "real_ip_header CF-Connecting-IP;"
    echo "real_ip_recursive on;"
  } > "$REALIP_FILE"
  echo "    wrote ${REALIP_FILE} ($(grep -c set_real_ip_from "$REALIP_FILE") ranges)"
fi

BACKUP=""
if [ -f "$SITE_FILE" ]; then
  BACKUP="${SITE_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"; cp "$SITE_FILE" "$BACKUP"
  echo "==> backed up existing config to ${BACKUP}"
fi

# ---------------------------------------------------------------- the site
STATIC_BLOCK=""
if [ -n "$STATIC_ROOT" ]; then
STATIC_BLOCK=$(cat <<'SBLOCK'
    # The build's hashed assets, straight off disk. These never reach node, which is
    # most of the request volume on any page load.
    #
    # =404 rather than a fallback to the app ON PURPOSE. In production every one of
    # these files exists, so the fallback would never fire; but if someone switches
    # back to `nuxt dev` without re-running this script, a fallback would quietly serve
    # them year-cached production assets. A loud 404 is the better failure.
    root __STATIC_ROOT__;

    location /_nuxt/ {
        try_files $uri =404;
        # Safe only because every filename here contains a content hash: a changed file
        # is a different URL, so nothing stale can be pinned.
        expires 1y;
        add_header Cache-Control "public, immutable";
        access_log off;
    }

    # favicon, robots.txt and anything else the build drops in public/ — served from
    # disk when it exists, handed to the app when it does not.
    location = /favicon.ico { try_files $uri @app; access_log off; }
    location = /robots.txt  { try_files $uri @app; access_log off; }
SBLOCK
)
STATIC_BLOCK="${STATIC_BLOCK//__STATIC_ROOT__/$STATIC_ROOT}"
fi

PROXY_BLOCK=$(cat <<'BLOCK'
__STATIC_BLOCK__
    # ONE auth gate for the whole origin. Both backends are reached through this app's
    # own /_stockey/ and /_systrader/ route rules, so they inherit this gate instead of
    # needing separate location blocks somebody could forget. That matters:
    # fundamentals/api/app.py's own docstring says it has NO auth and is "NOT hardened
    # for public exposure".
    auth_basic           "stockey screener";
    auth_basic_user_file __HTPASSWD__;

    location / {
        proxy_pass http://__UPSTREAM__;
        proxy_http_version 1.1;
        # The dev server streams HMR over a websocket; without Upgrade the page loads
        # and then sits there retrying forever.
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # The data-health page recomputes completeness over a 528M-row compressed
        # hypertable on a cold cache; the default 60s read timeout turns that into a 504.
        proxy_connect_timeout 15s;
        proxy_send_timeout    120s;
        proxy_read_timeout    120s;
    }

    location @app {
        proxy_pass http://__UPSTREAM__;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    client_max_body_size 10m;
    access_log /var/log/nginx/screener.access.log;
    error_log  /var/log/nginx/screener.error.log;
BLOCK
)
PROXY_BLOCK="${PROXY_BLOCK//__STATIC_BLOCK__/$STATIC_BLOCK}"
PROXY_BLOCK="${PROXY_BLOCK//__HTPASSWD__/$HTPASSWD}"
PROXY_BLOCK="${PROXY_BLOCK//__UPSTREAM__/$APP_UPSTREAM}"

echo "==> writing ${SITE_FILE}"
if [ -n "$ORIGIN_CERT" ]; then
  cat > "$SITE_FILE" <<NGINX
# Managed by screener/deploy/setup_nginx_proxy.sh -- re-running overwrites this file
# (the previous version is kept alongside as .bak.<timestamp>).
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl default_server;
    listen [::]:443 ssl default_server;
    http2 on;
    server_name _;

    ssl_certificate     ${ORIGIN_CERT};
    ssl_certificate_key ${ORIGIN_KEY};
    ssl_protocols TLSv1.2 TLSv1.3;

${PROXY_BLOCK}
}
NGINX
else
  cat > "$SITE_FILE" <<NGINX
# Managed by screener/deploy/setup_nginx_proxy.sh -- re-running overwrites this file
# (the previous version is kept alongside as .bak.<timestamp>).
#
# Plain :80 only. With Cloudflare in front this is the CDN-to-origin hop, which is
# encrypted only if Cloudflare's SSL mode is Flexible-or-better on the browser side --
# the hop ITSELF is cleartext unless you add an origin certificate and re-run with
# --origin-cert/--origin-key (Cloudflare SSL mode "Full (strict)").
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

${PROXY_BLOCK}
}
NGINX
fi

ln -sfn "$SITE_FILE" "/etc/nginx/sites-enabled/${SITE_NAME}"
if [ -e /etc/nginx/sites-enabled/default ]; then
  rm -f /etc/nginx/sites-enabled/default
  echo "==> disabled the stock default site (it also claims :80 default_server)"
fi

echo "==> validating nginx config"
if ! nginx -t; then
  echo "!! nginx config test FAILED -- rolling back, nothing reloaded" >&2
  rm -f "/etc/nginx/sites-enabled/${SITE_NAME}"
  [ -n "$BACKUP" ] && cp "$BACKUP" "$SITE_FILE"
  exit 1
fi
systemctl reload nginx
echo "==> nginx reloaded"

# ---------------------------------------------------------------- firewall
PORTS=(80); [ -n "$ORIGIN_CERT" ] && PORTS+=(443)
if [ "$MODE" = "cloudflare" ]; then
  echo "==> ufw: allowing ${PORTS[*]} from Cloudflare ranges ONLY"
  echo "    (open to the world and the origin is reachable at its own IP, bypassing the CDN)"
  # Drop any previous world-open rule for these ports so re-running tightens rather than
  # leaving the old permissive rule in place underneath.
  for p in "${PORTS[@]}"; do ufw --force delete allow "${p}/tcp" >/dev/null 2>&1 || true; done
  while read -r r; do
    [ -n "$r" ] || continue
    for p in "${PORTS[@]}"; do ufw allow from "$r" to any port "$p" proto tcp >/dev/null; done
  done <<< "$CF_V4"
  while read -r r; do
    [ -n "$r" ] || continue
    for p in "${PORTS[@]}"; do ufw allow from "$r" to any port "$p" proto tcp >/dev/null; done
  done <<< "$CF_V6"
else
  echo "==> ufw: allowing ${PORTS[*]} from anywhere (--direct)"
  for p in "${PORTS[@]}"; do ufw allow "${p}/tcp"; done
fi
ufw --force reload >/dev/null 2>&1 || true

cat <<DONE

  Done (mode: ${MODE}$( [ -n "$ORIGIN_CERT" ] && echo ", origin TLS on :443" )).

  Verify:
      curl -sI https://stockey.japlin.com/ | head -1                 # expect 401
      curl -sI -u ${USERNAME}:<pass> https://stockey.japlin.com/ | head -1   # expect 200

  Nuxt must stay listening on ${APP_UPSTREAM}:
      cd ~/code/trading/screener && ./deploy/build_and_deploy.sh      # production
      cd ~/code/trading/screener && setsid --fork npx nuxt dev --host 127.0.0.1 --port 3000
  Never start it with --host 0.0.0.0 -- that publishes :3000 directly and lets anyone
  reach the app without passing the auth gate.

DONE

if [ "$MODE" = "cloudflare" ] && [ -z "$ORIGIN_CERT" ]; then
cat <<'WARN'
  ONE THING LEFT TO DECIDE -- the Cloudflare-to-origin hop.

  Your browser's TLS ends at Cloudflare, so the password is encrypted on the public
  internet. But Cloudflare then talks to this box over plain HTTP on :80. If Cloudflare's
  SSL mode is "Flexible", that second hop is cleartext across the network between them.

  To close it: generate a Cloudflare Origin Certificate (dashboard -> SSL/TLS -> Origin
  Server -> Create Certificate), save the cert and key on this host, then re-run:

      sudo ./deploy/setup_nginx_proxy.sh <username> \
           --origin-cert /etc/ssl/cloudflare/origin.pem \
           --origin-key  /etc/ssl/cloudflare/origin.key

  ...and set Cloudflare's SSL mode to "Full (strict)".

WARN
fi
