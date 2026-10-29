FROM ros:humble-ros-base-jammy
RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-humble-rclpy ros-humble-std-msgs ros-humble-rmw-fastrtps-cpp \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Exactly the sealed contestant: code, sealed configuration and the strict contest launcher.
COPY rmtc_bot /app/rmtc_bot
COPY config /app/config
COPY scripts /app/scripts
COPY run.sh setup.py setup.cfg package.xml /app/
COPY resource /app/resource
# Formal entry: strict mode (explicit ROS domain, token bound to one match). Arguments come from
# the on-site notice, e.g. --stage 2 --team B --domain-id 31 --name 秋天 --log /tmp/match.jsonl
ENTRYPOINT ["bash", "/app/scripts/run_contestant.sh", "--config", "/app/config/default.json", \
            "--decision-config", "/app/config/decision_config.json"]
