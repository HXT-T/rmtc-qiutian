"""Strict input validation. Never coerce tick/session/coordinates from strings."""
import json
import math
from dataclasses import dataclass

DIRECTIONS = {"up": (0, -1), "right": (1, 0), "down": (0, 1), "left": (-1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


def integer(v, lo=0, hi=1000000):
    return type(v) is int and lo <= v <= hi


def number(v, lo=0, hi=1e9):
    return type(v) in (int, float) and math.isfinite(v) and lo <= v <= hi


def coordinate(v, width, height):
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError("coordinate must contain two integers")
    if not integer(v[0], 0, width - 1) or not integer(v[1], 0, height - 1):
        raise ValueError("coordinate outside map")
    return tuple(v)


def relative(v):
    if not isinstance(v, str) or len(v) != 89:
        raise ValueError("entity position must be a 9x9 string")
    rows = v.split("\n")
    if len(rows) != 9 or any(len(r) != 9 for r in rows):
        raise ValueError("invalid relative rows")
    if any(c not in "012" for r in rows for c in r) or rows[4][4] != "1":
        raise ValueError("invalid relative cells")
    if v.count("1") != 1 or v.count("2") > 1:
        raise ValueError("invalid relative markers")
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            if char == "2":
                return x - 4, y - 4
    return 0, 0


@dataclass
class Frame:
    tick: int
    session: str
    position: tuple
    width: int
    height: int
    radius: int
    own: dict
    entities: list
    score: dict
    missions: list
    mission_text: str
    event_text: str
    warnings: list

    @classmethod
    def parse(cls, raw, team="A"):
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > 2097152:
                raise ValueError("perception exceeds input limit")
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError("perception must be an object")
        tick, session = raw.get("tick"), raw.get("session_id")
        if not integer(tick) or not isinstance(session, str) or not session or len(session) > 512:
            raise ValueError("invalid tick/session_id")
        size, own = raw.get("map"), raw.get("self")
        if not isinstance(size, dict) or not isinstance(own, dict):
            raise ValueError("missing map/self")
        width, height = size.get("width"), size.get("height")
        # The official map editor allows 1..200 per side; rejecting a larger perception would idle the robot.
        if not integer(width, 1, 200) or not integer(height, 1, 200):
            raise ValueError("unsupported map size (1..200)")
        pos = coordinate(own.get("position"), width, height)
        if own.get("team", team) != team:
            raise ValueError("perception belongs to another seat")
        if type(own.get("alive")) is not bool or not number(own.get("hp")):
            raise ValueError("invalid alive/hp")
        if type(own.get("protected", False)) is not bool:
            raise ValueError("invalid protection")
        radius = raw.get("vision_radius")
        if not integer(radius, 0, 4):
            raise ValueError("invalid vision radius")
        visible = raw.get("visible_entities")
        if not isinstance(visible, list) or len(visible) > 1024:
            raise ValueError("invalid visible_entities")
        entities, warnings = [], []
        for item in visible:
            try:
                if not isinstance(item, dict):
                    raise ValueError("entity is not an object")
                if item.get("type") not in ("robot", "obstacle", "resource_normal", "resource_high"):
                    continue  # forward-compatible unknown entity type
                dx, dy = relative(item.get("position"))
                p = coordinate([pos[0] + dx, pos[1] + dy], width, height)
                if max(abs(dx), abs(dy)) > radius:
                    raise ValueError("entity outside declared vision")
                e = dict(item, pos=p)
                if e["type"].startswith("resource_") and not number(e.get("value"), 0.001):
                    raise ValueError("invalid resource value")
                if e["type"] == "robot":
                    if e.get("team") not in ("A", "B", "C", "D") or not number(e.get("hp")):
                        raise ValueError("invalid robot")
                    for key in ("protected", "defending"):
                        if type(e.get(key, False)) is not bool:
                            raise ValueError("invalid robot flag")
                entities.append(e)
            except (ValueError, TypeError) as exc:
                warnings.append(str(exc))
        # A bad visible entity may be an obstacle: do not plan through a partial frame.
        if warnings:
            raise ValueError("invalid entity: " + warnings[0])
        score = raw.get("score", {})
        if not isinstance(score, dict):
            raise ValueError("invalid score")
        for key in ("resource_value", "effective_damage", "invalid_actions", "mission_completed"):
            if key in score and not number(score[key]):
                raise ValueError("invalid score value")
        ids = score.get("mission_completed_ids", [])
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise ValueError("invalid completed mission IDs")
        missions = raw.get("missions", [])
        if not isinstance(missions, list) or len(missions) > 128:
            raise ValueError("invalid missions")
        valid = []
        for m in missions:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                raise ValueError("invalid mission")
            m = dict(m)
            kind = m.get("type")
            if kind not in ("reach", "normal_resources", "high_resources", "resource_value", "effective_damage"):
                continue
            if kind == "reach":
                m["position"] = coordinate(m.get("position"), width, height)
            elif not number(m.get("target"), 0.001):
                raise ValueError("invalid mission target")
            valid.append(m)
        for key in ("event_text", "mission_text"):
            if not isinstance(raw.get(key, ""), str) or len(raw.get(key, "")) > 65536:
                raise ValueError("invalid public text")
        return cls(tick, session, pos, width, height, radius, own, entities,
                   score, valid, raw.get("mission_text", ""), raw.get("event_text", ""), warnings)


def action(frame, kind, direction=None):
    if kind not in ("move", "attack", "defend", "wait"):
        raise ValueError("unknown action")
    result = {"tick": frame.tick, "session_id": frame.session, "action": kind}
    if kind in ("move", "attack"):
        if direction not in DIRECTIONS:
            raise ValueError("invalid direction")
        result["direction"] = direction
    return result
