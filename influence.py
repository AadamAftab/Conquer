"""
influence.py — Tactical influence map (Dave Mark, Game AI Pro 2) over the nav grid.

STATIC layer, computed once at startup: for every walkable cell, how much of its
neighbourhood is solid. A cell beside a building corner has a high cover score: one
step breaks line of sight.

DYNAMIC layer, evaluated on demand (only the handful of cells a query could pick):
threat around each known enemy — a mech holding lightning is lethal inside ~50 m, any
mech is dangerous inside ~60 m — and how many enemies can actually SEE the cell.

TACTICAL queries built from the two:
  hide     — nearest cell inside the safe zone that nobody can see (regen needs 5 s unhurt)
  isolate  — a corner where exactly our target can see us and the rest of the squad
             cannot (turns a 1-vs-N into a string of 1-vs-1s)
"""

from __future__ import annotations

import math
import time
from typing import List, Optional, Tuple

import config as C


class InfluenceMap:
    def __init__(self, grid, world) -> None:
        self.grid = grid
        self.world = world
        self.ok = bool(grid is not None and getattr(grid, "ok", False))
        self.cover: List[int] = []
        if self.ok:
            self._build_cover()

    # ------------------------------------------------------------------
    def _build_cover(self) -> None:
        g = self.grid
        nx, ny, free = g.nx, g.ny, g.free
        cover = [0] * (nx * ny)
        for j in range(1, ny - 1):
            row = j * nx
            for i in range(1, nx - 1):
                idx = row + i
                if free[idx] != 1:
                    continue
                ortho = 4 - (free[idx - 1] + free[idx + 1] + free[idx - nx] + free[idx + nx])
                diag = 4 - (free[idx - nx - 1] + free[idx - nx + 1] + free[idx + nx - 1] + free[idx + nx + 1])
                cover[idx] = ortho * 2 + diag          # orthogonal walls count double; 0..12
        self.cover = cover

    def cover_at(self, x: float, y: float) -> float:
        """Static cover score in [0, 1] of the cell containing (x, y)."""
        if not self.ok:
            return 0.0
        i, j = self.grid.cell_of(x, y)
        return min(1.0, self.cover[j * self.grid.nx + i] / 6.0)

    # ------------------------------------------------------------------
    def _threat(self, x: float, y: float, enemies: List[dict]) -> float:
        """Dynamic threat at a point: proximity to each enemy, doubled inside a lightning holder's reach."""
        th = 0.0
        for e in enemies:
            d = math.hypot(e["pos"][0] - x, e["pos"][1] - y)
            if d < 60.0:
                th += (60.0 - d) / 60.0
            if "lightning" in (e.get("weapons") or []) and d < C.LIGHTNING_KEEPOUT_M:
                th += 1.0
        return th

    def best_cell(self, pos, enemies: List[dict], zone: dict, mode: str = "hide",
                  target_id: Optional[int] = None, max_r: float = 40.0,
                  k_candidates: int = 14, max_enemies: int = 3,
                  budget_s: float = 0.008) -> Optional[Tuple[float, float]]:
        """
        mode "hide":    fewest enemies with sight of the cell (0 is the prize), near, covered, safe.
        mode "isolate": exactly the target sees the cell, nobody else does.
        Returns the cell centre or None (no grid, no candidates, or out of time).
        """
        if not self.ok or self.world is None or not enemies:
            return None
        g, w = self.grid, self.world
        t_end = time.perf_counter() + budget_s
        sx, sy = pos[0], pos[1]
        si, sj = g.cell_of(sx, sy)
        start = g.nearest_free(si, sj)
        if start is None:
            return None
        region = g.comp[start[1] * g.nx + start[0]]
        R = max(1, int(max_r / g.cell))
        zc, zr = zone["next_center"], zone["next_radius"]
        foes = sorted(enemies, key=lambda e: math.hypot(e["pos"][0] - sx, e["pos"][1] - sy))[:max_enemies]

        cands = []
        for dj in range(-R, R + 1):
            for di in range(-R, R + 1):
                i, j = start[0] + di, start[1] + dj
                if not g.is_free(i, j) or g.comp[j * g.nx + i] != region:
                    continue
                cx, cy = g.center(i, j)
                d = math.hypot(cx - sx, cy - sy)
                if d > max_r or math.hypot(cx - zc[0], cy - zc[1]) > zr - 10.0:
                    continue
                cov = self.cover[j * g.nx + i]
                if cov <= 0 and mode == "hide":
                    continue                    # open ground never hides anyone
                cands.append((cov * 10.0 - d - 12.0 * self._threat(cx, cy, foes), cx, cy, d, cov))
        if not cands:
            return None
        cands.sort(reverse=True)

        best, best_s = None, -1e18
        sight = getattr(w, "has_sight", w.has_los_2d)
        for pre, cx, cy, d, cov in cands[:k_candidates]:
            if time.perf_counter() > t_end:
                break
            cz = w.terrain_height(cx, cy)
            sight_ids = []
            for e in foes:
                if sight(cx, cy, cz + 4.5, e["pos"][0], e["pos"][1], e["pos"][2] + 8.0, step=C.MRM_LOS_STEP_M):
                    sight_ids.append(e["id"])
            n = len(sight_ids)
            if mode == "isolate":
                only_target = (n == 1 and sight_ids[0] == target_id)
                score = (60.0 if only_target else -45.0 * max(0, n - 1) - (30.0 if n == 0 else 0.0))
            else:
                score = 120.0 if n == 0 else -80.0 * n
            score += 6.0 * cov - 0.8 * d - 15.0 * self._threat(cx, cy, foes)
            if score > best_s:
                best, best_s = (cx, cy), score
        return best
