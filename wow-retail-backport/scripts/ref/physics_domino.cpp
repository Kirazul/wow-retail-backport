// Physics.cpp - see Physics.h. A port of the retail client's physics engine;
// every block names the retail function it was read from (Midnight 12.1.0
// macOS x86_64 VAs). Single precision throughout, as the client computes.
#include "physics_domino.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <xmmintrin.h>

namespace wow {
namespace ec {
namespace physics {
namespace {

constexpr float kPi = 3.14159274f;
constexpr float kTiny = 1.17549435e-35f;   // 0x057A0000-class "is it zero" guards
constexpr float kFltMax = 3.40282347e+38f;

// --- vectors / quaternions ---------------------------------------------------
inline V3 operator+(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline V3 operator-(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline V3 operator*(V3 a, float s) { return {a.x * s, a.y * s, a.z * s}; }
inline V3 operator-(V3 a) { return {-a.x, -a.y, -a.z}; }
inline V3 Mulv(V3 a, V3 b) { return {a.x * b.x, a.y * b.y, a.z * b.z}; }
inline float Dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline V3 Cross(V3 a, V3 b) { return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x}; }
inline float Len2(V3 a) { return Dot(a, a); }
inline float Len(V3 a) { return std::sqrt(Dot(a, a)); }
inline bool Finite(V3 a) { return std::isfinite(a.x) && std::isfinite(a.y) && std::isfinite(a.z); }

// Hamilton product, (x, y, z, w) lanes
inline Q Mul(Q a, Q b) {
    return {a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y, a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
            a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w, a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z};
}
inline Q Conj(Q q) { return {-q.x, -q.y, -q.z, q.w}; }
inline float QDot(Q a, Q b) { return a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w; }
inline Q QNormalize(Q q) {
    const float n2 = QDot(q, q);
    if (!(n2 > kTiny)) return q;
    const float k = 1.0f / std::sqrt(n2);
    return {q.x * k, q.y * k, q.z * k, q.w * k};
}
inline V3 QV(Q q) { return {q.x, q.y, q.z}; }

// The hardware estimates the client's SSE code starts from (rsqrtss / rcpss).
// PHYS_EXACT_ESTIMATES (tests only) makes them exact, as an emulator does, so
// the port can be compared with the client's code run in unicorn bit for bit.
#ifdef PHYS_EXACT_ESTIMATES
inline float EstRsqrt(float x) { return 1.0f / std::sqrt(x); }
inline float EstRcp(float d) { return 1.0f / d; }
#else
inline float EstRsqrt(float x) { return _mm_cvtss_f32(_mm_rsqrt_ss(_mm_set_ss(x))); }
inline float EstRcp(float d) { return _mm_cvtss_f32(_mm_rcp_ss(_mm_set_ss(d))); }
#endif
// The client's approximate reciprocals: the hardware estimate refined by one
// Newton step, exactly as its SSE code does (rsqrtss / rcpss).
inline float RsqrtN(float x) {
    const float r = EstRsqrt(x);
    return r * -0.5f * (x * r * r + -3.0f);
}
inline float RcpN(float d) {
    const float r = EstRcp(d);
    return (1.0f - d * r) * r + r;
}
// 1 / d where |d| clears the guard, else 0 (the client's masked reciprocal)
inline float RcpGuard(float d) { return std::fabs(d) > kTiny ? RcpN(d) : 0.0f; }
// |q|^2 summed as the client sums it: (z^2 + w^2) + (x^2 + y^2)
inline float QN2(Q q) { return (q.z * q.z + q.w * q.w) + (q.x * q.x + q.y * q.y); }
// the position solvers' / integrator's renormalisation (rsqrt + Newton)
inline Q QNormFast(Q q) {
    const float n2 = QN2(q);
    if (!(n2 > kTiny)) return q;
    const float k = RsqrtN(n2);
    return {q.x * k, q.y * k, q.z * k, q.w * k};
}
// v + 2 q.xyz x (q.xyz x v + w v), the client's rotate
inline V3 Rot(Q q, V3 v) {
    const V3 u = QV(q);
    const V3 t = Cross(u, v) + v * q.w;
    return v + Cross(u, t) * 2.0f;
}
inline V3 RotInv(Q q, V3 v) { return Rot(Conj(q), v); }
// q' = q + 0.5 (w (x) q), renormalised (rsqrt + Newton) - the position
// solvers' rotation step
inline Q Integrate(Q q, V3 w) {
    const Q d = Mul(Q{w.x, w.y, w.z, 0}, q);
    return QNormFast({q.x + 0.5f * d.x, q.y + 0.5f * d.y, q.z + 0.5f * d.z, q.w + 0.5f * d.w});
}

inline M3 Mat(Q q) {
    const float x = q.x, y = q.y, z = q.z, w = q.w;
    return {{1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y), 2 * (x * y + w * z),
             1 - 2 * (x * x + z * z), 2 * (y * z - w * x), 2 * (x * z - w * y), 2 * (y * z + w * x),
             1 - 2 * (x * x + y * y)}};
}
inline V3 Mulm(const M3& a, V3 v) {
    return {a.m[0] * v.x + a.m[1] * v.y + a.m[2] * v.z, a.m[3] * v.x + a.m[4] * v.y + a.m[5] * v.z,
            a.m[6] * v.x + a.m[7] * v.y + a.m[8] * v.z};
}
inline M3 Mulm(const M3& a, const M3& b) {
    M3 r;
    for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 3; ++j)
            r.m[i * 3 + j] = a.m[i * 3] * b.m[j] + a.m[i * 3 + 1] * b.m[3 + j] + a.m[i * 3 + 2] * b.m[6 + j];
    return r;
}
inline M3 Tr(const M3& a) { return {{a.m[0], a.m[3], a.m[6], a.m[1], a.m[4], a.m[7], a.m[2], a.m[5], a.m[8]}}; }
inline M3 Add(const M3& a, const M3& b) {
    M3 r;
    for (int i = 0; i < 9; ++i) r.m[i] = a.m[i] + b.m[i];
    return r;
}
inline M3 Scale(const M3& a, float s) {
    M3 r;
    for (int i = 0; i < 9; ++i) r.m[i] = a.m[i] * s;
    return r;
}
inline M3 Zero3() { return {{0, 0, 0, 0, 0, 0, 0, 0, 0}}; }
inline M3 Skew(V3 v) { return {{0, -v.z, v.y, v.z, 0, -v.x, -v.y, v.x, 0}}; }
// Domino's 3x3 inverse: adjugate / det, 0 when |det| is not above the guard
inline M3 Inverse(const M3& a) {
    const V3 r0{a.m[0], a.m[1], a.m[2]}, r1{a.m[3], a.m[4], a.m[5]}, r2{a.m[6], a.m[7], a.m[8]};
    const V3 c0 = Cross(r1, r2), c1 = Cross(r2, r0), c2 = Cross(r0, r1);
    const float det = Dot(r0, c0);
    const float k = std::fabs(det) > kTiny ? 1.0f / det : 0.0f;
    return {{c0.x * k, c1.x * k, c2.x * k, c0.y * k, c1.y * k, c2.y * k, c0.z * k, c1.z * k, c2.z * k}};
}
// the position solvers' 3x3 inverse: the same adjugate, 1 / det by rcp + Newton
inline M3 InverseFast(const M3& a) {
    const V3 r0{a.m[0], a.m[1], a.m[2]}, r1{a.m[3], a.m[4], a.m[5]}, r2{a.m[6], a.m[7], a.m[8]};
    const V3 c0 = Cross(r1, r2), c1 = Cross(r2, r0), c2 = Cross(r0, r1);
    const float k = RcpGuard(Dot(r0, c0));
    return {{c0.x * k, c1.x * k, c2.x * k, c0.y * k, c1.y * k, c2.y * k, c0.z * k, c1.z * k, c2.z * k}};
}

// 0x10323C300: rotation -> quaternion. The client hands it the matrix with the
// axis images as ROWS (m(r, c) = row r); the quaternion is built unnormalised
// from the largest of trace / diagonal and renormalised by rsqrt + Newton.
Q MatQ(const M3& M) {
    auto m = [&](int r, int c) { return M.m[r * 3 + c]; };
    const float m00 = m(0, 0), m11 = m(1, 1), m22 = m(2, 2);
    const float t = m22 + 1.0f + (m11 + m00);
    Q q;
    if (t >= 1.0f) {
        q = {m(1, 2) - m(2, 1), m(2, 0) - m(0, 2), m(0, 1) - m(1, 0), t};
    } else if (m00 > m11 && m00 > m22) {
        q = {(m00 + 1.0f) - (m11 + m22), m(0, 1) + m(1, 0), m(0, 2) + m(2, 0), m(1, 2) - m(2, 1)};
    } else if (m11 > m22) {
        q = {m(0, 1) + m(1, 0), (m11 + 1.0f) - (m00 + m22), m(2, 1) + m(1, 2), m(2, 0) - m(0, 2)};
    } else {
        q = {m(0, 2) + m(2, 0), m(2, 1) + m(1, 2), (1.0f - (m11 + m00)) + m22, m(0, 1) - m(1, 0)};
    }
    const float n2 = QN2(q);
    const float k = RsqrtN(n2);
    return {q.x * k, q.y * k, q.z * k, q.w * k};
}

// The client's fast atan2 (0.28 rational form), every quadrant as written
// (shoulder prepare 0x1006021C0 twist block).
float AtanApprox(float y, float x) {
    if (x == 0) return y > 0 ? kPi * 0.5f : (y == 0 ? 0.0f : -kPi * 0.5f);
    const float r = y / x;
    if (std::fabs(r) < 1.0f) {
        float a = r / (1.0f + 0.28f * r * r);
        if (x < 0) a += y >= 0 ? kPi : -kPi;
        return a;
    }
    return (y >= 0 ? kPi * 0.5f : -kPi * 0.5f) - r / (r * r + 0.28f);
}
inline float Wrap(float a) {   // into (-pi, pi]
    if (a < -kPi) a += 2 * kPi;
    else if (a > kPi) a -= 2 * kPi;
    return a;
}

// --- the private world ---------------------------------------------------------
struct Body {
    uint8_t type = kBodyDynamic;
    V3 x;                 // origin (the body frame), world
    Q q;
    V3 lc;                // local centre of mass
    V3 c;                 // centre of mass, world
    V3 v, w;
    float m = 0, im = 0;  // mass, inverse mass
    M3 I = Zero3(), iI = Zero3();   // local inertia about the COM (x inertia scale) and its inverse
    float gs = 1, ld = 0, ad = 0, is = 1;   // gravity scale, damping, inertia scale
    float sleep = 0;
    bool awake = true;
    bool gyro = true;          // +0x1B7 implicit gyroscopic step (body def + 0x3E)
    bool noAngCap = false;     // +0x1B5 no pi/4 rotation cap (body def + 0x3C)
    bool allowSleep = false;   // +0x1AD (body def + 0x38)
    std::vector<uint16_t> shapes;
    M3 R() const { return Mat(q); }
    M3 IWorldInv() const {   // R iI R^T
        const M3 r = R();
        return Mulm(Mulm(r, iI), Tr(r));
    }
    void SyncOrigin() { x = c - Rot(q, lc); }
    void SetTransform(V3 origin, Q rot) {
        x = origin;
        q = rot;
        c = x + Rot(q, lc);
    }
};

// --- shapes and mass (0x1005E8270 and the per-shape routines it calls) ------------
// A hull as the client keeps it (0x1005DCA30 builds it from PLYT): vertices,
// face planes, each face's first half-edge, half-edges of 4 bytes (+1 origin
// vertex, +3 next edge in the face); the file's centroid and volume are kept
// (scaled) and are what mass and centre use.
struct Hull {
    V3 centroid;
    float volume = 0, area = 0;
    std::vector<V3> v;
    std::vector<V3> n;              // face normals
    std::vector<float> d;           // face plane offsets
    std::vector<uint8_t> face;      // first half-edge of each face
    std::vector<uint8_t> edge;      // 4 bytes per half-edge
};

struct Shape {
    uint8_t type = kShapeCapsule;   // Domino 0 sphere, 1 capsule, 2 hull
    uint16_t body = 0;
    V3 a, b;            // sphere centre / capsule ends, body space, scaled
    float r = 0;
    float friction = 0.6f, restitution = 0, density = 1;
    Hull hull;
};

// a shape's inertia about a point p, with its own mass m and centre c
struct MassData { float m = 0; V3 c; M3 I = Zero3(); };

// I += m (|e|^2 E - e e^T), e = c - p
void ShiftTo(MassData& d, V3 p) {
    const V3 e = d.c - p;
    const float xx = e.x * e.x, yy = e.y * e.y, zz = e.z * e.z;
    d.I.m[0] += (yy + zz) * d.m;
    d.I.m[4] += (zz + xx) * d.m;
    d.I.m[8] += (xx + yy) * d.m;
    d.I.m[1] -= e.x * e.y * d.m; d.I.m[3] -= e.x * e.y * d.m;
    d.I.m[2] -= e.x * e.z * d.m; d.I.m[6] -= e.x * e.z * d.m;
    d.I.m[5] -= e.y * e.z * d.m; d.I.m[7] -= e.y * e.z * d.m;
}

// volumes (pass 1): 0x1005E16D0 sphere, 0x1005E1D40 capsule, the hull's kept volume
float ShapeVolume(const Shape& s) {
    if (s.type == kShapeSphere) return s.r * s.r * 4.18879032f * s.r;
    if (s.type == kShapeCapsule) {
        const V3 e = s.b - s.a;
        const float h = std::sqrt((e.z * e.z) + (e.x * e.x + e.y * e.y));
        return (h * kPi + s.r * 4.18879032f) * (s.r * s.r);
    }
    return s.hull.volume;
}
// centres (pass 1): sphere centre, capsule midpoint, the hull's kept centroid
V3 ShapeCentre(const Shape& s) {
    if (s.type == kShapeSphere) return s.a;
    if (s.type == kShapeCapsule) return (s.b + s.a) * 0.5f;
    return s.hull.centroid;
}

// 0x1005E15C0
MassData SphereInertia(const Shape& s, float rho, V3 p) {
    MassData d;
    d.m = s.r * s.r * (rho * 4.18879032f) * s.r;
    d.c = s.a;
    const float i = s.r * s.r * 0.4f * d.m;
    d.I = {{i, 0, 0, 0, i, 0, 0, 0, i}};
    ShiftTo(d, p);
    return d;
}

// 0x1005E1890: cylinder + two hemispheres, axis by rsqrt + Newton
MassData CapsuleInertia(const Shape& s, float rho, V3 p) {
    MassData d;
    const V3 ax = s.b - s.a;
    const float h2 = (ax.z * ax.z) + (ax.x * ax.x + ax.y * ax.y);
    const float rs = RsqrtN(h2);
    const float h = std::fabs(h2) < 1.17549435e-38f ? 0.0f : h2 * rs;
    const float r = s.r, r2 = r * r;
    const float rr = rho * r2;
    const float ms = 2.0f * (r * (2.09439516f * rr));   // two hemispheres
    const float mc = h * (kPi * rr);                    // cylinder
    d.m = ms + mc;
    d.c = (s.b + s.a) * 0.5f;
    const float off = 0.375f * r + h * 0.5f;
    const float Ipe = (off * off + r2 * 0.259375f) * ms + (r2 * 0.25f + h2 * 0.0833333358f) * mc;
    const float Iax = (mc * 0.5f + ms * 0.4f) * r2;
    V3 u{0, 0, 1};
    if (h > 0) u = ax * rs;
    M3 I = {{Ipe, 0, 0, 0, Ipe, 0, 0, 0, Ipe}};
    const float k = Iax - Ipe;
    I.m[0] += k * u.x * u.x; I.m[1] += k * u.x * u.y; I.m[2] += k * u.x * u.z;
    I.m[3] += k * u.y * u.x; I.m[4] += k * u.y * u.y; I.m[5] += k * u.y * u.z;
    I.m[6] += k * u.z * u.x; I.m[7] += k * u.z * u.y; I.m[8] += k * u.z * u.z;
    d.I = I;
    ShiftTo(d, p);
    return d;
}

// 0x1005DCF30: the polyhedral mass integrals (Eberly) over the hull's faces,
// fanned from each face's first vertex, about vertex 0; centre by rcp + Newton
MassData HullInertia(const Hull& H, float rho, V3 p) {
    MassData d;
    const V3 ref = H.v.empty() ? V3{} : H.v[0];
    float i0 = 0;
    V3 i1, i2, i3;
    auto perm = [](V3 a) { return V3{a.y, a.z, a.x}; };
    for (size_t f = 0; f < H.face.size(); ++f) {
        const uint8_t e0 = H.face[f];
        const V3 w0 = H.v[H.edge[e0 * 4 + 1]] - ref;
        uint8_t e = H.edge[e0 * 4 + 3];
        V3 w1 = H.v[H.edge[e * 4 + 1]] - ref;
        e = H.edge[e * 4 + 3];
        const V3 t1 = Mulv(w0, w0);
        for (int guard = 0; guard < 256; ++guard) {
            const V3 w2 = H.v[H.edge[e * 4 + 1]] - ref;
            const V3 n = Cross(w1 - w0, w2 - w0);
            const V3 t0 = w1 + w0;
            const V3 f1 = w2 + t0;
            const V3 t2 = Mulv(t0, w1) + t1;
            const V3 f2 = Mulv(f1, w2) + t2;
            const V3 f3 = Mulv(f2, w2) + (Mulv(t2, w1) + Mulv(t1, w0));
            const V3 g0 = Mulv(f1 + w0, w0) + f2;
            const V3 g1 = Mulv(f1 + w1, w1) + f2;
            const V3 g2 = Mulv(f1 + w2, w2) + f2;
            const V3 gs = (Mulv(perm(w1), g1) + Mulv(perm(w0), g0)) + Mulv(perm(w2), g2);
            i0 += f1.x * n.x;
            i1 = i1 + Mulv(f2, n);
            i2 = i2 + Mulv(f3, n);
            i3 = i3 + Mulv(gs, n);
            w1 = w2;
            e = H.edge[e * 4 + 3];
            if (e == e0) break;
        }
    }
    const float vol = i0 * 0.166666672f;
    i1 = i1 * 0.0416666679f;
    i2 = i2 * 0.0166666675f;
    i3 = i3 * 0.00833333377f;
    V3 c;
    if (vol > 0) {
        const float r = EstRcp(vol);
        const V3 q = i1 * r;
        c = (i1 - q * vol) * r + q;
    }
    d.m = vol * rho;
    d.c = ref + c;
    // about the reference vertex, then to the centre, then to p
    M3 I = {{(i2.y + i2.z) * rho, -i3.x * rho, -i3.z * rho, -i3.x * rho, (i2.z + i2.x) * rho, -i3.y * rho,
             -i3.z * rho, -i3.y * rho, (i2.x + i2.y) * rho}};
    MassData back;
    back.m = d.m;
    back.c = c;
    ShiftTo(back, {});
    for (int k = 0; k < 9; ++k) I.m[k] -= back.I.m[k];
    d.I = I;
    ShiftTo(d, p);
    return d;
}

MassData ShapeInertia(const Shape& s, float rho, V3 p) {
    if (s.type == kShapeSphere) return SphereInertia(s, rho, p);
    if (s.type == kShapeCapsule) return CapsuleInertia(s, rho, p);
    return HullInertia(s.hull, rho, p);
}

// --- joints ------------------------------------------------------------------------
struct Joint {
    uint8_t type = kJointShoulder;
    uint16_t a = 0, b = 0;
    V3 pa, pb;          // frame positions (x scale at creation)
    Q qa, qb;           // frame rotations
    // shoulder (0x100601F80)
    float twLo = 0, twHi = 0, cone = kPi, maxT = 0, hz = 1, zeta = 0.7f;
    int8_t mode = 0;
    uint8_t twState = 0, coneState = 1;
    V3 twAxis;          // persists while the swing is past 170 deg
    float twMass = 0;
    // revolute (0x1005FF280): 0x2C8 centre, 0x2CC half range, 0x2BC limit
    // state (0 = no limit), 0x2B8 axial mass
    float revCenter = 0, revHalf = 0;
    uint8_t limState = 0;
    float limMass = 0, hingeK = 0;
    // weld (0x1006063B0): 0x1F0 angular Hz, 0x1F4 angular zeta, 0x1F8 linear
    // Hz, 0x1FC linear zeta
    float angHz = 0, angZeta = 0, linHz = 0, linZeta = 0;
    // accumulated impulses
    V3 Plin, Pmot, Pang;
    float Ptw = 0, Pcone = 0, Pmot1 = 0, Plim = 0;
    // per-step solver data
    V3 rA, rB, axisT, axisC, iaT, ibT, iaC, ibC;
    M3 Klin = Zero3(), Kmot = Zero3(), Kang = Zero3();
    V3 motBias, motGamma;
    float motMax = 0, coneMass = 0;
    float m1Mass = 0, m1Bias = 0, m1Gamma = 0, m1Max = 0;
    V3 hingeAxis, iaH, ibH;          // 0xB0, 0xC0, 0xD0
    V3 lockJ1, lockJ2;               // 0x1E0..0x200 (stored transposed)
    float lockK[4] = {};             // 0x220 / 0x230: the 2x2 inverse
    V3 angBias, angGamma, linBias, linGamma;
    float angCap = kFltMax, angCap2 = kFltMax;   // weld 0xC0 / 0xD0
};

/// --- contacts ------------------------------------------------------------------------
// The manifold (0x100 bytes in the client): up to 4 points, each with its
// world point, 8-byte feature key (+0x10), separation (+0x18), normal impulse
// (+0x1C) and new-point flag (+0x22); the summed normal (+0xC0, normalised by
// the narrowphase); the friction state carried between steps: world tangent
// impulse (+0xD0), rolling impulse (+0xE0), twist impulse (+0xF0).
struct ManifoldPoint {
    V3 p;
    uint64_t key = 0;
    float sep = 0;
    float Pn = 0;
    bool fresh = true;
};
struct Manifold {
    int n = 0;
    ManifoldPoint pt[4];
    V3 normal;
    V3 Pt, Proll;
    float Ptw = 0;
};
// the per-step solver data of one point (0x60 + 0x30 bytes in the client)
struct ContactPoint {
    V3 rAn, iArAn, rBn, iBrBn;
    float mn = 0, gamma = 0, bias = 0, Pn = 0;
    V3 la, lb;                       // body-local anchors (position solve)
    float sep = 0, posMass = 0;
};
struct Contact {
    uint16_t sa = 0, sb = 0, ba = 0, bb = 0;
    float friction = 0, restitution = 0, rolling = 0;   // mixed at creation
    Manifold m;
    // the hull narrowphases' GJK simplex cache (12 bytes in the client: the
    // metric, then the simplex's vertex indices on A and on B; 0xFF on A ends it)
    // ...and the hull-hull SAT cache (8 bytes: separation, two feature
    // indices, type 1 face of A / 2 face of B / 3 edge pair; 0 = none)
    struct Cache {
        float metric = 0;
        uint8_t a[4] = {0xFF, 0xFF, 0xFF, 0xFF}, b[4] = {0, 0, 0, 0};
        float satSep = 0;
        uint8_t satA = 0, satB = 0, satType = 0;
    } cache;
    // solver (0x1F0 + 0xB0 bytes in the client)
    bool soft = false;
    int rigidPoints = 0;
    ContactPoint cp[4];
    V3 n, t1, t2, center, rAt1, rBt1, rAt2, rBt2, iAt1, iBt1, iAt2, iBt2;
    float k11 = 0, k12 = 0, k22 = 0, twMass = 0;
    float mu = 0, muTw = 0, roll = 0;
    M3 rollK = Zero3();
    V3 lam;                          // tangent 1, tangent 2, twist
    V3 lamRoll;
    float imA = 0, imB = 0, mA = 0, mB = 0;
    M3 iAw = Zero3(), iBw = Zero3();  // world inverse inertia at prepare
};
struct StepData {
    float h = 0, inv = 0, dtRatio = 0;
    bool warm = true;
};

// broadphase proxy: the fat box the client's tree keeps (margin 0.1, refit
// when the tight box leaves it or it grows past 4 margins, 0x1005D3280)
struct Proxy { V3 lo, hi; bool moved = true; };

}  // namespace

// ===========================================================================
//  The private world of one item instance.
// ===========================================================================
struct Instance::World {
    std::vector<Body> bodies;
    std::vector<Shape> shapes;
    std::vector<Proxy> proxies;
    std::vector<Joint> joints;
    std::vector<Contact> contacts;
    std::vector<uint32_t> joined;        // (min << 16 | max) of jointed bodies
    V3 gravity{0, 0, -10};               // 0x103523460
    float invDt0 = 0;
    bool islandAwake = true;
    // ------------------------------------------------------------------
    void ResetMass(Body& b);   // 0x1005E8270
    // joints
    void PrepareShoulder(Joint& j, const StepData& st);
    void VelShoulder(Joint& j, const StepData& st);
    void PosShoulder(Joint& j);
    void PrepareRevolute(Joint& j, const StepData& st);
    void VelRevolute(Joint& j, const StepData& st);
    void PosRevolute(Joint& j);
    void PrepareWeld(Joint& j, const StepData& st);
    void VelWeld(Joint& j, const StepData& st);
    void PosWeld(Joint& j);
    // contacts
    void TightBox(const Shape& s, V3* lo, V3* hi) const;
    void SyncProxies();
    void FindNewContacts();
    void Collide();
    void Narrow(Contact& c, Manifold& out);
    void PrepareContacts(const StepData& st);
    void VelContacts();
    void StoreImpulses();
    void PosContacts();
    // the step
    void Step(float dt, int velIters, int posIters);
    void Solve(const StepData& st, int velIters, int posIters);
};

namespace {
// shared joint helpers --------------------------------------------------------
inline void ApplyAng(Body& b, const M3& iIw, V3 L, float sign) {
    if (b.type != kBodyDynamic) return;
    b.w = b.w + Mulm(iIw, L) * sign;
}
inline void ApplyLin(Body& b, V3 P, V3 r, const M3& iIw, float sign) {
    if (b.type != kBodyDynamic) return;
    b.v = b.v + P * (b.im * sign);
    b.w = b.w + Mulm(iIw, Cross(r, P)) * sign;
}
// position-level: move c and q
inline void MovePos(Body& b, V3 P, V3 r, const M3& iIw, float sign) {
    if (b.type != kBodyDynamic) return;
    b.c = b.c + P * (b.im * sign);
    b.q = Integrate(b.q, Mulm(iIw, Cross(r, P)) * sign);
}
inline void MoveAng(Body& b, V3 dtheta) {
    if (b.type != kBodyDynamic) return;
    b.q = Integrate(b.q, dtheta);
}
// soft constraint (Box2D 2.x form the client uses): per axis effective mass m;
// w = 2 pi Hz, c = 2 zeta w m, k = w^2 m, gamma = 1 / ((k h + c) h) by rcp +
// Newton (0 under the guard), bias = C x h k gamma
inline void Soft(float hz, float zeta, float m, float h, float* gamma, float* biasK) {
    const float omega = 2 * kPi * hz;
    const float c = (zeta + zeta) * (omega * m), k = omega * omega * m;
    const float g = RcpGuard((k * h + c) * h);
    *gamma = g;
    *biasK = h * g * k;   // bias = C * biasK
}
}  // namespace

void Instance::World::ResetMass(Body& b) {
    // pass 1: mass = sum density x volume (finite densities above 0; a body
    // that is not dynamic counts density 1), centre = sum m c / M
    float M = 0;
    V3 mc;
    for (uint16_t si : b.shapes) {
        const Shape& s = shapes[si];
        const float rho = b.type != kBodyDynamic ? 1.0f : s.density;
        if (!std::isfinite(rho) || !(rho > 0)) continue;
        const float m = ShapeVolume(s) * rho;
        mc = mc + ShapeCentre(s) * m;
        M = M + m;
    }
    float im = 0;
    V3 lc;
    if (std::isfinite(M) && M > kTiny) {
        im = 1.0f / M;
        lc = mc * im;
    } else {
        M = 0;
    }
    // pass 2: each shape's inertia about the centre
    M3 I = Zero3();
    for (uint16_t si : b.shapes) {
        const Shape& s = shapes[si];
        const float rho = b.type != kBodyDynamic ? 1.0f : s.density;
        if (!std::isfinite(rho) || !(rho > 0)) continue;
        I = Add(I, ShapeInertia(s, rho, lc).I);
    }
    // x inertia scale (BDY4 0x1C), inverted when det is finite and above the
    // guard, else both zero
    const V3 r0{I.m[0], I.m[1], I.m[2]}, r1{I.m[3], I.m[4], I.m[5]}, r2{I.m[6], I.m[7], I.m[8]};
    const float det = Dot(r0, Cross(r1, r2));
    if (std::isfinite(det) && det > kTiny) {
        b.I = Scale(I, b.is);
        b.iI = Inverse(b.I);
    } else {
        b.I = b.iI = Zero3();
    }
    b.lc = lc;
    if (b.type != kBodyDynamic) {
        b.m = b.im = 0;
        b.I = b.iI = Zero3();
    } else {
        b.m = M;
        b.im = im;
    }
    b.c = b.x + Rot(b.q, b.lc);
}

// --- shoulder (Domino type 7) ------------------------------------------------------
// prepare 0x1006021C0, velocity 0x100603590, position 0x1006039B0
namespace {
struct Swing { float angle = 0; V3 axis; };
// frames' Z axes a, b: swing = atan2(|a x b|, a.b), axis = (a x b) / |a x b|.
// Prepare and the cone position step take |a x b| and the axis from rsqrt +
// Newton (|c|^2 summed (x^2 + z^2) + y^2; under the guard: length 0, the axis
// left raw); the twist position step takes the length by an exact sqrt.
Swing SwingOf(Q wfA, Q wfB, bool exact = false) {
    const V3 a = Rot(wfA, {0, 0, 1}), b = Rot(wfB, {0, 0, 1});
    const V3 c = Cross(a, b);
    const float d = Dot(a, b);
    Swing s;
    s.axis = c;
    if (exact) {
        const float c2 = (c.z * c.z + (c.x * c.x + c.y * c.y));
        s.angle = AtanApprox(std::sqrt(c2), d);
        return s;
    }
    const float c2 = c.y * c.y + (c.z * c.z + c.x * c.x);
    float l = 0;
    if (c2 > kTiny) {
        const float r = RsqrtN(c2);
        l = std::fabs(c2) < 1.17549435e-38f ? 0.0f : c2 * r;
        s.axis = c * r;
    }
    s.angle = AtanApprox(l, d);
    return s;
}
float TwistOf(Q wfA, Q wfB, Q* rel) {
    const Q r = Mul(Conj(wfA), wfB);
    if (rel) *rel = r;
    return Wrap(2.0f * AtanApprox(r.z, r.w));
}
// the error vector of frame B against frame A, world: 2 vec(qB qA^-1), shortest arc
V3 RotError(Q wfA, Q wfB) {
    Q e = Mul(wfB, Conj(wfA));
    if (e.w < 0) e = {-e.x, -e.y, -e.z, -e.w};
    return QV(e) * 2.0f;
}
inline M3 PointK(const Body& A, const Body& B, V3 rA, V3 rB, const M3& iA, const M3& iB) {
    const M3 sA = Skew(rA), sB = Skew(rB);
    M3 K = Add(Mulm(Mulm(sA, iA), Tr(sA)), Mulm(Mulm(sB, iB), Tr(sB)));
    const float ms = A.im + B.im;
    K.m[0] += ms;
    K.m[4] += ms;
    K.m[8] += ms;
    return K;
}
}  // namespace

void Instance::World::PrepareShoulder(Joint& j, const StepData& st) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
    const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
    const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
    // motor
    const M3 Kang = Add(iA, iB);
    if (j.mode == 0) {
        j.Kmot = Zero3();
        j.Pmot = {};
        j.motMax = 0;
        j.motBias = j.motGamma = {};
    } else if (j.mode == 1) {
        const M3 M = Inverse(Kang);
        const V3 m{M.m[0], M.m[4], M.m[8]};
        const V3 C = RotError(wfA, wfB);
        float g[3], bk[3];
        Soft(j.hz, j.zeta, m.x, st.h, &g[0], &bk[0]);
        Soft(j.hz, j.zeta, m.y, st.h, &g[1], &bk[1]);
        Soft(j.hz, j.zeta, m.z, st.h, &g[2], &bk[2]);
        j.motGamma = {g[0], g[1], g[2]};
        j.motBias = {C.x * bk[0], C.y * bk[1], C.z * bk[2]};
        M3 K = Kang;
        K.m[0] += g[0];
        K.m[4] += g[1];
        K.m[8] += g[2];
        j.Kmot = Inverse(K);
        j.motMax = kFltMax;            // position motors ignore the authored torque
    } else {
        j.Kmot = Inverse(Kang);
        j.motBias = {};                // the target velocity (0x290) is zero for .phys
        j.motGamma = {};
        j.motMax = j.maxT;
    }
    // swing / twist
    const Swing sw = SwingOf(wfA, wfB);
    Q rel;
    const float tw = TwistOf(wfA, wfB, &rel);
    bool twActive = false;
    if (j.twState != 0) {
        if (std::fabs(j.twHi - j.twLo) < 0.069813177f) {
            j.twState = 4;
            twActive = true;
        } else if (tw <= j.twLo) {
            if (j.twState != 2) { j.twState = 2; j.Ptw = 0; }
            twActive = true;
        } else if (tw >= j.twHi) {
            if (j.twState != 3) { j.twState = 3; j.Ptw = 0; }
            twActive = true;
        } else {
            j.twState = 1;
            j.Ptw = 0;
        }
    } else {
        j.Ptw = 0;
    }
    if (twActive && sw.angle < 2.96705961f) {
        const float qq = rel.w * rel.w + rel.z * rel.z;
        const float sc = RcpGuard(qq);
        const V3 loc{rel.x * rel.z + rel.y * rel.w, rel.y * rel.z - rel.x * rel.w, qq};
        j.twAxis = Rot(wfA, loc) * sc;
        j.iaT = Mulm(iA, j.twAxis);
        j.ibT = Mulm(iB, j.twAxis);
        const float k = Dot(j.twAxis, j.iaT + j.ibT);
        j.twMass = std::fabs(k) > kTiny ? 1.0f / k : 0.0f;
    }
    // cone
    if (sw.angle >= j.cone) {
        if (j.coneState != 3) { j.coneState = 3; j.Pcone = 0; }
        j.axisC = sw.axis;
        j.iaC = Mulm(iA, j.axisC);
        j.ibC = Mulm(iB, j.axisC);
        const float k = Dot(j.axisC, j.iaC + j.ibC);
        j.coneMass = std::fabs(k) > kTiny ? 1.0f / k : 0.0f;
    } else {
        j.coneState = 1;
        j.Pcone = 0;
    }
    // point
    j.rA = Rot(A.q, j.pa - A.lc);
    j.rB = Rot(B.q, j.pb - B.lc);
    j.Klin = Inverse(PointK(A, B, j.rA, j.rB, iA, iB));
    // warm start
    if (st.warm) {
        j.Plin = j.Plin * st.dtRatio;
        j.Pmot = j.Pmot * st.dtRatio;
        j.Ptw *= st.dtRatio;
        j.Pcone *= st.dtRatio;
        const V3 LA = Cross(j.rA, j.Plin), LB = Cross(j.rB, j.Plin);
        if (A.type == kBodyDynamic) {
            A.v = A.v - j.Plin * A.im;
            A.w = A.w - Mulm(iA, LA + j.Pmot) - j.iaT * j.Ptw - j.iaC * j.Pcone;
        }
        if (B.type == kBodyDynamic) {
            B.v = B.v + j.Plin * B.im;
            B.w = B.w + Mulm(iB, LB + j.Pmot) + j.ibT * j.Ptw + j.ibC * j.Pcone;
        }
    } else {
        j.Plin = j.Pmot = {};
        j.Ptw = j.Pcone = 0;
    }
}

void Instance::World::VelShoulder(Joint& j, const StepData& st) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
    const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
    // motor (3D, soft, clamped by length)
    if (j.mode != 0) {
        const V3 cd = B.w - A.w + j.motBias + Mulv(j.motGamma, j.Pmot);
        V3 np = j.Pmot - Mulm(j.Kmot, cd);
        const float mx = j.motMax * st.h;
        const float l2 = Len2(np);
        if (!(l2 <= mx * mx)) np = np * (mx / std::sqrt(l2));
        const V3 d = np - j.Pmot;
        j.Pmot = np;
        if (A.type == kBodyDynamic) A.w = A.w - Mulm(iA, d);
        if (B.type == kBodyDynamic) B.w = B.w + Mulm(iB, d);
    }
    // twist limit
    if (j.twState >= 2) {
        const float cd = Dot(B.w - A.w, j.twAxis);
        const float l = -cd * j.twMass;
        float d;
        if (j.twState == 2) { const float n = std::max(j.Ptw + l, 0.0f); d = n - j.Ptw; j.Ptw = n; }
        else if (j.twState == 3) { const float n = std::min(j.Ptw + l, 0.0f); d = n - j.Ptw; j.Ptw = n; }
        else { j.Ptw += l; d = l; }
        A.w = A.w - j.iaT * d;
        B.w = B.w + j.ibT * d;
    }
    // cone limit
    if (j.coneState == 3) {
        const float cd = Dot(B.w - A.w, j.axisC);
        const float n = std::min(j.Pcone - cd * j.coneMass, 0.0f);
        const float d = n - j.Pcone;
        j.Pcone = n;
        A.w = A.w - j.iaC * d;
        B.w = B.w + j.ibC * d;
    }
    // point
    const V3 cd = B.v + Cross(B.w, j.rB) - A.v - Cross(A.w, j.rA);
    const V3 l = -Mulm(j.Klin, cd);
    j.Plin = j.Plin + l;
    ApplyLin(A, l, j.rA, iA, -1);
    ApplyLin(B, l, j.rB, iB, +1);
}

void Instance::World::PosShoulder(Joint& j) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    constexpr float kSlop = 0.0349065885f, kMax = 0.52359879f, kBeta = 0.2f;
    // twist: runs while the limit is on and the swing (exact sqrt) is under
    // 170 deg; inside the limits the impulse is zero but the rotations are
    // still renormalised
    {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
        const Swing sw = SwingOf(wfA, wfB, true);
        Q rel;
        const float tw = TwistOf(wfA, wfB, &rel);
        if (j.twState != 0 && sw.angle < 2.96705961f) {
            const float qq = rel.w * rel.w + rel.z * rel.z;
            const float sc = RcpGuard(qq);
            const V3 axis = Rot(wfA, V3{rel.x * rel.z + rel.y * rel.w, rel.y * rel.z - rel.x * rel.w, qq}) * sc;
            const V3 ia = Mulm(iA, axis), ib = Mulm(iB, axis);
            const float k = Dot(ia + ib, axis);
            const float mass = std::fabs(k) > kTiny ? 1.0f / k : 0.0f;
            float C = 0;
            if (j.twState == 4) C = std::max(std::min(tw, kMax), -kMax) * kBeta;
            else if (tw > j.twLo) {
                if (tw >= j.twHi) C = std::max(0.0f, std::min(tw - j.twHi + -kSlop, kMax)) * kBeta;
            } else C = std::max(std::min(0.0f, tw - j.twLo + kSlop), -kMax) * kBeta;
            const float imp = -mass * C;
            MoveAng(A, ia * -imp);
            MoveAng(B, ib * imp);
        }
    }
    // cone: swing (rsqrt) at or past the cone
    {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
        const Swing sw = SwingOf(wfA, wfB);
        if (sw.angle >= j.cone) {
            const V3 ia = Mulm(iA, sw.axis), ib = Mulm(iB, sw.axis);
            const float k = Dot(ia + ib, sw.axis);
            const float mass = std::fabs(k) > kTiny ? 1.0f / k : 0.0f;
            const float C = std::max(0.0f, std::min(sw.angle - j.cone + -kSlop, kMax)) * kBeta;
            const float imp = -mass * C;
            MoveAng(A, ia * -imp);
            MoveAng(B, ib * imp);
        }
    }
    // point: full correction, 1 / det by rcp + Newton
    {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const V3 rA = Rot(A.q, j.pa - A.lc), rB = Rot(B.q, j.pb - B.lc);
        const V3 C = (B.c + rB) - (A.c + rA);
        const V3 P = -Mulm(InverseFast(PointK(A, B, rA, rB, iA, iB)), C);
        MovePos(A, P, rA, iA, -1);
        MovePos(B, P, rB, iB, +1);
    }
    A.SyncOrigin();
    B.SyncOrigin();
}

// --- revolute (Domino type 6) -------------------------------------------------------
// ctor 0x1005FF280, prepare 0x1005FF410, velocity 0x1006003C0, position 0x100600840
namespace {
// the hinge angle 2 atan(rel.z, rel.w) less the limits' centre, wrapped once by
// 4 pi into [-2 pi, 2 pi]
float HingeOf(const Joint& j, const Q& rel) {
    float a = 2.0f * AtanApprox(rel.z, rel.w) - j.revCenter;
    if (a < -2 * kPi) a += 4 * kPi;
    else if (a > 2 * kPi) a -= 4 * kPi;
    return a;
}
// the angular lock's rows: d(rel.x, rel.y)/dt = J (wB - wA),
// J_i = 0.5 Rot(wfA, rel.w e_i + rel.v x e_i)
void LockRows(Q wfA, const Q& rel, V3* J1, V3* J2) {
    const V3 rv = QV(rel);
    *J1 = Rot(wfA, V3{rel.w, 0, 0} + Cross(rv, V3{1, 0, 0})) * 0.5f;
    *J2 = Rot(wfA, V3{0, rel.w, 0} + Cross(rv, V3{0, 1, 0})) * 0.5f;
}
}  // namespace

void Instance::World::PrepareRevolute(Joint& j, const StepData& st) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
    const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
    const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
    const Q rel = Mul(Conj(wfA), wfB);
    // hinge axis (frame A's Z) and its axial mass 0x2B8 = 1 / K
    j.hingeAxis = Rot(wfA, {0, 0, 1});
    j.iaH = Mulm(iA, j.hingeAxis);
    j.ibH = Mulm(iB, j.hingeAxis);
    j.hingeK = Dot(j.iaH + j.ibH, j.hingeAxis);
    j.limMass = std::fabs(j.hingeK) > kTiny ? 1.0f / j.hingeK : 0.0f;
    // angular lock: 2 rows, 2x2 inverse (exact divide)
    {
        LockRows(wfA, rel, &j.lockJ1, &j.lockJ2);
        const M3 K = Add(iA, iB);
        const V3 K1 = Mulm(K, j.lockJ1), K2 = Mulm(K, j.lockJ2);
        const float a = Dot(j.lockJ1, K1), b = Dot(j.lockJ1, K2), c = Dot(j.lockJ2, K1), d = Dot(j.lockJ2, K2);
        const float det = a * d - b * c;
        const float k = std::fabs(det) > kTiny ? 1.0f / det : 0.0f;
        j.lockK[0] = d * k;
        j.lockK[1] = -b * k;
        j.lockK[2] = -c * k;
        j.lockK[3] = a * k;
    }
    // point
    j.rA = Rot(A.q, j.pa - A.lc);
    j.rB = Rot(B.q, j.pb - B.lc);
    j.Klin = Inverse(PointK(A, B, j.rA, j.rB, iA, iB));
    // limit state (0x2BC): locked keeps its impulse; otherwise the impulse
    // resets whenever the state changes, and always inside the range
    const float ang = HingeOf(j, rel);
    if (j.limState != 0) {
        if (j.revHalf < 0.0349065885f) {
            j.limState = 4;
        } else if (ang > -j.revHalf) {
            if (ang >= j.revHalf) {
                if (j.limState != 3) { j.limState = 3; j.Plim = 0; }
            } else {
                j.limState = 1;
                j.Plim = 0;
            }
        } else if (j.limState != 2) {
            j.limState = 2;
            j.Plim = 0;
        }
    } else {
        j.Plim = 0;
    }
    // motor (0x2A8): 1 = position (soft towards the centre, max FLT_MAX),
    // 0 = off, else velocity (target speed 0, max = maxTorque)
    if (j.mode == 1) {
        const float w = 2 * kPi * j.hz;
        const float c = (j.zeta + j.zeta) * (w * j.limMass), k = w * w * j.limMass;
        const float g = RcpGuard((k * st.h + c) * st.h);
        j.m1Gamma = g;
        j.m1Bias = (st.h * g) * (k * ang);
        const float km = g + j.hingeK;
        j.m1Mass = std::fabs(km) > kTiny ? 1.0f / km : 0.0f;
        j.m1Max = kFltMax;
    } else if (j.mode == 0) {
        j.m1Mass = j.m1Bias = j.m1Gamma = j.m1Max = 0;
        j.Pmot1 = 0;
    } else {
        j.m1Mass = j.limMass;
        j.m1Bias = -0.0f;
        j.m1Gamma = 0;
        j.m1Max = j.maxT;
    }
    if (st.warm) {
        j.Plin = j.Plin * st.dtRatio;
        j.Pang = j.Pang * st.dtRatio;
        j.Pmot1 *= st.dtRatio;
        j.Plim *= st.dtRatio;
        const V3 L = j.lockJ1 * j.Pang.x + j.lockJ2 * j.Pang.y;
        const float ax = j.Plim + j.Pmot1;
        if (A.type == kBodyDynamic) {
            A.v = A.v - j.Plin * A.im;
            A.w = A.w - Mulm(iA, Cross(j.rA, j.Plin)) - Mulm(iA, L) - j.iaH * ax;
        }
        if (B.type == kBodyDynamic) {
            B.v = B.v + j.Plin * B.im;
            B.w = B.w + Mulm(iB, Cross(j.rB, j.Plin)) + Mulm(iB, L) + j.ibH * ax;
        }
    } else {
        j.Plin = j.Pang = {};
        j.Pmot1 = j.Plim = 0;
    }
}

void Instance::World::VelRevolute(Joint& j, const StepData& st) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
    const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
    // motor (always solved; off = zero mass and zero bound)
    {
        const float cd = Dot(B.w - A.w, j.hingeAxis) + j.m1Bias;
        const float np0 = j.Pmot1 - (j.m1Gamma * j.Pmot1 + cd) * j.m1Mass;
        const float mx = st.h * j.m1Max;
        const float np = np0 < -mx ? -mx : std::min(mx, np0);
        const float d = np - j.Pmot1;
        j.Pmot1 = np;
        A.w = A.w - j.iaH * d;
        B.w = B.w + j.ibH * d;
    }
    // limit: 2 lower (>= 0), 3 upper (<= 0), 4 locked (free)
    if (j.limState >= 2) {
        float l = -Dot(B.w - A.w, j.hingeAxis) * j.limMass;
        if (j.limState == 2) { const float n = std::max(0.0f, j.Plim + l); l = n - j.Plim; j.Plim = n; }
        else if (j.limState == 3) { const float n = std::min(0.0f, j.Plim + l); l = n - j.Plim; j.Plim = n; }
        else if (j.limState == 4) j.Plim += l;
        A.w = A.w - j.iaH * l;
        B.w = B.w + j.ibH * l;
    }
    // angular lock (2 rows)
    {
        const V3 dw = B.w - A.w;
        const float c1 = Dot(j.lockJ1, dw), c2 = Dot(j.lockJ2, dw);
        const float l1 = -c2 * j.lockK[2] - c1 * j.lockK[0];
        const float l2 = -c2 * j.lockK[3] - c1 * j.lockK[1];
        j.Pang.x += l1;
        j.Pang.y += l2;
        const V3 L = j.lockJ1 * l1 + j.lockJ2 * l2;
        A.w = A.w - Mulm(iA, L);
        B.w = B.w + Mulm(iB, L);
    }
    // point
    const V3 cd = B.v + Cross(B.w, j.rB) - A.v - Cross(A.w, j.rA);
    const V3 l = -Mulm(j.Klin, cd);
    j.Plin = j.Plin + l;
    ApplyLin(A, l, j.rA, iA, -1);
    ApplyLin(B, l, j.rB, iB, +1);
}

void Instance::World::PosRevolute(Joint& j) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    constexpr float kSlop = 0.0349065885f, kMax = 0.52359879f, kBeta = 0.2f;
    // limit (when the joint has one): prepare's state picks locked or the
    // range test; inside the range the impulse is zero but the rotations are
    // still renormalised
    if (j.limState != 0) {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
        const float ang = HingeOf(j, Mul(Conj(wfA), wfB));
        const V3 axis = Rot(wfA, {0, 0, 1});
        const V3 ia = Mulm(iA, axis), ib = Mulm(iB, axis);
        const float mass = RcpGuard(Dot(ia + ib, axis));
        float C = 0;
        if (j.limState == 4) C = std::max(std::min(ang, kMax), -kMax) * kBeta;
        else if (ang > -j.revHalf) {
            if (ang >= j.revHalf) C = std::max(0.0f, std::min(ang - j.revHalf + -kSlop, kMax)) * kBeta;
        } else C = std::max(std::min(0.0f, ang + kSlop + j.revHalf), -kMax) * kBeta;
        const float imp = C * -mass;
        MoveAng(A, ia * -imp);
        MoveAng(B, ib * imp);
    }
    // angular lock: C = (rel.x, rel.y), full correction; the 2x2 from the
    // current rows (1 / det by rcp + Newton), applied along prepare's rows
    {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
        const Q rel = Mul(Conj(wfA), wfB);
        V3 J1, J2;
        LockRows(wfA, rel, &J1, &J2);
        const M3 K = Add(iA, iB);
        const V3 K1 = Mulm(K, J1), K2 = Mulm(K, J2);
        const float a = Dot(J1, K1), b = Dot(J1, K2), c = Dot(J2, K1), d = Dot(J2, K2);
        const float k = RcpGuard(a * d - b * c);
        const float e1 = -rel.x, e2 = -rel.y;
        const float l1 = (d * e1 - b * e2) * k, l2 = (a * e2 - c * e1) * k;
        const V3 L = j.lockJ1 * l1 + j.lockJ2 * l2;
        MoveAng(A, Mulm(iA, L) * -1.0f);
        MoveAng(B, Mulm(iB, L));
    }
    // point: full correction, 1 / det by rcp + Newton
    {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const V3 rA = Rot(A.q, j.pa - A.lc), rB = Rot(B.q, j.pb - B.lc);
        const V3 C = (B.c + rB) - (A.c + rA);
        const V3 P = -Mulm(InverseFast(PointK(A, B, rA, rB, iA, iB)), C);
        MovePos(A, P, rA, iA, -1);
        MovePos(B, P, rB, iB, +1);
    }
    A.SyncOrigin();
    B.SyncOrigin();
}

// --- weld (Domino type 9) ---------------------------------------------------------------
// ctor 0x1006063B0, prepare 0x1006064E0, velocity 0x100607320, position 0x100607690
namespace {
// the weld's rotation error, world: 2 Rot(wfA, rel.v), rel = conj(fA) fB on
// the short arc (w >= 0)
V3 WeldError(Q wfA, Q wfB) {
    Q rel = Mul(Conj(wfA), wfB);
    if (rel.w < 0) rel = {-rel.x, -rel.y, -rel.z, -rel.w};
    return Rot(wfA, QV(rel) * 2.0f);
}
// per-axis soft terms: c = (4 pi Hz) zeta m, k = (2 pi Hz)^2 m,
// gamma = 1 / ((k h + c) h) (rcp + Newton), bias = (k h) gamma C
void WeldSoft(float hz, float zeta, const M3& M, V3 C, float h, V3* gamma, V3* bias) {
    const float m[3] = {M.m[0], M.m[4], M.m[8]};
    const float c[3] = {C.x, C.y, C.z};
    const float w = 2 * kPi * hz;
    float g[3], b[3];
    for (int i = 0; i < 3; ++i) {
        const float cc = (hz * 12.566371f) * zeta * m[i];
        const float kk = w * w * m[i];
        g[i] = RcpGuard((kk * h + cc) * h);
        b[i] = ((kk * h) * g[i]) * c[i];
    }
    *gamma = {g[0], g[1], g[2]};
    *bias = {b[0], b[1], b[2]};
}
}  // namespace

void Instance::World::PrepareWeld(Joint& j, const StepData& st) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
    const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
    // angular: 0x70 = (iA + iB)^-1; soft when Hz (0x1F0) > 0, then
    // 0x70 = (iA + iB + diag gamma)^-1 and the impulse is capped
    const M3 Kang = Add(iA, iB);
    j.Kang = Inverse(Kang);
    j.angCap = j.angCap2 = kFltMax;
    if (j.angHz > 0) {
        const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
        WeldSoft(j.angHz, j.angZeta, j.Kang, WeldError(wfA, wfB), st.h, &j.angGamma, &j.angBias);
        M3 K = Kang;
        K.m[0] += j.angGamma.x;
        K.m[4] += j.angGamma.y;
        K.m[8] += j.angGamma.z;
        j.Kang = Inverse(K);
        // cap = (pi/4) x inv_dt x trace(I) of the dynamic side (the smaller of
        // two); none when neither is dynamic or cap^2 is not above the guard
        const bool da = A.type == kBodyDynamic, db = B.type == kBodyDynamic;
        if (da || db) {
            const float ta = A.I.m[0] + A.I.m[4] + A.I.m[8], tb = B.I.m[0] + B.I.m[4] + B.I.m[8];
            const float t = da && db ? std::min(ta, tb) : (da ? ta : tb);
            const float cap = t * 0.785398185f * st.inv;
            if (kTiny < cap * cap) {
                j.angCap = cap;
                j.angCap2 = cap * cap;
            }
        }
    } else {
        j.angGamma = j.angBias = {};
    }
    // linear: 0xE0 = K^-1, soft when Hz (0x1F8) > 0
    j.rA = Rot(A.q, j.pa - A.lc);
    j.rB = Rot(B.q, j.pb - B.lc);
    M3 K = PointK(A, B, j.rA, j.rB, iA, iB);
    j.Klin = Inverse(K);
    if (j.linHz > 0) {
        WeldSoft(j.linHz, j.linZeta, j.Klin, (B.c + j.rB) - (A.c + j.rA), st.h, &j.linGamma, &j.linBias);
        K.m[0] += j.linGamma.x;
        K.m[4] += j.linGamma.y;
        K.m[8] += j.linGamma.z;
        j.Klin = Inverse(K);
    } else {
        j.linGamma = j.linBias = {};
    }
    if (st.warm) {
        j.Plin = j.Plin * st.dtRatio;
        j.Pang = j.Pang * st.dtRatio;
        if (A.type == kBodyDynamic) {
            A.v = A.v - j.Plin * A.im;
            A.w = A.w - Mulm(iA, j.Pang + Cross(j.rA, j.Plin));
        }
        if (B.type == kBodyDynamic) {
            B.v = B.v + j.Plin * B.im;
            B.w = B.w + Mulm(iB, j.Pang + Cross(j.rB, j.Plin));
        }
    } else {
        j.Plin = j.Pang = {};
    }
}

void Instance::World::VelWeld(Joint& j, const StepData&) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
    const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
    // angular: |P| clamped to the cap, P x cap x rsqrt(|P|^2) (rsqrtps + Newton)
    {
        const V3 cd = Mulv(j.angGamma, j.Pang) + ((B.w - A.w) + j.angBias);
        V3 np = j.Pang - Mulm(j.Kang, cd);
        const float l2 = Len2(np);
        if (j.angCap2 < l2) {
            const float r = EstRsqrt(l2);
            const float rn = (0.5f - l2 * 0.5f * (r * r)) * r + r;
            np = {j.angCap * np.x * rn, j.angCap * np.y * rn, j.angCap * np.z * rn};
        }
        const V3 d = np - j.Pang;
        j.Pang = np;
        A.w = A.w - Mulm(iA, d);
        B.w = B.w + Mulm(iB, d);
    }
    // linear
    {
        const V3 cd = B.v + Cross(B.w, j.rB) - A.v - Cross(A.w, j.rA) + j.linBias + Mulv(j.linGamma, j.Plin);
        const V3 l = -Mulm(j.Klin, cd);
        j.Plin = j.Plin + l;
        ApplyLin(A, l, j.rA, iA, -1);
        ApplyLin(B, l, j.rB, iB, +1);
    }
}

void Instance::World::PosWeld(Joint& j) {
    Body& A = bodies[j.a];
    Body& B = bodies[j.b];
    // angular only when its Hz is exactly 0 (a negative Hz is rigid in the
    // velocity solve but gets no position correction); full correction
    if (j.angHz == 0) {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const Q wfA = Mul(A.q, j.qa), wfB = Mul(B.q, j.qb);
        const V3 P = -Mulm(InverseFast(Add(iA, iB)), WeldError(wfA, wfB));
        MoveAng(A, Mulm(iA, P) * -1.0f);
        MoveAng(B, Mulm(iB, P));
    }
    if (j.linHz == 0) {
        const M3 iA = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        const M3 iB = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        const V3 rA = Rot(A.q, j.pa - A.lc), rB = Rot(B.q, j.pb - B.lc);
        const V3 C = (B.c + rB) - (A.c + rA);
        const V3 P = -Mulm(InverseFast(PointK(A, B, rA, rB, iA, iB)), C);
        MovePos(A, P, rA, iA, -1);
        MovePos(B, P, rB, iB, +1);
    }
    A.SyncOrigin();
    B.SyncOrigin();
}

// --- contacts: narrowphase -----------------------------------------------------------
namespace {
// rsqrt refined the way the client's packed code does: (0.5 - 0.5 x r^2) r + r
inline float RsqrtP(float x) {
    const float r = EstRsqrt(x);
    return (0.5f - x * 0.5f * (r * r)) * r + r;
}
// x / d by the client's raw rcp + one refinement: q = x r, (x - d q) r + q
inline float DivN(float x, float d) {
    const float r = EstRcp(d);
    const float q = x * r;
    return (x - d * q) * r + q;
}
inline float Clamp01(float x) { return std::max(0.0f, std::min(x, 1.0f)); }
inline uint64_t Key(int iA, int iB, int tA, int tB) {
    return uint64_t(uint8_t(iA)) | uint64_t(uint8_t(iB)) << 8 | uint64_t(uint8_t(tA)) << 16 |
           uint64_t(uint8_t(tB)) << 24;
}

// 0x1005CD880: capsule A (a1, a2, ra) against capsule B (b1, b2, rb), world.
// The point sits between the surfaces, the normal points from A to B.
void CapsuleCapsule(V3 a1, V3 a2, float ra, V3 b1, V3 b2, float rb, Manifold& m) {
    m.n = 0;
    m.normal = {};
    const V3 dA = a2 - a1, dB = b2 - b1, r = a1 - b1;
    const float c = Dot(dA, r), f = Dot(r, dB);
    const float e = Dot(dB, dB), a = Dot(dA, dA);
    const float rs = ra + rb, rs2 = rs * rs;
    const bool okE = e * e * 3.0f > kTiny, okA = a * a * 3.0f > kTiny;
    auto finish = [&](V3 A, V3 B, uint64_t key) {   // one point
        const V3 d = B - A;
        const float d2 = Dot(d, d);
        if (rs2 < d2) return;
        const float r0 = EstRsqrt(d2);
        const float dist = std::fabs(d2) < 1.17549435e-38f ? 0.0f : d2 * r0 * -0.5f * (d2 * r0 * r0 + -3.0f);
        const V3 n = kTiny < d2 ? d * RsqrtP(d2) : V3{1, 0, 0};
        m.normal = n;
        m.n = 1;
        m.pt[0].p = A + n * (((ra - rb) + dist) * 0.5f);
        m.pt[0].sep = dist - rs;
        m.pt[0].key = key;
    };
    if (!(okE && okA)) {   // a degenerate segment
        V3 A = a1, B = b1;
        if (okA) A = a1 + dA * Clamp01(DivN(-c, a));
        else if (okE) B = b1 + dB * Clamp01(DivN(f, e));
        finish(A, B, 0);
        return;
    }
    const float b = Dot(dA, dB);
    const float cos2 = DivN(b * b, a * e);
    if (!(0.998782039f <= cos2)) {   // the closest points of the two lines, clamped
        const float rsA = RsqrtP(a), rsB = RsqrtP(e);
        const V3 uA = dA * rsA, uB = dB * rsB;
        const float num = (Dot(uB, r) * Dot(uA, uB) - Dot(uA, r)) * rsA;
        float s = Clamp01(DivN(num, 1.0f - cos2));
        float t = DivN(s * b + f, e);
        if (0 <= t) {
            if (!(t <= 1)) {
                t = 1;
                s = Clamp01(DivN(b - c, a));
            }
        } else {
            t = 0;
            s = Clamp01(DivN(-c, a));
        }
        const int iA = s == 0 ? 0 : (s == 1 ? 1 : 0), tA = (s == 0 || s == 1) ? 2 : 1;
        const int iB = t == 0 ? 0 : (t == 1 ? 1 : 0), tB = (t == 0 || t == 1) ? 2 : 1;
        finish(a1 + dA * s, b1 + dB * t, Key(iA, iB, tA, tB));
        return;
    }
    // parallel: each end of A against B, at most two points
    const float invE = 1.0f / e, invA = 1.0f / a;
    V3 Aend[2];
    V3 acc;
    int cnt = 0;
    for (int i = 0; i < 2; ++i) {
        const V3 P = i == 0 ? a1 : a2;
        V3 A, B;
        int iA = 0, iB = 0, tA = 0, tB = 0;
        const float t = Dot(P - b1, dB) * invE;
        bool project = true;
        if (0 < t) {
            if (t < 1) {
                B = b1 + dB * t;
                A = P;
                iA = i; tA = 2; iB = 0; tB = 1;
                project = false;
            } else {
                B = b2;
                iB = 1; tB = 2;
            }
        } else {
            B = b1;
            iB = 0; tB = 2;
        }
        if (project) {
            const float s = Dot(B - a1, dA) * invA;
            if (0 < s) {
                if (s < 1) { A = a1 + dA * s; iA = 0; tA = 1; }
                else { A = a2; iA = 1; tA = 2; }
            } else {
                A = a1; iA = 0; tA = 2;
            }
        }
        Aend[i] = A;
        const V3 d = B - A;
        const float d2 = Dot(d, d);
        if (rs2 < d2) continue;
        ManifoldPoint& mp = m.pt[cnt];
        if (d2 <= kTiny) {
            mp.p = A + V3{(ra - rb) * 0.5f, 0, 0};
            mp.sep = -rs;
        } else {
            const float r0 = EstRsqrt(d2);
            const float dist = std::fabs(d2) < 1.17549435e-38f ? 0.0f : d2 * r0 * -0.5f * (d2 * r0 * r0 + -3.0f);
            mp.p = A + d * (0.5f * RsqrtP(d2) * ((ra - rb) + dist));
            mp.sep = dist - rs;
        }
        mp.key = Key(iA, iB, tA, tB);
        acc = acc + d;
        ++cnt;
    }
    m.n = cnt;
    const float l2 = Dot(acc, acc);
    m.normal = kTiny < l2 ? acc * RsqrtP(l2) : V3{1, 0, 0};
    if (cnt != 0 && Aend[0].x == Aend[1].x && Aend[0].y == Aend[1].y && Aend[0].z == Aend[1].z) m.n = 1;
}

inline V3 ToWorld(const Body& b, V3 p) { return b.x + Rot(b.q, p); }
inline V3 ToLocal(const Body& b, V3 p) { return RotInv(b.q, p - b.x); }
}  // namespace

// hull narrowphases (0x1005DC920 hull-capsule, 0x1005E11D0 hull-hull): below
namespace {
void HullCapsule(const Hull& H, V3 c1, V3 c2, float r, Manifold& m, Contact::Cache& cache);
void HullHull(const Hull& A, const Hull& B, Q qBA, V3 pBA, Manifold& m, Contact::Cache& cache);
}  // namespace

// 0x1005EEEB0: the manifold for one contact. Capsules meet in the world; a
// capsule meets a hull in the hull's frame, a hull meets a hull in A's frame;
// points and normal go back to the world.
void Instance::World::Narrow(Contact& c, Manifold& m) {
    const Shape& A = shapes[c.sa];
    const Shape& B = shapes[c.sb];
    const Body& bA = bodies[c.ba];
    const Body& bB = bodies[c.bb];
    m.n = 0;
    m.normal = {};
    if (A.type == kShapeCapsule && B.type == kShapeCapsule) {
        CapsuleCapsule(ToWorld(bA, A.a), ToWorld(bA, A.b), A.r, ToWorld(bB, B.a), ToWorld(bB, B.b), B.r, m);
        return;
    }
    if (A.type == kShapeHull && B.type == kShapeCapsule) {
        const V3 c1 = ToLocal(bA, ToWorld(bB, B.a)), c2 = ToLocal(bA, ToWorld(bB, B.b));
        HullCapsule(A.hull, c1, c2, B.r, m, c.cache);
    } else if (A.type == kShapeHull && B.type == kShapeHull) {
        const Q qBA = Mul(Conj(bA.q), bB.q);
        const V3 pBA = RotInv(bA.q, bB.x - bA.x);
        HullHull(A.hull, B.hull, qBA, pBA, m, c.cache);
    } else {
        return;   // the client builds no other pair from .phys shapes
    }
    for (int i = 0; i < m.n; ++i) m.pt[i].p = ToWorld(bA, m.pt[i].p);
    m.normal = Rot(bA.q, m.normal);
}

// --- broadphase (one item's few shapes, the client's rules) -----------------------
void Instance::World::TightBox(const Shape& s, V3* lo, V3* hi) const {
    const Body& b = bodies[s.body];
    if (s.type == kShapeHull) {
        V3 l{kFltMax, kFltMax, kFltMax}, h{-kFltMax, -kFltMax, -kFltMax};
        for (const V3& p : s.hull.v) {
            const V3 w = ToWorld(b, p);
            l = {std::min(l.x, w.x), std::min(l.y, w.y), std::min(l.z, w.z)};
            h = {std::max(h.x, w.x), std::max(h.y, w.y), std::max(h.z, w.z)};
        }
        *lo = l;
        *hi = h;
        return;
    }
    const V3 p = ToWorld(b, s.a), q = s.type == kShapeCapsule ? ToWorld(b, s.b) : p;
    *lo = V3{std::min(p.x, q.x), std::min(p.y, q.y), std::min(p.z, q.z)} - V3{s.r, s.r, s.r};
    *hi = V3{std::max(p.x, q.x), std::max(p.y, q.y), std::max(p.z, q.z)} + V3{s.r, s.r, s.r};
}

// 0x1005D3280: refit a proxy when its tight box left the fat one, or the fat
// one is larger than the tight one grown by 4 margins
void Instance::World::SyncProxies() {
    constexpr float kMargin = 0.1f;
    for (size_t i = 0; i < shapes.size(); ++i) {
        V3 lo, hi;
        TightBox(shapes[i], &lo, &hi);
        Proxy& p = proxies[i];
        const bool inside = p.lo.x <= lo.x && p.lo.y <= lo.y && p.lo.z <= lo.z && hi.x <= p.hi.x && hi.y <= p.hi.y &&
                            hi.z <= p.hi.z;
        if (inside) {
            const V3 big{4 * kMargin, 4 * kMargin, 4 * kMargin};
            const V3 L = lo - big, H = hi + big;
            const bool small = L.x <= p.lo.x && L.y <= p.lo.y && L.z <= p.lo.z && p.hi.x <= H.x && p.hi.y <= H.y &&
                               p.hi.z <= H.z;
            if (small) continue;
        }
        p.lo = lo - V3{kMargin, kMargin, kMargin};
        p.hi = hi + V3{kMargin, kMargin, kMargin};
        p.moved = true;
    }
}

namespace {
inline bool Overlap(const Proxy& a, const Proxy& b) {
    return !(b.lo.x > a.hi.x || b.lo.y > a.hi.y || b.lo.z > a.hi.z || a.lo.x > b.hi.x || a.lo.y > b.hi.y ||
             a.lo.z > b.hi.z);
}
}  // namespace

// new pairs among moved proxies: different bodies, at least one dynamic, not
// jointed (collideConnected is 0), one contact per pair; the hull goes first
void Instance::World::FindNewContacts() {
    const size_t n = shapes.size();
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = i + 1; j < n; ++j) {
            if (!proxies[i].moved && !proxies[j].moved) continue;
            const Shape& si = shapes[i];
            const Shape& sj = shapes[j];
            if (si.body == sj.body) continue;
            if (bodies[si.body].type != kBodyDynamic && bodies[sj.body].type != kBodyDynamic) continue;
            const uint16_t lo = std::min(si.body, sj.body), hi = std::max(si.body, sj.body);
            if (std::find(joined.begin(), joined.end(), uint32_t(lo) << 16 | hi) != joined.end()) continue;
            if (!Overlap(proxies[i], proxies[j])) continue;
            bool exists = false;
            for (const Contact& c : contacts)
                if ((c.sa == i && c.sb == j) || (c.sa == j && c.sb == i)) { exists = true; break; }
            if (exists) continue;
            Contact c;
            const bool swap = sj.type > si.type;
            c.sa = uint16_t(swap ? j : i);
            c.sb = uint16_t(swap ? i : j);
            c.ba = shapes[c.sa].body;
            c.bb = shapes[c.sb].body;
            const Shape& A = shapes[c.sa];
            const Shape& B = shapes[c.sb];
            c.friction = std::sqrt(A.friction * B.friction);
            c.restitution = std::max(0.0f, std::min(A.restitution + B.restitution, 1.0f));
            c.rolling = std::sqrt(0.2f * 0.2f);   // fixture default rolling resistance (def + 0x20)
            contacts.push_back(c);
        }
    }
    for (Proxy& p : proxies) p.moved = false;
}

// destroy contacts whose fat boxes parted, update the rest (0x1005EEEB0's
// carrying of impulses: by the 8-byte feature key, first unmatched old point)
void Instance::World::Collide() {
    size_t w = 0;
    for (size_t i = 0; i < contacts.size(); ++i) {
        Contact& c = contacts[i];
        if (!Overlap(proxies[c.sa], proxies[c.sb])) continue;
        const Manifold old = c.m;
        Manifold nm = c.m;
        Narrow(c, nm);
        if (nm.n > 0) {
            nm.Pt = old.Pt;
            nm.Proll = old.Proll;
            nm.Ptw = old.Ptw;
            bool used[4] = {};
            for (int k = 0; k < nm.n; ++k) {
                nm.pt[k].Pn = 0;
                nm.pt[k].fresh = true;
                for (int o = 0; o < old.n; ++o) {
                    if (used[o] || old.pt[o].key != nm.pt[k].key) continue;
                    used[o] = true;
                    nm.pt[k].Pn = old.pt[o].Pn;
                    nm.pt[k].fresh = false;
                    break;
                }
            }
        }
        c.m = nm;
        if (w != i) contacts[w] = c;
        ++w;
    }
    contacts.resize(w);
}

// --- contacts: solver ------------------------------------------------------------------
// 0x1005EC2E0
void Instance::World::PrepareContacts(const StepData& st) {
    for (Contact& c : contacts) {
        if (c.m.n <= 0) continue;
        Body& A = bodies[c.ba];
        Body& B = bodies[c.bb];
        c.iAw = A.type == kBodyDynamic ? A.IWorldInv() : Zero3();
        c.iBw = B.type == kBodyDynamic ? B.IWorldInv() : Zero3();
        c.imA = A.im;
        c.imB = B.im;
        c.mA = A.m;
        c.mB = B.m;
        // softness: two dynamic bodies over a 10:1 mass ratio (10 Hz, 0.7), or a
        // kinematic one against the other's shape setting (10 Hz, 0.7); else rigid
        float omega = 10.0f, zeta = 0.7f;
        bool soft;
        if (A.type == kBodyDynamic && B.type == kBodyDynamic) {
            soft = !(A.m <= B.m * 10.0f) || !(B.m <= A.m * 10.0f);
        } else if (A.type == kBodyKinematic || B.type == kBodyKinematic) {
            soft = 10.0f > 0;   // the dynamic side's fixture Hz (default 10)
        } else {
            soft = false;
        }
        omega *= 2 * kPi;
        c.soft = soft;
        c.rigidPoints = soft ? 0 : c.m.n;
        c.n = c.m.normal;
        const V3 n = c.n;
        const float invCount = 1.0f / float(c.m.n);
        V3 sum;
        for (int i = 0; i < c.m.n; ++i) {
            const ManifoldPoint& mp = c.m.pt[i];
            ContactPoint& cp = c.cp[i];
            cp = ContactPoint{};
            cp.sep = mp.sep;
            const V3 rA = mp.p - A.c, rB = mp.p - B.c;
            cp.la = RotInv(A.q, rA);
            cp.lb = RotInv(B.q, rB);
            sum = sum + mp.p;
            cp.rAn = Cross(rA, n);
            cp.rBn = Cross(rB, n);
            cp.iArAn = Mulm(c.iAw, cp.rAn);
            cp.iBrBn = Mulm(c.iBw, cp.rBn);
            const float K = (A.im + B.im) + Dot(cp.iArAn, cp.rAn) + Dot(cp.iBrBn, cp.rBn);
            cp.mn = RcpGuard(K);
            if (soft) {
                const float sep = std::max(mp.sep, -0.25f);
                const float t = cp.mn * omega;
                const float cc = (zeta + zeta) * t;
                const float kh = t * st.h * omega;
                const float g = RcpGuard((cc + kh) * st.h);
                cp.gamma = g;
                cp.bias = kh * sep * g;
                cp.mn = RcpGuard(K + g);
            }
            cp.posMass = RcpGuard((Dot(cp.iBrBn, cp.rBn) + B.im) * B.m + (Dot(cp.iArAn, cp.rAn) + A.im) * A.m);
            const float vn = Dot((B.v + Cross(B.w, rB)) - (A.v + Cross(A.w, rA)), n);
            if (vn + 2.0f < 0) cp.bias = cp.bias + vn * c.restitution;
            if (st.warm) {
                cp.Pn = mp.Pn * st.dtRatio;
                const V3 P = n * cp.Pn;
                if (A.type == kBodyDynamic) {
                    A.v = A.v - P * A.im;
                    A.w = A.w - cp.iArAn * cp.Pn;
                }
                if (B.type == kBodyDynamic) {
                    B.v = B.v + P * B.im;
                    B.w = B.w + cp.iBrBn * cp.Pn;
                }
            }
        }
        // friction at the centre: 2D, twist, rolling
        c.center = sum * invCount;
        const V3 rA = c.center - A.c, rB = c.center - B.c;
        V3 t1 = std::fabs(n.y) > 0.5f ? V3{n.y, -n.x, 0} : V3{n.z, 0, -n.x};
        t1 = t1 * RsqrtN(Dot(t1, t1));
        c.t1 = t1;
        c.t2 = Cross(n, t1);
        c.rAt1 = Cross(rA, c.t1);
        c.rBt1 = Cross(rB, c.t1);
        c.rAt2 = Cross(rA, c.t2);
        c.rBt2 = Cross(rB, c.t2);
        c.iAt1 = Mulm(c.iAw, c.rAt1);
        c.iBt1 = Mulm(c.iBw, c.rBt1);
        c.iAt2 = Mulm(c.iAw, c.rAt2);
        c.iBt2 = Mulm(c.iBw, c.rBt2);
        const float mAB = A.im + B.im;
        const float K11 = (Dot(c.rAt1, c.iAt1) + Dot(c.rBt1, c.iBt1)) + mAB;
        const float K12 = Dot(c.rAt1, c.iAt2) + Dot(c.rBt1, c.iBt2);
        const float K22 = (Dot(c.rAt2, c.iAt2) + Dot(c.rBt2, c.iBt2)) + mAB;
        const float det = RcpGuard(K22 * K11 - K12 * K12);
        c.k11 = K22 * det;
        c.k12 = -det * K12;
        c.k22 = det * K11;
        c.twMass = RcpGuard(Dot(n, Mulm(c.iAw, n)) + Dot(n, Mulm(c.iBw, n)));
        float r2 = 0;
        for (int i = 0; i < c.m.n; ++i) r2 += Dot(c.m.pt[i].p - c.center, c.m.pt[i].p - c.center);
        r2 *= invCount;
        const float rr = std::fabs(r2) < 1.17549435e-38f ? 0.0f : r2 * RsqrtN(r2);
        c.mu = c.friction;
        c.muTw = c.friction * 0.5f * rr;
        c.roll = c.rolling;
        c.rollK = c.roll <= 0 ? Zero3() : Inverse(Add(c.iAw, c.iBw));
        if (st.warm) {
            c.lam = {Dot(c.m.Pt, c.t1) * st.dtRatio, Dot(c.m.Pt, c.t2) * st.dtRatio, c.m.Ptw * st.dtRatio};
            c.lamRoll = c.m.Proll * st.dtRatio;
            const V3 P = c.t1 * c.lam.x + c.t2 * c.lam.y;
            const V3 L = n * c.lam.z + c.lamRoll;
            if (A.type == kBodyDynamic) {
                A.v = A.v - P * A.im;
                A.w = A.w - Mulm(c.iAw, c.rAt1 * c.lam.x + L + c.rAt2 * c.lam.y);
            }
            if (B.type == kBodyDynamic) {
                B.v = B.v + P * B.im;
                B.w = B.w + Mulm(c.iBw, c.rBt1 * c.lam.x + L + c.rBt2 * c.lam.y);
            }
        } else {
            c.lam = {};
            c.lamRoll = {};
        }
    }
}

// 0x1005ED970: rolling, then 2D friction at the centre, twist, then normals
void Instance::World::VelContacts() {
    for (Contact& c : contacts) {
        if (c.m.n <= 0) continue;
        Body& A = bodies[c.ba];
        Body& B = bodies[c.bb];
        const bool dA = A.type == kBodyDynamic, dB = B.type == kBodyDynamic;
        float total = 0;
        for (int i = 0; i < c.m.n; ++i) total += c.cp[i].Pn;
        // rolling
        {
            V3 np = Mulm(c.rollK, A.w - B.w) + c.lamRoll;
            const float cap = c.roll * total, l2 = Dot(np, np);
            if (cap * cap < l2 && kTiny < l2) np = np * cap * RsqrtP(l2);
            const V3 d = np - c.lamRoll;
            c.lamRoll = np;
            if (dA) A.w = A.w - Mulm(c.iAw, d);
            if (dB) B.w = B.w + Mulm(c.iBw, d);
        }
        // 2D friction
        {
            const V3 dv = B.v - A.v;
            const float C1 = (Dot(c.t1, dv) + Dot(c.rBt1, B.w)) - Dot(c.rAt1, A.w);
            const float C2 = (Dot(c.t2, dv) + Dot(c.rBt2, B.w)) - Dot(c.rAt2, A.w);
            const float l1 = -(c.k12 * C2 + c.k11 * C1), l2 = -(c.k22 * C2 + c.k12 * C1);
            float n1 = c.lam.x + l1, n2 = c.lam.y + l2;
            const float cap = c.mu * total, m2 = n1 * n1 + n2 * n2;
            if (cap * cap < m2 && kTiny < m2) {
                const float k = cap * RsqrtP(m2);
                n1 *= k;
                n2 *= k;
            }
            const float d1 = n1 - c.lam.x, d2 = n2 - c.lam.y;
            c.lam.x = n1;
            c.lam.y = n2;
            const V3 P = c.t1 * d1 + c.t2 * d2;
            if (dA) {
                A.v = A.v - P * c.imA;
                A.w = A.w - (c.iAt1 * d1 + c.iAt2 * d2);
            }
            if (dB) {
                B.v = B.v + P * c.imB;
                B.w = B.w + (c.iBt1 * d1 + c.iBt2 * d2);
            }
        }
        // twist
        {
            const float cd = Dot(B.w - A.w, c.n);
            const float cap = c.muTw * total;
            const float np = std::max(-cap, std::min(c.lam.z - c.twMass * cd, cap));
            const float d = np - c.lam.z;
            c.lam.z = np;
            if (dA) A.w = A.w - Mulm(c.iAw, c.n) * d;
            if (dB) B.w = B.w + Mulm(c.iBw, c.n) * d;
        }
        // normals
        for (int i = 0; i < c.m.n; ++i) {
            ContactPoint& cp = c.cp[i];
            const V3 dv = Mulv(B.v - A.v, c.n);
            const V3 wb = Mulv(cp.rBn, B.w), wa = Mulv(cp.rAn, A.w);
            const V3 s = (wb + dv) - wa;
            const float lam = (((cp.gamma * cp.Pn + s.x) + s.y) + (s.z + cp.bias)) * cp.mn;
            const float np = std::max(cp.Pn - lam, 0.0f);
            const float d = np - cp.Pn;
            cp.Pn = np;
            const V3 P = c.n * d;
            if (dA) {
                A.v = A.v - P * c.imA;
                A.w = A.w - cp.iArAn * d;
            }
            if (dB) {
                B.v = B.v + P * c.imB;
                B.w = B.w + cp.iBrBn * d;
            }
        }
    }
}

// 0x1005EE0B0
void Instance::World::StoreImpulses() {
    for (Contact& c : contacts) {
        for (int i = 0; i < c.m.n; ++i) c.m.pt[i].Pn = c.cp[i].Pn;
        c.m.Pt = c.t1 * c.lam.x + c.t2 * c.lam.y;
        c.m.Ptw = c.lam.z;
        c.m.Proll = c.lamRoll;
    }
}

// 0x1005EE250 (beta 0.2, slop 0.005): rigid contacts only. A mass-normalised
// pseudo-impulse: each dynamic body moves its centre by P and turns by
// m I^-1 (r x P).
void Instance::World::PosContacts() {
    for (Contact& c : contacts) {
        if (c.rigidPoints <= 0) continue;
        Body& A = bodies[c.ba];
        Body& B = bodies[c.bb];
        const float sA = c.imA * c.mA, sB = c.imB * c.mB;
        for (int i = 0; i < c.rigidPoints; ++i) {
            const ContactPoint& cp = c.cp[i];
            const V3 rA = Rot(A.q, cp.la), rB = Rot(B.q, cp.lb);
            const V3 d = Mulv((B.c - (A.c + rA)) + rB, c.n);
            const float s = (d.z + cp.sep) + (d.x + d.y);
            const float C = std::min((s + 0.005f) * 0.2f, 0.0f);
            if (0 <= C) continue;
            const V3 P = c.n * (C * -cp.posMass);
            A.c = A.c - P * sA;
            if (A.type == kBodyDynamic) {
                const V3 wl = Mulm(A.iI, RotInv(A.q, Cross(rA, P))) * c.mA;
                const Q dq = Mul(A.q, Q{wl.x, wl.y, wl.z, 0});
                A.q = QNormFast({A.q.x - 0.5f * dq.x, A.q.y - 0.5f * dq.y, A.q.z - 0.5f * dq.z, A.q.w - 0.5f * dq.w});
            }
            B.c = B.c + P * sB;
            if (B.type == kBodyDynamic) {
                const V3 wl = Mulm(B.iI, RotInv(B.q, Cross(rB, P))) * c.mB;
                const Q dq = Mul(B.q, Q{wl.x, wl.y, wl.z, 0});
                B.q = QNormFast({B.q.x + 0.5f * dq.x, B.q.y + 0.5f * dq.y, B.q.z + 0.5f * dq.z, B.q.w + 0.5f * dq.w});
            }
        }
        A.SyncOrigin();
        B.SyncOrigin();
    }
}

// --- hull narrowphases ------------------------------------------------------------------
namespace {
constexpr float kHullRadius = 0.01f;           // every hull proxy's radius (0x1005CEB80)
constexpr float kEps = 1.1920929e-07f;          // FLT_EPSILON

// --- GJK (0x1005D05A0): Box2D's b2Distance in 3D, A and B point sets in one
// frame. Warm-started from the contact's cache: the cached simplex is kept
// when its metric is within 0.25..4 x the cached one (and >= FLT_EPSILON);
// at most 20 iterations; a repeated support pair ends it.
struct SV { V3 wA, wB, w; float a = 0; int iA = 0, iB = 0; };
struct GjkOut { V3 pA, pB, normal; float dist = 0; };

int Support(const V3* p, int n, V3 d) {
    int best = 0;
    float bv = Dot(p[0], d);
    for (int i = 1; i < n; ++i) {
        const float v = Dot(p[i], d);
        if (v > bv) { best = i; bv = v; }
    }
    return best;
}
// the cache metric of a simplex (0x1005D08AD): squared length, squared
// doubled area, signed doubled volume
float Metric(const SV* v, int n) {
    if (n == 2) { const V3 e = v[1].w - v[0].w; return Dot(e, e); }
    if (n == 3) { const V3 c = Cross(v[1].w - v[0].w, v[2].w - v[0].w); return Dot(c, c); }
    if (n == 4) return Dot(Cross(v[1].w - v[0].w, v[2].w - v[0].w), v[3].w - v[0].w);
    return 0;
}
// closest point of triangle (a, b, c) to the origin, as barycentrics
void ClosestTri(V3 a, V3 b, V3 c, float* u, float* v, float* w) {
    const V3 ab = b - a, ac = c - a, ap = -a;
    const float d1 = Dot(ab, ap), d2 = Dot(ac, ap);
    if (d1 <= 0 && d2 <= 0) { *u = 1; *v = *w = 0; return; }
    const V3 bp = -b;
    const float d3 = Dot(ab, bp), d4 = Dot(ac, bp);
    if (d3 >= 0 && d4 <= d3) { *v = 1; *u = *w = 0; return; }
    const float vc = d1 * d4 - d3 * d2;
    if (vc <= 0 && d1 >= 0 && d3 <= 0) { const float t = d1 / (d1 - d3); *u = 1 - t; *v = t; *w = 0; return; }
    const V3 cp = -c;
    const float d5 = Dot(ab, cp), d6 = Dot(ac, cp);
    if (d6 >= 0 && d5 <= d6) { *w = 1; *u = *v = 0; return; }
    const float vb = d5 * d2 - d1 * d6;
    if (vb <= 0 && d2 >= 0 && d6 <= 0) { const float t = d2 / (d2 - d6); *u = 1 - t; *w = t; *v = 0; return; }
    const float va = d3 * d6 - d5 * d4;
    if (va <= 0 && (d4 - d3) >= 0 && (d5 - d6) >= 0) {
        const float t = (d4 - d3) / ((d4 - d3) + (d5 - d6));
        *v = 1 - t; *w = t; *u = 0;
        return;
    }
    const float k = 1.0f / (va + vb + vc);
    *v = vb * k; *w = vc * k; *u = 1 - *v - *w;
}
void SolveTri(const SV* t, SV* out, int* n) {
    float bc[3];
    ClosestTri(t[0].w, t[1].w, t[2].w, &bc[0], &bc[1], &bc[2]);
    SV tmp[3];
    int k = 0;
    for (int i = 0; i < 3; ++i)
        if (bc[i] > 0) { tmp[k] = t[i]; tmp[k].a = bc[i]; ++k; }
    for (int i = 0; i < k; ++i) out[i] = tmp[i];
    *n = k;
}
// reduce the simplex to the feature nearest the origin; false = it holds the origin
bool Solve(SV* s, int* n) {
    if (*n == 1) { s[0].a = 1; return true; }
    if (*n == 2) {
        const V3 e = s[1].w - s[0].w;
        const float d2 = -Dot(s[0].w, e);
        if (d2 <= 0) { s[0].a = 1; *n = 1; return true; }
        const float d1 = Dot(s[1].w, e);
        if (d1 <= 0) { s[1].a = 1; s[0] = s[1]; *n = 1; return true; }
        const float k = 1.0f / (d1 + d2);
        s[0].a = d1 * k; s[1].a = d2 * k;
        return true;
    }
    if (*n == 3) { const SV t[3] = {s[0], s[1], s[2]}; SolveTri(t, s, n); return true; }
    static const int F[4][4] = {{0, 1, 2, 3}, {0, 2, 3, 1}, {0, 3, 1, 2}, {1, 3, 2, 0}};
    // inside = strictly on the opposite vertex's side of every face; otherwise
    // the nearest of all four faces (a flat tetrahedron - four points of one
    // hull face - has no inside and is solved by its nearest triangle)
    bool inside = true;
    for (const auto& f : F) {
        const V3 a = s[f[0]].w, b = s[f[1]].w, c = s[f[2]].w, d = s[f[3]].w;
        const V3 nrm = Cross(b - a, c - a);
        if (!(Dot(-a, nrm) * Dot(d - a, nrm) > 0)) { inside = false; break; }
    }
    if (inside) return false;
    float best = kFltMax;
    SV keep[3];
    int keepN = 0;
    for (const auto& f : F) {
        const SV t[3] = {s[f[0]], s[f[1]], s[f[2]]};
        SV r[3];
        int rn = 0;
        SolveTri(t, r, &rn);
        V3 p;
        for (int i = 0; i < rn; ++i) p = p + r[i].w * r[i].a;
        const float d2 = Dot(p, p);
        if (d2 < best) { best = d2; keepN = rn; for (int i = 0; i < rn; ++i) keep[i] = r[i]; }
    }
    for (int i = 0; i < keepN; ++i) s[i] = keep[i];
    *n = keepN;
    return true;
}
GjkOut Gjk(const V3* A, int nA, const V3* B, int nB, Contact::Cache& cache) {
    const V3 o = B[0];   // everything relative to B's first point, as the client does
    V3 la[256], lb[8];
    nA = std::min(nA, 256);
    nB = std::min(nB, 8);
    for (int i = 0; i < nA; ++i) la[i] = A[i] - o;
    for (int i = 0; i < nB; ++i) lb[i] = B[i] - o;
    SV s[5];
    int n = 0;
    while (n < 4 && cache.a[n] != 0xFF) ++n;
    for (int i = 0; i < n; ++i) {
        s[i].iA = std::min<int>(cache.a[i], nA - 1);
        s[i].iB = std::min<int>(cache.b[i], nB - 1);
        s[i].wA = la[s[i].iA];
        s[i].wB = lb[s[i].iB];
        s[i].w = s[i].wB - s[i].wA;
    }
    if (n > 1) {
        const float m2 = Metric(s, n), m1 = cache.metric;
        if (!(m2 >= 0.25f * m1 && !(4.0f * m1 < m2) && m2 >= kEps)) n = 0;
    }
    if (n == 0) {
        s[0].iA = 0;
        s[0].iB = 0;
        s[0].wA = la[0];
        s[0].wB = lb[0];
        s[0].w = s[0].wB - s[0].wA;
        n = 1;
    }
    bool overlap = false;
    for (int iter = 0; iter < 20; ++iter) {
        int sa[4], sb[4];
        const int sn = n;
        for (int i = 0; i < n; ++i) { sa[i] = s[i].iA; sb[i] = s[i].iB; }
        if (!Solve(s, &n)) { overlap = true; break; }
        V3 c;
        for (int i = 0; i < n; ++i) c = c + s[i].w * s[i].a;
        const V3 d = -c;
        if (Dot(d, d) < kEps * kEps) break;
        SV& v = s[n];
        v.iA = Support(la, nA, -d);
        v.wA = la[v.iA];
        v.iB = Support(lb, nB, d);
        v.wB = lb[v.iB];
        v.w = v.wB - v.wA;
        bool dup = false;
        for (int i = 0; i < sn; ++i)
            if (sa[i] == v.iA && sb[i] == v.iB) { dup = true; break; }
        if (dup) break;
        ++n;
    }
    GjkOut g;
    if (overlap)
        for (int i = 0; i < 4; ++i) s[i].a = 0.25f;
    for (int i = 0; i < n; ++i) {
        g.pA = g.pA + s[i].wA * s[i].a;
        g.pB = g.pB + s[i].wB * s[i].a;
    }
    if (overlap) g.pB = g.pA;
    g.normal = g.pB - g.pA;
    g.dist = Len(g.normal);
    g.pA = g.pA + o;
    g.pB = g.pB + o;
    cache.metric = Metric(s, n);
    for (int i = 0; i < 4; ++i) {
        cache.a[i] = i < n ? uint8_t(s[i].iA) : 0xFF;
        cache.b[i] = i < n ? uint8_t(s[i].iB) : 0;
    }
    return g;
}

// half-edge fields: +0 twin offset (int8), +1 origin vertex, +2 face, +3 next
inline int Twin(const Hull& H, int e) { return e + int8_t(H.edge[size_t(e) * 4]); }
inline int Origin(const Hull& H, int e) { return H.edge[size_t(e) * 4 + 1]; }
inline int FaceOf(const Hull& H, int e) { return H.edge[size_t(e) * 4 + 2]; }
inline int Next(const Hull& H, int e) { return H.edge[size_t(e) * 4 + 3]; }

// 0x1005D1780 with both radii 0: the closest points of segments (P1, Q1), (P2, Q2)
void ClosestSegSeg(V3 P1, V3 Q1, V3 P2, V3 Q2, V3* c1, V3* c2) {
    const V3 d1 = Q1 - P1, d2 = Q2 - P2, rr = P1 - P2;
    const float e = Dot(d2, d2), a = Dot(d1, d1);
    const bool okE = e * e * 3.0f > kTiny, okA = a * a * 3.0f > kTiny;
    float s = 0, t = 0;
    if (!okE && !okA) {
        *c1 = P1;
        *c2 = Len2(P2 - P1) > kTiny ? P2 : P1;
        return;
    }
    const float f = Dot(d2, rr);
    if (!okA) {
        t = Clamp01(DivN(f, e));
    } else {
        const float c = Dot(d1, rr);
        if (!okE) {
            s = Clamp01(DivN(-c, a));
        } else {
            const float b = Dot(d2, d1);
            const float denom = e * a - b * b;
            s = denom * denom * 3.0f > kTiny ? Clamp01(DivN(b * f - e * c, denom)) : 0.0f;
            t = DivN(b * s + f, e);
            if (!(0 <= t)) { t = 0; s = Clamp01(DivN(-c, a)); }
            else if (!(t <= 1)) { t = 1; s = Clamp01(DivN(b - c, a)); }
        }
    }
    *c1 = P1 + d1 * s;
    *c2 = P2 + d2 * t;
    if (!(Len2(*c2 - *c1) > kTiny)) *c2 = *c1;
}

// 0x1005DBAB0: the capsule's segment clipped to hull face f's side planes (its
// half-edges in `next` order; a side plane under the guard is skipped, the
// first one ends it); fewer than 2 points after a plane = no contact. A point
// within (d + 0.01) + r of the face is kept on the capsule's surface.
struct ClipPt { V3 p; uint64_t key; };
void FaceCapsule(const Hull& H, int f, V3 c1, V3 c2, float r, Manifold& m) {
    m.n = 0;
    const V3 n = H.n[size_t(f)];
    ClipPt cur[3] = {{c1, Key(f, 0, 0, 2)}, {c2, Key(f, 1, 0, 2)}, {}}, nxt[3];
    const int e0 = H.face[size_t(f)];
    int e = e0;
    V3 P = H.v[size_t(Origin(H, e0))];
    bool first = true;
    do {
        const int en = Next(H, e);
        const V3 Qv = H.v[size_t(Origin(H, en))];
        V3 N = Cross(Qv - P, n);
        const float l2 = (N.z * N.z + N.y * N.y) + N.x * N.x;
        if (l2 > kTiny) {
            N = N * RsqrtN(l2);
            const float dP = Dot(P, N);
            const float dA = Dot(cur[0].p, N) - dP, dB = Dot(cur[1].p, N) - dP;
            int k = 0;
            if (!(0 < dA)) nxt[k++] = cur[0];
            if (!(0 < dB)) nxt[k++] = cur[1];
            if (!(0 <= dA * dB)) {
                const float den = dA - dB;
                const float rc = EstRcp(den);
                const V3 t1 = (cur[1].p - cur[0].p) * dA;
                const V3 q = t1 * rc;
                nxt[k++] = {(t1 - q * den) * rc + q + cur[0].p, Key(e & 0xFE, 0, 1, 1)};
            }
            if (k != 2) return;
            cur[0] = nxt[0];
            cur[1] = nxt[1];
        } else if (first) {
            return;
        }
        first = false;
        P = Qv;
        e = en;
    } while (e != e0);
    const float thr = (H.d[size_t(f)] + kHullRadius) + r;
    int k = 0;
    for (int i = 0; i < 2; ++i) {
        const float sep = Dot(cur[i].p, n) - thr;
        if (0 < sep) continue;
        m.pt[k].p = cur[i].p - n * r;
        m.pt[k].sep = sep;
        m.pt[k].key = cur[i].key;
        ++k;
    }
    m.normal = n;
    m.n = k;
}

// the half-edge (even of its pair) from vertex a to vertex b, -1 when none (0x1005DD9F0)
int EdgeBetween(const Hull& H, int a, int b) {
    const int ne = int(H.edge.size() / 4);
    for (int e = 0; e < ne; ++e)
        if (Origin(H, e) == a && Origin(H, Twin(H, e)) == b) return e & 0xFFFE;
    return -1;
}

// 0x1005DC920 -> 0x1005DC020: hull (its own frame) against a capsule (c1, c2,
// r) in that frame. A hull carries a 0.01 radius; contacts are kept out to
// r + 0.01. GJK first: a clear gap (>= 0.005) takes the face within 5 deg of
// the GJK normal (clipped) or else one point from the GJK features; a deeper
// touch runs SAT (faces, then edges whose arcs the segment's great circle
// crosses, an edge winning only past 0.9 x best + 0.0025).
void HullCapsule(const Hull& H, V3 c1, V3 c2, float r, Manifold& m, Contact::Cache& cache) {
    m.n = 0;
    m.normal = {};
    if (H.v.empty() || H.n.empty()) return;
    const float margin = r + kHullRadius;
    const V3 seg[2] = {c1, c2};
    const GjkOut g = Gjk(H.v.data(), int(H.v.size()), seg, 2, cache);
    if (!(g.dist <= margin)) return;
    const float n2 = Dot(g.normal, g.normal);
    const bool partial = cache.a[0] == 0xFF || cache.a[1] == 0xFF || cache.a[2] == 0xFF || cache.a[3] == 0xFF;
    if (n2 > kTiny && 0.005f <= g.dist && partial) {
        const V3 N = g.normal * RsqrtN(n2);
        int best = 0;
        float bv = -kFltMax;
        for (size_t i = 0; i < H.n.size(); ++i) {
            const float d = Dot(H.n[i], N);
            if (d > bv) best = int(i);
            bv = std::max(d, bv);
        }
        if (bv > 0.99619472f) FaceCapsule(H, best, c1, c2, r, m);
        if (m.n != 0) return;
        // one point; its key from the GJK simplex (vertex 2, edge 1, none 3)
        const uint8_t* A = cache.a;
        const uint8_t* B = cache.b;
        int ia = A[0], ib = B[0], ta = 2, tb = 2;
        auto edgeKey = [&](int v0, int v1, bool sameB) {
            const int e = EdgeBetween(H, v0, v1);
            ia = e >= 0 ? e : 0;
            ta = e < 0 ? 3 : 1;
            if (!sameB) { tb = 1; ib = 0; }
        };
        if (A[0] != 0xFF && A[1] != 0xFF) {
            if (A[2] == 0xFF) {
                const bool sameB = B[1] == B[0];
                if (A[1] != A[0]) edgeKey(A[0], A[1], sameB);
                else if (!sameB) { tb = 1; ib = 0; }
            } else if (A[3] == 0xFF) {
                const bool sameB = B[1] == B[0] && B[2] == B[0];
                if (A[1] != A[0]) edgeKey(A[0], A[1], sameB);
                else if (A[2] != A[0]) edgeKey(A[0], A[2], sameB);
                else if (!sameB) { tb = 1; ib = 0; }
            }
        }
        m.n = 1;
        m.normal = N;
        m.pt[0].p = g.pB - N * r;
        m.pt[0].key = Key(ia, ib, ta, tb);
        m.pt[0].sep = g.dist - margin;
        return;
    }
    float bestSep = -kFltMax;
    int bestIdx = -1, type = 2;
    for (size_t i = 0; i < H.n.size(); ++i) {
        const float sA = Dot(c1, H.n[i]), sB = Dot(c2, H.n[i]);
        const float sep = (sB <= sA ? sB : sA) - H.d[i];
        if (!(sep <= margin)) return;
        if (!(sep <= bestSep)) { bestSep = sep; bestIdx = int(i); type = 0; }
    }
    const V3 D = c2 - c1;
    const int ne = int(H.edge.size() / 4);
    for (int e = 0; e < ne; e += 2) {
        const int tw = Twin(H, e);
        const float dA = Dot(H.n[size_t(FaceOf(H, e))], D), dB = Dot(H.n[size_t(FaceOf(H, tw))], D);
        if (dB * dA >= 0) continue;
        const V3 P = H.v[size_t(Origin(H, e))];
        V3 N = Cross(H.v[size_t(Origin(H, tw))] - P, D);
        const float l2 = (N.z * N.z + N.y * N.y) + N.x * N.x;
        float sep = -kFltMax;
        if (!(l2 < kEps)) {
            N = N * RsqrtN(l2);
            if (!(Dot(N, P - H.centroid) >= 0)) N = -N;
            sep = Dot(c1 - P, N);
        }
        if (!(sep <= margin)) return;
        if (!(sep <= bestSep * 0.9f + 0.0025f)) { bestSep = sep; bestIdx = e; type = 1; }
    }
    if (type == 0) {
        FaceCapsule(H, bestIdx, c1, c2, r, m);
        return;
    }
    if (type != 1) return;
    const int e = bestIdx, tw = Twin(H, e);
    const V3 P = H.v[size_t(Origin(H, e))], Qv = H.v[size_t(Origin(H, tw))];
    V3 cA, cB;
    ClosestSegSeg(P, Qv, c1, c2, &cA, &cB);
    V3 N = Cross(Qv - P, D);
    const float l2 = (N.z * N.z + N.y * N.y) + N.x * N.x;
    if (l2 < kEps) return;
    N = N * RsqrtN(l2);
    if (!(Dot(P - H.centroid, N) >= 0)) N = -N;
    const V3 d = Mulv(cB - cA, N);
    m.n = 1;
    m.normal = N;
    m.pt[0].p = (cA + cB) * 0.5f;
    m.pt[0].sep = (d.z + d.x) + (d.y - (kHullRadius + r));
    m.pt[0].key = Key(e, 0, 1, 1);
}
// --- hull against hull (0x1005E11D0 -> 0x1005E0500): Gregorius' SAT with a
// per-contact feature cache; both hulls' 0.01 radii make a 0.02 margin ------
constexpr float kHullPair = 0.02f;

// 0x1005DD8B0: the support vertex along d (measured from vertex 0, first wins)
int SupportIdx(const Hull& H, V3 d) {
    int idx = 0;
    float best = 0;
    const V3 v0 = H.v[0];
    for (size_t i = 1; i < H.v.size(); ++i) {
        const float s = Dot(H.v[i] - v0, d);
        if (!(s <= best)) { best = s; idx = int(i); }
    }
    return idx;
}
// 0x1005DD940: of the faces around vertex `vert`, the one most along d
int IncidentFace(const Hull& H, int vert, V3 d) {
    int ret = -1;
    float best = -kFltMax;
    for (size_t f = 0; f < H.n.size(); ++f) {
        const float s = Dot(H.n[f], d);
        if (!(s > best)) continue;
        const int e0 = H.face[f];
        int e = e0;
        do {
            if (Origin(H, e) == vert) { best = s; ret = int(f); break; }
            e = Next(H, e);
        } while (e != e0);
    }
    return ret;
}
struct XF {
    M3 R;
    V3 t;
    V3 operator()(V3 v) const { return Mulm(R, v) + t; }
};
struct PolyV { V3 p; int edge = 0; uint64_t key = 0; float sep = 0; };

// 0x1005DB4A0: more than 4 points -> 4: furthest along a tangent, furthest
// from it, largest triangle, furthest outside it (each pick with a 0.99
// hysteresis; under 0.02^2 stops)
void Reduce(const PolyV* p, int n, V3 nrm, Manifold& m) {
    V3 u = std::fabs(nrm.y) > 0.5f ? V3{nrm.y, -nrm.x, 0} : V3{nrm.z, 0, -nrm.x};
    u = u * RsqrtN(Dot(u, u));
    auto put = [&](int k, int i) { m.pt[k].p = p[i].p; m.pt[k].key = p[i].key; m.pt[k].sep = p[i].sep; };
    int i0 = -1;
    float best = -kFltMax;
    for (int i = 0; i < n; ++i) {
        const float d = Dot(p[i].p, u);
        if (d * 0.99f > best) { best = d; i0 = i; }
    }
    if (i0 < 0) i0 = 0;
    put(0, i0);
    const V3 a = p[i0].p;
    int i1 = -1;
    best = -kFltMax;
    for (int i = 0; i < n; ++i) {
        const float d = Len2(p[i].p - a);
        if (d * 0.99f > best) { best = d; i1 = i; }
    }
    if (!(best >= 0.0004f) || i1 < 0) { m.n = 1; return; }
    put(1, i1);
    const V3 b = p[i1].p, E = b - a;
    int i2 = -1;
    best = -kFltMax;
    for (int i = 0; i < n; ++i) {
        const float d = std::fabs(Dot(Cross(E, p[i].p - a), nrm));
        if (d * 0.99f > best) { best = d; i2 = i; }
    }
    if (!(best >= 0.0004f) || i2 < 0) { m.n = 2; return; }
    put(2, i2);
    const V3 c = p[i2].p;
    const float area = Dot(Cross(E, c - a), nrm);
    const float inv = 1.0f / area;
    int i3 = -1;
    best = kFltMax;
    for (int i = 0; i < n; ++i) {
        const V3 q = p[i].p;
        const float w0 = Dot(Cross(c - b, q - b), nrm), w1 = Dot(Cross(a - c, q - c), nrm),
                    w2 = Dot(Cross(E, q - a), nrm);
        const float mn = std::min(std::min(w2 * inv, w1 * inv), w0 * inv);
        if (mn * 0.99f < best) { best = mn; i3 = i; }
    }
    if (best > 0 || !(std::fabs(best * area) >= 0.0004f) || i3 < 0) { m.n = 3; return; }
    put(3, i3);
    m.n = 4;
}

// 0x1005DF950: the incident face's polygon (moved into the reference frame by
// x) clipped to the reference face's side planes pushed out by 0.02;
// points within 0.02 of the reference face are projected onto it
void FaceHull(const Hull& ref, int rf, const Hull& inc, int incF, const XF& x, Manifold& m) {
    m.n = 0;
    if (rf < 0 || incF < 0) return;
    PolyV bufA[33], bufB[33];
    int cnt = 0;
    {
        const int first = inc.face[size_t(incF)];
        const int r10 = Next(inc, first);
        int r11 = first, r12 = r10;
        for (;;) {
            const int cur = r11;
            r11 = r12;
            const int vtx = Origin(inc, cur);
            bufA[cnt].p = x(inc.v[size_t(vtx)]);
            bufA[cnt].edge = cur & 0xFE;
            bufA[cnt].key = Key(rf, vtx, 0, 2);
            r12 = Next(inc, r11);
            ++cnt;
            if (r12 == r10 || cnt >= 32) break;
        }
    }
    const V3 n = ref.n[size_t(rf)];
    const float d = ref.d[size_t(rf)];
    PolyV* in = bufA;
    PolyV* out = bufB;
    const int first = ref.face[size_t(rf)];
    int r10 = first;
    V3 P = ref.v[size_t(Origin(ref, first))];
    do {
        const int cur = r10;
        r10 = Next(ref, cur);
        const V3 Qv = ref.v[size_t(Origin(ref, r10))];
        V3 N = Cross(Qv - P, n);
        N = N * RsqrtN((N.z * N.z + N.y * N.y) + N.x * N.x);
        const float off = Dot(P, N) + kHullPair;
        const int ce = cur & 0xFE;
        P = Qv;
        int k = 0;
        if (cnt == 1) {
            if (!(0 < Dot(in[0].p, N) - off)) out[k++] = in[0];
        } else if (cnt == 2) {
            const float dA = Dot(in[0].p, N) - off, dB = Dot(in[1].p, N) - off;
            if (!(0 < dA)) out[k++] = in[0];
            if (!(0 < dB)) out[k++] = in[1];
            if (!(0 <= dB * dA)) {
                const float den = dA - dB;
                const float rc = EstRcp(den);
                const V3 t1 = (in[1].p - in[0].p) * dA, q = t1 * rc;
                out[k].p = (t1 - q * den) * rc + q + in[0].p;   // its edge word is left as it was
                out[k].key = Key(ce, in[0].edge, 1, 1);
                ++k;
            }
        } else if (cnt >= 3) {
            int prev = cnt - 1;
            for (int i = 0; i < cnt && k < 32; ++i) {
                const float dP = Dot(in[prev].p, N) - off;
                if (!(0 < dP)) out[k++] = in[prev];
                const float dC = Dot(in[i].p, N) - off;
                if (!(0 <= dC * dP) && k < 32) {
                    const float den = dP - dC;
                    const float rc = EstRcp(den);
                    const V3 t1 = (in[i].p - in[prev].p) * dP, q = t1 * rc;
                    out[k].p = (t1 - q * den) * rc + q + in[prev].p;
                    out[k].key = Key(ce, in[prev].edge, 1, 1);
                    out[k].edge = in[prev].edge;
                    ++k;
                }
                prev = i;
            }
        }
        cnt = k;
        std::swap(in, out);
    } while (r10 != first);
    m.normal = n;
    if (cnt <= 0) return;
    PolyV keep[33];
    int k = 0;
    for (int i = 0; i < cnt; ++i) {
        const float s = Dot(in[i].p, n) - d;
        if (kHullPair < s || k > 31) continue;
        keep[k].p = in[i].p - n * s;
        keep[k].sep = s - kHullPair;
        keep[k].key = in[i].key;
        ++k;
    }
    if (k > 4) {
        Reduce(keep, k, n, m);
        return;
    }
    for (int i = 0; i < k; ++i) {
        m.pt[i].p = keep[i].p;
        m.pt[i].key = keep[i].key;
        m.pt[i].sep = keep[i].sep;
    }
    m.n = k;
}

struct SatResult { float sep = -kFltMax; int a = -1, b = -1, type = 0; };

// 0x1005E0030: the edge pair's contact (closest points, normal EA x EB away
// from A's centroid); `check`: the points must agree with the separation
void EdgeHull(const Hull& A, const Hull& B, const XF& x, const SatResult& r, bool check, Manifold& m) {
    m.n = 0;
    const int eA = r.a, eB = r.b;
    const V3 PA = A.v[size_t(Origin(A, eA))], QA = A.v[size_t(Origin(A, Twin(A, eA)))];
    const V3 PB = x(B.v[size_t(Origin(B, eB))]), QB = x(B.v[size_t(Origin(B, Twin(B, eB)))]);
    V3 cA, cB;
    ClosestSegSeg(PA, QA, PB, QB, &cA, &cB);
    if (check && !(Len(cB - cA) * 0.5f <= std::fabs(kHullPair + r.sep))) return;
    V3 N = Cross(QA - PA, QB - PB);
    const float l2 = (N.z * N.z + N.y * N.y) + N.x * N.x;
    if (!(l2 > kTiny)) return;
    N = N * RsqrtN(l2);
    if (!(Dot(PA - A.centroid, N) >= 0)) N = -N;
    const V3 dd = Mulv(cB - cA, N);
    m.n = 1;
    m.normal = N;
    m.pt[0].p = (cA + cB) * 0.5f;
    m.pt[0].sep = (dd.z + dd.x) + (dd.y - kHullPair);
    m.pt[0].key = Key(eA, eB, 1, 1);
}

// a face of B as the reference: clipped in B's frame, back to A's (normal
// negated, the key's A and B halves swapped)
void FaceOfB(const Hull& A, const Hull& B, const XF& toA, const XF& toB, int refB, int vertA, Manifold& m) {
    const int incA = IncidentFace(A, vertA, -Mulm(toA.R, B.n[size_t(refB)]));
    FaceHull(B, refB, A, incA, toB, m);
    m.normal = -Mulm(toA.R, m.normal);
    for (int i = 0; i < m.n; ++i) {
        const uint64_t k = m.pt[i].key;
        m.pt[i].key = Key(int(k >> 8 & 0xFF), int(k & 0xFF), int(k >> 24 & 0xFF), int(k >> 16 & 0xFF));
        m.pt[i].p = toA(m.pt[i].p);
    }
}

// 0x1005DF430: every edge pair whose arcs cross on the Gauss map; false when
// one separates (the cache then holds it)
bool EdgeQuery(const Hull& A, const Hull& B, const XF& x, SatResult& best, Contact::Cache& cache) {
    best = SatResult{};
    best.type = 0;
    const int nA = int(A.edge.size() / 4) & ~1, nB = int(B.edge.size() / 4);
    if (nB <= 0 || nA < 2) return true;
    for (int j = 0; j < nB; j += 2) {
        const V3 c = Mulm(x.R, B.n[size_t(FaceOf(B, j))]), d = Mulm(x.R, B.n[size_t(FaceOf(B, j + 1))]);
        const V3 EB = Mulm(x.R, B.v[size_t(Origin(B, Twin(B, j)))] - B.v[size_t(Origin(B, j))]);
        for (int i = 0; i < nA; i += 2) {
            const V3 a = A.n[size_t(FaceOf(A, i))], b = A.n[size_t(FaceOf(A, i + 1))];
            const V3 EA = A.v[size_t(Origin(A, Twin(A, i)))] - A.v[size_t(Origin(A, i))];
            const float cE = Dot(EA, c), dE = Dot(EA, d), aE = Dot(a, EB), bE = Dot(b, EB);
            if (!(std::max(cE * bE, std::max(aE * bE, dE * cE)) < 0)) continue;
            V3 N = Cross(EA, EB);
            const float l2 = (N.z * N.z + N.y * N.y) + N.x * N.x;
            if (!(l2 > kTiny)) continue;
            N = N * RsqrtN(l2);
            const V3 PA = A.v[size_t(Origin(A, i))], PB = B.v[size_t(Origin(B, j))];
            const float sA = Dot(PA - A.centroid, N), sB = Dot(Mulm(x.R, PB - B.centroid), N);
            const bool flip = std::fabs(sB) < std::fabs(sA) ? sA < 0 : 0 < sB;
            if (flip) N = -N;
            const float sep = Dot(x(PB) - PA, N) - kHullPair;
            if (!(sep <= 0)) {
                cache.satType = 3;
                cache.satSep = sep;
                cache.satA = uint8_t(i);
                cache.satB = uint8_t(j);
                return false;
            }
            if (!(sep <= best.sep)) best = SatResult{sep, i, j, 3};
        }
    }
    return true;
}

void HullHull(const Hull& A, const Hull& B, Q qBA, V3 pBA, Manifold& m, Contact::Cache& cache) {
    m.n = 0;
    m.normal = {};
    if (A.v.empty() || B.v.empty() || A.n.empty() || B.n.empty()) return;
    XF toA{Mat(qBA), pBA};
    XF toB{Tr(toA.R), {}};
    toB.t = -Mulm(toB.R, pBA);
    // last frame's feature, while its separation moved under 0.005
    if (cache.satType == 1 || cache.satType == 2) {
        const bool onA = cache.satType == 1;
        const int rf = onA ? cache.satA : cache.satB;
        const Hull& R = onA ? A : B;
        const Hull& I = onA ? B : A;
        const XF& toR = onA ? toA : toB;
        const XF& toI = onA ? toB : toA;
        if (size_t(rf) < R.n.size()) {
            const V3 dir = -Mulm(toI.R, R.n[size_t(rf)]);
            const int v = SupportIdx(I, dir);
            const float sep = Dot(toR(I.v[size_t(v)]), R.n[size_t(rf)]) - (R.d[size_t(rf)] + kHullPair);
            if (!(sep <= 0)) return;
            if (std::fabs(sep - cache.satSep) < 0.005f) {
                if (onA) FaceHull(A, rf, B, IncidentFace(B, v, dir), toA, m);
                else FaceOfB(A, B, toA, toB, rf, v, m);
                if (m.n > 0) return;
            }
        }
    } else if (cache.satType == 3) {
        const int eA = cache.satA, eB = cache.satB;
        if (size_t(eA) + 1 < A.edge.size() / 4 + 1 && size_t(eB) + 1 < B.edge.size() / 4 + 1) {
            const V3 EA = A.v[size_t(Origin(A, Twin(A, eA)))] - A.v[size_t(Origin(A, eA))];
            const V3 PA = A.v[size_t(Origin(A, eA))];
            const V3 c = Mulm(toA.R, B.n[size_t(FaceOf(B, eB))]), d = Mulm(toA.R, B.n[size_t(FaceOf(B, eB + 1))]);
            const float cE = Dot(c, EA), dE = Dot(d, EA);
            const V3 EB = Mulm(toA.R, B.v[size_t(Origin(B, Twin(B, eB)))] - B.v[size_t(Origin(B, eB))]);
            const float bE = Dot(-A.n[size_t(FaceOf(A, eA))], EB), aE = Dot(-A.n[size_t(FaceOf(A, eA + 1))], EB);
            if (dE * cE < 0 && bE * aE < 0 && aE * cE > 0) {
                V3 N = Cross(EA, EB);
                const float l2 = (N.z * N.z + N.y * N.y) + N.x * N.x;
                float sep = -kFltMax;
                if (l2 > kTiny) {
                    N = N * RsqrtN(l2);
                    if (!(Dot(PA - A.centroid, N) >= 0)) N = -N;
                    sep = Dot(toA(B.v[size_t(Origin(B, eB))]) - PA, N) + -kHullPair;
                }
                if (!(sep <= 0)) return;
                if (std::fabs(sep - cache.satSep) < 0.005f) {
                    EdgeHull(A, B, toA, SatResult{sep, eA, eB, 3}, true, m);
                    if (m.n > 0) return;
                }
            }
        }
    }
    // full SAT: faces of A, faces of B (a B face must beat 0.98 x best + 0.0025)
    SatResult face;
    for (size_t i = 0; i < A.n.size(); ++i) {
        const int v = SupportIdx(B, -Mulm(toB.R, A.n[i]));
        const float sep = Dot(toA(B.v[size_t(v)]), A.n[i]) - (A.d[i] + kHullPair);
        if (!(sep <= 0)) {
            cache.satType = 1; cache.satSep = sep; cache.satA = uint8_t(i); cache.satB = uint8_t(v);
            return;
        }
        if (!(sep <= face.sep)) face = SatResult{sep, int(i), v, 1};
    }
    for (size_t j = 0; j < B.n.size(); ++j) {
        const int v = SupportIdx(A, -Mulm(toA.R, B.n[j]));
        const float sep = Dot(toB(A.v[size_t(v)]), B.n[j]) - (B.d[j] + kHullPair);
        if (!(sep <= 0)) {
            cache.satType = 2; cache.satSep = sep; cache.satA = uint8_t(v); cache.satB = uint8_t(j);
            return;
        }
        if (!(sep <= face.sep * 0.98f + 0.0025f)) face = SatResult{sep, v, int(j), 2};
    }
    SatResult edge;
    if (!EdgeQuery(A, B, toA, edge, cache)) return;
    // the face contact (0x1005E02F0), then the edge pair when clearly deeper
    if (face.type == 1) FaceHull(A, face.a, B, IncidentFace(B, face.b, -Mulm(toB.R, A.n[size_t(face.a)])), toA, m);
    else if (face.type == 2) FaceOfB(A, B, toA, toB, face.b, face.a, m);
    float thr = 0.0025f;
    if (m.n > 0) {
        float mn = 0;
        for (int i = 0; i < m.n; ++i) mn = std::min(m.pt[i].sep, mn);
        thr = mn * 0.9f + 0.0025f;
    }
    bool useEdge = false;
    if (edge.type == 3) {
        const bool clear = !(edge.sep <= face.sep * 0.9f + 0.0025f);
        useEdge = (m.n < 2 && clear) || !(edge.sep <= thr);
    }
    if (!useEdge) {
        cache.satSep = face.sep; cache.satType = uint8_t(face.type);
        cache.satA = uint8_t(face.a); cache.satB = uint8_t(face.b);
        return;
    }
    m.n = 0;
    EdgeHull(A, B, toA, edge, false, m);
    cache.satSep = edge.sep; cache.satType = 3;
    cache.satA = uint8_t(edge.a); cache.satB = uint8_t(edge.b);
}
}  // namespace

// --- the island step (0x1005F2E40) --------------------------------------------------------
void Instance::World::Solve(const StepData& st, int velIters, int posIters) {
    const float h = st.h;
    // velocities: torque, the implicit gyroscopic step (body space), gravity x
    // scale, damping clamp(1 - h d, 0, 1); kinematic bodies keep theirs
    for (Body& b : bodies) {
        if (b.type == kBodyStatic) { b.v = b.w = {}; continue; }
        if (b.type != kBodyDynamic) continue;
        const M3 iIw = b.IWorldInv();
        V3 w = b.w;   // no applied torque on item bodies
        (void)iIw;
        if (b.gyro) {
            const V3 wb = RotInv(b.q, w);
            const V3 Iw = Mulm(b.I, wb);
            const V3 f = Cross(wb, Iw) * h;
            const M3 J = Add(b.I, Scale(Add(Mulm(Skew(wb), b.I), Scale(Skew(Iw), -1.0f)), h));
            const V3 r0{J.m[0], J.m[1], J.m[2]}, r1{J.m[3], J.m[4], J.m[5]}, r2{J.m[6], J.m[7], J.m[8]};
            const V3 c0 = Cross(r1, r2), c1 = Cross(r2, r0), c2 = Cross(r0, r1);
            const float k = RcpGuard(Dot(r0, c0));
            const V3 x{Dot(c0, f) * k, Dot(c1, f) * k, Dot(c2, f) * k};
            w = Rot(b.q, wb - x);
        }
        const V3 v = b.v + (gravity * (b.m * b.gs)) * (b.im * h);
        b.v = v * std::max(0.0f, std::min(1.0f - b.ld * h, 1.0f));
        b.w = w * std::max(0.0f, std::min(1.0f - b.ad * h, 1.0f));
    }
    // prepare: joints, then contacts
    for (Joint& j : joints) {
        if (j.type == kJointShoulder) PrepareShoulder(j, st);
        else if (j.type == kJointRevolute) PrepareRevolute(j, st);
        else if (j.type == kJointWeld) PrepareWeld(j, st);
    }
    PrepareContacts(st);
    for (int it = 0; it < velIters; ++it) {
        for (Joint& j : joints) {
            if (j.type == kJointShoulder) VelShoulder(j, st);
            else if (j.type == kJointRevolute) VelRevolute(j, st);
            else if (j.type == kJointWeld) VelWeld(j, st);
        }
        VelContacts();
    }
    StoreImpulses();
    // positions: non-finite velocities zeroed; |h v| capped at 4, |h w| at
    // pi/4 (both by rsqrt + Newton); the rotation renormalised when above the guard
    for (Body& b : bodies) {
        if (b.type == kBodyStatic) continue;
        if (!Finite(b.v) || !Finite(b.w)) b.v = b.w = {};
        const V3 hv = b.v * h;
        const float l2 = hv.z * hv.z + (hv.x * hv.x + hv.y * hv.y);
        float s = 1.0f;
        if (l2 > 16.0f) s = RsqrtN(l2) * 4.0f;
        if (!b.noAngCap) {
            const V3 hw = b.w * h;
            const float a2 = hw.z * hw.z + (hw.x * hw.x + hw.y * hw.y);
            if (a2 > 0.616850317f) s = std::min(s, RsqrtN(a2) * 0.785398185f);
        }
        b.v = b.v * s;
        b.w = b.w * s;
        b.c = b.v * h + b.c;
        const Q d = Mul(Q{b.w.x, b.w.y, b.w.z, 0}, b.q);
        const float hh = 0.5f * h;
        Q q{d.x * hh + b.q.x, d.y * hh + b.q.y, d.z * hh + b.q.z, d.w * hh + b.q.w};
        const float n2 = QN2(q);
        if (n2 > kTiny) {
            const float k = RsqrtN(n2);
            q = {q.x * k, q.y * k, q.z * k, q.w * k};
        }
        b.q = q;
        b.SyncOrigin();
    }
    // positions: joints, then contacts, every iteration
    for (int it = 0; it < posIters; ++it) {
        for (Joint& j : joints) {
            if (j.type == kJointShoulder) PosShoulder(j);
            else if (j.type == kJointRevolute) PosRevolute(j);
            else if (j.type == kJointWeld) PosWeld(j);
        }
        PosContacts();
    }
    for (Body& b : bodies)
        if (b.type != kBodyStatic) b.SyncOrigin();
    // sleep (0x1005F3CB1): item bodies are made with sleeping off (game body
    // + 0x14 is never set), so the island never sleeps; the rule kept as written
    float minSleep = kFltMax;
    for (Body& b : bodies) {
        if (b.type == kBodyStatic) continue;
        if (!b.allowSleep || Dot(b.w, b.w) > 0.00761543633f || Dot(b.v, b.v) > 0.00250000018f) {
            b.sleep = 0;
            minSleep = 0;
        } else {
            b.sleep += h;
            minSleep = std::min(minSleep, b.sleep);
        }
    }
    if (minSleep >= 0.5f) {
        for (Body& b : bodies) b.v = b.w = {};
        islandAwake = false;
    }
}

// b2World::Step (0x10060CDE0): collide, solve, refit the proxies, new pairs
void Instance::World::Step(float dt, int velIters, int posIters) {
    StepData st;
    st.h = dt;
    st.inv = dt > 0 ? 1.0f / dt : 0.0f;
    st.dtRatio = std::min(invDt0 * dt, 2.0f);
    st.warm = true;
    Collide();
    if (dt > 0) Solve(st, velIters, posIters);
    SyncProxies();
    FindNewContacts();
    if (dt > 0) invDt0 = st.inv;
}

// ===========================================================================
//  The instance: the client's PhysicsRagdoll (0x1011ABC30 frame, 0x1011B3790
//  drive, 0x1011B4370 per body, 0x1011B4BF0 bones).
// ===========================================================================
namespace {
struct Entry { uint16_t bone = 0xFFFF, group = 0, body = 0; float carry = 0.9f; };
struct Limits { float linLo, linLo2, linHi2, linRange, angLo, angLo2, angHi2, angRange; };

// the client's placement split (0x1011B3790): position, normalised rotation
// (identity when an axis is under 0.016), scale = the second axis's length
void Decompose(const double P[16], V3* pos, Q* rot, float* scale) {
    V3 ax[3];
    float len[3];
    for (int c = 0; c < 3; ++c) {
        ax[c] = {float(P[0 * 4 + c]), float(P[1 * 4 + c]), float(P[2 * 4 + c])};
        const float l2 = Dot(ax[c], ax[c]);
        len[c] = l2 > 2.38418579e-07f ? std::sqrt(l2) : 0.0f;
    }
    *pos = {float(P[3]), float(P[7]), float(P[11])};
    *rot = Q{};
    if (len[0] > 0.016f && len[1] > 0.016f && len[2] > 0.016f) {
        V3 x = ax[0] * (1.0f / len[0]), y = ax[1] * (1.0f / len[1]), z = ax[2] * (1.0f / len[2]);
        if (Dot(Cross(x, y), z) < 0) z = -z;
        // rows of the client's row-vector matrix are our columns
        const M3 R = {{x.x, x.y, x.z, y.x, y.y, y.z, z.x, z.y, z.z}};
        *rot = MatQ(R);
    }
    *scale = std::max(len[1], 0.016f);
}

// a bone's world target: its pivot through the bone's world matrix, and the
// matrix's normalised axes (0x1010D0720)
void BoneTarget(const double M[16], V3 pivot, V3* pos, Q* rot) {
    *pos = {float(M[0] * pivot.x + M[1] * pivot.y + M[2] * pivot.z + M[3]),
            float(M[4] * pivot.x + M[5] * pivot.y + M[6] * pivot.z + M[7]),
            float(M[8] * pivot.x + M[9] * pivot.y + M[10] * pivot.z + M[11])};
    V3 ax[3];
    float len[3];
    for (int c = 0; c < 3; ++c) {
        ax[c] = {float(M[0 * 4 + c]), float(M[1 * 4 + c]), float(M[2 * 4 + c])};
        const float l2 = Dot(ax[c], ax[c]);
        len[c] = l2 > 2.38418579e-07f ? std::sqrt(l2) : 0.0f;
    }
    *rot = Q{};
    if (len[0] > kTiny && len[1] > kTiny && len[2] > kTiny) {
        V3 x = ax[0] * (1.0f / len[0]), y = ax[1] * (1.0f / len[1]), z = ax[2] * (1.0f / len[2]);
        if (Dot(Cross(x, y), z) < 0) z = -z;
        const M3 R = {{x.x, x.y, x.z, y.x, y.y, y.z, z.x, z.y, z.z}};
        *rot = MatQ(R);
    }
}
}  // namespace

struct Instance::State {
    World w;
    std::vector<Entry> entries;
    int root = -1;               // the 0x4000 entry
    bool first = true;
    V3 pos;
    Q rot;
    float scale = 1;
    bool made = false;
    std::vector<V3> pivots;      // per body: its bone's pivot (model space)
};

Instance::Instance(const Model* m) : m_(m), s_(new State) {}
Instance::~Instance() { delete s_; }

namespace {
Q QFromFrame(const float f[9]) {
    const M3 M = {{f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8]}};
    return MatQ(M);
}
}  // namespace

void Instance::Make(float scale, V3 pos, Q rot) {
    World& w = s_->w;
    const Model& m = *m_;
    w.bodies.resize(m.bodies.size());
    for (size_t i = 0; i < m.bodies.size(); ++i) {
        const BodyDef& d = m.bodies[i];
        Body& b = w.bodies[i];
        b.type = d.type;
        b.q = rot;
        b.x = Rot(rot, d.pos) * scale + pos;
        b.gs = d.gravity;
        b.is = d.inertia;
        b.ld = d.linDamp;
        b.ad = d.angDamp;
        b.gyro = true;          // body def + 0x3E
        b.noAngCap = false;     // body def + 0x3C
        b.allowSleep = false;   // body def + 0x38 (game body + 0x14, never set)
    }
    w.shapes.resize(m.shapes.size());
    for (size_t i = 0; i < m.shapes.size(); ++i) {
        const ShapeDef& d = m.shapes[i];
        Shape& s = w.shapes[i];
        s.type = d.type;
        s.body = d.body;
        s.friction = d.friction;
        s.restitution = d.restitution;
        s.density = d.density;
        s.a = d.a * scale;
        s.b = d.b * scale;
        s.r = d.r * scale;
        if (d.type == kShapeHull && d.hull < m.hulls.size()) {
            const HullDef& H = m.hulls[d.hull];
            s.hull.centroid = H.centroid * scale;
            s.hull.volume = H.volume * scale * scale * scale;
            s.hull.area = H.area * scale * scale;
            for (const V3& p : H.v) s.hull.v.push_back(p * scale);
            s.hull.n = H.n;
            for (float dd : H.d) s.hull.d.push_back(dd * scale);
            s.hull.face = H.face;
            s.hull.edge = H.edge;
        }
        if (d.body < w.bodies.size()) w.bodies[d.body].shapes.push_back(uint16_t(i));
    }
    for (Body& b : w.bodies) w.ResetMass(b);
    w.joints.resize(m.joints.size());
    for (size_t i = 0; i < m.joints.size(); ++i) {
        const JointDef& d = m.joints[i];
        Joint& j = w.joints[i];
        j.type = d.type;
        j.a = d.a;
        j.b = d.b;
        j.pa = d.pa * scale;
        j.pb = d.pb * scale;
        j.qa = QFromFrame(d.fa);
        j.qb = QFromFrame(d.fb);
        j.mode = d.mode;
        constexpr float kDeg = 0.0174532942f;
        if (d.type == kJointShoulder) {   // 0x100601F80
            j.twLo = d.f[0] * kDeg;
            j.twHi = d.f[1] * kDeg;
            j.twState = j.twLo < j.twHi ? 1 : 0;
            j.cone = std::max(0.174532935f, std::min(d.f[2] * kDeg, 2.96705961f));
            j.coneState = 1;
            j.maxT = d.f[3];
            j.hz = d.f[4];
            j.zeta = d.f[5];
        } else if (d.type == kJointRevolute) {   // 0x1005FF280
            const float lo = d.f[0] * kDeg, hi = d.f[1] * kDeg;
            const float lower = lo > 2 * kPi ? 2 * kPi : (lo < -2 * kPi ? -2 * kPi : lo);
            const float upper = hi < lower ? lower : (hi > 2 * kPi ? 2 * kPi : hi);
            j.revCenter = (upper + lower) * 0.5f;
            j.revHalf = (upper - lower) * 0.5f;
            j.limState = lo < hi ? 1 : 0;
            j.maxT = d.f[2];
            j.hz = d.f[3];
            j.zeta = d.f[4];
        } else if (d.type == kJointWeld) {   // 0x1006063B0
            j.angHz = d.f[0];
            j.angZeta = d.f[1];
            j.linHz = d.f[2];
            j.linZeta = d.f[3];
        }
        const uint16_t lo = std::min(j.a, j.b), hi = std::max(j.a, j.b);
        w.joined.push_back(uint32_t(lo) << 16 | hi);
    }
    // carry entries; groups: when none is set the first kinematic body heads
    s_->entries.resize(m.bodies.size());
    bool any = false;
    for (size_t i = 0; i < m.bodies.size(); ++i) {
        Entry& e = s_->entries[i];
        e.bone = m.bodies[i].bone;
        e.group = m.bodies[i].group;
        e.carry = m.bodies[i].carry;
        e.body = uint16_t(i);
        any = any || e.group != 0;
        if (e.group == 0x4000) s_->root = int(i);
    }
    if (!any)
        for (Entry& e : s_->entries)
            if (w.bodies[e.body].type == kBodyKinematic) { e.group = 0x8000; break; }
    w.proxies.assign(w.shapes.size(), Proxy{});
    for (size_t i = 0; i < w.shapes.size(); ++i) {
        V3 lo, hi;
        w.TightBox(w.shapes[i], &lo, &hi);
        w.proxies[i].lo = lo - V3{0.1f, 0.1f, 0.1f};
        w.proxies[i].hi = hi + V3{0.1f, 0.1f, 0.1f};
    }
    w.FindNewContacts();
    s_->scale = scale;
    s_->made = true;
}

namespace {
// 0x1011B4370: one kinematic body towards its bone. A fraction s of the move
// is a teleport, the rest is velocity; true when it teleported. The origin
// before and the delta (origin shift, rotation) go out for the carry.
bool DriveBody(Body& b, V3 tpos, Q tq, float inv, float carry, const Limits& L, V3* oldOrigin, V3* dPos, Q* dRot,
               bool wantDelta, bool* woke) {
    const V3 x = b.x;
    const Q q = b.q;
    *oldOrigin = x;
    const V3 rq = Rot(q, b.lc);
    const V3 toT = (tpos - x) + Rot(tq, b.lc);       // target COM - origin
    const V3 dcom = toT - rq;                        // target COM - COM
    const Q qf = QDot(q, tq) >= 0 ? tq : Q{-tq.x, -tq.y, -tq.z, -tq.w};
    auto angVel = [&](Q from, Q to) {
        const Q d{(to.x - from.x) * inv, (to.y - from.y) * inv, (to.z - from.z) * inv, (to.w - from.w) * inv};
        const Q r = Mul(d, Conj(from));
        return V3{r.x + r.x, r.y + r.y, r.z + r.z};
    };
    const V3 wReq = angVel(q, qf);
    const V3 vReq = dcom * inv;
    auto frac = [&](float l2, float lo, float lo2, float hi2, float range) {
        if (l2 <= lo2) return 0.0f;
        if (!(l2 <= hi2)) return 1.0f;
        return Clamp01(DivN(std::sqrt(l2) - lo, range)) * (1.0f - carry) + carry;
    };
    const float fa = frac(Dot(wReq, wReq), L.angLo, L.angLo2, L.angHi2, L.angRange);
    const float fl = frac(Dot(vReq, vReq), L.linLo, L.linLo2, L.linHi2, L.linRange);
    const float s = std::max(std::max(fa, fl), carry);
    if (s <= 0) {
        if (Finite(vReq)) b.v = vReq;
        if (Finite(wReq)) b.w = wReq;
        return false;
    }
    // nlerp the rotation, move the centre of mass by s of the way
    const Q qt = QDot(q, tq) >= 0 ? tq : Q{-tq.x, -tq.y, -tq.z, -tq.w};
    Q qn{(qt.x - q.x) * s + q.x, (qt.y - q.y) * s + q.y, (qt.z - q.z) * s + q.z, (qt.w - q.w) * s + q.w};
    V3 rn = rq;
    if (QN2(qn) > kTiny) {
        qn = QNormFast(qn);
        rn = Rot(qn, b.lc);
    } else {
        qn = q;
    }
    const V3 comShift = dcom * s + rq;
    const V3 origin = x + (comShift - rn);
    b.SetTransform(origin, qn);
    *woke = true;
    const Q qf2 = QDot(qn, tq) >= 0 ? tq : Q{-tq.x, -tq.y, -tq.z, -tq.w};
    const V3 wRem = angVel(qn, qf2);
    const V3 vRem = (toT - comShift) * inv;
    if (wantDelta) {
        *dPos = comShift - rn;
        *dRot = Mul(qn, Conj(q));
    }
    if (Finite(vRem)) b.v = vRem;
    if (Finite(wRem)) b.w = wRem;
    return true;
}

// a carried body: rotated about the head's old origin and shifted
void CarryBody(Body& b, V3 pivot, V3 dPos, Q dRot) {
    const V3 o = Rot(dRot, b.x - pivot) + dPos + pivot;
    b.SetTransform(o, Mul(dRot, b.q));
}
}  // namespace

void Instance::Step(const Frame& f) {
    if (!m_ || m_->bodies.empty()) return;
    State& S = *s_;
    float dt = std::min(f.dt, 1.0f / 30.0f);
    V3 ppos;
    Q prot;
    float pscale;
    Decompose(f.placement, &ppos, &prot, &pscale);
    if (!S.made) Make(pscale, ppos, prot);
    World& w = S.w;
    S.pivots.assign(m_->bodies.size(), V3{});
    for (size_t i = 0; i < m_->bodies.size(); ++i) {
        const uint16_t bone = m_->bodies[i].bone;
        if (bone < f.boneCount && f.pivots) S.pivots[i] = f.pivots[bone];
    }
    S.pos = ppos;
    S.rot = prot;
    // model scale changed by more than 0.1%: rescale the bodies (0x1005EA370)
    if (pscale < S.scale * 0.999f || S.scale * 1.001f < pscale) {
        const float k = std::max(pscale / S.scale, 0.016f);
        for (Shape& s : w.shapes) {
            s.a = s.a * k;
            s.b = s.b * k;
            s.r *= k;
            s.hull.centroid = s.hull.centroid * k;
            s.hull.volume *= k * k * k;
            for (V3& p : s.hull.v) p = p * k;
            for (float& d : s.hull.d) d *= k;
        }
        for (Joint& j : w.joints) {
            j.pa = j.pa * k;
            j.pb = j.pb * k;
        }
        for (Body& b : w.bodies) w.ResetMass(b);
        S.scale = pscale;
    }
    auto target = [&](const Entry& e, V3* p, Q* q) {
        if (e.bone >= f.boneCount || !f.boneWorld || !f.pivots) {
            *p = ppos;
            *q = prot;
            return;
        }
        BoneTarget(f.boneWorld[e.bone], f.pivots[e.bone], p, q);
    };
    if (S.first) {   // first frame: every body to its bone's pivot, the placement's rotation
        S.first = false;
        for (const Entry& e : S.entries) {
            V3 p;
            Q q;
            target(e, &p, &q);
            if (e.bone == 0xFFFF) p = ppos;
            w.bodies[e.body].SetTransform(p, prot);
        }
        return;
    }
    if (!(dt > 0)) return;
    const float inv = 1.0f / dt;
    const float invc = std::min(inv, 45.0f);
    Limits L;
    L.linLo = 0.25f * invc;
    L.linLo2 = L.linLo * L.linLo;
    const float linHi = 0.5f * invc;
    L.linHi2 = linHi * linHi;
    L.linRange = linHi - L.linLo;
    L.angLo = 0.392699093f * invc;
    L.angLo2 = L.angLo * L.angLo;
    const float angHi = 0.785398185f * invc;
    L.angHi2 = angHi * angHi;
    L.angRange = angHi - L.angLo;
    bool woke = false;
    // the root (0x4000) first, its delta kept
    V3 rootOld, rootD;
    Q rootQ;
    const int n = int(S.entries.size());
    if (S.root >= 0) {
        const Entry& e = S.entries[S.root];
        V3 p;
        Q q;
        target(e, &p, &q);
        DriveBody(w.bodies[e.body], p, q, inv, e.carry, L, &rootOld, &rootD, &rootQ, true, &woke);
    }
    for (int i = 0; i < n; ++i) {
        const Entry& e = S.entries[i];
        Body& b = w.bodies[e.body];
        if (b.type == kBodyKinematic && i != S.root) {
            V3 p, old, d;
            Q q, dq;
            target(e, &p, &q);
            const bool head = e.group == 0x8000;
            const bool tele = DriveBody(b, p, q, inv, e.carry, L, &old, &d, &dq, head, &woke);
            if (!tele) {
                if (head) woke = true;   // its dynamic followers are woken
                continue;
            }
            if (!head && S.root < 0) continue;
            V3 cd = d;
            Q cq = dq;
            if (S.root >= 0) { cd = rootD; cq = rootQ; }   // the root's delta, about this body's old origin
            while (i + 1 < n && w.bodies[S.entries[i + 1].body].type != kBodyKinematic) {
                ++i;
                CarryBody(w.bodies[S.entries[i].body], old, cd, cq);
                woke = true;
            }
            continue;
        }
        if (S.root < 0) { woke = true; continue; }
        // a dynamic body or the root: the following dynamic bodies ride the root
        while (i + 1 < n && w.bodies[S.entries[i + 1].body].type != kBodyKinematic) {
            ++i;
            CarryBody(w.bodies[S.entries[i].body], rootOld, rootD, rootQ);
        }
        woke = true;
    }
    if (woke) w.islandAwake = true;
    // the item's own world: only when every body is awake (0x1011B6040)
    if (w.islandAwake) w.Step(dt, 8, 12);
}

// 0x1011B4BF0: a dynamic body's bone, model space: [R_rel | p_rel / scale] x T(-pivot)
bool Instance::Output(size_t body, double out[16]) const {
    const State& S = *s_;
    if (!S.made || body >= S.w.bodies.size() || S.w.bodies[body].type != kBodyDynamic) return false;
    const Body& b = S.w.bodies[body];
    const Q rr = Mul(Conj(S.rot), b.q);
    const V3 p = RotInv(S.rot, b.x - S.pos) * (1.0f / S.scale);
    const M3 R = Mat(rr);
    const V3 pv = S.pivots[body];
    const V3 t = p - Mulm(R, pv);
    for (int r = 0; r < 3; ++r) {
        out[r * 4 + 0] = R.m[r * 3 + 0];
        out[r * 4 + 1] = R.m[r * 3 + 1];
        out[r * 4 + 2] = R.m[r * 3 + 2];
    }
    out[3] = t.x;
    out[7] = t.y;
    out[11] = t.z;
    out[12] = out[13] = out[14] = 0;
    out[15] = 1;
    return true;
}

// --- the table (PHY2) ------------------------------------------------------------------
bool NormPath(const char* in, char* out, size_t cap) {
    if (!in || !out || cap < 2) return false;
    size_t n = 0;
    for (; in[n]; ++n) {
        if (n + 1 >= cap) return false;
        char c = in[n];
        if (c == '/') c = '\\';
        if (c >= 'A' && c <= 'Z') c = char(c - 'A' + 'a');
        out[n] = c;
    }
    out[n] = 0;
    if (n >= 4 && (strcmp(out + n - 4, ".mdx") == 0 || strcmp(out + n - 4, ".mdl") == 0)) {
        out[n - 3] = 'm';
        out[n - 2] = '2';
        out[n - 1] = 0;
    }
    return n > 0;
}

uint32_t Fnv1a(const char* s) {
    uint32_t h = 0x811C9DC5u;
    for (; *s; ++s) h = (h ^ uint8_t(*s)) * 0x01000193u;
    return h;
}

namespace {
struct Reader {
    const uint8_t* p;
    size_t n, o;
    bool ok = true;
    template <class T> T get() {
        T v{};
        if (o + sizeof(T) > n) { ok = false; return v; }
        memcpy(&v, p + o, sizeof(T));
        o += sizeof(T);
        return v;
    }
    V3 v3() { V3 v; v.x = get<float>(); v.y = get<float>(); v.z = get<float>(); return v; }
    void align4() { o += (4 - (o & 3)) & 3; }
};
}  // namespace

bool Find(const uint8_t* blob, size_t size, const char* normPath, Model& out) {
    if (!blob || size < 8 || memcmp(blob, "PHY2", 4) != 0) return false;
    uint32_t count;
    memcpy(&count, blob + 4, 4);
    if (8 + size_t(count) * 8 > size) return false;
    const uint32_t h = Fnv1a(normPath);
    size_t lo = 0, hi = count;
    while (lo < hi) {
        const size_t mid = (lo + hi) / 2;
        uint32_t mh;
        memcpy(&mh, blob + 8 + mid * 8, 4);
        if (mh < h) lo = mid + 1;
        else hi = mid;
    }
    for (; lo < count; ++lo) {
        uint32_t mh, off;
        memcpy(&mh, blob + 8 + lo * 8, 4);
        memcpy(&off, blob + 8 + lo * 8 + 4, 4);
        if (mh != h) return false;
        Reader r{blob, size, off};
        const uint16_t pl = r.get<uint16_t>();
        if (!r.ok || r.o + pl > size) return false;
        const bool same = strlen(normPath) == pl && memcmp(blob + r.o, normPath, pl) == 0;
        r.o += pl;
        r.align4();
        if (!same) continue;
        Model m;
        const uint8_t nb = r.get<uint8_t>(), ns = r.get<uint8_t>(), nj = r.get<uint8_t>(), nh = r.get<uint8_t>();
        for (int i = 0; i < nb && r.ok; ++i) {
            BodyDef b;
            b.type = r.get<uint8_t>();
            r.get<uint8_t>();
            b.bone = r.get<uint16_t>();
            b.pos = r.v3();
            b.gravity = r.get<float>();
            b.inertia = r.get<float>();
            b.linDamp = r.get<float>();
            b.angDamp = r.get<float>();
            b.carry = r.get<float>();
            b.group = r.get<uint16_t>();
            r.get<uint16_t>();
            m.bodies.push_back(b);
        }
        for (int i = 0; i < ns && r.ok; ++i) {
            ShapeDef s;
            s.body = r.get<uint8_t>();
            s.type = r.get<uint8_t>();
            s.hull = r.get<uint16_t>();
            s.friction = r.get<float>();
            s.restitution = r.get<float>();
            s.density = r.get<float>();
            s.a = r.v3();
            s.b = r.v3();
            s.r = r.get<float>();
            if (s.body >= nb) return false;
            m.shapes.push_back(s);
        }
        for (int i = 0; i < nj && r.ok; ++i) {
            JointDef j;
            j.type = r.get<uint8_t>();
            j.a = r.get<uint8_t>();
            j.b = r.get<uint8_t>();
            j.mode = r.get<int8_t>();
            j.pa = r.v3();
            j.pb = r.v3();
            for (float& x : j.fa) x = r.get<float>();
            for (float& x : j.fb) x = r.get<float>();
            for (float& x : j.f) x = r.get<float>();
            if (j.a >= nb || j.b >= nb) return false;
            m.joints.push_back(j);
        }
        for (int i = 0; i < nh && r.ok; ++i) {
            HullDef H;
            const uint8_t nv = r.get<uint8_t>(), nf = r.get<uint8_t>(), ne = r.get<uint8_t>();
            r.get<uint8_t>();
            H.centroid = r.v3();
            H.volume = r.get<float>();
            H.area = r.get<float>();
            for (int k = 0; k < nv; ++k) H.v.push_back(r.v3());
            for (int k = 0; k < nf; ++k) {
                H.n.push_back(r.v3());
                H.d.push_back(r.get<float>());
            }
            for (int k = 0; k < nf; ++k) H.face.push_back(r.get<uint8_t>());
            for (int k = 0; k < ne * 4; ++k) H.edge.push_back(r.get<uint8_t>());
            r.align4();
            for (uint8_t fe : H.face)
                if (fe >= ne) return false;
            for (int k = 0; k < ne; ++k)
                if (H.edge[k * 4 + 1] >= nv || H.edge[k * 4 + 2] >= nf || H.edge[k * 4 + 3] >= ne ||
                    k + int8_t(H.edge[k * 4]) < 0 || k + int8_t(H.edge[k * 4]) >= ne)
                    return false;
            m.hulls.push_back(std::move(H));
        }
        if (!r.ok) return false;
        for (const ShapeDef& s : m.shapes)
            if (s.type == kShapeHull && s.hull >= m.hulls.size()) return false;
        out = std::move(m);
        return true;
    }
    return false;
}

// --- the client's matrices --------------------------------------------------------------
// Row-vector float B (translation 12..14) <-> column double (m[r * 4 + c]).
void Column(const float P[16], double out[16]) {
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c) out[r * 4 + c] = P[c * 4 + r];
}

void World(const float B[16], const float P[16], double out[16]) {
    double b[16], p[16];
    Column(B, b);
    Column(P, p);
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c) {
            double s = 0;
            for (int k = 0; k < 4; ++k) s += p[r * 4 + k] * b[k * 4 + c];
            out[r * 4 + c] = s;
        }
}

// B = M^T x modelMat (row vectors): model-space bone, then the model's own matrix
void FromModel(const double M[16], const float modelMat[16], float B[16]) {
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c) {
            double s = 0;
            for (int k = 0; k < 4; ++k) s += M[k * 4 + r] * modelMat[k * 4 + c];
            B[r * 4 + c] = float(s);
        }
}

namespace {
// inverse of a row-vector affine float matrix
bool InvertRow(const float* m, double* o) {
    const double a = m[0], b = m[1], c = m[2], d = m[4], e = m[5], f = m[6], g = m[8], h = m[9], i = m[10];
    const double det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
    if (std::fabs(det) < 1e-12) return false;
    const double k = 1.0 / det;
    o[0] = (e * i - f * h) * k; o[1] = (c * h - b * i) * k; o[2] = (b * f - c * e) * k; o[3] = 0;
    o[4] = (f * g - d * i) * k; o[5] = (a * i - c * g) * k; o[6] = (c * d - a * f) * k; o[7] = 0;
    o[8] = (d * h - e * g) * k; o[9] = (b * g - a * h) * k; o[10] = (a * e - b * d) * k; o[11] = 0;
    const double tx = m[12], ty = m[13], tz = m[14];
    o[12] = -(tx * o[0] + ty * o[4] + tz * o[8]);
    o[13] = -(tx * o[1] + ty * o[5] + tz * o[9]);
    o[14] = -(tx * o[2] + ty * o[6] + tz * o[10]);
    o[15] = 1;
    return true;
}
}  // namespace

void Carry(const int16_t* parent, size_t n, const float* before, float* bones, const bool* moved) {
    for (size_t c = 0; c < n; ++c) {
        if (moved[c]) continue;
        int p = parent[c];
        int guard = 0;
        while (p >= 0 && size_t(p) < n && !moved[p] && guard++ < 256) p = parent[p];
        if (p < 0 || size_t(p) >= n || !moved[p]) continue;
        double inv[16];
        if (!InvertRow(before + size_t(p) * 16, inv)) continue;
        // B'[c] = B[c] x B[p]^-1 x B'[p] (row vectors)
        double t[16];
        const float* bc = before + c * 16;
        for (int r = 0; r < 4; ++r)
            for (int k = 0; k < 4; ++k) {
                double s = 0;
                for (int q = 0; q < 4; ++q) s += bc[r * 4 + q] * inv[q * 4 + k];
                t[r * 4 + k] = s;
            }
        const float* bp = bones + size_t(p) * 16;
        float res[16];
        for (int r = 0; r < 4; ++r)
            for (int k = 0; k < 4; ++k) {
                double s = 0;
                for (int q = 0; q < 4; ++q) s += t[r * 4 + q] * bp[q * 4 + k];
                res[r * 4 + k] = float(s);
            }
        memcpy(bones + c * 16, res, sizeof res);
    }
}

}  // namespace physics
}  // namespace ec
}  // namespace wow
