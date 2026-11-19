# Self-hosting

Self-hosting runs a copy as containers on a Linux host and serves its site through either serve mode.

## The stack

A host needs Docker Engine with the Compose plugin, an account that writes `/var/run/docker.sock`, and Git. `docker-compose.yml` at the checkout root is a working copy materialized from `examples/docker-compose.yml`, and it defines the instance's services under the Compose project `course-explorer-<instance key>`: the database with pgvector, the embedding service, the site, the administration backend, the feedback sender and the same-machine worker. Every service declares `restart: unless-stopped`, so Docker starts it again after a host reboot unless an operator stopped it.

The embedding service and the four server services `web`, `admin`, `sender` and `worker` share the host network namespace, so the site's loopback bind, the console's loopback bind and the embedding service's loopback listener are the host's. The site reaches the database and the embedding service through the configured host and port values.

`scripts/self-host.sh up` builds and starts the instance's services, and `scripts/self-host.sh down` stops and removes its containers and network. [Starting, stopping and updating a copy](/docs/operations/server-containers.md#starting-stopping-and-updating-a-copy) lists the services, the profile-gated additions and the data both keep. `up` starts a copy whose working files are present, and a fresh checkout runs the example or real-database workflow first. [The image targets](/docs/operations/server-containers.md#the-image-targets) states what each image carries, and [mounts](/docs/operations/server-containers.md#mounts) states what each container sees.

## The two serve modes

The example setup asks the serve mode, and the mode decides the proxy arrangement.

| Mode | What the setup writes | What the proxy does |
|---|---|---|
| `local` | No site file. The site's address defaults to `http://localhost:<web_port>/`. | Nothing starts. Any external site address belongs to the operator's own proxy. |
| `domain` | `sites/<instance key>.caddy`, holding the domain from `app_url` and the instance's loopback site port. | The project starts when it is absent, and the running proxy reloads. |

A running copy changes its published site with the two publication verbs, which read the configured instance key, the site address and the shared directory:

```bash
scripts/self-host.sh publish --shared-root /srv/course-explorer
scripts/self-host.sh unpublish --shared-root /srv/course-explorer
```

`publish` reads `WEB_APP_URL`, writes `sites/<instance key>.caddy` with the instance's loopback site target, starts the proxy project when it is absent and reloads it. It refuses an absent address, a loopback address and an address that carries a port, as a domain-mode setup does. `unpublish` deletes that one site file and reloads a running project. `up` and `down` leave the site file alone. Each command prints one report of the configured site address and whether that site is published.

The console is never a proxy site in either mode. It binds `127.0.0.1` at `ADMIN_WEB_PORT` and answers its exact `ADMIN_WEB_ORIGIN`, so the operator reaches it through their own HTTPS proxy in front of that loopback port.

A domain-mode address is an HTTPS address outside loopback without an explicit port. The preflight reads the host's listening table on the configured bind address and the wildcard address, or on every address when none is configured, and a listener on port 80 or 443 that the running proxy does not hold refuses the run, because this account holds no capability that binds a port below 1024.

## The machine-wide site proxy

One Caddy container serves every published site on the host, from one Compose project in the machine-wide shared directory. `ADMIN_SHARED_ROOT/proxy/` holds the project, and a domain-mode setup creates or adopts it and starts it. `PROXY_BIND_ADDRESS` in `config/database.toml` optionally names the one IPv4 or IPv6 address the proxy binds, and an absent value lets it listen on every host address. Each site file carries the configured address in a `bind` line, and `publish` rewrites the instance's site file, so a changed value reaches the proxy at the next `publish`. [The machine-wide site proxy](/docs/operations/site-proxy.md) states the image, the mounts, the volumes, the capabilities and the reload.

## Removing a public site

```bash
scripts/self-host.sh unpublish --shared-root /srv/course-explorer
```

The command removes only this instance's site file and reloads a running proxy. Every other site serves throughout, and running `publish` again restores the site. It touches no instance container or data, so stopping the instance is a separate step:

```bash
scripts/self-host.sh down --shared-root /srv/course-explorer
```

A reload that Caddy refuses leaves the sites it already serves in service, so one bad site file never stops the other sites.

## Several instances on one server

Each checkout is one instance, and its `INSTANCE_KEY` names one university and one curriculum source. The key derives the Compose project `course-explorer-<key>`, every container name, the volume namespace and the built database image, so each instance owns its own database volume, data directories, dataset state and four ports, and two instances choose different keys and different ports. Each instance starts and stops independently through its own bootstrap, and a stopped instance retains its volume, databases, dataset pointers and ingested data. [The instance resources](/docs/operations/first-run-setup.md#the-instance-resources) states the derived names, and [the shared directory](/docs/operations/first-run-setup.md#the-shared-directory) states the one machine-wide path they share.

## Update a running copy

Run every command from the checkout root.

1. Take the updated source.

   ```bash
   git pull
   ```

2. Apply the schema migrations through the administration container.

   ```bash
   docker exec PROJECT-admin-1 python scripts/admin.py migrate selected
   docker exec PROJECT-admin-1 python -m alembic -c alembic_feedback.ini upgrade head
   ```

   The first command applies the checkout's migration head to the Selected dataset. On a bootstrap site that serves its configured database, `python scripts/admin.py migrate database` does the same to that database. The second applies only the feedback chain to the feedback application store, preserving its records. [The schema head and migration](/docs/operations/datasets.md#the-schema-head-and-migration) states the commands and their guards.

3. Build and start the updated images.

   ```bash
   scripts/self-host.sh up
   ```

   The command builds the database, embedding, site and administration images from the updated checkout and starts those services. It adds the feedback sender when the checkout's environment carries a publication token, and the worker container on a same-machine host. The image build compiles the native libraries and builds both browser apps, so `web/dist`, the administration console and the serving libraries in the image are the updated ones. The site build reads `config/wiki.json`, so an updated wiki page reaches the app after this step.

4. Restart a container whose configuration changed.

   ```bash
   docker restart PROJECT-web-1
   docker restart PROJECT-admin-1
   ```

   The site container reads the updated subsystem files and the current artifact build at its next start, and [Serving state](/docs/backend/interfaces/serving-state.md) and [Web supervisor](/docs/backend/interfaces/web-supervisor.md#startup-and-validation) state what the supervisor resolves and opens before it forks its workers. A restart is also what makes an in-place change to the served data visible to the browser. The administration container reads its own settings at its next start, and the console's own rebuild and restart actions do the same for the console.

A changed proxy file reaches its container through a start, and a changed site file reaches the running Caddy through a reload. The proxy project's Compose file is `proxy/docker-compose.yml` under the configured `ADMIN_SHARED_ROOT`, which the example file sets to `~/.local/share/course-explorer`:

```bash
docker compose --file ~/.local/share/course-explorer/proxy/docker-compose.yml up -d --wait proxy
docker compose --file ~/.local/share/course-explorer/proxy/docker-compose.yml exec -T proxy \
  caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --address unix//tmp/caddy-admin.sock
```

`scripts/self-host.sh publish --shared-root PATH` rewrites the configured instance's site file, starts the proxy project and reloads it, so it takes a changed site address into service.

`docker-compose.yml` at the checkout root and the files under `config/` are working copies, and Git ignores them, so an update to a tracked template under `examples/` reaches a running copy through the example workflow or a manual copy. A changed `WEB_APP_URL` reaches the instance's site file at the next `publish`. [Starting, stopping and updating a copy](/docs/operations/server-containers.md#starting-stopping-and-updating-a-copy) states the one Compose bridge and the single-service re-create.

## Where the reference is

- [Configuration](/docs/backend/configuration.md#subsystem-keys) states every key of the web and administration files.
- [The administration password](/docs/operations/setup-and-preflight.md#the-administration-password) states the account command and its prompts.
- [Console access and records](/docs/operations/admin-console/access-and-records.md) states sign-in, the lock and the command log.
- [Server containers](/docs/operations/server-containers.md) states the images, the mounts, the rebuilds and the update detail.
- [Machine-wide site proxy](/docs/operations/site-proxy.md) states the proxy project, its container and its reload.
