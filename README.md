# 秋天 · RMTC 参赛程序

Ubuntu 22.04 / ROS 2 Humble / Python 3.10。核心仅用 Python 标准库，ROS 端需要 `rclpy`、`std_msgs`。同一份代码覆盖两个阶段，
启动时由 `--stage` 选择分支，两阶段在第一阶段开始前一并封存（规则 §2.3）。技术说明见 [docs/技术报告.md](docs/技术报告.md)，
检录操作见 [docs/检录清单.md](docs/检录清单.md)。

## 运行环境与构建

- 系统与依赖：Ubuntu 22.04（x86_64）、ROS 2 Humble（自带 `rclpy`、`std_msgs`）、Python 3.10；不需要 pip 安装任何第三方包。
- 构建：Python 程序**无需编译**。`python3 -m rmtc_bot doctor` 确认环境可用。
- 启动目录：**仓库根目录**（克隆后的 `rmtc-qiutian/`），所有命令都在这里执行，比赛时不要运行其他目录的同名程序。

```bash
git clone https://github.com/HXT-T/rmtc-qiutian.git
cd rmtc-qiutian
```

## 目录

```text
rmtc_bot/
  __main__.py    命令行：run / replay / analyze / doctor
  ros_node.py    ROS 2 适配：收感知与公告，发动作与队名
  protocol.py    感知解析与校验，动作格式
  engine.py      每 Tick 主流程与动作打分
  planner.py     地图记忆、寻路、目标选择（束搜索）
  events.py      校验模型输出，查询事件效果
  ai.py          统一 AI 网关客户端（后台线程）
  scoring.py     v1.1 计分公式
  policies.py    四种决策类型
  audit.py       JSONL 审计日志与赛后分析
  config.py      全部可封存参数
config/          default.json（封存参数）、decision_config.json（现场唯一可切换项 decision_type）
scripts/         run_contestant.sh：正式比赛启动器（严格模式，令牌绑定本场）
tests/           单元、HTTP、适配与多 Tick 场景测试（95 项）
run.sh           练习启动脚本（默认 ROS 域 27，仅用于练习）
```

## 正式比赛启动

接入信息全部在启动时配置，无需修改代码：

| 项目 | 配置方式 |
|---|---|
| 阶段 | `--stage 1` 或 `--stage 2`，同一份代码 |
| 席位 | `--team A/B/C/D`，话题 `/team_X/...` 自动对应 |
| ROS 域 | `--domain-id 本场域编号` |
| AI 网关地址 | 环境变量 `RMTC_AI_URL`，完整地址以 `/v1/chat/completions` 结尾 |
| 本场令牌 | 环境变量 `RMTC_TEAM_TOKEN`，只在本终端隐藏输入，不写入命令行、文件或日志 |
| 决策类型 | `config/decision_config.json` 的 `decision_type` |

模型固定为 `deepseek-flash`，`max_tokens` 为 512，并关闭思考（`thinking: disabled`），均已写在程序里。

```bash
cd rmtc-qiutian
source /opt/ros/humble/setup.bash
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export RMTC_AI_URL='http://裁判通知地址:端口/v1/chat/completions'
read -rsp '本场席位令牌: ' RMTC_TEAM_TOKEN; echo
export RMTC_TEAM_TOKEN
# 不要设置 RMTC_AI_MODEL：比赛只允许 deepseek-flash，设置其他模型会拒绝启动
bash scripts/run_contestant.sh --stage 2 --team 本场席位 --domain-id 本场域编号 --name '秋天' \
  --config config/default.json --decision-config config/decision_config.json --log logs/match.jsonl
```

第一阶段（只用 A 席位，不需要 AI 网关与令牌）：

```bash
cd rmtc-qiutian
source /opt/ros/humble/setup.bash
export ROS_LOCALHOST_ONLY=0
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
bash scripts/run_contestant.sh --stage 1 --team A --domain-id 本场域编号 --name '秋天' \
  --config config/default.json --decision-config config/decision_config.json --log logs/stage1.jsonl
```

- 严格模式不默认 ROS 域；第二阶段必须提供网关和令牌。令牌绑定进程看到的第一场；换场后须带新令牌重启（日志 `token_refresh_required`）。
- 同场重启按感知位置恢复；同 Tick 重发感知只返回缓存动作，不推进策略。最终帧只读取回执和分数，不再发布动作。
- 现场只允许修改 `decision_config.json` 中的 `decision_type`（balanced / collect / combat / survive），批准后重启生效。

## 运行逻辑

每个 Tick：`ros_node` 收到本席位感知 → `protocol` 校验 → `planner` 更新地图记忆 → 新公告交给 `ai` 后台线程由大模型理解（不阻塞本 Tick），
`events` 校验模型输出 → `planner` 选目标（束搜索）→ `engine` 为攻击、防御、移动、等待打分取最高 → 回填本帧 tick 与 session_id 发布动作，`audit` 写日志。

## 自检

```bash
python3 -m rmtc_bot doctor                                   # 运行环境只读检查
python3 -m unittest discover -s tests -t . -q                # 95 项测试
python3 -m rmtc_bot replay --stage 1 --input 感知.jsonl --log logs/replay.jsonl   # 离线回放，不发 HTTP/ROS
python3 -m rmtc_bot analyze logs/match.jsonl                 # 赛后审计
```
