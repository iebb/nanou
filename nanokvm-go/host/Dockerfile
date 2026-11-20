FROM rust:bookworm@sha256:ade39a710ef85f3506cffcfec3e8a24ba090badef1dff2fb798f71d878833fea AS toolchain
ARG NANOU_TARGET=armv7
COPY target.sh /target.sh
RUN set -eu; . /target.sh; \
    set --; \
    if [ "$(dpkg --print-architecture)" != "$deb_arch" ]; then \
        dpkg --add-architecture "$deb_arch"; \
        set -- "crossbuild-essential-$deb_arch"; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends "$@" \
       "libx11-dev:$deb_arch" "libxext-dev:$deb_arch" "libxtst-dev:$deb_arch" "libxfixes-dev:$deb_arch"; \
    rm -rf /var/lib/apt/lists/*; \
    rustup target add "$rust_target"
ENV UU_NO_GTK=1

FROM toolchain AS compile
WORKDIR /src
COPY uu-host/ /src/
COPY compile-host.sh /compile-host.sh
RUN --mount=type=cache,target=/usr/local/cargo/registry sh /compile-host.sh "$NANOU_TARGET"

FROM scratch
COPY --from=compile /output/ /
