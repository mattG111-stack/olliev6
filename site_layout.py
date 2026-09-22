"""Can the terraces we counted actually stand on the section?

    "it woul be cool to try do a muck up on each site"

The yield is arithmetic: land area, less a share for shared access, divided by
the land a terrace needs. It is a good way to rank a thousand sites and it
cannot see the one thing a developer looks at first, which is the shape.

The section on this week's screenshot is the example. It holds 979 m², so the
arithmetic says six terraces. It also runs 49.8 m along one boundary with its
depth falling from 29.9 m at one end to 9.4 m at the other. It is a wedge, and a
terrace row needs consistent depth along its length. Six is what the area allows
and not what the ground allows.

So this packs the real boundary. Rectangles of the same size the yield assumes,
laid in rows, each one tested for fitting wholly inside the parcel. What comes
back is a count that can be smaller than the arithmetic's and never larger, and
the rectangles themselves, so the difference can be looked at rather than
argued about.

WHAT THIS IS NOT. It is not a site plan and it must never be presented as one.
Three things it does not know:

    Which side is the road. Terraces front a street and a rear site needs an
    accessway that eats the frontage. There is no road data in the system, so
    every boundary is tried as the street and the best result wins — which is
    an upper bound, not a proposal.

    The zone's own controls. Coverage, outdoor living space, height in relation
    to boundary and the real yard setbacks are what actually cap a site. None
    of them are here. The margin below is a plain keep-clear, not a yard.

    The ground. Slope, trees, overland flow paths, the existing crossing.

Which is why the honest use of this is subtractive. When it fits fewer than the
arithmetic claimed, that is a finding and it is reliable — the rectangles really
do not fit. When it fits as many, that means the shape is not the binding
constraint, not that consent would follow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from pricing.subdivision import THAB_LOT_M2

# One terrace's patch of land, as a shape rather than as an area. Width is the
# street-facing dimension; a row is a run of these shoulder to shoulder.
LOT_WIDTH_M = 6.0
LOT_DEPTH_M = THAB_LOT_M2 / LOT_WIDTH_M          # 20 m, so the two cannot drift
# Shared driveway between rows, and behind the front row on a rear-lot layout.
ACCESS_WIDTH_M = 3.5
# A plain keep-clear off every boundary. NOT a yard setback — the zone decides
# those and the zone is not known here. Named for what it is so nobody reads a
# planning rule into it later.
BOUNDARY_CLEAR_M = 1.0
# Beyond this there is no point drawing: the count is capped the same place the
# subdivision engine caps it.
MAX_LOTS = 20

XY = tuple[float, float]


@dataclass
class Lot:
    """One terrace's footprint, as four corners in local metres, east/north."""
    corners: list[XY]


@dataclass
class Layout:
    """What fits, against what the arithmetic said.

    `by_area` is the yield the subdivision engine produces. `fits` is how many
    of those rectangles could be placed inside the actual boundary. When they
    differ, the second one is the answer and the first one is the reason the
    site looked better than it is.
    """
    fits: int
    by_area: int
    lots: list[Lot] = field(default_factory=list)
    # Which boundary was treated as the street to achieve this. An index into
    # the ring's sides, so the drawing can show which assumption produced it.
    street_side: int | None = None
    shape_limited: bool = False


# ---- geometry ----------------------------------------------------------------
#
# Point-in-polygon and segment crossing, written out rather than pulled in.
# Shapely would do both and is a compiled dependency on a deploy that has fallen
# over for less; this is forty lines and no install.

def _inside(p: XY, poly: list[XY]) -> bool:
    """Ray casting. True for a point strictly within the ring."""
    x, y = p
    hit = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xc:
                hit = not hit
    return hit


def _crosses(a1: XY, a2: XY, b1: XY, b2: XY) -> bool:
    """Do two segments properly cross? Touching at an endpoint does not count."""
    def side(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    d1, d2 = side(a1, a2, b1), side(a1, a2, b2)
    d3, d4 = side(b1, b2, a1), side(b1, b2, a2)
    return d1 != d2 and d3 != d4 and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def _rect_inside(rect: list[XY], poly: list[XY]) -> bool:
    """Is a rectangle wholly within the parcel?

    Corners alone are not enough. Real parcels are routinely concave — L-shaped,
    or notched around a right of way — and a rectangle can have all four corners
    inside while its middle bulges out across the notch. That is the case that
    would put a house on the neighbour's land, so the edges are checked too.
    """
    if not all(_inside(c, poly) for c in rect):
        return False
    n, m = len(rect), len(poly)
    for i in range(n):
        for j in range(m):
            if _crosses(rect[i], rect[(i + 1) % n],
                        poly[j], poly[(j + 1) % m]):
                return False
    return True


def _ring_metres(ring: list[list[float]],
                 origin: tuple[float, float] | None = None) -> list[XY]:
    """A lat/lng ring as local metres east/north, closing point dropped.

    `origin` IS NOT A DETAIL. The editor these lots are handed to places every
    building as metres east and north OF THE LISTING'S COORDINATES, so a layout
    measured from the parcel's own centroid instead would arrive correct in
    shape and wrong in position — every house shifted by the distance between
    the pin and the middle of the section, with nothing on screen to say so.
    Same class of fault as the lon/lat flip already guarded in the geo router,
    and just as quiet.

    Defaults to the centroid, which is right for measuring a parcel on its own.
    Pass the pin whenever the result has to line up with anything else.
    """
    pts = [p for p in ring]
    if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-9 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts.pop()
    if len(pts) < 3:
        return []
    if origin is None:
        lat0 = sum(p[0] for p in pts) / len(pts)
        lng0 = sum(p[1] for p in pts) / len(pts)
    else:
        lat0, lng0 = origin
    mx = 111320.0 * math.cos(math.radians(lat0))
    return [((p[1] - lng0) * mx, (p[0] - lat0) * 110574.0) for p in pts]


def _rotate(pts: list[XY], rad: float) -> list[XY]:
    c, s = math.cos(rad), math.sin(rad)
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _counter_clockwise(poly: list[XY]) -> list[XY]:
    """The same ring, always wound the same way.

    THE PACKER'S ANSWER DEPENDED ON THIS AND NOTHING SAID SO. Rows are anchored
    at the bottom of the rotated bounding box, and the rotation puts the
    candidate street edge along the x axis — so where the interior lands,
    above the axis or below it, decides whether the rows start at the STREET or
    at the REAR boundary. Which side the interior is on is exactly the winding
    order, and a boundary can be digitised either way.

    The cost is not a rounding difference. The same 609 m² section, mirrored
    north for south, took three terraces one way round and none the other:
    anchored at the rear, the single row that fits in 33 m lands on the tapered
    back of the site instead of the wide street end. Both answers looked like
    findings about the shape.
    """
    if len(poly) < 3:
        return poly
    twice_area = sum(a[0] * b[1] - b[0] * a[1]
                     for a, b in zip(poly, poly[1:] + poly[:1]))
    return poly if twice_area > 0 else poly[::-1]


def _pack_against(poly: list[XY], rad: float) -> list[list[XY]]:
    """Fill the parcel with lots, rows running parallel to one boundary.

    The parcel is rotated so the candidate street lies along the x axis, which
    turns the packing into a grid walk. Rows step back by a lot depth plus a
    driveway; within a row, lots step sideways by a lot width. Every rectangle
    is placed at a real position and then tested, rather than counted from a
    formula — the whole point is to find the ones that do not fit.
    """
    rp = _rotate(poly, -rad)
    xs = [p[0] for p in rp]
    ys = [p[1] for p in rp]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    # The keep-clear is applied by inflating each lot before the containment
    # test, so a lot placed hard against a boundary fails. Cheaper and more
    # honest than offsetting the polygon, which for a concave ring is a job in
    # itself and gets the notches wrong.
    m = BOUNDARY_CLEAR_M
    out: list[list[XY]] = []

    # The walk starts a full keep-clear in from the boundary, and the rectangle
    # it tests is inflated by a HAIR LESS than that. Two bugs live here and they
    # pull in opposite directions:
    #
    #   Starting on the boundary put the first test rectangle a metre outside
    #   the parcel, so on a plain rectangular site every position failed and the
    #   answer came back as zero lots on land that plainly takes five.
    #
    #   Nudging the start inwards instead spent that nudge out of the width
    #   budget, so a 14 m frontage — exactly two 6 m lots plus two 1 m margins —
    #   came back as one lot, short by a centimetre.
    #
    # Taking the slack off the TEST rather than the POSITION fixes both: the
    # rectangle sits a hair inside the line the containment test treats as
    # outside, and the arithmetic that decides how many fit stays exact.
    EPS = 0.01
    infl = m - EPS
    row_pitch = LOT_DEPTH_M + ACCESS_WIDTH_M
    y = y0 + m
    while y + LOT_DEPTH_M + m <= y1 + 1e-9 and len(out) < MAX_LOTS:
        x = x0 + m
        while x + LOT_WIDTH_M + m <= x1 + 1e-9 and len(out) < MAX_LOTS:
            test = [(x - infl, y - infl), (x + LOT_WIDTH_M + infl, y - infl),
                    (x + LOT_WIDTH_M + infl, y + LOT_DEPTH_M + infl),
                    (x - infl, y + LOT_DEPTH_M + infl)]
            if _rect_inside(test, rp):
                out.append([(x, y), (x + LOT_WIDTH_M, y),
                            (x + LOT_WIDTH_M, y + LOT_DEPTH_M),
                            (x, y + LOT_DEPTH_M)])
                x += LOT_WIDTH_M
            else:
                # Slide along rather than give up on the row. A wedge is narrow
                # at one end and usable at the other, and stopping at the first
                # failure would report the narrow end as the whole site.
                x += LOT_WIDTH_M / 3.0
        y += row_pitch

    return [_rotate(r, rad) for r in out]


def terrace_layout(ring: list[list[float]], by_area: int,
                   origin: tuple[float, float] | None = None) -> Layout:
    """Place `by_area` terrace lots on a real boundary, and see how many land.

    `ring` is the parcel as [[lat, lng], ...]; `by_area` is the count the
    subdivision engine produced from the area alone. Returns whichever boundary
    gives the best result, because which side is the road is not known.

    `origin` is the point the returned metres are measured from — pass the
    listing's lat/lng when the lots are going to the building editor, which
    measures from the pin. See _ring_metres.
    """
    poly = _counter_clockwise(_ring_metres(ring, origin))
    if len(poly) < 3 or by_area <= 0:
        return Layout(fits=0, by_area=max(by_area, 0))

    best: list[list[XY]] = []
    best_side: int | None = None
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        if math.dist(a, b) < LOT_WIDTH_M:
            continue                       # too short to be a street frontage
        rad = math.atan2(b[1] - a[1], b[0] - a[0])
        got = _pack_against(poly, rad)
        if len(got) > len(best):
            best, best_side = got, i

    # Never more than the arithmetic allows. This answers "does the shape take
    # the number we quoted", and a packing that found room for more is not
    # licence to raise the yield — the area model carries the access share and
    # the practical cap, and both of them still apply.
    lots = best[:by_area]
    return Layout(
        fits=len(lots),
        by_area=by_area,
        lots=[Lot(corners=r) for r in lots],
        street_side=best_side if lots else None,
        shape_limited=len(lots) < by_area,
    )
