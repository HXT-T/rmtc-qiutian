#!/usr/bin/env bash
set -eo pipefail
cd -- "$(dirname -- "$0")/.."
source /opt/ros/humble/setup.bash
exec python3 -m rmtc_bot run --contest "$@"
