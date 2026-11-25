#!/bin/sh
set -eu

project="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
case "$#:${1:-}:${2:-}" in
    0::) target=armv7; package_dir=remote; archive=NanoU-armv7.zip ;;
    2:--linux:amd64|2:--linux:arm64)
        target=$2; package_dir=NanoU-linux-$target; archive=$package_dir.tar.gz ;;
    *) echo "Usage: $0 [--linux amd64|arm64]" >&2; exit 2 ;;
esac

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
upstream="$scratch/uu-host"
wheel_hash=af248a825037ef591efbf6ed20cc5faa03d3b47b9e5a2230a529eeee1c1fc3ef
revision=e9844a0dd2fa9fdafaffb07e584d024eea5ea95d
stage="$scratch/stage/$package_dir"

git init -q "$upstream"
git -C "$upstream" fetch --depth 1 https://github.com/VallierDev/uu-remote-linux.git "$revision"
git -C "$upstream" checkout --detach FETCH_HEAD
git -C "$upstream" apply "$project/host/nanokvm.patch"
cp "$project/host/nanokvm.rs" "$project/host/low_latency.rs" "$project/host/h264_dimensions.rs" "$upstream/src/"

mkdir -p "$stage" "$scratch/wheels"
cp "$project/nanou/audio.py" "$project/nanou/bridge.py" "$project/nanou/runtime.py" \
   "$project/nanou/settings.py" "$stage/"
if [ "$target" = armv7 ]; then
    cp "$project/nanou/main.py" "$project/nanou/boot.py" "$project/nanou/app.json" "$stage/"
else
    cp "$project/host/bridge_cli.py" "$stage/nanou-bridge"
    chmod 755 "$stage/nanou-bridge"
fi
cp "$project/host/Dockerfile" "$project/host/compile-host.sh" "$project/host/target.sh" "$scratch/"
printf '%s\n' 'uu-host/.git/' 'stage/' 'wheels/' > "$scratch/.dockerignore"
docker build --build-arg "NANOU_TARGET=$target" --output "type=local,dest=$scratch/runtime" "$scratch"
cp -R "$scratch/runtime/." "$stage/"
cp "$project/../LICENSE" "$stage/"

printf 'websocket-client==1.9.0 --hash=sha256:%s\n' "$wheel_hash" > "$scratch/requirements.txt"
python3 -m pip download --no-deps --require-hashes -r "$scratch/requirements.txt" -d "$scratch/wheels"
unzip -q "$scratch/wheels/"*.whl -d "$stage/python"
if [ "$target" = armv7 ]; then
    (cd "$scratch/stage" && zip -q -r "$scratch/$archive" "$package_dir")
else
    tar -czf "$scratch/$archive" -C "$scratch/stage" "$package_dir"
fi
mv "$scratch/$archive" "$project/$archive"
