"""
measure.py - two facts about the TARGET client, measured once per client.

1. HELM SHIFT  ->  <work>/client/helm_shift.json  {"HuM": {"shift": [x,y,z], "how": ...}}
   A retail helm cut is authored for retail's helm point (attachment 11, which
   on retail bodies sits ON the head bone). A 3.3.5 body's helm point sits
   elsewhere (legacy Human male: 6 cm forward, 18 cm up). Placed as-is, every
   retail helm floats ~20 cm high on that client.
   MEASURED: the client ships helm files that retail also ships (same name,
   same vertex count). Each such pair differs by one constant per race/sex -
   that constant is the shift (median over every pair; the spread is reported).
   ESTIMATED (only for a body with no pairs): keep the helm where it sits
   relative to the HEAD BONE: shift = (retail helm point - retail head pivot)
   - (client helm point - client head pivot). Marked "estimated": look at the
   sheet for that body before shipping.

2. GEOSET MAP  ->  <work>/client/geoset_map.json  {"5": {"506": 503, ...}}
   An item row asks the body to show <group>01 + GeosetGroup (boots 501+GG0,
   gloves 401+GG0, ...). Retail bodies grew variants the 3.3.5 bodies lack
   (boots 506..510, kneepads 905, pants 1105, belts 1803/1804): a missing
   variant shows NOTHING (the leg vanished under boots). Each retail variant
   is mapped to the client variant of the closest SHAPE (mean nearest-
   neighbour distance both ways, retail body vs client body of the same race
   and sex), majority over the PLAYABLE bodies. Among them a body votes only
   when its mesh is retail-like (some shared variant within 1 cm) unless none
   is, in which case every playable body votes (a legacy client).
"""
from __future__ import annotations

import collections
import re

import numpy as np

import bodies as BD
import bpcore
import retail_m2 as RM
import scan as SC
from wago import Files, listfile

GROUPS = (4, 5, 8, 9, 10, 11, 12, 13, 15, 18)
PAIRS_PER_BODY = 40


def _verts(data: bytes) -> np.ndarray:
    return np.array([v[0] for v in RM.vertices(RM.md20(data))], dtype=np.float64)


def helm_shift(prj, log) -> dict:
    sc = SC.load(prj)
    c = BD.client(prj)
    pres = {r["prefix"].lower(): r for r in sc["races"] if r["prefix"]}
    rx = re.compile(r"^item\\objectcomponents\\head\\(.+)_(%s)([mf])\.m2$" % "|".join(re.escape(p) for p in pres), re.I)
    by_path = {v.replace("/", "\\"): k for k, v in listfile(prj).items()}
    F = Files(prj)
    deltas = collections.defaultdict(list)
    names = c.listfiles()
    # the shift is ONE constant per body (measured spread: a few cm), so 40
    # pairs per body decide it; files already cached are used first, then
    # downloads stop as soon as a body has its 40
    cands = []
    for low in sorted(names):
        m = rx.match(low)
        fid = by_path.get(low) if m else None
        if fid:
            cands.append((not F.have(fid), low, m, fid))
    cands.sort(key=lambda x: (x[0], x[1]))
    # download in parallel what each body still needs (a cold cache), at most
    # PAIRS_PER_BODY + 10 candidates per body (some pairs differ in vertex count)
    per = collections.defaultdict(list)
    for need, _low, m, fid in cands:
        per[(m.group(2).lower(), m.group(3).upper())].append((need, fid))
    want = [fid for v in per.values() for need, fid in v[:PAIRS_PER_BODY + 10] if need]
    if want:
        log("  downloading %d retail helm files to pair with the client's (parallel)..." % len(want))
        F.fetch(want, quiet=True)
    for _need, low, m, fid in cands:
        if len(deltas[(m.group(2).lower(), m.group(3).upper())]) >= PAIRS_PER_BODY:
            continue
        if not F.have(fid):
            continue            # downloads are bounded by the prefetch above
        rd = F.try_read(fid)
        cd = c.read(names[low])
        if not rd or not cd or cd[:4] != b"MD20":
            continue
        try:
            rv, cv = _verts(rd), _verts(cd)
        except Exception:      # noqa: BLE001
            continue
        if len(rv) and len(rv) == len(cv):
            deltas[(m.group(2).lower(), m.group(3).upper())].append((cv - rv).mean(0))
    out = {}
    for r in sc["races"]:
        for sx in r["bodies"]:
            key = (r["prefix"].lower(), sx)
            tag = r["prefix"] + sx
            v = np.array(deltas.get(key, []))
            est = None
            if len(v) < 3:          # the estimate is only needed where no pairs measure it
                rb = BD.retail_body(prj, r["retail"], "MF".index(sx)) if r.get("retail") is not None else None
                cb = BD.client_body(prj, r, sx)
                if rb is not None and cb is not None and rb.helm_point() is not None and cb.helm_point() is not None \
                        and rb.head_pivot() is not None and cb.head_pivot() is not None:
                    est = ((rb.helm_point() - rb.head_pivot()) - (cb.helm_point() - cb.head_pivot()))
            if len(v) >= 3:
                med = np.median(v, 0)
                out[tag] = {"shift": med.round(4).tolist(), "how": "measured", "pairs": len(v),
                            "p90_spread": np.percentile(np.abs(v - med), 90, axis=0).round(4).tolist(),
                            "estimate_would_be": None if est is None else est.round(4).tolist()}
            elif est is not None:
                out[tag] = {"shift": est.round(4).tolist(), "how": "estimated (no helm pairs): CHECK THE SHEET",
                            "pairs": len(v)}
            else:
                out[tag] = {"shift": [0.0, 0.0, 0.0], "how": "UNKNOWN (no pairs, no retail body): CHECK THE SHEET",
                            "pairs": len(v)}
            o = out[tag]
            log("  helm shift %-5s %-28s %s %s" % (tag, str(o["shift"]), o["how"],
                                                   ("pairs %d p90 %s" % (o["pairs"], o.get("p90_spread"))) if o["how"] == "measured" else ""))
    bpcore.save_json(prj.path("client", "helm_shift.json"), out)
    return out


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    """Mean nearest-neighbour distance both ways (brute force in blocks)."""
    def one(x, y):
        best = []
        for i in range(0, len(x), 512):
            d = ((x[i:i + 512, None, :] - y[None, :, :]) ** 2).sum(-1)
            best.append(np.sqrt(d.min(1)))
        return float(np.concatenate(best).mean())
    return 0.5 * (one(a, b) + one(b, a))


def geoset_map(prj, log) -> dict:
    sc = SC.load(prj)
    pairs = []
    for r in sc["races"]:
        # the bodies players wear decide the map (NPC bodies are often retail's
        # own file and would out-vote them)
        if r.get("retail") is None or not r.get("playable", True):
            continue
        for sx in r["bodies"]:
            rb = BD.retail_body(prj, r["retail"], "MF".index(sx))
            cb = BD.client_body(prj, r, sx)
            if rb is not None and cb is not None:
                pairs.append((r["prefix"] + sx, rb, cb))
    retail_like = set()
    for tag, rb, cb in pairs:
        shared = [g for g in rb.geosets if g in cb.parts and g // 100 in GROUPS]
        if any(_dist(rb.part(g), cb.part(g)) < 0.01 for g in shared[:6] if len(rb.part(g)) and len(cb.part(g))):
            retail_like.add(tag)
    voters = [p for p in pairs if p[0] in retail_like] or pairs
    log("  geoset map voters: %s (%s)" % ([p[0] for p in voters],
                                          "retail-like meshes" if retail_like else "no retail-like body: all vote"))
    votes = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    have = {}
    for tag, rb, cb in voters:
        for g in GROUPS:
            cv = [k for k in cb.geosets if k // 100 == g and len(cb.part(k))]
            if cv:
                have[g] = set(cv) if g not in have else have[g] & set(cv)
            for rv in (k for k in rb.geosets if k // 100 == g and len(rb.part(k))):
                if not cv:
                    continue
                if rv in cv:
                    votes[g][rv][rv] += 1
                    continue
                d = {k: _dist(rb.part(rv), cb.part(k)) for k in cv}
                votes[g][rv][min(d, key=d.get)] += 1
    out = {}
    for g, t in votes.items():
        out[str(g)] = {str(r): int(c.most_common(1)[0][0]) for r, c in sorted(t.items())}
        moved = {r: v for r, v in out[str(g)].items() if int(r) != v}
        log("  group %2d: %s" % (g, moved or "identity"))
    out["_client_has"] = {str(g): sorted(v) for g, v in have.items()}
    bpcore.save_json(prj.path("client", "geoset_map.json"), out)
    return out


def run(prj, log, only=None) -> int:
    if only in (None, "helms"):
        log("measuring the helm shift per body...")
        helm_shift(prj, log)
    if only in (None, "geosets"):
        log("measuring the body geoset map...")
        geoset_map(prj, log)
    log("next: python bp.py build --work <W>")
    return 0


_SHIFT = {}
_GMAP = {}


def shift_for(prj, prefix: str, sx: str):
    p = prj.path("client", "helm_shift.json")
    if p not in _SHIFT:
        _SHIFT[p] = bpcore.load_json(p, None) if __import__("os").path.isfile(p) else None
    t = _SHIFT[p]
    if t is None:
        raise SystemExit("no helm shift measured: run  python bp.py measure --work <W>")
    got = t.get(prefix + sx)
    return got["shift"] if got else None


def client_value(prj, group: int, value: int) -> int:
    """The GeosetGroup value this client's bodies can show for retail's."""
    if value <= 0:
        return max(0, value)
    p = prj.path("client", "geoset_map.json")
    if p not in _GMAP:
        if not __import__("os").path.isfile(p):
            raise SystemExit("no geoset map measured: run  python bp.py measure --work <W>")
        _GMAP[p] = bpcore.load_json(p)
    want = group * 100 + 1 + value
    got = _GMAP[p].get(str(group), {}).get(str(want), want)
    return got - group * 100 - 1
