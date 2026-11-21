"""Mech Royale v1 placement bot (protocol ``mechfight/1``).

Placement is everything: kills only matter for cubes.  Policy, in priority order:

1. ``flee``  - something is hitting us / aiming at us: disengage, kite away, break line of sight,
               never trade damage (we face the attacker so hits land on the front armor).
2. ``zone``  - rotate with the zone and stay ahead of it; late game hold the zone edge facing inward.
3. ``avoid`` - an enemy is visible but not engaging: slip out of its line of sight, move quietly.
4. ``loot``  - only when safe (no visible threats, zone-safe): cubes and landed supply drops.
5. third-party shots - only when zone-safe, nobody aims at us, and the target is busy with someone else.

Everything is O(candidates x enemies) with simple geometry; a decision takes a few ms.
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mechfight as mf  # noqa: E402
from walls import Walls  # noqa: E402
from world import Enemy, World, Zone, unit, wrap_angle  # noqa: E402

N_DIRS = 16
STEP = 22.0                 # look-ahead distance for candidate headings (m)
STEALTH = 0.4               # move magnitude that stays under the 5 m/s step-sound threshold
SOFT_BUDGET_S = 0.020       # skip the expensive line-of-sight scoring past this
UPGRADE_PRIORITY = ["armor", "nanites", "plating", "servos", "jets", "capacitor", "sensors",
                    "reactor", "gyros"]


def aim_at(src: List[float], muzzle: float, target: List[float], vel: Optional[List[float]],
           speed: float) -> List[float]:
    """[yaw, pitch] to hit a moving target's centre of mass with a straight-flying bullet."""
    ox, oy, oz = src[0], src[1], src[2] + muzzle
    vx, vy, vz = (vel[0], vel[1], vel[2]) if vel else (0.0, 0.0, 0.0)
    flight = 0.0
    px, py, pz = target[0], target[1], target[2] + 4.5
    for _ in range(3):
        px, py, pz = target[0] + vx * flight, target[1] + vy * flight, target[2] + 4.5 + vz * flight
        flight = math.dist((ox, oy, oz), (px, py, pz)) / max(speed, 1.0)
    horiz = math.hypot(px - ox, py - oy)
    return [math.atan2(py - oy, px - ox), math.atan2(pz - oz, max(horiz, 1e-6))]


class Bot:
    """Heuristic placement bot."""

    name = "placement-v1"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello["config"]
        self.cfg = cfg
        self.world = World(cfg, int(hello["slot"]))
        self.walls = Walls.load(hello["map"]["dir"], hello["map"].get("files"))
        self.walk = float(cfg["mech"]["walk_speed"])
        self.pickup_r = float(cfg["cubes"].get("pickup_radius", 3.5)) if "cubes" in cfg else 3.5
        self.last_k = -1
        self.flee_until = -1.0
        self.boosting = False
        self.trail: Deque[Tuple[float, float, float]] = deque(maxlen=20)
        self.stuck_until = -1.0
        self.stuck_sign = 1.0
        self.mode = "hold"
        self.target_loot: Optional[Tuple[float, float]] = None
        self.mode_loot_quiet = True

    # ------------------------------------------------------------------ main entry
    def act(self, obs: Dict[str, Any]) -> Dict[str, Any]:
        """Decide one action for this observation."""
        t0 = time.perf_counter()
        w = self.world
        w.update(obs)
        x, y, z = w.pos
        zone = w.zone
        assert zone is not None
        act: Dict[str, Any] = {}

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])

        engagers = w.engagers()
        unseen = w.unseen_attack_bearings()
        under_fire = bool(engagers) or bool(unseen)
        if under_fire:
            self.flee_until = w.t + 2.0
        fleeing = w.t < self.flee_until

        goal, late, urgent, travel_s = self._zone_goal(zone)
        exposed = self._exposed_threats(t0)
        visible_mechs = w.mechs(visible_only=True)
        near_npcs = [n for n in w.npcs() if n.visible and n.dist < 55.0]
        safe = (not fleeing and not visible_mechs and not near_npcs and w.in_zone
                and w.t - max((d["t"] for d in w.damage_log), default=-1e9) > 4.0)

        move = (0.0, 0.0)
        mag = 0.0
        want_boost = False
        face: Optional[List[float]] = None
        fire = False

        if fleeing:
            self.mode = "flee"
            away = self._away_vector(engagers, unseen)
            gv = unit(goal[0] - x, goal[1] - y) if goal else (0.0, 0.0)
            mv = self._choose(gv, 0.8 if goal else 0.0, away, 2.0, 2.5, exposed or engagers, t0,
                              strafe=True)
            move, mag = mv, 1.0
            want_boost = self.energy_ok(25.0)
            face = self._face_for_flee(engagers, unseen)
        elif goal is not None:
            self.mode = "zone"
            gv = unit(goal[0] - x, goal[1] - y)
            away = self._away_vector(w.mechs(), [])
            move = self._choose(gv, 2.0, away, 0.7 if visible_mechs else 0.0, 0.6, exposed, t0)
            gd = math.hypot(goal[0] - x, goal[1] - y)
            slack = self._slack(zone, travel_s)
            mag = 1.0 if (urgent or slack < 12.0 or exposed) else STEALTH
            if gd < 4.0:
                mag = 0.0
            want_boost = (not w.in_zone or urgent) and self.energy_ok(15.0)
        elif exposed:
            self.mode = "avoid"
            away = self._away_vector(exposed, [])
            move = self._choose((0.0, 0.0), 0.0, away, 1.0, 2.5, exposed, t0)
            aimed = any(e.aims_at_me for e in exposed)
            mag = 1.0 if aimed else STEALTH
        elif safe and self._pick_loot(zone):
            self.mode = "loot"
            assert self.target_loot is not None
            lv = unit(self.target_loot[0] - x, self.target_loot[1] - y)
            move = self._choose(lv, 2.0, (0.0, 0.0), 0.0, 0.0, [], t0)
            mag = STEALTH if self.mode_loot_quiet else 1.0
        else:
            self.mode = "hold"

        # ---- third-party shots: never while fleeing, only when zone-safe and nobody aims at us
        target = self._third_party_target(zone) if (not fleeing and not engagers) else None
        if target is not None:
            gun_idx = self._gun_slot()
            if gun_idx is not None:
                face = aim_at(w.pos, float(w.stats.get("muzzle_height", 7.0)), target.pos, target.vel,
                              w.gun_speed)
                err = abs(wrap_angle(face[0] - w.aim[0]))
                ammo = (w.weapons[gun_idx] or {}).get("ammo", 0)
                if err < 0.1 and ammo > 0 and target.dist < 140.0:
                    fire = True
                    act["fire"] = [gun_idx == 0, gun_idx == 1]

        # ---- stuck handling: pushing against something for 1.5 s -> hop and sidestep
        jump = False
        self.trail.append((w.t, x, y))
        if mag >= 0.9 and move != (0.0, 0.0):
            if w.t >= self.stuck_until and len(self.trail) >= 15 and w.t - self.trail[0][0] >= 1.4:
                if math.hypot(x - self.trail[0][1], y - self.trail[0][2]) < 2.5:
                    self.stuck_until = w.t + 1.0
                    self.stuck_sign = -self.stuck_sign
                    self.trail.clear()
            if w.t < self.stuck_until:
                move = (-move[1] * self.stuck_sign, move[0] * self.stuck_sign)
                jump = bool(obs["self"].get("grounded", True))
                want_boost = want_boost or self.energy_ok(15.0)

        boost = self._update_boost(want_boost and mag > 0.0)

        mx, my = move[0] * mag, move[1] * mag
        norm = math.hypot(mx, my)
        if norm > 0.9998:  # keep the vector length <= 1 even after rounding
            mx, my = mx / norm * 0.9998, my / norm * 0.9998
        act["move"] = [round(mx, 4), round(my, 4)]
        act["boost"] = boost
        if jump:
            act["jump"] = True

        # ---- aim: face the attacker when fleeing, else where we are going / the zone centre
        if face is None:
            if mag > 0.0 and move != (0.0, 0.0):
                face = [math.atan2(move[1], move[0]), 0.0]
            else:
                inward = math.atan2(zone.ncy - y, zone.ncx - x)
                face = [inward + 0.7 * math.sin(w.t * 0.9), 0.0]  # sweep to widen the 120 deg view
        act["aim"] = [round(face[0], 4), round(max(-0.7, min(1.3, face[1])), 4)]

        # ---- reload when calm
        gi = self._gun_slot()
        if gi is not None and not fire and not fleeing:
            g = w.weapons[gi] or {}
            mag_size = float(g.get("stats", {}).get("mag", 12))
            if g.get("ammo", mag_size) < mag_size * 0.5 and not g.get("reload", 0):
                act["reload"] = True

        self._debug(goal, engagers, t0, fire)
        return act

    # ------------------------------------------------------------------ small helpers
    def energy_ok(self, floor: float) -> bool:
        """Energy above ``floor`` (and plenty left once we started boosting)."""
        return self.world.energy >= (floor if not self.boosting else 5.0)

    def _update_boost(self, wanted: bool) -> bool:
        self.boosting = bool(wanted and self.world.energy > 3.0)
        return self.boosting

    def _gun_slot(self) -> Optional[int]:
        for i, wp in enumerate(self.world.weapons):
            if wp and wp.get("kind") == "gun":
                return i
        return None

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        ids = [c["id"] for c in offer.get("choices", [])]
        for want in UPGRADE_PRIORITY:
            if want in ids:
                return ids.index(want)
        return 0

    # ------------------------------------------------------------------ zone logic
    def _zone_goal(self, zone: Zone) -> Tuple[Optional[Tuple[float, float]], bool, bool, float]:
        """Where to stand to be safe: (goal or None to hold, late_game, urgent, travel_seconds).

        Before the next shrink we hold if the current circle leaves plenty of margin; once travel
        time to the next circle's interior (plus slack) reaches the shrink start we go, so we arrive
        ahead of the wall.  Late game we sit on the edge (~80% of the radius) on our own side.
        """
        w = self.world
        x, y = w.pos[0], w.pos[1]
        t = w.t
        late = zone.nr <= 140.0
        d1 = math.hypot(x - zone.ncx, y - zone.ncy)
        inner = 0.75 * zone.nr
        travel_s = max(0.0, d1 - inner) / self.walk
        lead = travel_s * 1.3 + 8.0
        moving = t >= zone.shrink_start - lead
        cur = zone.at(t)
        cur_margin = Zone.margin(x, y, cur)
        nxt_margin = zone.nr - d1
        urgent = (not w.in_zone) or (t >= zone.shrink_start - 3.0 and nxt_margin < 0.0) \
            or cur_margin < 0.0
        if not moving and not late and cur_margin > 25.0 and w.in_zone:
            return None, late, False, travel_s
        # the hold band: already well placed in the next circle
        if late:
            lo, hi = 0.6 * zone.nr, 0.88 * zone.nr
            if lo <= d1 <= hi and nxt_margin > 6.0:
                return None, late, urgent, travel_s
        elif d1 <= 0.8 * zone.nr and nxt_margin > 15.0:
            return None, late, urgent, travel_s
        ux, uy = unit(x - zone.ncx, y - zone.ncy)
        if ux == 0.0 and uy == 0.0:
            ux, uy = unit(zone.cx - zone.ncx, zone.cy - zone.ncy)
            if ux == 0.0 and uy == 0.0:
                ux = 1.0
        rad = (0.8 if late else 0.65) * zone.nr
        return (zone.ncx + ux * rad, zone.ncy + uy * rad), late, urgent, travel_s

    def _slack(self, zone: Zone, travel_s: float) -> float:
        """Seconds to spare before we must be inside the next circle."""
        t = self.world.t
        if t < zone.shrink_start:
            return zone.shrink_start - t - travel_s
        return max(0.0, (zone.shrink_end - t) * 0.7) - travel_s

    # ------------------------------------------------------------------ threats
    def _exposed_threats(self, t0: float) -> List[Enemy]:
        """Visible enemy mechs (<=160 m) that actually have line of sight to us per the map."""
        w = self.world
        out: List[Enemy] = []
        for e in w.mechs(visible_only=True):
            if e.dist > 160.0:
                continue
            if time.perf_counter() - t0 < SOFT_BUDGET_S and self.walls.los_blocked(e.pos, w.pos):
                continue
            out.append(e)
        return out[:4]

    def _away_vector(self, enemies: List[Enemy], bearings: List[float]) -> Tuple[float, float]:
        """Normalised direction pointing away from threats, nearer ones weigh more."""
        w = self.world
        ax = ay = 0.0
        for e in enemies:
            ux, uy = unit(w.pos[0] - e.pos[0], w.pos[1] - e.pos[1])
            wt = 0.3 + max(0.0, 1.0 - e.dist / 160.0) + (1.0 if e.aims_at_me else 0.0)
            ax += ux * wt
            ay += uy * wt
        for b in bearings:
            ax -= math.cos(b)
            ay -= math.sin(b)
        for b, _ in w.sound_threats:
            ax -= 0.4 * math.cos(b)
            ay -= 0.4 * math.sin(b)
        n = math.hypot(ax, ay)
        return (ax / n, ay / n) if n > 1e-9 else (0.0, 0.0)

    def _face_for_flee(self, engagers: List[Enemy], bearings: List[float]) -> Optional[List[float]]:
        """Face the attacker (front arc takes 1x damage, rear 1.5x)."""
        w = self.world
        if engagers:
            e = engagers[0]
            return [math.atan2(e.pos[1] - w.pos[1], e.pos[0] - w.pos[0]), 0.0]
        if bearings:
            return [bearings[-1], 0.0]
        return None

    # ------------------------------------------------------------------ movement scoring
    def _choose(self, goal_v: Tuple[float, float], w_goal: float, away: Tuple[float, float],
                w_away: float, w_los: float, los_enemies: List[Enemy], t0: float,
                strafe: bool = False) -> Tuple[float, float]:
        """Pick the best of N_DIRS headings: toward goal, away from threats, out of sight, in zone."""
        w = self.world
        x, y, z = w.pos
        zone = w.zone
        assert zone is not None
        circle = zone.at(w.t + 4.0)
        circle_next = (zone.ncx, zone.ncy, zone.nr)
        perp = (-away[1], away[0])
        sgn = 1.0 if int(w.t / 0.8) % 2 == 0 else -1.0
        check_los = bool(los_enemies) and w_los > 0.0
        best: Optional[Tuple[float, int, Tuple[float, float]]] = None
        for k in range(N_DIRS):
            ang = 2.0 * math.pi * k / N_DIRS
            dx, dy = math.cos(ang), math.sin(ang)
            ex, ey = x + dx * STEP, y + dy * STEP
            if self.walls.blocked(x, y, ex, ey, z + 1.2, z + 9.0):
                continue
            s = w_goal * (dx * goal_v[0] + dy * goal_v[1]) + w_away * (dx * away[0] + dy * away[1])
            if strafe:
                s += 0.5 * sgn * (dx * perp[0] + dy * perp[1])
            m = min(Zone.margin(ex, ey, circle), Zone.margin(ex, ey, circle_next) + 15.0)
            if m < 8.0:
                s -= 3.0
            elif m < 30.0:
                s -= 1.0
            if check_los and time.perf_counter() - t0 < SOFT_BUDGET_S:
                hidden = all(self.walls.los_blocked(e.pos, [ex, ey, z]) for e in los_enemies[:3])
                if hidden:
                    s += w_los
            if k == self.last_k:
                s += 0.2
            if best is None or s > best[0]:
                best = (s, k, (dx, dy))
        if best is None:  # boxed in: just push toward the goal / away
            self.last_k = -1
            return goal_v if goal_v != (0.0, 0.0) else away
        self.last_k = best[1]
        return best[2]

    # ------------------------------------------------------------------ loot
    def _pick_loot(self, zone: Zone) -> bool:
        """Choose a safe nearby cube or landed supply drop; sets ``target_loot``."""
        w = self.world
        x, y = w.pos[0], w.pos[1]
        npcs = [n for n in w.npcs()]
        best: Optional[Tuple[float, Tuple[float, float], bool]] = None

        def ok(px: float, py: float) -> bool:
            if Zone.margin(px, py, (zone.ncx, zone.ncy, zone.nr)) < 12.0:
                return False
            return all(math.hypot(px - n.pos[0], py - n.pos[1]) > 50.0 for n in npcs)

        for c in w.cube_list:
            px, py = c["pos"][0], c["pos"][1]
            d = math.hypot(px - x, py - y)
            if d < 45.0 and ok(px, py) and (best is None or d < best[0]):
                best = (d, (px, py), True)
        for dr in w.drops:
            px, py = dr["pos"][0], dr["pos"][1]
            d = math.hypot(px - x, py - y)
            arrives = d / self.walk + 1.0
            if d < 150.0 and dr.get("lands_in", 99.0) <= arrives and dr.get("holder") in (None, w.slot) \
                    and ok(px, py):
                score = d * 0.4  # a supply drop is worth 3 cubes + full heal: prefer it
                if best is None or score < best[0]:
                    best = (score, (px, py), False)
        if best is None:
            self.target_loot = None
            return False
        self.target_loot = best[1]
        self.mode_loot_quiet = best[2]
        return True

    # ------------------------------------------------------------------ third-party
    def _third_party_target(self, zone: Zone) -> Optional[Enemy]:
        """A visible mech aiming at someone else, 40-140 m away, while we are zone-safe and healthy."""
        w = self.world
        if not w.in_zone or w.hp < 0.6 * w.max_hp:
            return None
        if Zone.margin(w.pos[0], w.pos[1], (zone.ncx, zone.ncy, zone.nr)) < 20.0:
            return None
        mechs = w.mechs(visible_only=True)
        if any(e.aims_at_me for e in mechs) or len(mechs) > 3:
            return None
        if self._gun_slot() is None:
            return None
        for e in mechs:
            if not (40.0 < e.dist < 140.0):
                continue
            if self._busy_with_other(e, mechs):
                return e
        return None

    def _busy_with_other(self, e: Enemy, mechs: List[Enemy]) -> bool:
        """True if ``e`` aims at another visible mech (within ~9 degrees) instead of at us."""
        for o in mechs:
            if o.id == e.id:
                continue
            dx, dy = o.pos[0] - e.pos[0], o.pos[1] - e.pos[1]
            d = math.hypot(dx, dy)
            if d < 150.0 and abs(wrap_angle(math.atan2(dy, dx) - e.aim[0])) < 0.16:
                return True
        return False

    # ------------------------------------------------------------------ debug
    def _debug(self, goal: Optional[Tuple[float, float]], engagers: List[Enemy], t0: float,
               fire: bool) -> None:
        w = self.world
        mf.draw.label(f"{self.mode} hp{w.hp:.0f} e{w.energy:.0f}{' FIRE' if fire else ''}")
        if goal is not None:
            mf.draw.point([goal[0], goal[1], w.pos[2]], "#40c0ff", 6)
        for e in engagers[:4]:
            mf.draw.line([w.pos[0], w.pos[1], w.pos[2] + 7], [e.pos[0], e.pos[1], e.pos[2] + 4.5], "#ff5040")


if __name__ == "__main__":
    mf.run(Bot)
