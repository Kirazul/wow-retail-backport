// ===========================================================================
//  Physics.h - retail runtime physics (.phys) for the items that move by it:
//  helm plumes, shoulder ribbons, buckle chains, weapon tassels.
//
//  A PORT of the retail client's own engine (Blizzard's "Domino", a 3D
//  Box2D-2.x family solver), read instruction by instruction from the
//  Midnight 12.1.0.69933 client (macOS x86_64; addresses below are its VAs)
//  and cross-checked against the MoP 5.4.8 client (readable Windows build).
//  Nothing here is tuned: every constant and every rule is the client's.
//
//  PER ITEM INSTANCE (retail PhysicsRagdoll, list world+0x78):
//    its own private world (0x1005C98F0), gravity (0,0,-10) yd/s^2;
//    each frame (0x1011ABC30):
//      dt = min(frame dt, 1/30)
//      DRIVE (0x1011B3790/0x1011B4370): every kinematic body is moved to its
//        bone (pivot through the bone matrix and the placement); a fraction
//        s = max(fracAngular, fracLinear, carry) of the move is a teleport
//        (carry = BDY4 0x28, default 0.9), the rest velocity; the dynamic
//        bodies of its carry group (BDY4 0x2C) are carried by the same delta
//      STEP (0x1011B6040 -> 0x1005C9980): only when every body is awake;
//        8 velocity / 12 position iterations (container +8 / +0xC)
//      BONES (0x1011B4BF0): every dynamic body writes its bone,
//        [R_rel | p_rel / scale] x T(-pivot), model space
//  Plan: docs/superpowers/plans/2026-10-10-runtime-physics.md.
// ===========================================================================
#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace wow {
namespace ec {
namespace physics {

struct V3 { float x = 0, y = 0, z = 0; };
struct Q { float x = 0, y = 0, z = 0, w = 1; };       // retail lane order
struct M3 { float m[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1}; };  // row major

// --- the table (packs\midnight\midnight_physics.edb, "PHY2") ---------------
// Raw retail fields; every conversion the client makes (degrees, clamps,
// defaults, mass) is made by this file, as the client makes it.
enum : uint8_t { kBodyDynamic = 0, kBodyKinematic = 1, kBodyStatic = 2 };   // Domino body types
enum : uint8_t { kShapeSphere = 0, kShapeCapsule = 1, kShapeHull = 2 };     // Domino shape types
enum : uint8_t { kJointDistance = 2, kJointPrismatic = 5, kJointRevolute = 6, kJointShoulder = 7,
                 kJointSpherical = 8, kJointWeld = 9 };                     // Domino joint types

struct BodyDef {
    uint8_t type = kBodyDynamic;
    uint16_t bone = 0xFFFF;
    V3 pos;                        // model space (+ the file's pivot shift)
    float gravity = 1, inertia = 1, linDamp = 0, angDamp = 0, carry = 0.9f;
    uint16_t group = 0;            // BDY4 0x2C
};
struct ShapeDef {
    uint8_t body = 0, type = kShapeCapsule;
    uint16_t hull = 0;             // hull: index into Model::hulls
    float friction = 0.6f, restitution = 0, density = 1;
    V3 a, b;                       // sphere: a = centre; capsule: a, b (body space)
    float r = 0;
};
// A PLYT polytope as the file has it: vertices, face planes (n . x = d), each
// face's first half-edge, half-edges of 4 bytes {int8 twin offset, origin
// vertex, face, next half-edge}, and the file's centroid, volume and area.
struct HullDef {
    V3 centroid;
    float volume = 0, area = 0;
    std::vector<V3> v, n;
    std::vector<float> d;
    std::vector<uint8_t> face, edge;
};
struct JointDef {
    uint8_t type = kJointShoulder, a = 0, b = 0;
    int8_t mode = 0;               // motor mode (signed byte, as the client reads it)
    V3 pa, pb;                     // frame positions (body space, model units)
    float fa[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1};   // frame rotations: the file's 3x3,
    float fb[9] = {1, 0, 0, 0, 1, 0, 0, 0, 1};   // axis images as rows
    // shoulder: twistLo, twistHi, cone (degrees), maxTorque, Hz, damping
    // revolute: lower, upper (degrees), maxTorque, Hz, damping
    // weld:     angular Hz, angular damping, linear Hz, linear damping
    float f[6] = {};
};
struct Model {
    std::vector<BodyDef> bodies;
    std::vector<ShapeDef> shapes;
    std::vector<JointDef> joints;
    std::vector<HullDef> hulls;
};

// The table's key: lowercase, '/' -> '\', .mdx / .mdl -> .m2. False when it
// does not fit.
bool NormPath(const char* in, char* out, size_t cap);
uint32_t Fnv1a(const char* s);
// The row for a normalised path in a PHY2 blob, decoded; false when absent or
// malformed (every read is bounds-checked).
bool Find(const uint8_t* blob, size_t size, const char* normPath, Model& out);

// --- the frame's inputs -----------------------------------------------------
// Column form, row major (m[r * 4 + c]); a bone matrix maps a model-bind
// point to the WORLD; the placement maps model space to the world.
struct Frame {
    const double (*boneWorld)[16] = nullptr;   // per bone
    const V3* pivots = nullptr;                // per bone, model space
    size_t boneCount = 0;
    double placement[16] = {};
    float dt = 0;                              // the game's frame time, s
};

class Instance {
public:
    explicit Instance(const Model* m);
    ~Instance();
    Instance(const Instance&) = delete;
    Instance& operator=(const Instance&) = delete;
    // One frame: drive, step, outputs.
    void Step(const Frame& f);
    // The MODEL-SPACE matrix (column form) to draw dynamic body i's bone
    // with; false for a body that does not write its bone.
    bool Output(size_t body, double out[16]) const;
    const Model* model() const { return m_; }
    struct World;
    struct State;
private:
    void Make(float scale, V3 pos, Q rot);
    const Model* m_;
    State* s_;
};

// THE CLIENT'S MATRICES (glue helpers). A posed bone matrix B (row-vector,
// translation in 12..14) takes a model-bind point into the scene's
// CAMERA-RELATIVE space; the scene matrix S (model + 0x28 -> + 0xC4) takes
// that space to the world. S is a MIRROR (det -1, measured live 2026-10-11),
// so it is never decomposed: the instance's placement (retail's model
// position / rotation / scale) is the model's own matrix (model + 0xF4,
// camera-relative, the same space as B) through S. World(B, S) is the
// column-form world matrix; FromModel turns retail's model-space bone back
// into the client's B through the model's own matrix.
void World(const float B[16], const float S[16], double out[16]);
void Column(const float P[16], double out[16]);
void FromModel(const double M[16], const float modelMat[16], float B[16]);
// Every bone under a moved one follows it: B'[c] = B[c] x B[p]^-1 x B'[p] for
// the nearest moved ancestor p. Bones are n x 16 floats.
void Carry(const int16_t* parent, size_t n, const float* before, float* bones, const bool* moved);

// THE LIVE ENTRY (PhysicsLive.cpp): after the client built `model`'s bone
// matrices. Nothing happens for a model whose file has no physics row, or
// when packs\midnight\midnight_physics.edb is absent.
void StepLive(void* model);

}  // namespace physics
}  // namespace ec
}  // namespace wow
