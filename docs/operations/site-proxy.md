# Machine-wide site proxy

This page states the shared Caddy project that publishes every site on one host and the two verbs
that change it.

## The project

One Caddy container per host publishes the sites, from its own Compose project
`course-explorer-proxy` in the machine-wide shared directory. `src/course_explorer_admin/proxy.py`
owns the project: `PROXY_DIRECTORY`, `SITES_DIRECTORY`, `CADDYFILE`, `COMPOSE_FILE` and
`ADMIN_ADDRESS` name its parts.

The first `publish` materializes the project from `examples/deploy/proxy/`. `_materialize` creates
`proxy/` and `proxy/sites/` at mode 0755, copies the two project files from the templates only while
they are absent, writes them at mode 0644 and leaves a present file byte for byte.
`_refuse_irregular` refuses a symlinked or non-regular project path instead of following it.

| Path under `proxy/` | Holds |
|---|---|
| `Caddyfile` | The top Caddyfile: Caddy's administration endpoint on `unix//tmp/caddy-admin.sock` inside the container, and one import of `sites/*.caddy` |
| `docker-compose.yml` | The proxy project's Compose file, with its own project name and relative bind sources |
| `sites/<instance key>.caddy` | One site file per published instance |

The Compose file declares the project name itself, so `_compose` runs `docker compose --file` with
no environment bridge and no project argument, and every relative mount source resolves against the
file's directory. Each command is bounded by the configured database operation timeout.

## The proxy container

| Part | Value |
|---|---|
| Image | `caddy:2.11.7-alpine`, pinned by manifest digest |
| Network | `network_mode: host`, so the proxy's listeners are host ports |
| Bind address | `PROXY_BIND_ADDRESS` from the publishing checkout's `config/database.toml`, one IPv4 or IPv6 address, written into each site file as its `bind` line. Absent, every host address |
| Mounts | The project's `Caddyfile` at `/etc/caddy/Caddyfile` and its `sites` directory at `/etc/caddy/sites`, both read-only, with no host-path creation |
| Volumes | `course-explorer-proxy_caddy_data` at `/data` and `course-explorer-proxy_caddy_config` at `/config` |
| Capabilities | `cap_drop: ALL` with `cap_add: NET_BIND_SERVICE` |
| Security | `no-new-privileges:true` |
| Restart | `restart: unless-stopped` |

The host network namespace carries the host's privileged-port boundary, so `NET_BIND_SERVICE` is the
one capability the container needs to bind ports 80 and 443. Caddy supplies each domain's
certificate and the HTTP-to-HTTPS redirect, and it stores its certificates and its configuration in
the two named volumes.

## The site file

`render_site(*, domain, host, port, bind_address)` returns one Caddyfile site: a leading comment
line that names it the public site for the configured domain, the `https://<domain> {` opening line,
an optional `bind <address>` line, a `reverse_proxy <host>:<port>` line and the closing `}`. An IPv6
site target is bracketed. The file is written on every `publish`, so a changed site address,
loopback port or bind address reaches the proxy at that publish.

## Publishing and unpublishing

`publish_configured_site` in `src/course_explorer_admin/self_host.py` reads the web, shared-storage
and instance settings, refuses an address the proxy cannot serve, and calls `proxy.publish` with the
checkout root, the shared root, the instance key, the site address, the loopback host and port and
the configured bind address. `_require_public_site_address` refuses an absent address, a loopback
host and an address that carries an explicit port. `publish` derives the domain from the address
with `urlsplit(...).hostname` and refuses an address that names no host.

A publish writes the instance's site file, starts the project with `up -d --wait proxy` and reloads
the running container with `caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
--address unix//tmp/caddy-admin.sock`. `scripts/self-host.sh publish` runs the same owner for the
configured instance.

`unpublish_configured_site` reads the instance key and the shared directory and calls
`proxy.unpublish`, which refuses an irregular site path, removes that one site file and reloads a
running project. An absent site file is a no-op, and the site file is the instance's one publication
record, so every other published site retains its bytes and its service.

A reload that Caddy refuses leaves the configuration it loaded last in service, so every site it
already serves continues to answer.

## The bind address

`PROXY_BIND_ADDRESS` in `config/database.toml` names one IPv4 or IPv6 literal. The settings read
normalizes the text and refuses a blank string or any other text with
`PROXY_BIND_ADDRESS must be an IPv4 or IPv6 address`, and an absent value binds every host address.
The value reaches `render_site` as `bind_address` and becomes the site file's `bind` line, so a
changed value reaches the proxy at the next `publish` of that site.

## The public port preflight

A domain-mode run calls `proxy.listening_sockets()`, which decodes the listen state of
`/proc/net/tcp` and `/proc/net/tcp6` in the host network namespace the proxy shares. A listener on
port 80 or 443 on the configured bind address or the wildcard address, or on any address when none
is configured, refuses the run unless `proxy.running` reports the project container up. The read is
the kernel's listen table rather than a bind probe, because the account holds no capability to bind
a port below 1024.

## Separation from an instance stack

The proxy project is separate from every instance project, so one instance's `up` and `down` reach
that instance's services alone and a stopped instance leaves every published site serving. The
container mounts its own two project paths and the two named volumes, and an instance publishes
through its site file alone. [Server containers](/docs/operations/server-containers.md) states the
instance services and their mounts.

## Tests that pin this behavior

| Test module | Covers |
|---|---|
| `tests/admin/test_self_host.py` | The domain-mode setup writing the instance's site file with its bind line, copying both project files byte for byte from the templates, starting the project and reloading Caddy in that order, the listen-table read of a bound loopback port, and the unpublish that removes one instance's site file, leaves another's bytes as they are and reloads only a running project |
