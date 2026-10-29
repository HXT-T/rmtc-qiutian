import copy


def relative(dx, dy):
    rows = [["0"] * 9 for _ in range(9)]
    rows[4][4] = "1"
    if dx or dy:
        rows[dy + 4][dx + 4] = "2"
    return "\n".join("".join(row) for row in rows)


def frame(tick=0, session="test-session", pos=(2, 2), entities=(), **kw):
    f = {"tick": tick, "session_id": session, "vision_radius": 4,
         "self": {"team": "A", "position": list(pos), "hp": 100,
                  "alive": True, "protected": False, "last_action": None},
         "visible_entities": [], "score": {"resource_value": 0, "effective_damage": 0,
             "mission_completed_ids": [], "invalid_actions": 0, "mission_completed": 0},
         "missions": [], "mission_text": "", "event_text": "",
         "map": {"width": 12, "height": 12}}
    for entity in entities:
        e = copy.deepcopy(entity)
        x, y = e.pop("pos")
        e["position"] = relative(x - pos[0], y - pos[1])
        f["visible_entities"].append(e)
    f.update(kw)
    return f


def resource(pos, value=10, id="r1"):
    return {"type": "resource_high" if value >= 20 else "resource_normal", "id": id,
            "value": value, "pos": pos}


def enemy(pos, hp=100, protected=False, defending=False, team="B"):
    return {"type": "robot", "team": team, "hp": hp,
            "protected": protected, "defending": defending, "pos": pos}
