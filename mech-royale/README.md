# Mech Royale v1 placement bot

Heuristic bot for the Builders Buildathon *Mech Royale* track (protocol `mechfight/1`).
Standard library only, fully offline. Everything lives in this directory.

## Run the checks

From the repository root:

```sh
# protocol conformance (the kit's check, replaying a recorded 300-tick stream)
python3 mech-royale/check/check.py -- python3 -u mech-royale/bot.py

# behaviour scenarios the recorded stream doesn't cover (flee, zone, loot, third-party, timing)
python3 mech-royale/selftest.py
```

Both pass. Decisions take about 1 ms on the dev machine (budget: 50 ms).

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | Policy (entry point) |
| `world.py` | World model: self, zone interpolation/timing, tracked enemies, damage log, engagement detection |
| `walls.py` | Building-prism geometry from `arena/v2/world.json` for path-blocking and line-of-sight tests |
| `mechfight.py`, `mechfight.schema.json`, `protocol.md`, `check/`, `arena/` | Copied unchanged from the starter kit |
| `selftest.py` | Scenario tests |
| `Dockerfile` | Packaging for submission (see below) |

## Policy (placement first)

1. **Flee:** if something hit us in the last 3 s or is aiming at us (inside 150 m), disengage for at least 2 s.
   Pick the heading that points away, strafes, breaks line of sight behind a building and stays in the zone,
   boost when energy allows, and face the attacker so hits land on the front armor. No return fire.
2. **Zone:** hold while the current circle has margin. Start rotating early enough to be inside the *next* circle
   before the shrink starts. Late game (next radius <= 140 m) take position at about 80% of the radius on our own side,
   facing the centre with a small sweep.
3. **Avoid:** an enemy with line of sight that isn't engaging: step out of its sight, moving at 0.4 speed
   (under the 5 m/s step-sound threshold) unless it is aiming at us.
4. **Loot:** only with no visible mech or nearby NPC, in the zone, and no damage for 4 s. Cubes within 45 m and landed
   (or about to land) supply drops. The target must be inside the next circle and 50 m from any drone.
5. **Third-party shots:** gun only, only when zone-safe, HP >= 60%, nobody aims at us, and the target (40-140 m)
   is aiming at another visible mech.

It also picks upgrades by a fixed priority (armor, nanites, plating, ...), reloads when calm, and un-sticks itself
by hopping and sidestepping after 1.5 s without progress. Debug drawings show mode, zone goal and attackers.

## Left unfinished

- **Weapon pickups are ignored.** Only the gun is used; there is no mortar, laser or lightning logic.
- **No terrain model:** `terrain.i16` is not read. Buildings are handled; steep slopes and ledges taller than
  1.2 m are not (only the stuck hop). Trees, bridges and holes' walkability are ignored.
- **No projectile dodging:** incoming mortar shells and gun bullets aren't tracked. Fleeing and strafing is all we do.
- **Cover search is coarse:** 16 headings, 22 m look-ahead, flat-ground assumption. It can oscillate in tight spots.
- **Crates and NPC farming are ignored.** No deliberate cube hunting beyond what is in view.
- **Zone endgame:** the last circles (radius 25 -> 0) aren't specially handled.
- **Never played in a real match.** The conformance stream doesn't react to our actions, so thresholds (hold band, lead time,
  stealth speed, engagement cones) are untuned guesses.
- The `Dockerfile` is untested here (no Docker); the submission also needs it at the top of a public repo, and the match
  runs with the map mounted at `/arena`.
