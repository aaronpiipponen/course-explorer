# Build the database image with the exact semantic scan extension installed.
#
# Both stages pin the stock base by digest as well as tag, so a rebuild starts from the same
# upstream image. Compose builds the image from this file and names the result after the effective
# project and service, so each project owns its prepared image.
FROM pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff AS extension-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ make postgresql-server-dev-18 \
    && rm -rf /var/lib/apt/lists/*

COPY docker/db/extension /build
RUN make -C /build && make -C /build install

FROM pgvector/pgvector:0.8.6-pg18-trixie@sha256:78bf48b801e792f99e3ac62b5036fd3876e9be48afda16c1e331af1c75ceb2ff

# The setup readiness read consumes this capability to distinguish this prepared image from a
# base image without the extension.
LABEL org.course-explorer.db-extension="1.0"

COPY --from=extension-build /usr/lib/postgresql/18/lib/course_embedding_search.so \
    /usr/lib/postgresql/18/lib/
COPY --from=extension-build /usr/share/postgresql/18/extension/course_embedding_search.control \
    /usr/share/postgresql/18/extension/
COPY --from=extension-build \
    /usr/share/postgresql/18/extension/course_embedding_search--1.0.sql \
    /usr/share/postgresql/18/extension/
