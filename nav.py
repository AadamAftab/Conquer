"""
nav.py — Coarse walkability grid + time-budgeted A* path planner.

NavGrid
    Built once at startup from WorldIndex: the map is cut into NAV_CELL_M
    cells; a cell is free if a mech (radius inflated) can stand there at
    ground level (no prism at step/head height, slope walkable).

PathFollower
    .direction(pos, goal, t, budget_s) -> (dx, dy)
    Plans with A* (8-connected, no corner cutting) under a strict time and
    node budget, keeps the path between ticks, replans when the goal moves,
    the path goes stale, or we stray from it. If the budget runs out, it
    follows the partial path toward the most promising node and replans soon.
    Falls back to a straight line when the grid is unavailable.
"""

from __future__ import annotations

import heapq
import math
import time
from typing import List, Optional, Tuple

import config as C

_SQRT2 = math.sqrt(2.0)
_NEIGH = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
          (1, 1, _SQRT2), (1, -1, _SQRT2), (-1, 1, _SQRT2), (-1, -1, _SQRT2)]


class NavGrid:
    def __init__(self, world, cell: float = None) -> None:
        self.cell = cell or C.NAV_CELL_M
        self.ok = False
        if world is None or getattr(world, "_terrain", None) is None:
            return
        b = getattr(world, "bounds", None)
        if not b:
            return
        self.x0 = float(b["minX"])
        self.y0 = float(b["minY"])
        self.nx = max(1, int((float(b["maxX"]) - self.x0) / self.cell))
        self.ny = max(1, int((float(b["maxY"]) - self.y0) / self.cell))
        self.free = bytearray(self.nx * self.ny)

        r = C.MECH_RADIUS + 0.3
        offs = [(0.0, 0.0), (r, 0.0), (-r, 0.0), (0.0, r), (0.0, -r)]
        pip = world.point_in_prism
        th = world.terrain_height
        for j in range(self.ny):
            cy = self.y0 + (j + 0.5) * self.cell
            row = j * self.nx
            for i in range(self.nx):
                cx = self.x0 + (i + 0.5) * self.cell
                z = th(cx, cy)
                ok = True
                for ox, oy in offs:
                    px, py = cx + ox, cy + oy
                    if pip(px, py, z + 1.5) or pip(px, py, z + 8.5):
                        ok = False
                        break
                if ok:
                    # slope check (same rule as WorldIndex.is_walkable)
                    gx = (th(cx + 1.0, cy) - th(cx - 1.0, cy)) * 0.5
                    gy = (th(cx, cy + 1.0) - th(cx, cy - 1.0)) * 0.5
                    if gx * gx + gy * gy > 1.42:
                        ok = False
                self.free[row + i] = 1 if ok else 0
        # the outer ring is the arena wall
        for i in range(self.nx):
            self.free[i] = 0
            self.free[(self.ny - 1) * self.nx + i] = 0
        for j in range(self.ny):
            self.free[j * self.nx] = 0
            self.free[j * self.nx + self.nx - 1] = 0
        self.ok = True

    # ------------------------------------------------------------------
    def cell_of(self, x: float, y: float) -> Tuple[int, int]:
        i = int((x - self.x0) / self.cell)
        j = int((y - self.y0) / self.cell)
        return max(0, min(self.nx - 1, i)), max(0, min(self.ny - 1, j))

    def center(self, i: int, j: int) -> Tuple[float, float]:
        return self.x0 + (i + 0.5) * self.cell, self.y0 + (j + 0.5) * self.cell

    def is_free(self, i: int, j: int) -> bool:
        return 0 <= i < self.nx and 0 <= j < self.ny and self.free[j * self.nx + i] == 1

    def nearest_free(self, i: int, j: int, max_r: int = 6) -> Optional[Tuple[int, int]]:
        if self.is_free(i, j):
            return i, j
        for r in range(1, max_r + 1):
            best = None
            for di in range(-r, r + 1):
                for dj in (-r, r):
                    for a, b in ((i + di, j + dj), (i + dj, j + di)):
                        if self.is_free(a, b):
                            d = (a - i) ** 2 + (b - j) ** 2
                            if best is None or d < best[0]:
                                best = (d, a, b)
            if best is not None:
                return best[1], best[2]
        return None

    def line_free(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """True if every cell along the segment is free (sampled at half a cell)."""
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / (self.cell * 0.5)))
        for s in range(1, n + 1):
            t = s / n
            i, j = self.cell_of(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if self.free[j * self.nx + i] != 1:
                return False
        return True

    # ------------------------------------------------------------------
    def astar(self, start: Tuple[int, int], goal: Tuple[int, int],
              budget_s: float, max_expand: int) -> Tuple[List[Tuple[int, int]], bool]:
        """Returns (cells from start to goal-or-best, reached_goal)."""
        nx = self.nx
        free = self.free
        gi, gj = goal
        s_idx = start[1] * nx + start[0]
        g_idx = gj * nx + gi

        def h(i, j):
            dx, dy = abs(i - gi), abs(j - gj)
            return (dx + dy) + (_SQRT2 - 2.0) * min(dx, dy)

        deadline = time.perf_counter() + budget_s
        g = {s_idx: 0.0}
        came = {}
        heap = [(h(*start), 0.0, s_idx)]
        best_idx, best_h = s_idx, h(*start)
        expanded = 0
        while heap:
            f, gc, idx = heapq.heappop(heap)
            if gc > g.get(idx, 1e18):
                continue
            if idx == g_idx:
                best_idx = idx
                break
            expanded += 1
            if expanded > max_expand or (expanded & 31 == 0 and time.perf_counter() > deadline):
                break
            i, j = idx % nx, idx // nx
            hh = h(i, j)
            if hh < best_h:
                best_h, best_idx = hh, idx
            for di, dj, cost in _NEIGH:
                a, b = i + di, j + dj
                if not (0 <= a < nx and 0 <= b < self.ny):
                    continue
                n_idx = b * nx + a
                if free[n_idx] != 1:
                    continue
                if di and dj and (free[j * nx + a] != 1 or free[b * nx + i] != 1):
                    continue   # no corner cutting
                ng = gc + cost
                if ng < g.get(n_idx, 1e18):
                    g[n_idx] = ng
                    came[n_idx] = idx
                    heapq.heappush(heap, (ng + h(a, b), ng, n_idx))

        reached = best_idx == g_idx
        path = []
        idx = best_idx
        while idx != s_idx and idx in came:
            path.append((idx % nx, idx // nx))
            idx = came[idx]
        path.reverse()
        return path, reached


class PathFollower:
    """
    Goal-rooted incremental A*.

    The search grows a tree of parent pointers outward FROM THE GOAL toward
    us, and it is kept between ticks while the goal stays put. Each tick
    resumes it under a time budget; as soon as our current cell is in the
    tree, the route is just "follow parents to the goal" — valid from any
    cell the tree already covers, so moving along (or being knocked off) the
    path costs nothing. Long routes simply finish over a few ticks instead
    of being restarted from scratch.
    """

    def __init__(self, grid: Optional[NavGrid]) -> None:
        self.grid = grid if (grid is not None and grid.ok) else None
        self.path: List[Tuple[float, float]] = []    # next few waypoints (for debugging)
        self.goal: Optional[Tuple[float, float]] = None
        self._goal_idx: Optional[int] = None
        self._heap: list = []
        self._g: dict = {}
        self._parent: dict = {}
        self._closed: set = set()
        self._exhausted = False

    def reset(self) -> None:
        self.path = []
        self.goal = None
        self._goal_idx = None

    def _restart(self, goal: Tuple[float, float], goal_idx: int) -> None:
        self.goal = goal
        self._goal_idx = goal_idx
        self._heap = [(0.0, 0.0, goal_idx)]
        self._g = {goal_idx: 0.0}
        self._parent = {goal_idx: -1}
        self._closed = set()
        self._exhausted = False

    def _search(self, start_idx: int, budget_s: float) -> bool:
        """Grow the goal-rooted tree until it covers start_idx or time runs out."""
        if start_idx in self._closed:
            return True
        grid = self.grid
        nx, ny, free = grid.nx, grid.ny, grid.free
        si, sj = start_idx % nx, start_idx // nx
        heap, g, parent, closed = self._heap, self._g, self._parent, self._closed
        push, pop = heapq.heappush, heapq.heappop
        k = _SQRT2 - 2.0
        deadline = time.perf_counter() + budget_s
        n = 0
        while heap:
            _, gc, idx = pop(heap)
            if idx in closed or gc > g.get(idx, 1e18):
                continue
            closed.add(idx)
            if idx == start_idx:
                return True
            n += 1
            if (n & 15) == 0 and time.perf_counter() > deadline:
                return False
            i, j = idx % nx, idx // nx
            for di, dj, cost in _NEIGH:
                a, b = i + di, j + dj
                if a < 0 or b < 0 or a >= nx or b >= ny:
                    continue
                n_idx = b * nx + a
                if free[n_idx] != 1 or n_idx in closed:
                    continue
                if di and dj and (free[j * nx + a] != 1 or free[b * nx + i] != 1):
                    continue   # no corner cutting
                ng = gc + cost
                if ng < g.get(n_idx, 1e18):
                    g[n_idx] = ng
                    parent[n_idx] = idx
                    dx, dy = abs(a - si), abs(b - sj)
                    push(heap, (ng + dx + dy + k * min(dx, dy), ng, n_idx))
        self._exhausted = True          # nothing left to expand: start unreachable
        return start_idx in closed

    def direction(self, pos, goal: Tuple[float, float], t: float,
                  budget_s: float = None) -> Tuple[float, float]:
        sx, sy = pos[0], pos[1]
        gx, gy = goal
        if budget_s is None:
            budget_s = C.NAV_BUDGET_S
        if self.grid is None:
            return _unit(gx - sx, gy - sy)
        grid = self.grid

        # Straight line is clear: no planning needed
        if math.hypot(gx - sx, gy - sy) < grid.cell * 1.5 or grid.line_free(sx, sy, gx, gy):
            self.path = []
            return _unit(gx - sx, gy - sy)

        gc = grid.nearest_free(*grid.cell_of(gx, gy), max_r=10)
        sc = grid.nearest_free(*grid.cell_of(sx, sy))
        if gc is None or sc is None:
            return _unit(gx - sx, gy - sy)
        goal_idx = gc[1] * grid.nx + gc[0]
        start_idx = sc[1] * grid.nx + sc[0]
        if (self._goal_idx is None or self.goal is None
                or math.hypot(gx - self.goal[0], gy - self.goal[1]) > C.NAV_GOAL_MOVE_M):
            self._restart(goal, goal_idx)

        if start_idx in self._closed:
            done = True
        elif self._exhausted or budget_s <= 0.001:
            done = False
        else:
            done = self._search(start_idx, budget_s)
        if not done:
            self.path = []
            return _unit(gx - sx, gy - sy)   # tree not here yet: head for the goal meanwhile

        # Next few waypoints: follow parents toward the goal
        pts = []
        idx = start_idx
        parent = self._parent
        while len(pts) < 9:
            idx = parent.get(idx, -1)
            if idx < 0:
                pts.append((gx, gy))
                break
            pts.append(grid.center(idx % grid.nx, idx // grid.nx))
        self.path = pts

        # String-pull: head for the farthest of them we can walk straight to
        target = pts[0]
        for wp in pts[1:]:
            if grid.line_free(sx, sy, wp[0], wp[1]):
                target = wp
            else:
                break
        return _unit(target[0] - sx, target[1] - sy)


def _unit(dx: float, dy: float) -> Tuple[float, float]:
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return 0.0, 0.0
    return dx / d, dy / d
