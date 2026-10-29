#!/usr/bin/env bash
# Build the contestant image and play one practice stage-2 match from the container.
# Usage (Ubuntu with Docker and ROS 2 Humble, official practice package unpacked):
#   bash scripts/docker_smoke.sh /path/to/rmtc_practice
# The container uses host networking; the referee and its three reference bots run on the host.
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
PRACTICE=${1:?path to the official rmtc_practice folder}
DOMAIN=${DOMAIN:-150}
PORT=${PORT:-19800}
OUT=$(mktemp -d)

# Host network + proxy build args so apt inside the build can use a local proxy (e.g. 127.0.0.1:7890);
# image pulls themselves need the Docker daemon's own proxy setting.
docker build --network host ${HTTP_PROXY:+--build-arg HTTP_PROXY=$HTTP_PROXY} \
  ${HTTPS_PROXY:+--build-arg HTTPS_PROXY=$HTTPS_PROXY} -t rmtc-bot:final .
docker run --rm --entrypoint bash rmtc-bot:final -lc \
  'source /opt/ros/humble/setup.bash && python3 -m rmtc_bot doctor'

export ROS_DOMAIN_ID=$DOMAIN ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
(cd "$PRACTICE" && bash start_referee.sh --stage 2 --bots B,C,D --port "$PORT" --data-dir "$OUT/referee" \
   > "$OUT/referee.log" 2>&1) &
REFEREE=$!
# Keep the script's own exit status: the cleanup must not turn a failed check into success.
trap 'rc=$?; kill -INT $REFEREE 2>/dev/null || true; docker rm -f rmtc-smoke >/dev/null 2>&1 || true; exit $rc' EXIT
until grep -q '已就绪' "$OUT/referee.log" 2>/dev/null; do sleep 1; done

# Same machine as the referee: Fast DDS uses shared memory between local participants, so the
# container shares the host IPC namespace and runs as the host user (root cannot open its segments).
docker run -d --name rmtc-smoke --network host --ipc host --user "$(id -u):$(id -g)" -e HOME=/tmp -e ROS_LOG_DIR=/tmp/roslog \
  -e ROS_LOCALHOST_ONLY=0 -e RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
  -e RMTC_AI_URL="http://127.0.0.1:$PORT/v1/chat/completions" -e RMTC_TEAM_TOKEN=practice-A \
  rmtc-bot:final --stage 2 --team A --domain-id "$DOMAIN" --name smoke --log /tmp/match.jsonl
sleep 8
# Same request as the page's 开始 / 继续 button; repeat until the referee is ticking.
until python3 -c "import json,urllib.request as u,sys; sys.exit(json.load(u.urlopen('http://127.0.0.1:$PORT/api/state'))['status'].get('tick',0)<1)" 2>/dev/null; do
  curl -s -X POST -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:$PORT/api/resume" >/dev/null || true
  sleep 3
done
until ls "$OUT"/referee/practice-*/score.json >/dev/null 2>&1 && \
      python3 -c "import json,glob,sys; sys.exit(json.load(open(glob.glob('$OUT/referee/practice-*/score.json')[0]))['tick']!=500)"; do
  sleep 5
done
docker cp rmtc-smoke:/tmp/match.jsonl "$OUT/A.jsonl"
python3 - "$OUT/A.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding='utf-8') if l.strip()]
dec = {r['tick']: r['action'].get('action') for r in rows if r['kind'] == 'decision' and r['tick'] < 500}
rec = {r['last_action']['tick'] for r in rows if r['kind'] == 'receipt' and isinstance(r.get('last_action'), dict)}
ai = sum(1 for r in rows if r['kind'] == 'ai_result' and r.get('http_success'))
print(f'decisions {len(dec)}, matched receipts {len(rec & set(dec))}, AI successes {ai}')
sys.exit(0 if len(rec & set(dec)) >= 475 and ai >= 1 else 1)
EOF
echo "Docker smoke test passed; logs in $OUT"
