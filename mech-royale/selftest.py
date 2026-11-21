"""Offline self-test for the placement bot: ``python3 mech-royale/selftest.py``.

Replays the kit's recorded stream through ``Bot.act`` directly (catching exceptions the wrapper would
swallow) and runs synthetic scenarios that the recorded stream does not cover: being shot at, being
outside the zone, a visible but non-engaging enemy behind/without cover, and a third-party fight.
"""
from __future__ import annotations

import copy
import gzip
import json
import math
import os
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bot as botmod  # noqa: E402
from walls import Walls  # noqa: E402


def load_stream() -> List[Dict[str, Any]]:
    with gzip.open(os.path.join(HERE, "check", "stream.jsonl.gz"), "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def mech(eid: int, pos: List[float], aim_yaw: float, hp: float = 1400.0) -> Dict[str, Any]:
    return {"id": eid, "kind": "mech", "pos": pos, "vel": [0, 0, 0], "aim": [aim_yaw, 0.0],
            "hp": hp, "max_hp": 1400, "cubes": 0, "weapons": ["gun"], "charging": False}


def main() -> int:
    stream = load_stream()
    hello = stream[0]
    hello["map"]["dir"] = os.path.join(HERE, "arena")
    obs_list = [m for m in stream if m.get("type") == "obs"]
    fails: List[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # 1. recorded stream: no exceptions, schema-ish sanity, timing
    b = botmod.Bot(hello)
    worst = 0.0
    for o in obs_list:
        t0 = time.perf_counter()
        a = b.act(o)
        worst = max(worst, time.perf_counter() - t0)
        mv = a["move"]
        assert math.hypot(mv[0], mv[1]) <= 1.0 and len(a["aim"]) == 2
    check(worst < 0.02, f"recorded stream: {len(obs_list)} ticks, worst decision {worst * 1000:.1f} ms")

    base = copy.deepcopy(obs_list[100])
    me = base["self"]
    base["mechs"], base["events"], base["cubes"], base["drops"], base["sounds"] = [], [], [], [], []
    base["zone"] = {"center": [me["pos"][0], me["pos"][1]], "radius": 400, "next_center": [me["pos"][0], me["pos"][1]],
                    "next_radius": 300, "shrink_start": base["t"] + 200, "shrink_end": base["t"] + 250, "dps": 20}
    me["in_zone"] = True
    x, y, z = me["pos"]

    # 2. quiet + inside the zone -> hold still (silent)
    b = botmod.Bot(hello)
    a = b.act(copy.deepcopy(base))
    check(a["move"] == [0.0, 0.0] and b.mode == "hold", f"quiet and zone-safe: hold (mode={b.mode})")

    # 3. outside the zone -> move toward the centre and boost
    o = copy.deepcopy(base)
    o["self"]["in_zone"] = False
    o["zone"]["center"] = o["zone"]["next_center"] = [x + 500, y]
    b = botmod.Bot(hello)
    a = b.act(o)
    check(a["move"][0] > 0.5 and a["boost"], f"outside zone: run toward centre {a['move']} boost={a['boost']}")

    # 4. shot at from the east -> move away (west-ish), face the attacker, do not fire
    o = copy.deepcopy(base)
    o["mechs"] = [mech(7, [x + 60, y, z], math.pi)]
    o["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 7}]
    b = botmod.Bot(hello)
    a = b.act(o)
    check(b.mode == "flee" and a["move"][0] < -0.2, f"focus-fired: flee west {a['move']} mode={b.mode}")
    check(abs(a["aim"][0]) < 0.2 and "fire" not in a, f"focus-fired: face attacker, no shots (aim={a['aim']})")

    # 5. enemy aiming at us with a laser charge also triggers fleeing without prior damage
    o = copy.deepcopy(base)
    e = mech(9, [x, y + 80, z], -math.pi / 2)
    e["charging"] = True
    o["mechs"] = [e]
    b = botmod.Bot(hello)
    a = b.act(o)
    check(b.mode == "flee" and a["move"][1] < 0, f"aimed at by laser: flee south {a['move']}")

    # 6. third-party fight: enemy 80 m east shooting at another mech, nobody aims at us -> shoot, hold position
    o = copy.deepcopy(base)
    o["mechs"] = [mech(5, [x + 80, y, z], 0.0), mech(6, [x + 140, y, z], 0.0)]
    o["self"]["aim"] = [0.0, 0.0]
    b = botmod.Bot(hello)
    a = b.act(o)
    check(a.get("fire") == [True, False], f"third party: fires gun at a busy enemy (fire={a.get('fire')})")

    # 7. low HP: no third-party shots
    o2 = copy.deepcopy(o)
    o2["self"]["hp"] = 300
    b = botmod.Bot(hello)
    a = b.act(o2)
    check("fire" not in a, "low HP: no third-party shots")

    # 8. cube nearby and safe -> go loot it; with an enemy visible -> do not
    o = copy.deepcopy(base)
    o["cubes"] = [{"id": 5001, "pos": [x + 20, y, z]}]
    b = botmod.Bot(hello)
    a = b.act(o)
    check(b.mode == "loot" and a["move"][0] > 0.0, f"safe cube pickup: mode={b.mode} move={a['move']}")
    o["mechs"] = [mech(8, [x - 100, y, z], 0.0)]
    b = botmod.Bot(hello)
    b.act(o)
    check(b.mode != "loot", f"enemy visible: no looting (mode={b.mode})")

    # 9. late-game: hold the edge facing inward (we start near the centre -> move out to ~80% radius)
    o = copy.deepcopy(base)
    o["zone"].update(radius=140, next_radius=70, shrink_start=o["t"] + 10, shrink_end=o["t"] + 40)
    o["zone"]["next_center"] = [x + 10, y]
    o["zone"]["center"] = [x + 10, y]
    b = botmod.Bot(hello)
    goal = b._zone_goal(b.world.__class__ and (b.world.update(o) or b.world.zone))[0]
    check(goal is not None and 0.6 * 70 <= math.hypot(goal[0] - (x + 10), goal[1] - y) <= 0.9 * 70,
          f"late game: goal sits on the zone edge band {goal}")

    # 10. walls: a segment through the real map is blocked somewhere, and an empty map never blocks
    walls = Walls.load(os.path.join(HERE, "arena"))
    check(len(walls.prisms) > 200, f"map loaded: {len(walls.prisms)} building prisms")
    blocked = sum(walls.blocked(px, 0, px + 40, 0, 1.2, 9) for px in range(-400, 400, 5))
    check(blocked > 0, f"map has obstacles along y=0 ({blocked} blocked 40 m segments)")
    check(not Walls.load("/nonexistent").blocked(0, 0, 100, 100, 0, 9), "missing map: degrades to open field")

    # 11. timing with an exposed enemy and real walls on every candidate heading
    o = copy.deepcopy(base)
    o["mechs"] = [mech(i, [x + 30 + 10 * i, y + 20 * i, z], math.pi) for i in range(4)]
    o["events"] = [{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 0.0, "by": 0}]
    b = botmod.Bot(hello)
    t0 = time.perf_counter()
    for _ in range(50):
        b.act(copy.deepcopy(o))
    per = (time.perf_counter() - t0) / 50
    check(per < 0.02, f"worst-case scenario decision {per * 1000:.2f} ms")

    print("\n%s" % ("ALL PASS" if not fails else "%d FAILED" % len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
