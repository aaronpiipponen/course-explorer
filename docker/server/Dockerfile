# Build the site, administration and same-machine worker containers as images. Each target starts
# from the locked Python environment built here and adds only the packages, tools and built frontend
# it runs, so the site image carries no administration tooling. Configuration, runtime state, the
# shared directory and the checkout arrive as mounts.

FROM python:3.14-slim-trixie AS python-runtime

# The three native serving libraries link libstdc++, the prerequisite library links libpq as well,
# and the settings loaders read certificates through the system store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /usr/local/bin/

# The shared caches map an anonymous memfd, so the interpreter must expose that capability before
# any stage depends on it.
RUN python -c 'import os; os.close(os.memfd_create("check", os.MFD_CLOEXEC))'

WORKDIR /app
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    HOME=/home/course

# The account the containers run as has no home directory of its own, so the directory exists and
# every uid may write it as `HOME` at run time.
RUN mkdir -p /home/course && chmod 1777 /home/course

FROM python-runtime AS python-dependencies

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project --no-managed-python --python /usr/local/bin/python3

ENV PATH=/app/.venv/bin:$PATH

FROM python-dependencies AS worker

# The same-machine worker runs the worker-side helpers on this checkout's own image. It carries the
# Docker CLI and Compose client the Compose bridge and the generated service start need, git for the
# checkout reads, the runtime libraries a host-built llama.cpp binary links, and util-linux for the
# namespace entry the fixed same-machine restart runs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg git iproute2 util-linux libdrm-amdgpu1 libdrm2 libelf1 libgomp1 libnuma1 \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/app/src \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY universities.toml ./
COPY src/ ./src/
COPY examples/ ./examples/

CMD ["sleep", "infinity"]

FROM python-runtime AS native-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ make libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY native /app/native
# Debian keeps the libpq headers under /usr/include/postgresql, and the prerequisite target
# includes <libpq-fe.h> by its own name.
ENV CPLUS_INCLUDE_PATH=/usr/include/postgresql
RUN make -C /app/native clean && make -C /app/native all

FROM node:22.22.2-slim AS web-build

WORKDIR /app
COPY web/package.json web/package-lock.json web/
RUN npm --prefix web ci --no-audit --no-fund
# The wiki plugin resolves the repository root three levels above `web/tools/wiki` and reads the
# checkout's own page selection, so the build carries `docs/` and `config/wiki.json`.
COPY web/ web/
COPY docs/ docs/
COPY config/wiki.json config/wiki.json
RUN npm --prefix web run build

FROM node:22.22.2-slim AS admin-build

WORKDIR /app
COPY admin-web/package.json admin-web/package-lock.json admin-web/
RUN npm --prefix admin-web ci --no-audit --no-fund
COPY admin-web/ admin-web/
RUN npm --prefix admin-web run build

FROM python-dependencies AS site

# The feedback service reads the feedback migration chain on the first submission it accepts.
COPY universities.toml alembic_feedback.ini ./
COPY migrations_feedback/ ./migrations_feedback/
COPY src/course_explorer/ ./src/course_explorer/
COPY src/course_explorer_web/ ./src/course_explorer_web/
COPY --from=native-build /app/native/*.so /app/native/
COPY --from=web-build /app/web/dist /app/web/dist

CMD ["python", "-m", "course_explorer_web.launch"]

FROM python-dependencies AS admin

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg git openssh-client \
    && rm -rf /var/lib/apt/lists/*

# The Compose client and the GitHub CLI come from their publishers' own package repositories for
# this image's Debian suite, each signed by its published keyring.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin docker-compose-plugin gh \
    && rm -rf /var/lib/apt/lists/*

# The guide importers launch a headless Chromium, so the browser lives at a fixed path outside the
# project environment and every uid may read it.
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright /app/.venv/bin/playwright install --with-deps --only-shell chromium \
    && chmod -R a+rX /opt/ms-playwright

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# The whole package tree is copied, so the image carries every package this checkout implements and
# its `scripts/` adapters run against the mounted configuration and state.
COPY universities.toml pyproject.toml alembic.ini alembic_feedback.ini ./
COPY migrations/ ./migrations/
COPY migrations_feedback/ ./migrations_feedback/
COPY examples/ ./examples/
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY --from=native-build /app/native/*.so /app/native/
COPY --from=admin-build /app/admin-web/dist /app/admin-web/dist

CMD ["python", "-m", "course_explorer_admin_web"]
