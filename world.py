"""
world.py — Static map intelligence, precomputed at startup.

Parses arena/v2/world.json and terrain.i16 once and exposes:

  WorldIndex
    .terrain_height(x, y)   → ground elevation (m) at world coords
    .rooftops                → list of Rooftop (centroid, elevation, polygon)
    .rooftops_in_zone(cx, cy, r) → rooftops whose centroid is inside the circle
    .nearest_rooftop(x, y, cx, cy, r)  → closest safe rooftop
    .prisms                  → all solid prisms for LOS approximation
    .point_in_prism(x, y, z) → True if inside any solid prism

All geometry is in world-frame metres (x east, y north, z up).
"""

from __future__ import annotations

import json
import math
import os
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import config as C
import mechfight as mf

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Rooftop:
    """An accessible elevated building rooftop."""
    centroid_x: float          # world x of centroid
    centroid_y: float          # world y of centroid
    elevation: float           # rooftop height above terrain origin (m)
    polygon: List[Tuple[float, float]]  # [(x0,y0), (x1,y1), ...] footprint
    approach_x: float = 0.0    # best ground approach point x (near wall)
    approach_y: float = 0.0    # best ground approach point y

@dataclass
class Tree:
    """
    A tree from world.json. Trees block VISION only: trunks stop a sight line
    outright, crowns fade it (dense crowns block, thin foliage does not). They
    never stop movement or shots.
    """
    x: float
    y: float
    z: float          # base height
    height: float
    crown_r: float
    crown_base: float  # height above z where the crown starts
    trunk_r: float

    def trunk_block(self, px: float, py: float, pz: float) -> bool:
        if pz < self.z or pz > self.z + self.crown_base:
            return False
        return (px - self.x) ** 2 + (py - self.y) ** 2 <= self.trunk_r * self.trunk_r

    def crown_contains(self, px: float, py: float, pz: float) -> bool:
        """True if (px,py,pz) sits inside this tree's crown volume."""
        if pz < self.z + self.crown_base or pz > self.z + self.height:
            return False
        d2 = (px - self.x) ** 2 + (py - self.y) ** 2
        return d2 <= self.crown_r * self.crown_r


@dataclass
class Prism:
    """A solid obstacle prism from world.json."""
    kind: str                  # 'building', 'bridge', 'stadium'
    base: float                # bottom z (m)
    top: float                 # top z (m)
    ring: List[Tuple[float, float]]  # 2D footprint polygon [(x,y), ...]
    holes: List[List[Tuple[float, float]]] = field(default_factory=list)
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0


# ---------------------------------------------------------------------------
# Polygon helpers (no dependencies beyond math)
# ---------------------------------------------------------------------------

def _point_in_polygon(px: float, py: float,
                      poly: List[Tuple[float, float]]) -> bool:
    """Ray-casting test: True if (px, py) is strictly inside the polygon."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _polygon_centroid(poly: List[Tuple[float, float]]) -> Tuple[float, float]:
    """Compute the centroid of a 2D polygon (shoelace formula)."""
    n = len(poly)
    area = 0.0
    cx = cy = 0.0
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area  += cross
        cx    += (x0 + x1) * cross
        cy    += (y0 + y1) * cross
    area *= 0.5
    if abs(area) < 1e-6:
        # Degenerate polygon — just average vertices
        cx = sum(p[0] for p in poly) / n
        cy = sum(p[1] for p in poly) / n
        return cx, cy
    cx /= (6.0 * area)
    cy /= (6.0 * area)
    return cx, cy


def _ring_to_pairs(flat: List[float]) -> List[Tuple[float, float]]:
    """Convert a flat [x0, y0, x1, y1, ...] ring to [(x,y), ...] pairs."""
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


def _approach_point(centroid_x: float, centroid_y: float,
                    poly: List[Tuple[float, float]],
                    offset: float = 3.5) -> Tuple[float, float]:
    """
    Find the polygon edge midpoint closest to the polygon centroid and
    return a point `offset` metres outside it (the wall approach waypoint
    a ground-level mech should walk to before jumping).
    """
    best_dist = float("inf")
    best_mx = centroid_x
    best_my = centroid_y
    best_nx = 0.0
    best_ny = 1.0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        mx = (x0 + x1) * 0.5
        my = (y0 + y1) * 0.5
        dist = math.hypot(mx - centroid_x, my - centroid_y)
        if dist < best_dist:
            best_dist = dist
            best_mx, best_my = mx, my
            # Outward normal (points away from centroid)
            dx = mx - centroid_x
            dy = my - centroid_y
            length = math.hypot(dx, dy) or 1.0
            best_nx = dx / length
            best_ny = dy / length
    # Approach point: edge midpoint + offset outward
    return best_mx + best_nx * offset, best_my + best_ny * offset


# ---------------------------------------------------------------------------
# Terrain sampling
# ---------------------------------------------------------------------------

def _load_terrain(path: str, terrain_meta: dict):
    """
    Load terrain.i16 into a flat list of centimetres (int16, little-endian).
    Returns (data_bytes, nx, ny, min_x, min_y, step).
    """
    nx    = int(terrain_meta["nx"])
    ny    = int(terrain_meta["ny"])
    min_x = float(terrain_meta["minX"])
    min_y = float(terrain_meta["minY"])
    step  = float(terrain_meta.get("step", 1.0))
    with open(path, "rb") as fh:
        raw = fh.read()
    # Unpack all int16 values at once
    n_expected = nx * ny
    values = struct.unpack_from(f"<{n_expected}h", raw, 0)
    return values, nx, ny, min_x, min_y, step


# ---------------------------------------------------------------------------
# Main WorldIndex class
# ---------------------------------------------------------------------------

class WorldIndex:
    """
    Precomputed spatial intelligence built once from the arena files.

    Parameters
    ----------
    arena_dir : str
        Path to the arena directory (contains v2/world.json and v2/terrain.i16).
    min_roof_h : float
        Minimum rooftop elevation (m) to consider as a viable high-ground position.
    max_roof_h : float
        Maximum rooftop elevation (m) to consider reachable.
    """

    def __init__(self,
                 arena_dir: str,
                 min_roof_h: float = 12.0,
                 max_roof_h: float = 28.0) -> None:

        world_path = None
        terrain_path = None

        candidates = [arena_dir]
        if arena_dir:
            # If a Windows drive path like C:\arena is passed into a Linux container:
            if len(arena_dir) >= 2 and arena_dir[1] == ':':
                stripped = arena_dir[2:].replace('\\', '/')
                candidates.append(stripped)
                candidates.append('/' + stripped.lstrip('/'))
            candidates.append(arena_dir.replace('\\', '/'))
        candidates.extend(['/arena', '/arena/v2', './arena', './arena/v2', '../arena', '../arena/v2', 'arena', 'arena/v2'])

        for cand in candidates:
            if not cand:
                continue
            p1 = os.path.join(cand, "world.json")
            t1 = os.path.join(cand, "terrain.i16")
            if os.path.exists(p1) and os.path.exists(t1):
                world_path, terrain_path = p1, t1
                break
            p2 = os.path.join(cand, "v2", "world.json")
            t2 = os.path.join(cand, "v2", "terrain.i16")
            if os.path.exists(p2) and os.path.exists(t2):
                world_path, terrain_path = p2, t2
                break

        if world_path is None or not os.path.exists(world_path):
            mf.log(f"[world] WARNING: Could not find world.json from arena_dir='{arena_dir}'. Running in fallback mode.")
            self._terrain = None
            self._tnx = self._tny = self._tstep = 1
            self._tmin_x = self._tmin_y = 0.0
            self.prisms = []
            self.rooftops = []
            self.trees = []
            self._tree_grid = {}
            self.bounds = None
            return

        with open(world_path, "r") as fh:
            world = json.load(fh)
        self.bounds = world.get("bounds")

        terrain_meta = world["terrain"]
        self._terrain, self._tnx, self._tny, \
            self._tmin_x, self._tmin_y, self._tstep = \
            _load_terrain(terrain_path, terrain_meta)

        # --- Parse all prisms & build 2D Spatial Hash Grid ---
        self.CELL_SIZE = 20.0
        self.prisms: List[Prism] = []
        self._grid: dict = {}

        for raw in world.get("prisms", []):
            ring  = _ring_to_pairs(raw.get("ring", []))
            holes = [_ring_to_pairs(h) for h in raw.get("holes", [])]
            if len(ring) >= 3:
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                min_x, max_x = min(xs), max(xs)
                min_y, max_y = min(ys), max(ys)
                prism = Prism(
                    kind  = raw.get("kind", "building"),
                    base  = float(raw.get("base", 0.0)),
                    top   = float(raw.get("top",  0.0)),
                    ring  = ring,
                    holes = holes,
                    min_x = min_x,
                    min_y = min_y,
                    max_x = max_x,
                    max_y = max_y,
                )
                self.prisms.append(prism)

                # Populate spatial grid cells
                gx0 = int(math.floor(min_x / self.CELL_SIZE))
                gx1 = int(math.floor(max_x / self.CELL_SIZE))
                gy0 = int(math.floor(min_y / self.CELL_SIZE))
                gy1 = int(math.floor(max_y / self.CELL_SIZE))
                for gx in range(gx0, gx1 + 1):
                    for gy in range(gy0, gy1 + 1):
                        cell = (gx, gy)
                        if cell not in self._grid:
                            self._grid[cell] = []
                        self._grid[cell].append(prism)

        # --- Extract rooftops ---
        self.rooftops: List[Rooftop] = []
        for prism in self.prisms:
            if prism.kind not in ("building", "stadium"):
                continue
            if prism.top < min_roof_h or prism.top > max_roof_h:
                continue
            cx, cy = _polygon_centroid(prism.ring)
            ax, ay = _approach_point(cx, cy, prism.ring, offset=3.5)
            self.rooftops.append(Rooftop(
                centroid_x = cx,
                centroid_y = cy,
                elevation  = prism.top,
                polygon    = prism.ring,
                approach_x = ax,
                approach_y = ay,
            ))

        # --- Trees (vision only) ---
        # world.json stores them as a flat `data` array with named columns in
        # `fields`. Parse defensively: column order is taken from `fields`, and
        # any row that does not line up is skipped rather than raising.
        self.trees: List[Tree] = []
        self._tree_grid: dict = {}
        trees_raw = world.get("trees") or {}
        fields = trees_raw.get("fields") or []
        data = trees_raw.get("data") or []
        if fields and data:
            idx = {name: i for i, name in enumerate(fields)}
            need = ("x", "y")
            if all(k in idx for k in need):
                ncol = len(fields)
                for r in range(0, len(data) - ncol + 1, ncol):
                    row = data[r:r + ncol]
                    try:
                        tx = float(row[idx["x"]])
                        ty = float(row[idx["y"]])
                        tz = float(row[idx["z"]]) if "z" in idx else 0.0
                        th = float(row[idx["height"]]) if "height" in idx else 8.0
                        cr = float(row[idx["crownR"]]) if "crownR" in idx else 3.0
                        cb = float(row[idx["crownBase"]]) if "crownBase" in idx else max(0.0, th * 0.4)
                        tr = float(row[idx["trunkR"]]) if "trunkR" in idx else 0.3
                    except (TypeError, ValueError, IndexError):
                        continue
                    if th <= 0.0 or cr <= 0.0:
                        continue
                    tree = Tree(x=tx, y=ty, z=tz, height=th,
                                crown_r=cr, crown_base=cb, trunk_r=tr)
                    self.trees.append(tree)
                    # Spatial grid, same cell size as prisms.
                    gx = int(math.floor(tx / self.CELL_SIZE))
                    gy = int(math.floor(ty / self.CELL_SIZE))
                    cell = (gx, gy)
                    if cell not in self._tree_grid:
                        self._tree_grid[cell] = []
                    self._tree_grid[cell].append(tree)

        mf.log(f"[world] loaded {len(self.prisms)} prisms, "
               f"{len(self.rooftops)} viable rooftops "
               f"({min_roof_h}–{max_roof_h} m), {len(self.trees)} trees")

    # ------------------------------------------------------------------
    # Terrain sampling
    # ------------------------------------------------------------------

    def terrain_height(self, x: float, y: float) -> float:
        """
        Bilinear interpolation of the terrain heightfield at world (x, y).
        Returns height in metres above the map origin.
        """
        if self._terrain is None:
            return 0.0

        nx, ny  = self._tnx, self._tny
        min_x   = self._tmin_x
        min_y   = self._tmin_y
        step    = self._tstep
        terrain = self._terrain

        fx = (x - min_x) / step
        fy = (y - min_y) / step

        ix = max(0, min(nx - 2, int(fx)))
        iy = max(0, min(ny - 2, int(fy)))

        tx = fx - ix
        ty = fy - iy

        def sample(i: int, j: int) -> float:
            idx = j * nx + i
            if 0 <= idx < len(terrain):
                return terrain[idx] * 0.01   # cm → m
            return 0.0

        h00 = sample(ix,     iy)
        h10 = sample(ix + 1, iy)
        h01 = sample(ix,     iy + 1)
        h11 = sample(ix + 1, iy + 1)

        return (h00 * (1 - tx) * (1 - ty) +
                h10 * tx       * (1 - ty) +
                h01 * (1 - tx) * ty       +
                h11 * tx       * ty)

    # ------------------------------------------------------------------
    # Prism collision & LOS Queries (Accelerated via Spatial Grid)
    # ------------------------------------------------------------------

    def point_in_prism(self, x: float, y: float, z: float) -> bool:
        """
        Returns True if the world point (x, y, z) lies inside any solid prism.
        Accelerated via 2D spatial hash grid (O(1) lookup).
        """
        if not hasattr(self, "_grid"):
            return False
        gx = int(math.floor(x / self.CELL_SIZE))
        gy = int(math.floor(y / self.CELL_SIZE))
        candidate_prisms = self._grid.get((gx, gy), [])
        for prism in candidate_prisms:
            if z < prism.base or z > prism.top:
                continue
            if x < prism.min_x or x > prism.max_x or y < prism.min_y or y > prism.max_y:
                continue
            if _point_in_polygon(x, y, prism.ring):
                in_hole = any(_point_in_polygon(x, y, h) for h in prism.holes)
                if not in_hole:
                    return True
        return False

    def has_los_2d(self,
                   x0: float, y0: float, z0: float,
                   x1: float, y1: float, z1: float,
                   step: Optional[float] = None) -> bool:
        """
        Line-of-sight check between two 3D points.
        Steps along the 2D segment (every 3.0 m), interpolates z linearly (z = z0 + (z1 - z0) * t),
        and tests prisms in the current 20m x 20m grid cell.
        Returns False immediately if any prism blocks the ray.
        """
        if not hasattr(self, "_grid"):
            return True
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist < 0.1:
            return True

        # Step every LOS_STEP_M along the segment (finer than most wall thicknesses)
        steps = max(2, int(math.ceil(dist / (step or C.LOS_STEP_M))))
        current_cell = None
        cell_prisms = []
        terrain = self._terrain
        if terrain is not None:
            t_nx, t_ny = self._tnx, self._tny
            t_mx, t_my = self._tmin_x, self._tmin_y
            t_inv = 1.0 / self._tstep

        for s in range(1, steps):
            t = s / steps
            mx = x0 + (x1 - x0) * t
            my = y0 + (y1 - y0) * t
            mz = z0 + (z1 - z0) * t

            # Hills block shots and sight too (nearest 1 m terrain cell, inlined for speed)
            if terrain is not None:
                ti = int((mx - t_mx) * t_inv + 0.5)
                tj = int((my - t_my) * t_inv + 0.5)
                if 0 <= ti < t_nx and 0 <= tj < t_ny and mz * 100.0 < terrain[tj * t_nx + ti]:
                    return False

            cell = (int(mx // self.CELL_SIZE), int(my // self.CELL_SIZE))
            if cell != current_cell:
                current_cell = cell
                cell_prisms = self._grid.get(cell, [])

            for prism in cell_prisms:
                if prism.base <= mz <= prism.top:
                    if prism.min_x <= mx <= prism.max_x and prism.min_y <= my <= prism.max_y:
                        if _point_in_polygon(mx, my, prism.ring):
                            in_hole = any(_point_in_polygon(mx, my, h) for h in prism.holes)
                            if not in_hole:
                                return False
        return True

    def has_sight(self,
                  x0: float, y0: float, z0: float,
                  x1: float, y1: float, z1: float,
                  step: Optional[float] = None,
                  canopy_block_t: int = 3) -> bool:
        """
        VISION line of sight: like has_los_2d, but trees also count. Trunks
        stop sight outright; crowns fade it, and a sample inside `canopy_block_t`
        overlapping crowns counts as blocked (protocol §8: thin foliage does not
        block, a dense crown does).

        Use this for "can I see/hear it" and for cover scoring. Keep has_los_2d
        for shots: trees never stop a bullet.
        """
        if not self.has_los_2d(x0, y0, z0, x1, y1, z1, step=step):
            return False
        if not self.trees:
            return True

        dist = math.hypot(x1 - x0, y1 - y0)
        if dist < 0.1:
            return True
        steps = max(2, int(math.ceil(dist / (step or C.LOS_STEP_M))))
        current_cell = None
        cell_trees: List[Tree] = []

        for s in range(1, steps):
            t = s / steps
            mx = x0 + (x1 - x0) * t
            my = y0 + (y1 - y0) * t
            mz = z0 + (z1 - z0) * t

            cell = (int(mx // self.CELL_SIZE), int(my // self.CELL_SIZE))
            if cell != current_cell:
                current_cell = cell
                cell_trees = self._tree_grid.get(cell, [])
            if not cell_trees:
                continue

            crowns = 0
            for tree in cell_trees:
                if tree.trunk_block(mx, my, mz):
                    return False
                if tree.crown_contains(mx, my, mz):
                    crowns += 1
                    if crowns >= canopy_block_t:
                        return False
        return True

    def is_walkable(self, x: float, y: float, z: float) -> bool:
        """
        Returns False if (x, y, z + 1.5) (step_up 1.2 m) or (x, y, z + 8.5) (9 m head/bridge clearance)
        is inside any solid obstacle prism, or if terrain slope exceeds 50 degrees.
        """
        if self.point_in_prism(x, y, z + 1.5):
            return False
        if self.point_in_prism(x, y, z + 8.5):
            return False

        if self._terrain is not None:
            h_east  = self.terrain_height(x + 1.0, y)
            h_west  = self.terrain_height(x - 1.0, y)
            h_north = self.terrain_height(x, y + 1.0)
            h_south = self.terrain_height(x, y - 1.0)
            dx = (h_east - h_west) * 0.5
            dy = (h_north - h_south) * 0.5
            # tan(50°)^2 ≈ 1.42
            if (dx * dx + dy * dy) > 1.42:
                return False
        return True

    def point_in_rooftop(self, x: float, y: float, rooftop: Rooftop) -> bool:
        """Returns True if (x, y) is strictly inside the given rooftop's polygon."""
        if rooftop is None or not rooftop.polygon:
            return False
        return _point_in_polygon(x, y, rooftop.polygon)

    # ------------------------------------------------------------------
    # Rooftop queries
    # ------------------------------------------------------------------

    def rooftops_in_zone(self, cx: float, cy: float,
                          radius: float) -> List[Rooftop]:
        """
        Return all rooftops whose centroid falls inside the zone circle.
        Uses Euclidean 2D distance (z ignored).
        """
        return [
            r for r in self.rooftops
            if math.hypot(r.centroid_x - cx, r.centroid_y - cy) <= radius
        ]

    def nearest_rooftop(self,
                        self_x: float, self_y: float,
                        zone_cx: float, zone_cy: float,
                        zone_r: float,
                        min_height: float = 12.0) -> Optional[Rooftop]:
        """
        Return the closest rooftop to (self_x, self_y) that:
          - lies inside the zone circle
          - has elevation >= min_height

        Prefers rooftops near the zone edge over rooftops in the centre
        (edge-riding gives rear-armour protection from the gas wall).
        Returns None if no suitable rooftop found.
        """
        candidates = self.rooftops_in_zone(zone_cx, zone_cy, zone_r * 0.90)
        candidates = [r for r in candidates if r.elevation >= min_height]
        if not candidates:
            return None

        def score(r: Rooftop) -> float:
            dist_to_self = math.hypot(r.centroid_x - self_x,
                                      r.centroid_y - self_y)
            # Incentivise rooftops near the zone edge
            dist_from_centre = math.hypot(r.centroid_x - zone_cx,
                                          r.centroid_y - zone_cy)
            edge_bonus = dist_from_centre / (zone_r + 1.0)  # 0..1
            # Combined score: nearby + near zone edge + tall
            return dist_to_self - edge_bonus * 40.0 - r.elevation * 1.5

        return min(candidates, key=score)
