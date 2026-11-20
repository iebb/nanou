#!/bin/sh
# Sourced by the toolchain and build stages.
case "$NANOU_TARGET" in
    armv7)
        deb_arch=armhf
        triple=arm-linux-gnueabihf
        rust_target=armv7-unknown-linux-gnueabihf
        loader=ld-linux-armhf.so.3
        ;;
    arm64)
        deb_arch=arm64
        triple=aarch64-linux-gnu
        rust_target=aarch64-unknown-linux-gnu
        loader=ld-linux-aarch64.so.1
        ;;
    amd64)
        deb_arch=amd64
        triple=x86_64-linux-gnu
        rust_target=x86_64-unknown-linux-gnu
        loader=ld-linux-x86-64.so.2
        ;;
    *) echo "Unsupported NanoU build target: $NANOU_TARGET" >&2; exit 2 ;;
esac
