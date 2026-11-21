"""Static-map geometry for the bot: 2D segment tests against the building prisms of ``v2/world.json``.

Only what the heuristics need: "is this walking path blocked by a building?" and "does a building
break the line of sight between two points?".  Terrain slope and trees are ignored (trees block
vision only, and terrain is gentle on campus).  Standard library only; if the map cannot be read
the object is empty and every query answers "not blocked", so the bot degrades to open-field play.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Set, Tuple

# (min_x, min_y, max_x, max_y, base, top, rings) -- rings holds the outline and any holes (flat lists).
Prism = Tuple[float, float, float, float, float, float, List[List[float]]]

CELL = 32.0
MAX_CELLS = 400  # safety cap on cells scanned per query


def _ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(ax: float, ay: float, bx: float, by: float,
                    cx: float, cy: float, dx: float, dy: float) -> bool:
    """True if segment AB properly crosses (or touches) segment CD."""
    d1 = _ccw(ax, ay, bx, by, cx, cy)
    d2 = _ccw(ax, ay, bx, by, dx, dy)
    d3 = _ccw(cx, cy, dx, dy, ax, ay)
    d4 = _ccw(cx, cy, dx, dy, bx, by)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _hits_ring(ax: float, ay: float, bx: float, by: float, ring: List[float]) -> bool:
    n = len(ring) // 2
    for i in range(n):
        j = (i + 1) % n
        if _segments_cross(ax, ay, bx, by, ring[2 * i], ring[2 * i + 1], ring[2 * j], ring[2 * j + 1]):
            return True
    return False


class Walls:
    """Spatially bucketed building prisms with segment-blocking queries."""

    def __init__(self, prisms: Optional[List[Prism]] = None) -> None:
        self.prisms: List[Prism] = prisms or []
        self.grid: Dict[Tuple[int, int], List[int]] = {}
        for idx, p in enumerate(self.prisms):
            for cx in range(int(p[0] // CELL), int(p[2] // CELL) + 1):
                for cy in range(int(p[1] // CELL), int(p[3] // CELL) + 1):
                    self.grid.setdefault((cx, cy), []).append(idx)

    @classmethod
    def load(cls, map_dir: str, files: Optional[List[str]] = None) -> "Walls":
        """Load ``world.json`` from the hello's map dir; return an empty map on any problem."""
        rel = next((f for f in (files or []) if f.endswith("world.json")), "v2/world.json")
        try:
            with open(os.path.join(map_dir, rel), "r", encoding="utf-8") as fh:
                world = json.load(fh)
            prisms: List[Prism] = []
            for pr in world.get("prisms", []):
                if pr.get("kind") == "bridge":
                    continue  # bridges are walked and shot under
                rings = [pr["ring"]] + list(pr.get("holes") or [])
                xs = pr["ring"][0::2]
                ys = pr["ring"][1::2]
                prisms.append((min(xs), min(ys), max(xs), max(ys), float(pr["base"]), float(pr["top"]), rings))
            return cls(prisms)
        except (OSError, ValueError, KeyError, TypeError):
            return cls()

    def _candidates(self, ax: float, ay: float, bx: float, by: float) -> Set[int]:
        x0, x1 = sorted((int(ax // CELL), int(bx // CELL)))
        y0, y1 = sorted((int(ay // CELL), int(by // CELL)))
        out: Set[int] = set()
        if (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_CELLS:
            return out
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                out.update(self.grid.get((cx, cy), ()))
        return out

    def blocked(self, ax: float, ay: float, bx: float, by: float, z_lo: float, z_hi: float) -> bool:
        """True if a building overlapping heights [z_lo, z_hi] crosses the 2D segment A->B.

        Walking uses ``z_lo`` = feet + step_up and ``z_hi`` = feet + height; a roof you stand on is
        therefore ignored.  Sight lines pass heights around the mechs' bodies.
        """
        lo_x, hi_x = min(ax, bx), max(ax, bx)
        lo_y, hi_y = min(ay, by), max(ay, by)
        for idx in self._candidates(ax, ay, bx, by):
            mnx, mny, mxx, mxy, base, top, rings = self.prisms[idx]
            if top <= z_lo or base >= z_hi:
                continue
            if mxx < lo_x or mnx > hi_x or mxy < lo_y or mny > hi_y:
                continue
            for ring in rings:
                if _hits_ring(ax, ay, bx, by, ring):
                    return True
        return False

    def los_blocked(self, a: List[float], b: List[float]) -> bool:
        """True if a building probably blocks the sight line between two world points (feet coords)."""
        lo = min(a[2], b[2]) + 4.5
        hi = max(a[2], b[2]) + 8.0
        return self.blocked(a[0], a[1], b[0], b[1], lo, hi)
