"""Per-tick world model for the placement bot.

``World.update(obs)`` folds one observation into: our own state, the zone (with interpolation and
timing), tracked enemy mechs / NPC drones (including who is engaging us), recent damage taken, and
visible loot (cubes, supply drops).  It makes no decisions; ``bot.py`` does.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

Vec = List[float]

MEMORY_S = 3.0        # keep an enemy that left view this long (the server also tracks for ~5 s)
DAMAGE_WINDOW_S = 4.0  # how long damage events stay in the log
NEAR_SOUNDS = ("gun", "mortar", "laser", "laser_charge", "lightning", "explosion")


def wrap_angle(a: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a


def unit(dx: float, dy: float) -> Tuple[float, float]:
    """Unit vector of (dx, dy); (0, 0) for a zero vector."""
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else (0.0, 0.0)


class Enemy:
    """A tracked mech or NPC drone."""

    __slots__ = ("id", "is_npc", "pos", "vel", "aim", "hp", "max_hp", "weapons", "charging",
                 "last_seen", "visible", "dist", "aims_at_me", "damaged_me_t")

    def __init__(self, eid: int, is_npc: bool) -> None:
        self.id = eid
        self.is_npc = is_npc
        self.pos: Vec = [0.0, 0.0, 0.0]
        self.vel: Vec = [0.0, 0.0, 0.0]
        self.aim: Vec = [0.0, 0.0]
        self.hp = 0.0
        self.max_hp = 1.0
        self.weapons: List[str] = []
        self.charging = False
        self.last_seen = -1e9
        self.visible = False
        self.dist = 1e9
        self.aims_at_me = False
        self.damaged_me_t = -1e9


class Zone:
    """Current zone state with helpers for interpolation and margins."""

    def __init__(self, z: Dict[str, Any]) -> None:
        self.cx, self.cy = z["center"][0], z["center"][1]
        self.r = float(z["radius"])
        self.ncx, self.ncy = z["next_center"][0], z["next_center"][1]
        self.nr = float(z["next_radius"])
        self.shrink_start = float(z["shrink_start"])
        self.shrink_end = float(z["shrink_end"])
        self.dps = float(z.get("dps", 0.0))

    def at(self, t: float) -> Tuple[float, float, float]:
        """Zone circle (cx, cy, r) at match time ``t`` (linear shrink between start and end)."""
        if t <= self.shrink_start or self.shrink_end <= self.shrink_start:
            return self.cx, self.cy, self.r
        if t >= self.shrink_end:
            return self.ncx, self.ncy, self.nr
        f = (t - self.shrink_start) / (self.shrink_end - self.shrink_start)
        return (self.cx + (self.ncx - self.cx) * f, self.cy + (self.ncy - self.cy) * f,
                self.r + (self.nr - self.r) * f)

    @staticmethod
    def margin(x: float, y: float, circle: Tuple[float, float, float]) -> float:
        """Metres of slack inside ``circle`` (negative = outside)."""
        return circle[2] - math.hypot(x - circle[0], y - circle[1])


class World:
    """Everything the policy needs, rebuilt incrementally from observations."""

    def __init__(self, cfg: Dict[str, Any], slot: int) -> None:
        self.cfg = cfg
        self.slot = slot
        self.gun_speed = float(cfg.get("weapons", {}).get("gun", {}).get("speed", 252.0))
        self.enemies: Dict[int, Enemy] = {}
        self.damage_log: List[Dict[str, Any]] = []   # {t, by, bearing, amount, weapon}
        self.sound_threats: List[Tuple[float, float]] = []  # (bearing, weight) from near gunfire
        # own state
        self.t = 0.0
        self.pos: Vec = [0.0, 0.0, 0.0]
        self.vel: Vec = [0.0, 0.0, 0.0]
        self.aim: Vec = [0.0, 0.0]
        self.hp = 1.0
        self.max_hp = 1.0
        self.energy = 0.0
        self.max_energy = 1.0
        self.in_zone = True
        self.cubes = 0
        self.alive = 16
        self.zone: Optional[Zone] = None
        self.cube_list: List[Dict[str, Any]] = []
        self.drops: List[Dict[str, Any]] = []
        self.weapons: List[Optional[Dict[str, Any]]] = [None, None]
        self.stats: Dict[str, float] = dict(cfg.get("mech", {}))

    # ------------------------------------------------------------------ update
    def update(self, obs: Dict[str, Any]) -> None:
        """Fold one observation into the model."""
        me = obs["self"]
        self.t = float(obs["t"])
        self.pos = me["pos"]
        self.vel = me["vel"]
        self.aim = me["aim"]
        self.hp, self.max_hp = float(me["hp"]), float(me["max_hp"])
        self.energy, self.max_energy = float(me["energy"]), float(me["max_energy"])
        self.in_zone = bool(me.get("in_zone", True))
        self.cubes = int(me.get("cubes", 0))
        self.alive = int(obs.get("alive", 16))
        self.weapons = me.get("weapons", [None, None])
        self.stats = me.get("stats", self.stats)
        self.zone = Zone(obs["zone"])
        self.cube_list = obs.get("cubes", [])
        self.drops = obs.get("drops", [])
        self._update_damage(obs.get("events", []))
        self._update_enemies(obs.get("mechs", []))
        self._update_sounds(obs.get("sounds", []))

    def _update_damage(self, events: List[Dict[str, Any]]) -> None:
        for ev in events:
            if ev.get("type") == "damaged" and ev.get("weapon") != "zone" and ev.get("by") is not None:
                self.damage_log.append({"t": self.t, "by": int(ev["by"]), "bearing": ev.get("bearing"),
                                        "amount": float(ev.get("amount", 0)), "weapon": ev.get("weapon")})
        self.damage_log = [d for d in self.damage_log if self.t - d["t"] <= DAMAGE_WINDOW_S]

    def _update_enemies(self, mechs: List[Dict[str, Any]]) -> None:
        mx, my = self.pos[0], self.pos[1]
        for e in self.enemies.values():
            e.visible = False
            e.aims_at_me = False
        for m in mechs:
            eid = int(m["id"])
            if eid == self.slot:
                continue
            e = self.enemies.get(eid)
            if e is None:
                e = self.enemies[eid] = Enemy(eid, m.get("kind") == "npc")
            e.pos, e.vel, e.aim = m["pos"], m.get("vel", [0.0, 0.0, 0.0]), m.get("aim", [0.0, 0.0])
            e.hp = float(m.get("hp", e.hp or 0.0))
            e.max_hp = float(m.get("max_hp", e.max_hp or 1.0))
            e.weapons = list(m.get("weapons", e.weapons))
            e.charging = bool(m.get("charging", False))
            e.last_seen = self.t
            e.visible = True
        for eid in [i for i, e in self.enemies.items() if self.t - e.last_seen > MEMORY_S]:
            del self.enemies[eid]
        for e in self.enemies.values():
            dx, dy = mx - e.pos[0], my - e.pos[1]
            e.dist = math.hypot(dx, dy)
            # Does their torso point at us?  Tolerance grows as they get closer (we are 5 m wide).
            if e.visible and e.dist < 200.0:
                err = abs(wrap_angle(math.atan2(dy, dx) - e.aim[0]))
                e.aims_at_me = err < 0.14 + 3.0 / max(e.dist, 10.0)
            e.damaged_me_t = max((d["t"] for d in self.damage_log if d["by"] == e.id), default=-1e9)

    def _update_sounds(self, sounds: List[Dict[str, Any]]) -> None:
        self.sound_threats = [(float(s["bearing"]), 1.0 if s.get("dist") == "near" else 0.0)
                              for s in sounds if s.get("kind") in NEAR_SOUNDS and s.get("dist") == "near"]

    # ------------------------------------------------------------------ queries
    def mechs(self, visible_only: bool = False) -> List[Enemy]:
        """Tracked enemy mechs (not NPCs), nearest first."""
        out = [e for e in self.enemies.values() if not e.is_npc and (e.visible or not visible_only)]
        out.sort(key=lambda e: e.dist)
        return out

    def npcs(self) -> List[Enemy]:
        """Tracked NPC drones, nearest first."""
        out = [e for e in self.enemies.values() if e.is_npc]
        out.sort(key=lambda e: e.dist)
        return out

    def recent_damage(self, window: float) -> List[Dict[str, Any]]:
        """Non-zone damage events from the last ``window`` seconds."""
        return [d for d in self.damage_log if self.t - d["t"] <= window]

    def engagers(self) -> List[Enemy]:
        """Enemies that hit us in the last 3 s or are aiming at us from inside 150 m."""
        out = []
        for e in self.enemies.values():
            hit = self.t - e.damaged_me_t <= 3.0
            aimed = e.aims_at_me and e.dist < 150.0 and (not e.is_npc or e.dist < 60.0)
            if hit or aimed:
                out.append(e)
        out.sort(key=lambda e: e.dist)
        return out

    def unseen_attack_bearings(self) -> List[float]:
        """Bearings of recent damage from attackers we cannot currently place."""
        return [float(d["bearing"]) for d in self.recent_damage(2.0)
                if d["bearing"] is not None and (d["by"] not in self.enemies or not self.enemies[d["by"]].visible)]
