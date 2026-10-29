import time
from dataclasses import replace
from .events import EventBook, validate_plan
from .planner import Planner, add, manhattan
from .protocol import Frame, DIRECTIONS, OPPOSITE, action
from .scoring import marginal_value


class Engine:
    def __init__(self, config, ai=None, log=None):
        self.config, self.ai = config, ai
        self.log = log or (lambda *args, **kwargs: None)
        self.session = None
        self.retired_sessions = set()
        self.tick = -1
        self.cached = None
        self.planner = Planner(config)
        self.events = EventBook()
        self.history = {}
        self.enemy_history = {}
        self.announcements = {}  # content identity; never parse text for mechanics
        self.strategy = config.decision_type
        self.latest = None
        self.last_move_target = None
        self.last_move_tick = -1

    def _reset(self, frame):
        if self.session is not None:
            self.retired_sessions.add(self.session)
        self.session, self.tick, self.cached = frame.session, -1, None
        self.planner, self.events = Planner(self.config), EventBook()
        self.history, self.enemy_history, self.announcements = {}, {}, {}
        self.strategy = self.config.decision_type
        self.last_move_target = None
        self.last_move_tick = -1
        if self.ai:
            self.ai.reset(frame.session)
        self.log("session", session_id=frame.session, first_tick=frame.tick,
                 stage=self.config.stage, team=self.config.team)

    def add_announcement(self, text):
        if self.session and isinstance(text, str) and text.strip() and len(text) <= 65536:
            # Opaque content deduplication only. A newline is not an event parser.
            if text not in self.announcements:
                self.announcements[text] = self.tick
            while sum(len(s) for s in self.announcements) > 180000:
                del self.announcements[next(iter(self.announcements))]

    def _poll_ai(self, frame):
        if not self.ai:
            return
        for result in self.ai.poll():
            if (result["session"] != frame.session or result["generation"] != self.ai.generation):
                self.log("ai_discarded", reason="session", request_tick=result["tick"])
                continue
            self.log("ai_result", request_tick=result["tick"],
                     status=result.get("status"), http=result.get("http"),
                     code=result.get("code"), http_success=result.get("http_success", False),
                     latency_ms=result.get("latency_ms"), error_type=result.get("error_type"))
            if frame.tick - result["tick"] > self.config.ai_max_age:
                self.log("ai_discarded", reason="age", request_tick=result["tick"])
                continue
            if "plan" in result:
                try:
                    strategy, events, errors = validate_plan(
                        result["plan"], frame.width, frame.height, partial=True,
                        relative=self.config.ai_relative_time, known_ticks=set(self.announcements.values()))
                except (ValueError, TypeError, KeyError) as exc:
                    self.ai.reject(result["digest"])
                    # Our own validation message only; never model text or HTTP details.
                    self.log("ai_discarded", reason="schema", detail=str(exc)[:120], request_tick=result["tick"])
                    continue
                self.events.merge(events)
                # A sealed explicit policy remains fixed; balanced can accept AI advice.
                if self.config.decision_type == "balanced":
                    self.strategy = strategy
                if errors:
                    # Keep the valid events, but allow a bounded retry of this announcement batch.
                    self.ai.reject(result["digest"])
                    self.log("ai_partial", request_tick=result["tick"],
                             rejected=[dict(index=i, reason=m[:120]) for i, m in errors])
                else:
                    self.ai.acknowledge(result["digest"], result["tick"])
                self.log("ai_plan", request_tick=result["tick"], strategy=strategy,
                         events=[dict(kind=e.kind, start=e.start, end=e.end,
                                      rect=e.rect, point=e.point, params=e.params) for e in events])

    def step(self, raw):
        started = time.perf_counter()
        try:
            frame = Frame.parse(raw, self.config.team)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.log("perception_rejected", reason=str(exc)[:200])
            return None
        if frame.session in self.retired_sessions:
            self.log("perception_discarded", reason="retired_session", tick=frame.tick)
            return None
        if frame.session != self.session:
            self._reset(frame)
        if frame.tick < self.tick:
            return None
        if frame.tick == self.tick:
            # Pause/resume can republish the same tick. Never advance state twice.
            return dict(self.cached) if self.cached else None
        if self.latest and self.latest.session == frame.session:
            if (frame.width, frame.height) != (self.latest.width, self.latest.height):
                self.log("perception_rejected", reason="map dimensions changed within session")
                return None
        self.tick, self.latest = frame.tick, frame
        if frame.own.get("position_reason") == "initial":
            self.events.spawn = frame.position
        receipt = frame.own.get("last_action")
        if isinstance(receipt, dict):
            self.log("receipt", tick=frame.tick, last_action=receipt)
            if (receipt.get("tick") == self.last_move_tick and not receipt.get("moved", False)
                    and receipt.get("error") == "E_BLOCKED" and self.last_move_target):
                # Occupancy/temporary barriers are not permanent obstacles.
                self.planner.memory.fail_until[self.last_move_target] = frame.tick + 2
        self.history[frame.tick] = {"position": frame.position, "alive": frame.own["alive"],
                                    "moved": bool(isinstance(receipt, dict)
                                                  and receipt.get("tick") == frame.tick - 1
                                                  and receipt.get("moved") is True),
                                    "inverted": bool(isinstance(receipt, dict)
                                        and receipt.get("tick") == self.last_move_tick
                                        and receipt.get("moved") is True and self.cached
                                        and self.cached.get("direction") in OPPOSITE
                                        and receipt.get("effective_direction") == OPPOSITE[self.cached["direction"]])}
        while len(self.history) > 3000:
            del self.history[next(iter(self.history))]
        enemies = [e for e in frame.entities if e["type"] == "robot" and e["team"] != self.config.team]
        for e in enemies:
            history = self.enemy_history.setdefault(e["team"], {})
            previous = history.get(frame.tick - 1)
            history[frame.tick] = {"position": e["pos"], "alive": True,
                                   "moved": previous is not None and previous["position"] != e["pos"]}
            while len(history) > 3000:
                del history[next(iter(history))]
        self.add_announcement(frame.event_text)
        self._poll_ai(frame)
        if self.ai:
            self.ai.request(frame, self.announcements)
        self.planner.memory.update(frame, self.events)
        if not frame.own["alive"] or frame.tick >= self.config.horizon:
            result, reason = action(frame, "wait"), "dead_or_final"
        else:
            route = self.planner.route(frame, self.events, self.history, enemies)
            result, reason = self._choose(frame, route, enemies)
        self.cached = result
        if result["action"] == "move":
            effective = result["direction"]
            if self.events.inverted(frame, self.history):
                effective = OPPOSITE[effective]
            self.last_move_target = add(frame.position, DIRECTIONS[effective])
            self.last_move_tick = frame.tick
        else:
            self.last_move_target, self.last_move_tick = None, -1
        elapsed = (time.perf_counter() - started) * 1000
        self.log("decision", tick=frame.tick, session_id=frame.session,
                 position=frame.position, hp=frame.own["hp"], action=result,
                 reason=reason, planning_ms=round(elapsed, 3),
                 resource_value=frame.score.get("resource_value", 0),
                 effective_damage=frame.score.get("effective_damage", 0),
                 invalid_actions=frame.score.get("invalid_actions", 0),
                 mission_completed=frame.score.get("mission_completed", 0),
                 ai_http_successes=self.ai.successes if self.ai else 0)
        return dict(result)

    def _enemy_power(self, enemy, frame):
        fake = replace(frame, position=enemy["pos"], own={"team": enemy["team"]})
        history = self.enemy_history.get(enemy["team"], {})
        return self.events.fortress_power(fake, history)

    def _incoming(self, frame, enemies, defending=False, future_position=None):
        if frame.own.get("protected", False) and future_position is None:
            return 0.0
        p = future_position or frame.position
        total = 0.0
        for e in enemies:
            if e.get("protected", False):
                continue
            power = self._enemy_power(e, frame)
            if power:
                radius = power.params.get("attack_radius", 2)
                hittable = max(abs(e["pos"][0] - p[0]), abs(e["pos"][1] - p[1])) <= radius
                damage = power.params.get("defended_damage", 10) if defending else power.params.get("damage", 50)
            else:
                hittable = ((p[0] == e["pos"][0] or p[1] == e["pos"][1])
                            and 1 <= manhattan(p, e["pos"]) <= self.config.attack_range)
                # The nearest other robot can shield us, even if protected.
                if hittable:
                    for other in enemies:
                        if other is e:
                            continue
                        if (manhattan(e["pos"], other["pos"]) < manhattan(e["pos"], p)
                                and manhattan(e["pos"], other["pos"]) + manhattan(other["pos"], p)
                                == manhattan(e["pos"], p)):
                            hittable = False
                damage = self.config.defended_damage if defending else self.config.attack_damage
            if hittable:
                # Last defending flag is evidence of a tendency, never a guaranteed action.
                total += damage * (0.5 if e.get("defending", False) else 0.85)
        return total * self.events.vulnerable(frame.tick, self.history)

    def _targets(self, frame, enemies, direction, power):
        if power:
            radius = power.params.get("attack_radius", 2)
            return [e for e in enemies if max(abs(e["pos"][0] - frame.position[0]),
                                              abs(e["pos"][1] - frame.position[1])) <= radius]
        dx, dy = DIRECTIONS[direction]
        for distance in range(1, self.config.attack_range + 1):
            p = frame.position[0] + dx * distance, frame.position[1] + dy * distance
            targets = [e for e in enemies if e["pos"] == p]
            if targets:
                return targets[:1]
        return []  # ordinary attacks ignore obstacles

    def _choose(self, frame, route, enemies):
        c = self.config
        if c.stage == 1:
            return (action(frame, "move", route.direction), route.reason) if route.direction else (action(frame, "wait"), route.reason)
        # Start-of-tick forced respawn precedes all actions.
        for e in self.events.active("split", frame.tick):
            if e.start == frame.tick and (frame.position[0] == e.point[0] or frame.position[1] == e.point[1]):
                if frame.position != self.events.spawn:
                    return action(frame, "wait"), "event_forced_respawn"
        unavoidable = self.events.hazard(frame.position, frame.tick, frame.own.get("protected", False))
        hp = frame.own["hp"] - unavoidable
        if hp <= 0:
            return action(frame, "wait"), "event_death"
        power = self.events.fortress_power(frame, self.history)
        reflected = bool(self.events.active("reflection", frame.tick))
        incoming = self._incoming(frame, enemies)
        defended = self._incoming(frame, enemies, True)
        survival_weight = {"balanced": 0.07, "collect": 0.10, "combat": 0.035, "survive": 0.18}[self.strategy]
        current_damage = frame.score.get("effective_damage", 0)
        pending_missions = any(m["id"] not in frame.score.get("mission_completed_ids", []) for m in frame.missions)
        # No global scoreboard is available to the policy. When known collection
        # and mission work is exhausted, damage still matters for the tiebreak.
        tiebreak_weight = 0.05 if not self.planner.memory.resources and not pending_missions else 0.009
        def damage_reward(damage):
            reward = marginal_value(current_damage, damage, cap=c.damage_cap,
                death_penalty=c.death_penalty, tie_weight=tiebreak_weight)
            return reward * (1.6 if self.strategy == "combat" else 1.0)
        def survival_loss(damage, own_gain=0):
            # Approximate danger from public observations. v1.1 death also debits
            # adjusted damage AFTER this tick's own damage and kill rewards.
            debit = -marginal_value(current_damage + own_gain, deaths=1,
                cap=c.damage_cap, death_penalty=c.death_penalty, tie_weight=tiebreak_weight)
            return damage * survival_weight + (5.0 + debit if damage >= hp else 0.0)
        def kill_share(target):
            # Other visible robots can share a kill; their next actions are unknown.
            # Count plausible ordinary contributors conservatively, not as truth.
            others = sum(1 for e in enemies if e is not target and not e.get("protected", False)
                and (e["pos"][0] == target["pos"][0] or e["pos"][1] == target["pos"][1])
                and 1 <= manhattan(e["pos"], target["pos"]) <= c.attack_range)
            return c.kill_reward / (1 + others)
        def fire_risk(position, moving=False):
            if frame.own.get("protected", False) and not moving:
                return 0.0
            risk = 0.0
            for event in self.events.active("chili", frame.tick):
                if position != event.point and (position[0] == event.point[0] or position[1] == event.point[1]):
                    if any(manhattan(e["pos"], event.point) == 1 for e in enemies):
                        risk += event.params.get("damage", 50) * 0.4
            return risk
        stationary_fire = fire_risk(frame.position)
        options = [(0.0 - survival_loss(incoming + stationary_fire), "wait", None, "idle")]
        if defended < incoming:
            reflect_gain = 0.0
            if reflected and not frame.own.get("protected", False):
                amount = max(e.params.get("reflection_damage", 20)
                             for e in self.events.active("reflection", frame.tick))
                for target in enemies:
                    if target["hp"] <= amount and not target.get("protected", False):
                        enemy_power = self._enemy_power(target, frame)
                        distance = manhattan(target["pos"], frame.position)
                        if enemy_power:
                            can_hit = max(abs(target["pos"][0]-frame.position[0]),
                                abs(target["pos"][1]-frame.position[1])) <= enemy_power.params.get("attack_radius", 2)
                        else:
                            can_hit = ((target["pos"][0] == frame.position[0] or target["pos"][1] == frame.position[1])
                                and 1 <= distance <= c.attack_range
                                and not any(other is not target
                                    and manhattan(target["pos"], other["pos"]) < distance
                                    and manhattan(target["pos"], other["pos"]) + manhattan(other["pos"], frame.position) == distance
                                    for other in enemies))
                        if can_hit:
                            reflect_gain += kill_share(target) * (0.5 if target.get("defending", False) else 0.85)
            options.append((damage_reward(reflect_gain) - 0.15
                - survival_loss(defended + stationary_fire, reflect_gain), "defend", None, "reduce_incoming"))
        if route.reason == "fortress" and not route.direction:
            options.append((1.2 - survival_loss(defended), "defend", None, "fortress_dwell"))
        if not frame.own.get("protected", False):
            for direction in DIRECTIONS:
                targets = self._targets(frame, enemies, direction, power)
                damage, backlash, awards = 0.0, 0.0, 0.0
                for target in targets:
                    if target.get("protected", False):
                        continue
                    defense_prob = 0.75 if target.get("defending", False) else 0.2
                    full = power.params.get("damage", 50) if power else c.attack_damage
                    reduced = power.params.get("defended_damage", 10) if power else c.defended_damage
                    target_factor = self.events.vulnerable(frame.tick, self.enemy_history.get(target["team"], {}))
                    full *= target_factor
                    reduced *= target_factor
                    if reflected:
                        reflect_damage = max(e.params.get("reflection_damage", 20)
                                             for e in self.events.active("reflection", frame.tick))
                        backlash += reflect_damage * defense_prob
                        reduced = 0.0
                    # Branch before HP clamping, so overkill isn't scored twice.
                    damage += ((1 - defense_prob) * min(target["hp"], full)
                               + defense_prob * min(target["hp"], reduced))
                    lethal = ((1 - defense_prob) * (full >= target["hp"])
                              + defense_prob * (reduced >= target["hp"]))
                    awards += kill_share(target) * lethal
                if damage:
                    gain = damage + awards
                    reward = damage_reward(gain) - survival_loss(incoming + backlash + stationary_fire, gain)
                    options.append((reward, "attack", direction, "damage_opportunity"))
        occupied = {e["pos"] for e in enemies}
        inverted = self.events.inverted(frame, self.history)
        for effective, delta in DIRECTIONS.items():
            p = add(frame.position, delta)
            if (not (0 <= p[0] < frame.width and 0 <= p[1] < frame.height)
                    or p in self.planner.memory.obstacles or p in occupied
                    or p in self.planner.memory.fail_until or self.events.blocked(p, frame.tick)):
                continue
            sent = OPPOSITE[effective] if inverted else effective
            follows_route = sent == route.direction
            reward = max(0.35, min(3.5, route.utility)) if follows_route else -0.25
            if route.goal and not follows_route:
                reward += (manhattan(frame.position, route.goal) - manhattan(p, route.goal)) * 0.15
            if p in self.planner.memory.resources:
                value = self.planner.memory.resources[p]["value"]
                reward += self.planner.resource_gain(frame, value)
            for event in self.events.active("chili", frame.tick):
                if p == event.point and p != frame.position:
                    hurt = sum(min(e["hp"], event.params.get("damage", 50))
                               for e in enemies if not e.get("protected", False)
                               and (e["pos"][0] == p[0] or e["pos"][1] == p[1]) and e["pos"] != p)
                    # Fire HP loss is not ordinary attack contribution. Kill awards
                    # are possible; postmove positions/actions remain hypotheses.
                    reward += min(2.0, hurt * 0.02)
                    reward += damage_reward(sum(kill_share(e) * 0.5 for e in enemies
                        if not e.get("protected", False) and e["hp"] <= event.params.get("damage", 50)
                        and (e["pos"][0] == p[0] or e["pos"][1] == p[1]) and e["pos"] != p))
            # Current attacks resolve before movement. Only next-tick threat differs.
            future_risk = self._incoming(frame, enemies, future_position=p) * survival_weight * 0.45
            future_hazard = self.events.hazard(p, frame.tick + 1, False)
            if future_hazard:
                reward -= future_hazard * 0.2 + (6 if future_hazard >= hp else 0)
            # Preannounced imminent explosion: prioritize leaving its region in time.
            current_future = self.events.hazard(frame.position, frame.tick + 1, False)
            if current_future and not future_hazard:
                reward += 8.0
            reward -= survival_loss(incoming + fire_risk(p, True)) + future_risk
            for event in self.events.active("vulnerability", frame.tick):
                if (frame.tick < event.params.get("trigger_end", min(event.start + 20, event.end))
                        and self.events.vulnerable(frame.tick, self.history) == 1):
                    reward -= future_risk * max(0, event.params.get("factor", 2) - 1)
            reason = route.reason if follows_route else "tactical_reposition"
            options.append((reward, "move", sent, reason))
        _, kind, direction, reason = max(options, key=lambda choice: choice[0])
        return action(frame, kind, direction), reason
