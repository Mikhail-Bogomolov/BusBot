import math

R = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, 0..360, clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def locate_on_path(path: list[tuple[float, float, float]], lat: float, lon: float, heading: float,
                   max_off_m: float = 60.0, max_angle: float = 50.0) -> list[tuple[float, float]]:
    """Where on a polyline [(lat, lon, at_m), ...] a vehicle at (lat, lon) moving along `heading` can be.
    Returns (at_m, offset_m) for every segment within max_off_m whose direction matches the heading."""
    kx = math.cos(math.radians(lat)) * 111320.0   # metres per degree, local flat approximation
    ky = 110540.0
    pad = max_off_m / ky * 1.5
    out: list[tuple[float, float]] = []
    for (la, lo, at), (lb, lob, bt) in zip(path, path[1:]):
        if (lat < min(la, lb) - pad or lat > max(la, lb) + pad
                or lon < min(lo, lob) - pad * 2 or lon > max(lo, lob) + pad * 2):
            continue
        ax, ay = (lo - lon) * kx, (la - lat) * ky
        bx, by = (lob - lon) * kx, (lb - lat) * ky
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1.0:
            continue
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        off = math.hypot(ax + t * dx, ay + t * dy)
        if off > max_off_m:
            continue
        if angle_diff(heading, (math.degrees(math.atan2(dx, dy)) + 360) % 360) > max_angle:
            continue
        out.append((at + t * (bt - at), off))
    return out
