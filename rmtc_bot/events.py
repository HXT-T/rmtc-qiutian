"""Validated LLM semantics; no parsing of natural-language events here."""
from dataclasses import dataclass, field
from .protocol import coordinate, integer, number

KINDS = {"explosion", "invert", "heal", "fortress", "split", "chili", "reflection", "vulnerability"}


@dataclass
class Event:
    kind: str
    start: int
    end: int
    rect: tuple = ()
    point: tuple = ()
    team: str = ""
    params: dict = field(default_factory=dict)

    @property
    def key(self):
        return self.kind, self.start, self.rect, self.point, self.team

    def contains(self, p):
        if self.rect:
            x1, y1, x2, y2 = self.rect
            return x1 <= p[0] <= x2 and y1 <= p[1] <= y2
        return self.point == p

    def active(self, tick):
        return self.start <= tick < self.end


ALLOWED_PARAMS = {"damage", "defended_damage", "factor", "heal_per_tick", "heal_duration",
                  "dwell_ticks", "attack_radius", "trigger_end", "reflection_damage"}


def validate_event(item, width, height):
    if not isinstance(item, dict) or item.get("kind") not in KINDS:
        raise ValueError("unknown event kind")
    start, end = item.get("start"), item.get("end")
    if not integer(start) or not integer(end) or end <= start:
        raise ValueError("event needs absolute start < end")
    kind = item["kind"]
    rect, point = (), ()
    if kind in ("explosion", "invert", "fortress"):
        v = item.get("rect")
        if not isinstance(v, list) or len(v) != 4:
            raise ValueError("event needs rect [x1,y1,x2,y2]")
        a, b = coordinate(v[:2], width, height), coordinate(v[2:], width, height)
        if a[0] > b[0] or a[1] > b[1]:
            raise ValueError("unordered event rectangle")
        rect = (*a, *b)
    if kind in ("heal", "split", "chili"):
        point = coordinate(item.get("point"), width, height)
    team = item.get("team", "") or ""
    if team not in ("", "A", "B", "C", "D"):
        raise ValueError("unknown event team")
    params = item.get("params", {})
    if not isinstance(params, dict) or set(params) - ALLOWED_PARAMS:
        raise ValueError("unknown event parameter")
    for key, value in params.items():
        if not number(value, 0, 1000000):
            raise ValueError("invalid event parameter")
        if key in ("heal_duration", "dwell_ticks", "attack_radius", "trigger_end") and not integer(value):
            raise ValueError("integer event parameter required")
    if "trigger_end" in params and not start <= params["trigger_end"] <= end:
        raise ValueError("trigger window outside event")
    return Event(kind, start, end, rect, point, team, params)


def absolutize(item, known_ticks=(), tolerance=2):
    """Relative-time output: the model copies the numbers stated in the announcement
    (announce_tick, delay, duration) and the program adds them. Cross-checks use only the
    model output and our own record of when announcements arrived."""
    if not isinstance(item, dict) or ('delay' not in item and 'duration' not in item):
        return item
    announce, delay, duration = item.get('announce_tick'), item.get('delay'), item.get('duration')
    if not integer(announce) or not integer(delay) or not integer(duration) or delay < 0 or duration < 1:
        raise ValueError("relative event needs integer announce_tick, delay >= 0, duration >= 1")
    if known_ticks and min(abs(announce - t) for t in known_ticks) > tolerance:
        raise ValueError("announce_tick matches no received announcement")
    start = announce + delay
    if 'start' in item and item['start'] != start:
        raise ValueError("stated start disagrees with announce_tick + delay")
    out = {k: v for k, v in item.items() if k not in ('announce_tick', 'delay', 'duration')}
    out['start'], out['end'] = start, start + duration  # start inclusive, end exclusive
    params = dict(out.get('params') or {})
    if 'trigger_ticks' in params:
        window = params.pop('trigger_ticks')
        if not integer(window) or window < 0:
            raise ValueError("trigger_ticks must be a nonnegative integer")
        params['trigger_end'] = start + window
    if params or 'params' in out:
        out['params'] = params
    return out


def validate_plan(obj, width, height, partial=False, relative=False, known_ticks=()):
    """Strict by default. With partial=True one malformed event no longer discards the
    valid ones: returns (strategy, events, errors) with each rejected item's reason."""
    if not isinstance(obj, dict) or not isinstance(obj.get("events"), list) or len(obj["events"]) > 128:
        raise ValueError("model must return an events array")
    errors = []
    strategy = obj.get("strategy", "balanced")
    if strategy not in ("balanced", "collect", "combat", "survive"):
        if not partial:
            raise ValueError("invalid model strategy")
        errors.append((-1, "invalid model strategy"))
        strategy = "balanced"
    events = []
    for i, item in enumerate(obj["events"]):
        try:
            events.append(validate_event(absolutize(item, known_ticks) if relative else item, width, height))
        except ValueError as exc:
            if not partial:
                raise
            errors.append((i, str(exc)))
    return (strategy, events, errors) if partial else (strategy, events)


class EventBook:
    def __init__(self):
        self.events = {}
        self.spawn = None

    def merge(self, events):
        for event in events:
            self.events[event.key] = event

    def active(self, kind, tick):
        return [e for e in self.events.values() if e.kind == kind and e.active(tick)]

    def blocked(self, p, tick):
        # Only our initial spawn is publicly known as a spawn exemption.
        return any((p[0] == e.point[0] or p[1] == e.point[1]) and p != self.spawn
                   for e in self.active("split", tick))

    def inverted(self, frame, history):
        for e in self.active("invert", frame.tick):
            if e.team and e.team != frame.own.get("team"):
                continue
            start = history.get(e.start)
            if start and start["alive"] and e.contains(start["position"]):
                return True
            if not start and any(e.start <= t - 1 < e.end and sample.get("inverted")
                                 for t, sample in history.items() if t <= frame.tick):
                return True  # late join: authoritative move receipt supplies evidence
        return False

    def vulnerable(self, tick, history):
        for e in self.active("vulnerability", tick):
            trigger_end = e.params.get("trigger_end", min(e.start + 20, e.end))
            for t, sample in history.items():
                # Frame t reports the result of action t-1. Amplification starts at t.
                if e.start <= t - 1 < trigger_end and t <= tick and sample.get("moved"):
                    return e.params.get("factor", 2.0)
        return 1.0

    def fortress_power(self, frame, history):
        for e in self.active("fortress", frame.tick):
            if e.team and e.team != frame.own.get("team"):
                continue
            dwell, required = 0, max(1, e.params.get("dwell_ticks", 10))
            # Frame N+1 is post-move position of action N; do not count pre-start.
            for t in sorted(history):
                if not e.start < t <= frame.tick:
                    continue
                sample = history[t]
                previous = history.get(t - 1)
                if previous is None:
                    dwell = 0  # missed frames cannot prove continuous residence
                dwell = dwell + 1 if sample["alive"] and e.contains(sample["position"]) else 0
                if dwell >= required:
                    return e  # persists through death until event end
        return None

    def hazard(self, p, tick, protected=False):
        cost = 0.0
        for e in self.active("explosion", tick):
            if tick == e.start and e.contains(p) and not protected:
                cost += e.params.get("damage", 80)
        if self.blocked(p, tick) and any(e.start == tick for e in self.active("split", tick)):
            cost += 1000
        return cost

    def opportunities(self, frame, history):
        goals = []
        for e in self.events.values():
            if not e.active(frame.tick) or (e.team and e.team != frame.own.get("team")):
                continue
            if e.kind == "heal" and frame.own["hp"] < 85:
                # Entering is required. The planner handles leaving/re-entering if on point.
                goals.append((e.point, (100 - frame.own["hp"]) * 0.16, e.end, "heal"))
            if e.kind == "fortress" and not self.fortress_power(frame, history):
                x1, y1, x2, y2 = e.rect
                center = ((x1 + x2) // 2, (y1 + y2) // 2)
                goals.append((center, 15.0, e.end - e.params.get("dwell_ticks", 10), "fortress"))
        return goals
