# Final Fantasy: Crystal Chronicles (GameCube) — File Format Specification

This document describes the on-disc binary formats used by FFCC's map and
character assets, reverse engineered from the retail GameCube disc image and
the game's executable (`ffcc.dol`, analyzed in Ghidra; addresses below are
raw DOL file offsets). It's written to be sufficient for an independent
implementation of a parser/importer/converter, without needing to read the
Python code in the FFCC Importer Blender addon.

All multi-byte integers and floats are **big-endian** (GameCube/PowerPC).

---

## 1. The universal chunk format

Every container format below (`.mpl`, `.mtx`, `.otm`, `.chm`, `.tex`, `.cha`)
is built from the same recursive chunk structure:

```
struct ChunkHeader {
    char     magic[4];   // ASCII tag, e.g. "MESH", "VSET", "NAME" — not
                         // necessarily NUL-padded to 4 bytes if it's
                         // exactly 4 printable characters (e.g. "UV  " is
                         // "UV" + 2 spaces; some tags use exactly 4 letters)
    uint32   size;       // payload size in bytes, NOT including this header
                         // and NOT including inter-chunk padding
    uint32   field1;     // meaning depends on chunk type: often a sub-count
                         // (e.g. array element count), sometimes a flag
    uint32   field2;     // meaning depends on chunk type; frequently unused
                         // (0) at the outer container level
};
// followed by `size` bytes of payload, then padding to the next multiple
// of the container's padding unit (see below), before the next sibling
// chunk header.
```

- Chunk padding is **16 bytes** in most containers (`.chm`, `.otm`, `.cha`,
  `.tex`, and the map `.mpl`'s inner sub-chunks). The map `.mtx` texture set
  uses **32-byte** padding between `TXTR` entries.
- A chunk "contains" child chunks when its payload is itself a sequence of
  ChunkHeader+payload records; whether a chunk is a container or a leaf
  (raw bytes) is fixed per magic value, not self-describing.
- Parsing in general: read the header, if `magic` isn't 4 printable ASCII
  bytes (`0x20`–`0x7E`) treat it as end-of-stream/unknown and stop; otherwise
  recurse into or store the payload, then advance
  `offset += align(16 + size, pad_unit)` and repeat until you run past the
  parent's declared end.
- The outer-most chunk in every file is the file's own magic + total payload
  size (e.g. a `.mpl` file starts with `MESH` covering the whole file).

### 1.1 GX display-list vertex/geometry sub-format

Several formats (map `.mpl`, character `.chm` mesh data) embed GameCube GX
hardware display lists as raw opcode streams. This is **not** a chunked
format — it's a flat sequence of:

```
struct GxPrimitive {
    uint8    opcode;      // primitive type, ORed with a "use indices" bit
                         // pattern; the low 3 bits are masked off when
                         // checking the primitive type (opcode & 0xF8)
    uint16   vertex_count; // big-endian
    Vertex   vertices[vertex_count];
};
```

Recognized primitive base opcodes (after masking `& 0xF8`):

| Value  | Primitive          |
|--------|--------------------|
| 0x80   | Quads              |
| 0x90   | Triangles          |
| 0x98   | Triangle strip     |
| 0xA0   | Triangle fan       |

A stream ends at a `0x00` opcode byte, or at any byte that doesn't match a
known primitive base.

Each `Vertex` is 4 big-endian `uint16` **indices** (not raw data) into
separately-stored attribute arrays, in this fixed order:
`(position_index, normal_index, color_index, uv_index)` — 8 bytes/vertex.

Materials with **two** texture indices (`TIDX` count ≥ 2, i.e. multi-texture
blending) use a **10-byte** vertex instead: the same 4 indices plus one
extra `uint16` (a second UV/texcoord index for `GX_VA_TEX1`) appended after
the first four. There is no in-band flag for this — the reader must know
from the associated material (see §2) how many texture slots it references,
and should additionally sanity-check by parsing to completion at both
strides and picking whichever consumes the buffer exactly (`gx_stream_fits`
in the reference implementation).

**Rendering a dual-texture material**: `TIDX[0]` is the base texture (its
own UV index is the vertex's normal 4th field); `TIDX[1]` is a SECOND,
independently-UV-mapped texture layer composited on top of the base — in
practice this is how eye and mouth "flipbook" textures (a texture atlas
laid out as a grid of expression/blink frames) are pasted onto a
character's base skin texture: the decal's own small UV rect selects one
grid cell, alpha-blended over the base color using the decal texture's
own alpha channel. Confirmed by direct inspection of a player character's
face mesh (pc/c101): every corner belonging to a dual-texture material's
DLST has a second UV index that's numerically DIFFERENT from its first
(selecting a different point in the shared UV array), while single-texture
materials' 4-byte vertices trivially have no second index at all. A
reader that only uses `TIDX[0]` will render dual-texture materials with
just the base texture — geometrically present but visually indistinguishable
from "missing" whenever the base texture alone doesn't suggest eyes/mouth
(e.g. a plain skin-colored face texture with the eye/mouth decals never
composited on top).

Quad primitives are consumed 4 vertices at a time and fanned into 2
triangles `(v0,v1,v2)` + `(v0,v2,v3)`. Triangle strips alternate winding
per Bird-index (`(v[i],v[i+1],v[i+2])` for even `i`, `(v[i+1],v[i],v[i+2])`
for odd `i`, keeping consistent facing). Triangle fans are `(v0, vi, vi+1)`.

---

## 2. Map format (`.mpl` + `.mtx` + `.otm`)

A map is spread across three related files sharing a `mapNNN` prefix (`NNN`
= 3-digit zone number), typically with additional numeric suffixes for
multi-part zones (e.g. `map002_0.mpl`, `map002_1.mpl`):

- **`.mpl`** — mesh geometry (vertex/normal/UV/color arrays + GX display
  lists), no materials or scene graph.
- **`.mtx`** — texture set (raw pixel data).
- **`.otm`** — scene graph (instance placement/transform) + material table.

### 2.1 `.mpl` — mesh file

```
MESH (top-level chunk, size = whole payload)
  VSET                         -- vertex attribute arrays for one "pair"
    VERT   -- raw f32[3] positions, 12 bytes/vertex, ALWAYS float
    NORM   -- normals: EITHER f32[3] (12B, early "stg000"-era maps)
              OR s16[3] fixed-point /16384.0 (6B, later maps)
    UV     -- texcoords: EITHER f32[2] (8B, early maps)
              OR s16[2] fixed-point /1024.0 (4B, later maps)
    COLR   -- raw RGBA8 bytes, 4 bytes/color, always byte format
  DSET                         -- one or more display lists for the
                                  immediately-preceding VSET ("pair")
    DLHD
      DLST*                    -- see §2.1.1 below; one per material used
  (VSET, DSET pairs repeat — each DSET's geometry indexes into the
   VSET that immediately precedes it)
```

**Float-vs-fixed-point ambiguity**: whether `NORM`/`UV` are stored as
floats or GX fixed-point ints is NOT flagged in the file; it varies by
which development period the map data was authored in. Disambiguate by
computing the maximum normal/UV index actually referenced by the DSET's
display lists (`max_ni`, `max_ui`) and checking which element size makes
`array_byte_length / element_size` exceed that max index — the wrong
interpretation reads half as many elements, which is smaller than an index
that's actually used, and the correct interpretation isn't. (`VERT` and
`COLR` do not have this ambiguity — they're always float/byte respectively.)

#### 2.1.1 `DLST` sub-header

```
struct DlstSubHeader {
    uint16  tex_idx;      // index into the OTM's MSET/MATL table (§2.3),
                         // NOT a direct texture slot index
    // remaining sub-header bytes (size = DLST.size - DLST.field1) are
    // unused/unparsed by the reference implementation
};
// followed immediately by DLST.field1 bytes of raw GX opcode stream
// (see §1.1) — DLST.field1 is the GX payload's exact byte length.
```

Vertex stride for the embedded GX stream is 8 bytes normally, 10 bytes if
the resolved material (via `tex_idx` → OTM MATL → `TIDX` count) has 2+
texture indices.

### 2.2 `.mtx` — texture set

```
TSET (top level; field1 = texture count in this file, or 1 if this is the
      last file in a concatenated sequence — see §2.4)
  TXTR*                        -- one per texture, 32-byte inter-entry pad
    NAME   -- NUL-terminated ASCII string
    FMT    -- first byte = FFCC format code (see §2.5)
    SIZE   -- uint32 width, uint32 height
    IMAG   -- raw pixel data in the format given by FMT
```

Texture index (for `.mtx`'s own array) = position in file order among
`TXTR` entries (0-based).

### 2.3 `.otm` — scene graph + material table

```
OTM (top level)
  SCEN
    SCAL?                      -- (legacy-era only, not always present)
    MSET                       -- material table
      MATL*
        TIDX  -- field1 = count of following uint32 texture indices into
                 the map's runtime MTX texture set (§2.4); count MAY be 0
                 for an untextured material (this is what makes MATL
                 index ≠ texture index)
        NAME  -- NUL-terminated ASCII
        ATRB  -- byte at offset 4 = blend mode: 0x04 = opaque,
                 0x00/0x01/0x03 = alpha-blended/translucent variants
        FUR? / BUMP? / JIME? / WATR?   -- optional extra texture layers +
                 animation-type byte at MATL+0xA2 (not required for basic
                 static import)
        TSCL? -- optional UV-scroll animation keyframes
    TAST?
    NODE*                      -- flat scene-graph instance list
      PIDX   -- uint16[2]; PIDX[1] ("pair index") identifies which VSET/
                DSET pair (by 0-based order in the .mpl) this instance
                places. A value of 0xFFFF means "no mesh, transform-only
                node" — skip it.
      TFRM   -- 9x f32: Translation(x,y,z), Rotation(rx,ry,rz in DEGREES,
                extrinsic XYZ order i.e. R = Rz·Ry·Rx), Scale(sx,sy,sz)
```

Multiple `NODE`s may reference the same pair index (instancing); apply each
instance's TRS as `p' = R·(p·S) + T` in that GC-space coordinate before any
Blender/engine-specific axis remap. De-duplicate exact-identical transforms
if desired (harmless but wasteful to instance-render twice).

### 2.4 Assembling the runtime texture set

The game's own loader (`Map_LoadMTXFiles`, DOL 0x22318) builds a map's
active texture index space by **concatenating**, in numeric order,
`mapNNN_0.mtx`, `mapNNN_1.mtx`, `mapNNN_2.mtx`, ... — texture index 0 is the
first texture of `_0.mtx`, and indices continue increasing across file
boundaries. Each file's `TSET.field1` holds the running total texture count
up to and including that file; the last file in the sequence has
`field1 == 1`, used by the game as an end-of-sequence sentinel — do not rely
on filename enumeration alone to know when to stop.

The plain `mapNNN.mtx` (no `_K` suffix) is a **separate** texture set
belonging to the "MID" object-model system and is never part of the map's
own index space — do not merge it in when resolving DLST `tex_idx` values.

### 2.5 Texture pixel formats

Only two format codes are needed for the vast majority of retail assets
(other codes exist in the format but are not documented here):

- **`0x00` = RGBA8** (GX hardware format `GX_TF_RGBA8`): 4×4-pixel tiles,
  64 bytes/tile, split into a 32-byte AR block followed by a 32-byte GB
  block. Within a block, pixel order is row-major within the 4×4 tile;
  each of the 16 pixels contributes one interleaved 2-byte pair per block
  (`[A,R]` pairs in the AR block, `[G,B]` pairs in the GB block, same pixel
  order in both blocks).
- **`0x06` = CMPR** (GX hardware format `GX_TF_CMPR`, ~S3TC/DXT1): organized
  in 8×8-pixel "super-tiles", each super-tile = four 4×4 DXT1 sub-blocks in
  sub-tile order `(0,0), (4,0), (0,4), (4,4)` (top-left, top-right,
  bottom-left, bottom-right), 8 bytes/sub-block (2× RGB565 color endpoints +
  16× 2-bit index nibbles). GX's CMPR index-bit order is **MSB-first**
  within each index byte (unlike some PC DXT1 tools which are LSB-first):
  for row `r`, `idx = (byte >> (6 - col*2)) & 3`.  Standard DXT1 4-color/
  3-color-with-alpha palette rules apply based on `c0 > c1` vs `c0 <= c1`.

Tile grids are traversed left-to-right, top-to-bottom, at whatever tile
size the format uses (4×4 for RGBA8, 8×8 super-tiles for CMPR); the image
itself is stored top-row-first in the tile traversal (the reference
importer flips rows when uploading to Blender only because Blender's own
image-buffer convention is bottom-row-first — this is an output-target
detail, not part of the source format).

---

## 3. Character format (`.chm` + `.tex`)

A character consists of a `.chm` (skeleton + mesh + embedded material
table) and a matching `.tex` (texture set, format identical to §2.2/§2.5).
There are **two CHM sub-formats**; detect which one a file uses by looking
at CHM's first child chunk tag:

- First child `INFO` → **modern** format (§3.1) — the vast majority of
  files (151/156 in a full pc+npc+mon sample sweep).
- First child `SCEN` → **legacy** format (§3.2) — a small minority (5/156),
  found in a few early character IDs (e.g. `pc/c000`).

### 3.1 Modern CHM format

```
CHM
  INFO
  QUAN
  MSET                        -- material table; IDENTICAL layout to the
                                 map OTM's MSET/MATL (§2.3) — TIDX, NAME,
                                 ATRB all present and mean the same thing
  TAST?
  MSST                        -- mesh container
    MESH*                     -- see §3.1.2
  NSET                        -- flat skeleton node list, see §3.1.1
    NODE*
  DGRP?
  BANK?
```

#### 3.1.1 `NSET` — flat bone/attachment list

Every entry — real skeleton bones AND mesh-attachment placeholder nodes —
lives in the SAME flat array, indexed 0..N-1 by position in the file:

```
NODE
  INFO   -- first int32 (big-endian, signed) = pidx: parent's index into
            this same NSET array, or a negative sentinel for "no parent"
            (root). A mesh-carrying node's own separate MESH.INFO field
            (§3.1.2) also holds an "attach index" back into this array —
            that's a DIFFERENT field from this pidx.
  NAME   -- NUL-terminated ASCII bone/node name
  TFRM   -- 12x f32, row-major 3x4 LOCAL bind matrix (parent-relative):
            [ r00 r01 r02 tx ]
            [ r10 r11 r12 ty ]
            [ r20 r21 r22 tz ]
            (implicit 4th row = [0 0 0 1]). This is a raw, non-decomposed
            matrix — NOT stored/loaded as separate Euler angles anywhere
            in the game's own node-loading code (confirmed: the loader
            does a straight 48-byte memcpy into the runtime node struct).
  BINF?  -- optional, not required for basic skeleton/mesh import
```

A world-space bind matrix for node `i` is `world[i] = world[pidx] @
local[i]` (root nodes have no parent term). Note some NSET entries are pure
attachment placeholders (a node whose name is generic, e.g. `"obj"`) rather
than an actual animatable bone — these still participate in the same
parent-chain math, they simply never carry mesh-deforming weight data of
their own.

#### 3.1.2 `MSST` / `MESH` — mesh attachment

```
MESH
  INFO   -- first int32 = attach_idx: which NSET index (§3.1.1) this mesh
            is rigidly parented to for its own local space (its vertex
            data is expressed relative to that node's bind matrix)
  MNAM   -- mesh name (NUL-terminated ASCII)
  VERT   -- s16[3] fixed-point, /256.0  (NOT /16384 — see note below)
  NORM   -- s16[3] fixed-point, /4096.0 (NOT /16384)
  UV     -- s16[2] fixed-point, /4096.0 (NOT /1024)
  COLR   -- single default RGBA8 color (not per-vertex; character meshes
            do not carry vertex-baked shading the way maps do)
  SKIN   -- per-vertex bone weights, see §3.1.3
  DLHD
    DLST*  -- IDENTICAL format to the map's DLHD/DLST (§2.1.1): tex_idx
              indexes THIS CHM's own MSET/MATL table; same 8/10-byte
              vertex-stride rule for dual-texture materials
```

**On the fixed-point divisors**: character mesh data uses different
fixed-point radixes than map mesh data, verified empirically (not
documented anywhere in the executable): VERT/256 was confirmed by matching
a mesh's own bounding height against its parent bone's known rest-pose
world height; NORM/4096 was confirmed by checking that decoded normals
come out to unit length (an incorrect divisor produces a suspiciously
uniform non-1.0 magnitude across every vertex, e.g. exactly 0.25 at
/16384); UV/4096 was confirmed by comparing against known correct texture
placement (an incorrect divisor visibly over- or under-scales the UV
island from a fixed origin).

#### 3.1.3 `SKIN` — per-vertex bone weights

```
SKIN
  NODE*  -- each: a single big-endian uint32 = a GLOBAL NSET index (§3.1.1)
            this mesh references as a possible influence. Position in this
            list (0-based) is the "skin_idx" used by the weight records
            below — i.e. skin_idx is a LOCAL index into this NODE list,
            which must be resolved to a global NSET/bone index via
            bone_refs[skin_idx].
  ONE?   -- rigid single-bone-influence records
  TWO?   -- two-bone blend records
  RMIN?  -- additive remainder records (3rd+ influence)
```

These three record arrays are flat byte streams (not further chunked);
each is parsed until its declared payload length is exactly consumed —
any mismatch means the record layout below doesn't apply (rare edge case;
one sample in a full corpus sweep). All fields are big-endian `uint16`.

```
ONE  { skin_idx, vert_idx, ncount, norm_idx[ncount] }
     -- vertex vert_idx is rigidly bound to bone skin_idx at weight 1.0.
        norm_idx lists which NORM array entries (transformed by the same
        bone matrix) belong to this vertex — for smoothed/split normals.

TWO  { skinA, skinB, wA, wB, vert_idx, ncount, norm_idx[ncount] }
     -- two-bone blend: deformed_v = (matA @ v) * (wA/4096.0)
                                    + (matB @ v) * (wB/4096.0)
        wA + wB is usually ~4096 (== 1.0) but not always exact — any
        remainder is supplied by a matching RMIN record for the same vertex.

RMIN { skin_idx, w, vert_idx, ncount, norm_idx[ncount] }
     -- ADDITIVE remainder for vertices with 3+ total bone influences:
        deformed_v += (mat @ v) * (w / 4096.0)
```

`mat` in all three cases is the world-space skinning matrix for the
referenced bone: `inverse(bone_bind_world) @ current_bone_world`, i.e. the
standard "move the vertex into bone-local space at bind time, then
re-apply the bone's current world transform" skin matrix — identity at
rest pose.

Weight quantization is confirmed as `raw_u16 / 4096.0` (`0x1000` = 1.0):
`TWO` weight-pairs cluster right around summing to 4095–4096, and any
observed shortfall is exactly made up by a companion `RMIN` weight.

Not every mesh has weight data (~30% of samples in a full corpus sweep
have no `SKIN` weight sections at all — these are rigid, single-bone
attachments; use the mesh's sole `SKIN.NODE` bone reference directly, or
fall back to nearest-bone-by-distance heuristics if no weight data and
multiple candidate bone references exist).

### 3.2 Legacy CHM format

```
CHM
  SCEN
    SCAL?
    MSET             -- MATL{TIDX} ONLY — no NAME/ATRB fields present
    TAST?
    NODE*            -- flat scene-graph list (same shape idea as NSET,
                        different field layout):
      PIDX  -- int32 parent index (same semantics as modern's INFO.pidx)
      NAME  -- NUL-terminated ASCII
      TFRM  -- 9x f32: T(x,y,z), R(euler DEGREES, extrinsic XYZ), S(x,y,z)
               — i.e. the SAME 9-float TRS layout as the map OTM's TFRM,
               NOT the modern CHM's 12-float raw matrix
      MIDX? -- int32 material index (mesh nodes only)
      -- presence of a VERT chunk marks this NODE as a mesh node:
      VERT  -- f32[3] positions, RAW FLOAT (not fixed-point)
      NORM  -- f32[3] normals, RAW FLOAT
      UV    -- f32[2] texcoords, RAW FLOAT
      FACE  -- flat array of 28-byte (14x uint16) TRIANGLE RECORDS:
               struct FaceRecord {
                   uint16 header[2];   // usually (0,0), occasionally
                                       // (0,1) in every sample checked;
                                       // purpose not needed for geometry
                                       // (possibly a smoothing/flag bit)
                   struct { uint16 pos_idx, norm_idx, unused, uv_idx; }
                       corners[3];     // unused is always 0 in every
                                       // sample (legacy has no per-vertex
                                       // COLR array to index into — just
                                       // one default color for the whole
                                       // mesh)
               };
               CONFIRMED (2026, corrected from an earlier wrong "flat
               (pos,norm) pairs, no header" reading that garbled meshes
               into a spike of triangles converging on one vertex): every
               sample's pos_idx/norm_idx/uv_idx maxima exactly equal
               (their respective array's length - 1) with zero
               out-of-bounds indices, across meshes ranging from 6 to
               2146 triangles. Legacy DOES have real per-corner UV data —
               it was simply never being read from the right offset.
      SKIN? -- same structure as modern's SKIN (§3.1.3): NODE{u32 global
               index}* bone references followed by ONE/TWO/RMIN weight
               tables — with ALL RECORD FIELDS WIDENED TO uint32 (the
               modern format uses uint16). Record semantics are otherwise
               identical. Confirmed by exact byte fit on real samples:
               ONE payload size == (3*n_verts + n_norms)*4 with every
               vertex covered exactly once. In every legacy sample seen,
               TWO and RMIN are present but zero-sized (all-rigid
               skinning); their u32 field layout is inferred by analogy
               and untested. An earlier revision of this spec wrongly
               claimed legacy had no weight tables at all — that was a
               side effect of the same misparse that garbled FACE.
```

### 3.3 `.tex` — character texture set

```
TEX
  SCEN
    TSET
      TXTR*   -- byte-for-byte identical layout to the map .mtx's TXTR
                 (§2.2); reuse that parser directly.
```

---

## 4. Animation format (`.cha`)

A `.cha` file mirrors the CHM modern/legacy split (in a full sample sweep:
~2794/2806 modern, ~11/2806 legacy, plus rare degenerate/empty files).
Detect by CHA's first child tag, same rule as CHM: `SCEN` → legacy,
`ANIM` → modern.

### 4.1 Legacy CHA (pairs with legacy CHM characters)

```
CHA
  SCEN
    ANIM
      FRAM  -- 2x uint32: [unknown/unused, numFrames]
      NODE*
        NIDX  -- uint32, index into the legacy CHM's flat NODE list (§3.2)
        NAME  -- NUL-terminated ASCII (redundant with NIDX but present)
        TRAN? -- flat array of 16-byte records: (frame:uint32 [1-BASED],
                 x:f32, y:f32, z:f32)
        ROT?  -- same 16-byte record shape; x/y/z = Euler angles in DEGREES
        SCAL? -- same 16-byte record shape; x/y/z = scale factors
```

A channel with exactly one record is a static (unanimated) value HELD at
every frame — not just applied at its own record's frame. Multi-record
channels are sampled by matching `frame`; hold the most recent record at
or before the queried frame for any gaps. A channel absent entirely falls
back to the bind `TFRM`'s own component for that bone.

**Channel semantics: ALL legacy channels are ABSOLUTE local values**,
fully replacing the bind `TFRM` for that frame:
`local_animated = Translation(T) · Euler_XYZ_extrinsic(R degrees) ·
Scale(S)` — the same 9-float TRS convention and euler order as the bind
`TFRM` itself (§3.2). This differs from the modern format, where only
translation is absolute and rotation is a delta (§4.2.2).

Two verification pitfalls worth recording (both were mistaken for
delta-rotation evidence at one point):
- Static ROT records frequently express the SAME rotation as the bind
  `TFRM` in a different euler representation — e.g. `(0,180,180)` ≡
  `(-180,0,0)`, `(180,180,180)` ≡ identity, `(0,0,-140.2)` ≡
  `(-180,180,39.8)`. Compare rotations as matrices, never as raw euler
  triples, when validating against the bind pose.
- An animated bone's channel values legitimately differ from its bind
  pose (that's what animation is) — a large bind rotation paired with
  small channel values does NOT imply delta semantics. Validate with a
  full-skeleton pose reconstruction (e.g. checking feet stay near the
  ground and secondary chains like hair stay near the head across a whole
  walk cycle) rather than per-bone value comparisons.

Scale semantics are unconfirmed (every sample's SCAL is a static 1,1,1 —
same caveat as the modern format's scale channels).

### 4.2 Modern CHA (pairs with modern CHM characters)

```
CHA
  ANIM (chunk.field1 = node/track count)
    FRAM   -- single uint32 = numFrames
    INFO   -- 3x uint32 quantization shifts: (t_shift, r_shift, s_shift).
              Divisor for each channel group = 2^shift. Observed values
              for player-character (pc/c1xx) files: (11, 13, 10), i.e.
              translation /2048.0, rotation /8192.0, scale /1024.0.
              THESE SHIFTS ARE PER-FILE, NOT A GLOBAL CONSTANT — always
              read them from this chunk rather than hardcoding a divisor.
    NODE*
      NAME   -- NUL-terminated ASCII; matches an NSET bone/node name in
                the paired CHM, OR a "_chn"-suffixed old-rig pivot name
                that has NO corresponding entry in the modern CHM's NSET
                (see caveat below)
      DATA   -- exactly 9x (count:uint32, offset:uint32) pairs, in FIXED
                channel order: TX,TY,TZ, RX,RY,RZ, SX,SY,SZ. A pair with
                count==0 means that channel/axis is entirely absent for
                this node (not merely static-zero — see semantics below).
                offset is a BYTE offset into the BANK payload (§4.2.1).
    INTP?  -- occasionally present; format is (byte flag, uint32 value);
              purpose not required for correct pose reconstruction and
              not further reverse engineered.
    BANK   -- raw payload, see §4.2.1
```

#### 4.2.1 `BANK` payload and channel sample counts

`BANK`'s payload is a flat sequence of big-endian `int16` values; a
channel's `count` int16s starting at `BANK_payload_base + offset` are that
channel's samples, one sample per animation frame, densely and
sequentially indexed — **key index `i` directly equals frame number `i`,
there are no explicit per-sample frame numbers** (confirmed across a large
sample sweep: `count == numFrames + 1` in the overwhelming majority of
files, with only a handful of genuinely single-frame/static poses as
exceptions). A `count == 1` channel is a single static sample applied
identically at every frame (hold the one value; do not extrapolate zero
beyond index 0).

Convert a raw sample to its physical value as `raw / (1 << shift)`, using
the T/R/S-group-appropriate shift from `INFO` (§4.2) — translation values
come out in the SAME game-unit space as the CHM's own `TFRM` translations;
rotation values come out in radians.

#### 4.2.2 Channel semantics — CRITICAL, and easy to get backwards

- **Translation (TX/TY/TZ) is an ABSOLUTE local value that REPLACES the
  bind pose's translation on that axis**, not a delta added on top of it.
  This was verified two ways: (a) a bone's static translation sample
  varies wildly (both in sign and by an order of magnitude) across
  different animation files for the exact same bone — far too much
  variance for a small additive correction on a fixed bind offset; (b) a
  bone's raw static sample, divided by its file's translation shift,
  reproduces that bone's bind-pose `TFRM` translation on that axis
  *exactly* in the common case where the pose is meant to be at rest
  (e.g. a "stand" animation). When a channel's `count == 0` (axis absent
  entirely), fall back to the bind pose's own translation value for that
  axis — do not treat it as zero.
- **Rotation (RX/RY/RZ) is a DELTA applied on top of the bind rotation**
  (i.e. final local rotation = bind_rotation ∘ Euler(RX,RY,RZ), NOT an
  absolute replacement). Verified: bones whose bind pose already carries a
  large fixed rotation (e.g. 90° or 180°, common on arm/hand bones so a
  common rig can be reused mirrored) carry near-ZERO rotation samples in a
  neutral "stand" animation — if rotation were absolute, a neutral pose
  would need to reproduce that same 90°/180° value in every file, which it
  does not.
- Composing the per-frame local matrix for a bone with both channels
  present: `local_animated = Translation(T_abs) · bind_rotation_only ·
  Euler_XYZ(R_delta)` where `bind_rotation_only` is the bind `TFRM`'s
  rotation submatrix (translation zeroed) and `T_abs` is the resolved
  absolute translation vector (§ above). `bind_rotation_only` must be
  applied BEFORE the delta rotation (i.e. delta swings around the bone's
  OWN already-bind-rotated frame), not after — reversed order was
  empirically confirmed wrong by comparing against known-good neutral
  poses for every composition-order hypothesis tried.
- Scale channels (`SX/SY/SZ`) were observed to always be entirely absent
  (`count == 0`) in every sample encountered; treat as bind-scale
  (typically 1,1,1) if ever populated, using whatever absolute-vs-delta
  rule a fuller sample set would confirm — not resolved here since no
  sample exercises it.

#### 4.2.3 The `_chn` pivot-node caveat

The modern CHA's NODE list is systematically denser than the paired modern
CHM's NSET skeleton: it still carries old rig ("Maya") pivot helper joints
with a `_chn` name suffix (e.g. both `"hip_chn"` and `"hip"` tracks exist),
which the modern CHM's NSET has already consolidated into a single bone.
There is no parent/child or hierarchy information anywhere in the CHA file
that would let you correctly recombine a `_chn` pivot's own small
contribution back into its consolidated NSET bone — an implementation that
skips `_chn`-suffixed tracks entirely (applying only exact NSET name
matches) drops a small, usually visually negligible extra contribution
from those joints, and this is the best documented approach without
further reverse engineering.

### 4.3 Applying an animation to a skeleton, per frame

Given a skeleton's flat bind-`world[i]` matrices (per §3.1.1) and a parsed
CHA's per-bone per-frame local deltas (per §4.2.2 or §4.1), the pose for
bone `i` at frame `f` in the SAME coordinate space as the bind skeleton is:

```
posed_local[i]  = the per-frame local_animated matrix from §4.2.2 (modern)
                  or the absolute local TRS matrix directly (legacy)
posed_world[i]  = posed_world[parent(i)] @ posed_local[i]     (recursive,
                  root bones have no parent term, same as bind world[])
```

This recursive composition — parent bones evaluated strictly before their
children each frame — is required regardless of the target engine/DCC
tool's own bone-pose representation; how you then map `posed_world[i]`
into that tool's specific pose API (e.g. Blender's `PoseBone`) is an
implementation detail of the target, not part of the FFCC format itself.

**Implementation note for tools with a fixed bone rest/pivot frame** (e.g.
Blender, whose bones have their own head/tail/roll-derived rest frame that
generally does NOT match this format's raw per-node `TFRM` frame): do not
try to force the tool's rest frame to equal the FFCC `TFRM` frame, and do
not directly assign `posed_world[i]` as the tool's absolute pose transform
if the tool derives mesh deformation or bone rendering from its OWN rest
frame convention (as Blender does: `deform = posed_pose_matrix @
tool_rest_matrix⁻¹`). Instead, compute the bind-local-frame delta
`delta[i] = bind_local[i]⁻¹ @ (bind_world[parent(i)]⁻¹ @ posed_world[i])`
and then re-express that delta as a similarity transform in the tool's OWN
rest frame: `tool_pose_delta[i] = C[i] @ delta[i] @ C[i]⁻¹`, where `C[i] =
tool_rest_matrix[i]⁻¹ @ bind_world[i]`. This guarantees an identity delta
produces an exact identity pose in the tool's own convention regardless of
how that tool's rest frame differs from the raw FFCC TFRM frame — verified
as the fix that finally produced correct-looking animation in Blender
after several incorrect approaches (forcing the tool's rest frame to match
TFRM, or assigning world matrices directly) produced grossly wrong poses
(perpendicular limbs, ballooning scale, or floating skeletons).

---

## 5. Coordinate system

FFCC's native coordinate space (as stored in every `TFRM`, `VERT`, etc.) is
a conventional right-handed 3D space; empirically, +Y behaves as "up" (bone
chains run up the +Y axis, e.g. hip-to-head is a large +Y bind offset).
Nothing in this document depends on a target engine's own axis convention
— that remapping (e.g. FFCC-space `(x,y,z)` → Blender-space `(-x,z,y)`,
used by the reference Blender importer because Blender is Z-up) is purely
an output-target detail and not part of the FFCC format itself. Any such
remap of a vector is a linear change of basis; the SAME change of basis
must be applied by conjugation (`C @ M @ C⁻¹`, for a self-inverse
orthonormal `C`) to any FULL TRANSFORM MATRIX (not just point/direction
vectors) before it is combined with other matrices already expressed in
the target space, or rotations will end up applied around the wrong axis.

---

## 6. Practical unknowns / not fully resolved

- The exact per-frame runtime pose EVALUATOR code in `ffcc.dol` is
  virtual-dispatched into a REL (relocatable module) not present in the
  retail DOL image — everything in §4 was derived by cross-referencing the
  CHA/CHM loader code (which IS present in the DOL) against real sample
  data, not by reading the evaluator itself. The semantics documented here
  (§4.2.2 in particular) were validated by comparing full reconstructed
  poses against many real "stand"/neutral animation files and checking
  that limbs, translations, and rotations land close to the expected bind
  pose — but were not confirmed against the literal game source.
- Legacy CHM mesh loading code has the same REL-module blocker as the
  animation evaluator (§4). The FACE record layout (§3.2) was reconstructed
  purely from cross-sample index-range analysis (checking that a record
  layout's indices exactly cover every referenced array with zero
  out-of-bounds hits, across meshes of very different sizes), not from any
  loader code — the same technique that caught and fixed an earlier wrong
  reading of this exact chunk that had shipped undetected because it
  merely satisfied a weaker "total byte length divides evenly" check.
  The header field's 2 shorts have no confirmed purpose (present in every
  record, usually zero) — not required for correct geometry.
- `.cha`'s `INTP` chunk (§4.2) and `.chm`'s `DGRP`/`BANK` chunks (§3.1) are
  parsed-but-unused by the reference importer; their contents were not
  needed to reconstruct correct meshes/skeletons/animations and were not
  further investigated.
- Scale-channel (`SX/SY/SZ`) absolute-vs-delta semantics in modern CHA are
  unconfirmed (§4.2.2) — no sample file was found that actually animates
  scale.
