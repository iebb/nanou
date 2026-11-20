#!/bin/sh
set -eu

project="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
upstream="$scratch/uu-host"
wheel_hash=af248a825037ef591efbf6ed20cc5faa03d3b47b9e5a2230a529eeee1c1fc3ef
revision=e9844a0dd2fa9fdafaffb07e584d024eea5ea95d
stage="$scratch/remote"

git init -q "$upstream"
git -C "$upstream" fetch --depth 1 https://github.com/VallierDev/uu-remote-linux.git "$revision"
git -C "$upstream" checkout --detach FETCH_HEAD
git -C "$upstream" apply "$project/host/nanokvm.patch"
cp "$project/host/nanokvm.rs" "$project/host/low_latency.rs" "$upstream/src/"

mkdir -p "$stage" "$scratch/wheels"
cp "$project/nanou/"*.py "$project/nanou/app.json" "$stage/"
cp "$project/host/Dockerfile" "$project/host/compile-arm.sh" "$scratch/"
printf '%s\n' 'uu-host/.git/' 'remote/' 'wheels/' > "$scratch/.dockerignore"
docker build --output "type=local,dest=$scratch/runtime" "$scratch"
cp -R "$scratch/runtime/." "$stage/"

printf 'websocket-client==1.9.0 --hash=sha256:%s\n' "$wheel_hash" > "$scratch/requirements.txt"
python3 -m pip download --no-deps --require-hashes -r "$scratch/requirements.txt" -d "$scratch/wheels"
unzip -q "$scratch/wheels/"*.whl -d "$stage/python"
(cd "$scratch" && zip -q -r NanoU-armv7.zip remote)
mv "$scratch/NanoU-armv7.zip" "$project/NanoU-armv7.zip"
