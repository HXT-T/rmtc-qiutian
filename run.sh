#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -f /opt/ros/humble/setup.bash ]]; then
  echo "ROS 2 Humble not found. Run on Ubuntu 22.04 with ROS 2 Humble." >&2
  exit 2
fi
set +u
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-27}"
export ROS_LOCALHOST_ONLY="${ROS_LOCALHOST_ONLY:-0}"
export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"
exec python3 -m rmtc_bot run "$@"
