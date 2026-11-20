#!/bin/sh
set -eu

NANOU_TARGET=$1
. /target.sh
linker_var="CARGO_TARGET_$(printf '%s' "$rust_target" | tr '[:lower:]-' '[:upper:]_')_LINKER"
env "$linker_var=$triple-gcc" cargo build --locked --release --target "$rust_target" --bin uuremote-server
mkdir -p /output/bin /output/lib /output/licenses
"$triple-strip" -o /output/bin/uuremote-server "target/$rust_target/release/uuremote-server"

for library in "$loader" libc.so.6 libm.so.6 libgcc_s.so.1 \
    libX11.so.6 libXext.so.6 libXtst.so.6 libXfixes.so.3 libXi.so.6 \
    libxcb.so.1 libXau.so.6 libXdmcp.so.6 libbsd.so.0 libmd.so.0
do
    cp -L "/usr/lib/$triple/$library" /output/lib/
done
for package in libc6 libgcc-s1 libx11-6 libxext6 libxtst6 libxfixes3 \
    libxi6 libxcb1 libxau6 libxdmcp6 libbsd0 libmd0
do
    cp -L "/usr/share/doc/$package/copyright" "/output/licenses/debian-$package.txt"
done
cp LICENSE /output/licenses/uu-remote-linux.txt
