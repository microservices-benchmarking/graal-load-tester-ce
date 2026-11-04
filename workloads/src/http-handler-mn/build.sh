#!/usr/bin/env bash
set -e

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 [jit|aot|aot-graalos|nib]"
  exit 1
fi

if ! command -v mvn >/dev/null 2>&1; then
    echo "Error: Maven (mvn) is not installed or not in PATH" >&2
    exit 1
fi
MVN_PATH=$(command -v mvn)
echo "Found maven at: $MVN_PATH"

CONFIG="$1"

case "$CONFIG" in
  "jit")
    mvn --quiet clean package
    ;;
  "aot")
    mvn --quiet clean package native:compile -Dmanifest.addClasspath=false -DaditionalNativeImageArg=" "
    ;;
  "aot-graalos")
    mvn --quiet clean package native:compile -Dmanifest.addClasspath=false -DaditionalNativeImageArg="-H:+UnlockExperimentalVMOptions -H:+GraalOS"
    ;;
  "nib")
    mvn --quiet clean package native:compile -Dmanifest.addClasspath=false -DaditionalNativeImageArg="--bundle-create=http-handler-mn.nib,dry-run"
    ;;
   *)
    echo "Unknown config $CONFIG."
    exit 1
esac
