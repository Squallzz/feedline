# Feedline security review

Scope: `feedline_server.py` (stdlib HTTP proxy) and `feedline.html` (single page). Method: manual code review of every route and every
place printer- or user-supplied data reaches a shell-like sink (G-code), the filesystem, an outbound request or `innerHTML`. No automated
scanner or external penetration test was run, and nothing was tested against hostile network conditions beyond local checks.

Threat model: a home/lab LAN. The attacker is (a) another device on the LAN, (b) a web page the user visits that tries to reach the
dashboard through the browser, or (c) a compromised printer/Spoolman feeding bad data to the page.

## Findings

| # | Sev | Finding | Status |
| - | --- | --- | --- |
| 1 | High | No authentication: anyone reaching the port can start/stop moves, heat, change macros, edit printer config files | **Mitigated**: optional HTTP Basic login (`auth` / `FEEDLINE_PASSWORD`, constant-time compare); startup warning when off. Not on by default: you must set it |
| 2 | Med | DNS rebinding: a malicious site could re-point a name at the dashboard's IP and drive it from the victim's browser | **Fixed**: Host header allowlist (IPs, single-label and common local suffixes, plus `allowed_hosts`); others get 421 |
| 3 | Med | Cross-site requests (CSRF) | Already covered: `/control` requires the custom `X-Feedline-Control` header (cannot be sent cross-site without CORS, which the server never grants) |
| 4 | Med | Clickjacking and MIME sniffing | **Fixed**: `X-Frame-Options: DENY` (unless `allow_embed`), `nosniff`, `Referrer-Policy: no-referrer` |
| 5 | Med | Stored XSS from printer-supplied names/titles (heater, fan, lane names) and uploaded SVG graphics | **Fixed**: those values are HTML-escaped; uploaded SVG has scripts, handlers, external links and non-local `url()` removed, and packs are served with `Content-Security-Policy: sandbox` |
| 6 | Low | Request-size and slow-client handling | **Fixed**: negative/oversized `Content-Length` rejected (6 MB cap), 30 s socket timeout |
| 7 | Low | Unbounded growth of config backups | **Fixed**: newest 20 per file kept |
| 8 | Low | Config editing is powerful (a Klipper config can run G-code on restart) | `allow_config_edit: false` makes it read-only; edits are limited to text config types, 512 KB, with a backup and a changed-on-printer check; never restarts Klipper |
| 9 | Low | Path traversal in config/pack paths | Reviewed: strict allowlist regex, no `..`, no hidden files |
| 10 | Low | G-code injection | Reviewed: macro buttons and custom macro text are limited to `NAME KEY=value` of commands the printer reports; restart/shutdown-style commands denied; temperatures and fan speeds range-checked |
| 11 | Info | SSRF through "add printer" / Spoolman address | Loopback, link-local, unspecified and multicast refused; private LAN addresses are allowed on purpose (that is the point). A DNS name could change between check and use |

## Residual risks (known, not fixed)

- No TLS. With `auth` on, the password travels in clear text unless you front it with a TLS reverse proxy (Caddy, nginx, Home Assistant).
- A single shared login; no rate limiting on login attempts. Use a long random password (the installer generates one).
- `config.json` must be readable by the service user, so it is world-readable on the host when installed by `install.sh`.
- Error messages from failed upstream requests are passed to the browser (can reveal internal addresses to someone already logged in).
- The server follows HTTP redirects from Moonraker/Spoolman and trusts webcam URLs the printer reports.
- The page itself is a large inline script; a strict CSP is not possible without rewriting it.

## Recommendations

1. Set `auth`, or put the dashboard behind a reverse proxy that does.
2. Bind to localhost (`--bind 127.0.0.1`) when a reverse proxy is on the same machine; firewall port 8081 otherwise.
3. Never expose it to the internet directly.
4. Set `allow_config_edit: false` if you do not need the Files tab.
