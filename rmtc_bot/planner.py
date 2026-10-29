"""Receding-horizon reward routing on memory built only from public frames."""
import heapq
import math
from collections import Counter
from dataclasses import dataclass
from .protocol import DIRECTIONS, OPPOSITE


def add(a, b):
    return a[0] + b[0], a[1] + b[1]


def manhattan(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


class WorldMemory:
    def __init__(self):
        self.seen = set()
        self.observed_at = {}
        self.enemy_sightings = {}
        self.obstacles = set()
        self.obstacle_first_seen = {}
        self.obstacle_last_seen = {}
        self.resources = {}
        self.visits = Counter()
        self.fail_until = {}
        self.collected = Counter()
        self.last_score = 0.0
        self.last_tick = -1
        self.distance_cache = (None, {})  # terrain key -> per-source Dijkstra maps

    def update(self, frame, events=None):
        if events:
            for event in events.events.values():
                if event.kind == "split" and frame.tick >= event.end:
                    # A line first observed during this temporary event is no longer
                    # known to be blocked after expiry. Reobserve before asserting it.
                    for p in list(self.obstacles):
                        if ((p[0] == event.point[0] or p[1] == event.point[1])
                                and event.start <= self.obstacle_first_seen.get(p, -1) < event.end
                                and self.obstacle_last_seen.get(p, event.end) < event.end):
                            self.obstacles.discard(p)
                            self.obstacle_first_seen.pop(p, None)
                            self.obstacle_last_seen.pop(p, None)
        if not frame.own["alive"]:
            return
        current_value = frame.score.get("resource_value", 0)
        # Subtype accounting is only inferred when the previously observed resource
        # matches both current location and this frame's actual score increase.
        previous = self.resources.get(frame.position)
        if previous and self.last_tick == frame.tick - 1 and current_value > self.last_score:
            if abs(current_value - self.last_score - previous["value"]) < 1e-6:
                self.collected["high_resources" if previous["value"] >= 20 else "normal_resources"] += 1
        self.last_score, self.last_tick = current_value, frame.tick
        px, py = frame.position
        visible = {(x, y) for y in range(max(0, py - frame.radius), min(frame.height, py + frame.radius + 1))
                   for x in range(max(0, px - frame.radius), min(frame.width, px + frame.radius + 1))}
        self.seen.update(visible)
        self.observed_at.update({p: frame.tick for p in visible})
        robots = {e["team"]: e for e in frame.entities if e["type"] == "robot"}
        # Last positions are investigation hypotheses, never current combat truth.
        self.enemy_sightings = {
            team: sighting for team, sighting in self.enemy_sightings.items()
            if frame.tick - sighting["tick"] <= 30
            and not (sighting["pos"] in visible and team not in robots)}
        for team, robot in robots.items():
            self.enemy_sightings[team] = {"pos": robot["pos"], "tick": frame.tick}
        present_obstacles = {e["pos"] for e in frame.entities if e["type"] == "obstacle"}
        for p in visible - present_obstacles:
            self.obstacles.discard(p)
            self.obstacle_first_seen.pop(p, None)
            self.obstacle_last_seen.pop(p, None)
        for p in visible:
            self.resources.pop(p, None)
        for entity in frame.entities:
            if entity["type"] == "obstacle":
                self.obstacles.add(entity["pos"])
                self.obstacle_first_seen.setdefault(entity["pos"], frame.tick)
                self.obstacle_last_seen[entity["pos"]] = frame.tick
            elif entity["type"].startswith("resource_"):
                self.resources[entity["pos"]] = dict(entity, last_seen=frame.tick)
        self.visits[frame.position] += 1
        self.fail_until = {p: until for p, until in self.fail_until.items() if until > frame.tick}


@dataclass
class Route:
    direction: str = ""
    goal: tuple = ()
    reason: str = "wait"
    utility: float = 0.0
    path: tuple = ()


class Planner:
    def __init__(self, config):
        self.config = config
        self.memory = WorldMemory()
        self.patrol_goal = None

    def resource_gain(self, frame, value, planned_value=0):
        if self.config.stage == 1:
            return value * 0.2
        left = max(0, self.config.resource_cap - frame.score.get("resource_value", 0) - planned_value)
        scored = min(value, left)
        # A small residual reflects the resource tiebreak, not uncapped points.
        return scored * 0.25 + (value - scored) * 0.012

    def distances(self, start, frame, events, occupied=(), avoid_robots=False):
        """Dijkstra: unknown cells are traversable hypotheses with a small penalty."""
        memory = self.memory
        # Only split lines block and only explosions/splits add hazard; skip the
        # per-edge event scan when no such event is known (identical result).
        timed = any(e.kind in ("split", "explosion") for e in events.events.values())
        key = None
        if not timed and not occupied and not avoid_robots:
            # Without timed events the map depends only on remembered terrain.
            # `seen` only grows within one memory, so its size identifies it.
            key = (frame.width, frame.height, frozenset(memory.obstacles), len(memory.seen),
                   frozenset(memory.fail_until))
            if memory.distance_cache[0] != key:
                memory.distance_cache = (key, {})
            hit = memory.distance_cache[1].get(start)
            if hit:
                return hit
        costs, steps, parent = {start: 0.0}, {start: 0}, {}
        heap = [(0.0, start)]
        # Hot loop: neighbours are precomputed per map size in DIRECTIONS order and lookups
        # are bound locally. Arithmetic and push order match the plain version exactly.
        neighbours = self._neighbours(frame.width, frame.height)
        obstacles, seen, fail_until = memory.obstacles, memory.seen, memory.fail_until
        pop, push, best, inf, tick = heapq.heappop, heapq.heappush, costs.get, math.inf, frame.tick
        while heap:
            cost, p = pop(heap)
            if cost > costs[p] + 1e-9:
                continue
            distance = steps[p] + 1
            for q in neighbours[p]:
                # Planning hypotheses use a time estimate, not hidden world state.
                if q in obstacles or (timed and events.blocked(q, tick + distance - 1)):
                    continue
                if p == start and q in fail_until:
                    continue
                if avoid_robots and q in occupied:
                    continue
                extra = 0.15 if q not in seen else 0.0
                if timed:
                    extra += events.hazard(q, tick + distance, False) * 0.12
                extra += 5.0 if q in occupied else 0.0
                candidate = cost + 1.0 + extra
                if candidate + 1e-9 < best(q, inf):
                    costs[q], steps[q], parent[q] = candidate, distance, p
                    push(heap, (candidate, q))
        if key is not None:
            memory.distance_cache[1][start] = costs, steps, parent
        return costs, steps, parent

    def _neighbours(self, width, height):
        if getattr(self, "_neighbour_size", None) != (width, height):
            self._neighbour_size = (width, height)
            self._neighbour_table = {
                (x, y): [q for q in (add((x, y), d) for d in DIRECTIONS.values())
                         if 0 <= q[0] < width and 0 <= q[1] < height]
                for y in range(height) for x in range(width)}
        return self._neighbour_table

    def _mission_bonus(self, frame, value, counts):
        done = set(frame.score.get("mission_completed_ids", []))
        bonus = 0.0
        kind = "high_resources" if value >= 20 else "normal_resources"
        for mission in frame.missions:
            if mission["id"] in done:
                continue
            if mission["type"] == kind:
                left = max(1, mission["target"] - self.memory.collected[kind])
                if counts[kind] <= left:
                    bonus += 5.0 / left
            elif mission["type"] == "resource_value":
                left = max(0, mission["target"] - frame.score.get("resource_value", 0))
                if left:
                    before = max(0, counts["planned_value"] - value)
                    bonus += 5 * min(value, max(0, left - before)) / left
        return bonus

    def route(self, frame, events, history, enemies):
        c, memory = self.config, self.memory
        occupied = {e["pos"] for e in enemies}
        costs, steps, parent = self.distances(frame.position, frame, events, occupied, True)
        remaining = max(0, c.horizon - frame.tick)
        if not remaining:
            return Route()
        if c.stage == 2:
            # Escape pre-move hazards before activation, allowing route slack.
            for event in sorted(events.events.values(), key=lambda e: e.start):
                if event.start <= frame.tick:
                    continue
                if event.kind == "explosion":
                    affected = event.contains(frame.position)
                    safe = lambda p: not event.contains(p)
                elif event.kind == "split":
                    affected = ((frame.position[0] == event.point[0] or frame.position[1] == event.point[1])
                                and frame.position != events.spawn)
                    safe = lambda p: (p[0] != event.point[0] and p[1] != event.point[1]) or p == events.spawn
                else:
                    continue
                if affected:
                    exits = [p for p in steps if safe(p)]
                    if exits:
                        target = min(exits, key=lambda p: (steps[p], costs[p], p))
                        slack = event.start - frame.tick - steps[target]
                        if slack <= 2:
                            return self._route_to(target, parent, frame, events, history, "evacuate", 5.0)
        goals = []
        for p, resource in memory.resources.items():
            if p not in steps or steps[p] > remaining:
                continue
            staleness = max(0.45, 1 - (frame.tick - resource["last_seen"]) * 0.006)
            # Race estimates use only currently visible competitors, never their private state.
            rival = min((manhattan(e["pos"], p) for e in enemies), default=1000)
            race = 0.45 if rival + 1 < steps[p] else 1.0
            # Rival distance is only a lower bound using currently visible robots.
            # Prioritize narrowly winnable races before safe, uncontested pickups.
            urgency = 1.45 if c.stage == 2 and steps[p] <= rival + 1 <= steps[p] + 4 else 1.0
            goals.append({"pos": p, "base": self.resource_gain(frame, resource["value"]) * staleness * race,
                          "kind": "resource", "value": resource["value"], "expiry": c.horizon,
                          "staleness": staleness, "race": race, "urgency": urgency})
        done = set(frame.score.get("mission_completed_ids", []))
        if c.stage == 2:
            for mission in frame.missions:
                p = mission.get("position")
                if mission["type"] == "reach" and mission["id"] not in done and p in steps:
                    goals.append({"pos": p, "base": 5.5, "kind": "mission", "value": 0, "expiry": c.horizon})
            for p, reward, expiry, kind in events.opportunities(frame, history):
                if p in steps and frame.tick + steps[p] < expiry:
                    goals.append({"pos": p, "base": reward, "kind": kind, "value": 0, "expiry": expiry})
            fade = max(0.0, 1 - frame.tick / 150)
            if c.explore_weight and fade:
                frontier = []
                unseen = self._unseen_counter(frame)
                for p in steps:
                    if 0 < steps[p] <= remaining:
                        gain = unseen(p)
                        if gain:
                            frontier.append((gain / (costs[p] + 2.5) ** 0.85, gain, p))
                for _, gain, p in sorted(frontier, reverse=True)[:2]:
                    goals.append({"pos": p, "base": self.explore_value(frame, gain),
                                  "kind": "explore", "value": 0, "expiry": c.horizon})

        # Candidate truncation is based on reachable value/distance, not input ordering.
        def initial_reward(g):
            counts = Counter({"high_resources" if g["value"] >= 20 else "normal_resources": 1,
                              "planned_value": g["value"]})
            return (g["base"] + (self._mission_bonus(frame, g["value"], counts) if c.stage == 2 and g["value"] else 0)) * g.get("urgency", 1)
        goals.sort(key=lambda g: (-initial_reward(g) / (steps[g["pos"]] + 2), g["pos"], g["kind"]))
        goals = goals[:20]
        pair = {frame.position: (costs, steps)}
        for g in goals:
            if g["pos"] not in pair:
                gc, gs, _ = self.distances(g["pos"], frame, events)
                pair[g["pos"]] = gc, gs
        if c.stage == 1 and goals:
            # Minimize a tour of all known resources when the time budget permits.
            # This avoids value-ratio detours while retaining reward search near expiry.
            best_tour = None
            nearest_distance = min(steps[g["pos"]] for g in goals)
            for first in range(len(goals)):
                if steps[goals[first]["pos"]] != nearest_distance:
                    continue
                order, unused = [first], set(range(len(goals))) - {first}
                while unused:
                    current = goals[order[-1]]["pos"]
                    possible = [i for i in unused if goals[i]["pos"] in pair[current][1]]
                    if not possible:
                        break
                    nxt = min(possible, key=lambda i: (pair[current][1][goals[i]["pos"]], i))
                    order.append(nxt)
                    unused.remove(nxt)
                if unused:
                    continue
                def length(route):
                    result, current = 0, frame.position
                    for i in route:
                        target = goals[i]["pos"]
                        if target not in pair[current][1]:
                            return math.inf
                        result += max(1, pair[current][1][target])
                        current = target
                    return result
                # Bounded 2-opt pass removes open-tour crossings.
                for a in range(1, len(order) - 1):
                    for b in range(a + 1, len(order)):
                        candidate = order[:a] + list(reversed(order[a:b + 1])) + order[b + 1:]
                        if length(candidate) < length(order):
                            order = candidate
                total = length(order)
                rank = (total, steps[goals[order[0]]["pos"]], order[0])
                if total <= remaining and (best_tour is None or rank < best_tour[0]):
                    best_tour = rank, order
            if best_tour:
                target = goals[best_tour[1][0]]["pos"]
                return self._route_to(target, parent, frame, events, history, "resource_tour", 1.0)
        # Beam search over short resource/mission tours. Discount later rewards;
        # use distance-efficiency to avoid consuming the entire match on one detour.
        beam = [(0.0, frame.position, 0, 0.0, -1, Counter(), 0)]
        best = (0.0, -1)
        for depth in range(min(c.beam_depth, len(goals))):
            expanded = []
            for _, current, used, reward, first, counts, elapsed in beam:
                distances, lengths = pair[current]
                for i, g in enumerate(goals):
                    if used & (1 << i) or g["pos"] not in lengths:
                        continue
                    length = lengths[g["pos"]]
                    arrival = elapsed + max(1, length)
                    if arrival > remaining or frame.tick + arrival > g["expiry"]:
                        continue
                    new_counts = counts.copy()
                    bonus = 0.0
                    if g["value"] and c.stage == 2:
                        key = "high_resources" if g["value"] >= 20 else "normal_resources"
                        new_counts[key] += 1
                        new_counts["planned_value"] += g["value"]
                        bonus = self._mission_bonus(frame, g["value"], new_counts)
                    base_reward = g["base"]
                    if g["value"] and c.stage == 2:
                        base_reward = self.resource_gain(frame, g["value"], counts["planned_value"]) * g["staleness"] * g["race"]
                    urgency = g.get("urgency", 1) if depth == 0 else 1.0
                    new_reward = reward + (base_reward + bonus) * urgency * (0.91 ** depth)
                    efficiency = new_reward / (arrival + 3.5) ** 0.78
                    first_index = i if first == -1 else first
                    candidate = (efficiency, g["pos"], used | (1 << i), new_reward,
                                 first_index, new_counts, arrival)
                    expanded.append(candidate)
                    if efficiency > best[0]:
                        best = efficiency, first_index
            if not expanded:
                break
            expanded.sort(key=lambda s: (-s[0], s[4], s[2]))
            beam = expanded[:c.beam_width]
        if best[1] >= 0:
            g = goals[best[1]]
            return self._route_to(g["pos"], parent, frame, events, history,
                                  g["kind"], best[0])

        # Once collection/mission opportunities are exhausted, approach visible
        # opponents before exploring empty corners. Only visible enemies influence
        # immediate attack, defense, occupancy and damage calculations.
        if c.stage == 2 and enemies:
            engagements = []
            for enemy in enemies:
                target = enemy["pos"]
                for p in steps:
                    if (1 <= manhattan(p, target) <= c.attack_range
                            and (p[0] == target[0] or p[1] == target[1])):
                        engagements.append((costs[p], enemy["team"], p))
            if engagements:
                _, _, target = min(engagements)
                return self._route_to(target, parent, frame, events, history, "engage", 1.2)
        if c.stage == 2:
            sightings = [(costs[s["pos"]] + (frame.tick - s["tick"]) * 0.25,
                          team, s["pos"])
                         for team, s in memory.enemy_sightings.items()
                         if s["pos"] in steps and 0 < steps[s["pos"]] <= remaining]
            if sightings:
                _, _, target = min(sightings)
                return self._route_to(target, parent, frame, events, history, "investigate", 0.9)

        # Exploration frontier: maximize new visible cells per travel cost.
        # Revisits are penalized, so coverage continues after local resources vanish.
        frontier = []
        unseen = self._unseen_counter(frame)
        for p in sorted(steps):
            if steps[p] > remaining or steps[p] == 0:
                continue
            gain = unseen(p)
            if gain:
                utility = gain / (costs[p] + 2.5) ** 0.85 / (1 + memory.visits[p] * 0.4)
                frontier.append((utility, p))
        if frontier:
            utility, target = max(frontier, key=lambda pair: (pair[0], -pair[1][1], -pair[1][0]))
            return self._route_to(target, parent, frame, events, history, "explore", utility * 0.15)
        if c.stage == 2:
            # Revisit old observation areas after coverage is complete. This is a
            # search policy, not access to unseen enemies or their spawn positions.
            target = self.patrol_goal
            if (target not in steps or steps.get(target, 0) == 0
                    or memory.observed_at.get(target, -1) == frame.tick):
                candidates = [p for p in steps if 0 < steps[p] <= remaining
                              and memory.observed_at.get(p, -1) < frame.tick]
                target = max(candidates, key=lambda p: (
                    (frame.tick - memory.observed_at.get(p, -1)) / (costs[p] + 4) ** 0.7,
                    -memory.visits[p], p), default=None)
                self.patrol_goal = target
            if target is not None:
                return self._route_to(target, parent, frame, events, history, "patrol", 0.8)
        return Route()

    def explore_value(self, frame, unseen):
        """Expected points behind `unseen` cells: the published resource count spread over
        this match's map at the published mean value, fading out by Tick 150 as the pool empties."""
        c = self.config
        density = c.resource_count / (frame.width * frame.height)
        fade = max(0.0, 1 - frame.tick / 150)
        return c.explore_weight * unseen * density * (c.resource_total / c.resource_count) * 0.25 * fade

    def _unseen_counter(self, frame):
        """Unseen cells in the vision window around p, via a 2D prefix sum built once per call
        (O(1) per query instead of rescanning the (2r+1)^2 window)."""
        w, h, r, seen = frame.width, frame.height, frame.radius, self.memory.seen
        table = [[0] * (w + 1) for _ in range(h + 1)]
        for y in range(h):
            row, above, run = table[y + 1], table[y], 0
            for x in range(w):
                run += (x, y) not in seen
                row[x + 1] = above[x + 1] + run
        def count(p):
            x0, x1 = max(0, p[0] - r), min(w, p[0] + r + 1)
            y0, y1 = max(0, p[1] - r), min(h, p[1] + r + 1)
            return table[y1][x1] - table[y0][x1] - table[y1][x0] + table[y0][x0]
        return count

    def _route_to(self, target, parent, frame, events, history, reason, utility):
        if target == frame.position:
            if reason == "heal":
                for direction, delta in DIRECTIONS.items():
                    p = add(target, delta)
                    if (0 <= p[0] < frame.width and 0 <= p[1] < frame.height
                            and p not in self.memory.obstacles and not events.blocked(p, frame.tick)):
                        sent = OPPOSITE[direction] if events.inverted(frame, history) else direction
                        return Route(sent, target, "heal_reenter", utility, (target, p))
            return Route("", target, reason, utility, (target,))
        path, p = [target], target
        while p != frame.position:
            if p not in parent:
                return Route()
            p = parent[p]
            path.append(p)
        path.reverse()
        delta = path[1][0] - path[0][0], path[1][1] - path[0][1]
        direction = next(k for k, v in DIRECTIONS.items() if v == delta)
        if events.inverted(frame, history):
            direction = OPPOSITE[direction]
        return Route(direction, target, reason, utility, tuple(path))
