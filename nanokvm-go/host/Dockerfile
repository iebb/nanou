FROM rust:bookworm@sha256:ade39a710ef85f3506cffcfec3e8a24ba090badef1dff2fb798f71d878833fea AS toolchain
RUN dpkg --add-architecture armhf \
    && apt-get update \
    && apt-get install -y --no-install-recommends crossbuild-essential-armhf \
       libx11-dev:armhf libxext-dev:armhf libxtst-dev:armhf libxfixes-dev:armhf \
    && rm -rf /var/lib/apt/lists/*
RUN rustup target add armv7-unknown-linux-gnueabihf
ENV CARGO_TARGET_ARMV7_UNKNOWN_LINUX_GNUEABIHF_LINKER=arm-linux-gnueabihf-gcc UU_NO_GTK=1

FROM toolchain AS compile
WORKDIR /src
COPY uu-host/ /src/
COPY compile-arm.sh /compile-arm.sh
RUN --mount=type=cache,target=/usr/local/cargo/registry sh /compile-arm.sh

FROM scratch
COPY --from=compile /output/ /
