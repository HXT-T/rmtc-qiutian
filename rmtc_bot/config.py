from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    stage: int = 2
    team: str = "A"
    name: str = "秋天"
    total_ticks: int = 0  # 0 selects the published stage default
    decision_type: str = "balanced"
    contest: bool = False  # one token is bound to one observed match
    attack_damage: float = 20.0
    defended_damage: float = 10.0
    attack_range: int = 2
    resource_cap: float = 120.0
    damage_cap: float = 500.0
    resource_count: int = 16  # published per-match resource total (rules 4.2)
    resource_total: float = 200.0
    kill_reward: float = 60.0
    death_penalty: float = 60.0
    beam_width: int = 20
    beam_depth: int = 4
    # >0 lets frontier cells compete with resources/missions at their expected
    # resource value during the opening (fades to zero by Tick 150).
    explore_weight: float = 1.0  # adopted: batch-perf8-reconfirm500 (fresh distinct 24x24 maps, 500 ms, SPRT H1)
    ai_url: str = "http://127.0.0.1:8080/v1/chat/completions"
    ai_model: str = "deepseek-flash"  # handbook and gateway sample: the unified model
    # Upstream may take up to 30 s; a client timeout below the model latency still consumes quota.
    ai_timeout: float = 6.0  # candidate 20 s pending the slow-model test
    ai_max_inflight: int = 1  # candidate 2 (protocol allows 2 per token; second only for new text)
    ai_min_interval: float = 1.0
    # Participant handbook: max_tokens 512 (thinking off); recorded plans peak near 450 tokens.
    ai_max_tokens: int = 512
    # Gateway confirmed JSON mode; matches the practice proxy all real-LLM tests used.
    ai_json_mode: bool = True
    # The formal gateway forwards every field except model and does not switch thinking off;
    # DeepSeek thinks by default. All real-model tests ran with thinking disabled.
    ai_disable_thinking: bool = True
    # Model copies announce Tick / delay / duration; the program does the arithmetic.
    ai_relative_time: bool = False
    ai_max_calls: int = 0  # 0: no client cap; obey server quota
    ai_max_attempts: int = 0  # optional process-lifetime request budget, including failures
    ai_max_digest_attempts: int = 3  # bounded retries for one opaque announcement batch
    ai_max_age: int = 40

    @property
    def horizon(self):
        return self.total_ticks or (100 if self.stage == 1 else 500)

    def __post_init__(self):
        if self.stage not in (1, 2) or self.team not in "ABCD" or len(self.team) != 1:
            raise ValueError("stage must be 1/2; team must be A/B/C/D")
        if self.stage == 1 and self.team != "A":
            raise ValueError("stage 1 only permits seat A")
        if not 1 <= len(self.name.encode("utf-8")) <= 96 or any(c in self.name for c in "\r\n"):
            raise ValueError("team name must be 1..96 UTF-8 bytes, without newlines")
        if self.decision_type not in ("balanced", "collect", "combat", "survive"):
            raise ValueError("unknown decision_type")
        if not (0 <= self.total_ticks <= 100000 and 1 <= self.beam_width <= 100
                and 1 <= self.beam_depth <= 6 and 1 <= self.attack_range <= 8
                and 0 <= self.explore_weight <= 10):
            raise ValueError("invalid planner limits")
        if min(self.attack_damage, self.defended_damage, self.resource_cap, self.damage_cap,
               self.resource_count, self.resource_total) <= 0:
            raise ValueError("combat/scoring parameters must be positive")
        from .protocol import number
        if not number(self.kill_reward) or not number(self.death_penalty):
            raise ValueError("kill reward and death penalty must be finite and nonnegative")
        if not (0.1 <= self.ai_timeout <= 30 and self.ai_min_interval >= 0.2 and self.ai_max_inflight in (1, 2)
                and 128 <= self.ai_max_tokens <= 16000 and type(self.ai_json_mode) is bool and type(self.ai_disable_thinking) is bool and type(self.ai_relative_time) is bool and self.ai_max_calls >= 0
                and self.ai_max_age >= 0):
            raise ValueError("invalid AI limits")
        if (any(type(v) is not int or v < 0 for v in (self.ai_max_calls, self.ai_max_attempts))
                or type(self.ai_max_digest_attempts) is not int or not 1 <= self.ai_max_digest_attempts <= 10):
            raise ValueError("AI request budgets must be nonnegative integers; digest retries 1..10")
