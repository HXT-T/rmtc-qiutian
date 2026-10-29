# 秋天 · RMTC 参赛程序

Ubuntu 22.04 / ROS 2 Humble / Python 3.10。核心仅用 Python 标准库，ROS 端需要 `rclpy`、`std_msgs`。同一份代码覆盖两个阶段，
启动时由 `--stage` 选择分支，两阶段在第一阶段开始前一并封存（规则 §2.3）。技术说明见 [docs/技术报告.md](docs/技术报告.md)，
检录操作见 [docs/检录清单.md](docs/检录清单.md)，入门讲解见 [docs/新手讲解.md](docs/新手讲解.md)。

## 目录

```text
rmtc_bot/        策略、协议解析、AI 客户端、ROS 适配、审计日志与命令行
config/          default.json（封存参数）、decision_config.json（现场唯一可切换项 decision_type）
scripts/         run_contestant.sh：正式比赛启动器（严格模式，令牌绑定本场）
tests/           单元、HTTP、适配与多 Tick 场景测试（93 项）
run.sh           练习启动脚本（默认 ROS 域 27，仅用于练习）
Dockerfile       镜像配方（仅参考，本次不提交镜像）
```

## 正式比赛启动

席位、ROS 域编号、AI 网关地址、令牌、模型均使用现场通知值。令牌只在本终端隐藏输入，不写入命令行、文件或日志。

```bash
cd contestant
source /opt/ros/humble/setup.bash
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_AI_URL='http://裁判通知地址:端口/v1/chat/completions'
read -rsp '本场席位令牌: ' RMTC_TEAM_TOKEN; echo
export RMTC_TEAM_TOKEN
# 不要设置 RMTC_AI_MODEL：比赛只允许统一默认模型（请求不带 model 字段，设置了会拒绝启动）
bash scripts/run_contestant.sh --stage 2 --team 本场席位 --domain-id 本场域编号 --name '秋天' \
  --config config/default.json --decision-config config/decision_config.json --log logs/match.jsonl
```

第一阶段：`--stage 1 --team A`，不需要 AI 网关与令牌。

- 严格模式不默认 ROS 域；第二阶段必须提供网关和令牌。令牌绑定进程看到的第一场；换场后须带新令牌重启（日志 `token_refresh_required`）。
- 同场重启按感知位置恢复；同 Tick 重发感知只返回缓存动作，不推进策略。最终帧只读取回执和分数，不再发布动作。
- 现场只允许修改 `decision_config.json` 中的 `decision_type`（balanced / collect / combat / survive），批准后重启生效。

## 自检

```bash
python3 -m rmtc_bot doctor                                   # 运行环境只读检查
python3 -m unittest discover -s tests -t . -q                # 93 项测试
python3 -m rmtc_bot replay --stage 1 --input 感知.jsonl --log logs/replay.jsonl   # 离线回放，不发 HTTP/ROS
python3 -m rmtc_bot analyze logs/match.jsonl                 # 赛后审计
```

## 提交方式

按规则 v1.2 提交代码仓库（本目录）与语雀技术报告，不提交镜像。目录中的 `Dockerfile` 与 `scripts/docker_smoke.sh` 仅为参考，未用于提交；若日后需要容器，同机联调时须加 `--ipc host --user "$(id -u):$(id -g)"`（Fast DDS 共享内存）。
