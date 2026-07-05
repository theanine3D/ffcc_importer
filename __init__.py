bl_info = {
    "name": "FFCC Importer",
    "author": "Theanine3D",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "File > Import > FFCC Map (.mpl) / FFCC Character (.chm)",
    "description": "Import Final Fantasy Crystal Chronicles map and character models",
    "category": "Import-Export",
}

import hashlib
import math
import re

import bpy
import struct
import os
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, CollectionProperty, EnumProperty
from bpy.types import Operator, OperatorFileListElement
from mathutils import Matrix, Vector


# ---- Binary parsing helpers ----

def read_hdr(data, off):
    magic = data[off:off+4]
    size  = struct.unpack_from('>I', data, off+4)[0]
    u1    = struct.unpack_from('>I', data, off+8)[0]
    u2    = struct.unpack_from('>I', data, off+12)[0]
    return magic, size, u1, u2

def is_printable(b):
    return len(b) == 4 and all(0x20 <= c < 0x7F for c in b)


# ---- Texture decoders ----


def _rgb565(c):
    r = ((c >> 11) & 0x1F) * 255 // 31
    g = ((c >> 5)  & 0x3F) * 255 // 63
    b = (c & 0x1F) * 255 // 31
    return r, g, b

def _dxt1_block(raw, off):
    c0 = struct.unpack_from('>H', raw, off)[0]
    c1 = struct.unpack_from('>H', raw, off+2)[0]
    r0,g0,b0 = _rgb565(c0)
    r1,g1,b1 = _rgb565(c1)
    if c0 > c1:
        palette = [
            (r0,g0,b0,255), (r1,g1,b1,255),
            ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3, 255),
            ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3, 255),
        ]
    else:
        palette = [
            (r0,g0,b0,255), (r1,g1,b1,255),
            ((r0+r1)//2, (g0+g1)//2, (b0+b1)//2, 255),
            (0,0,0,0),
        ]
    block = []
    for row in range(4):
        byte = raw[off+4+row]
        for col in range(4):
            idx = (byte >> (6 - col*2)) & 3
            block.append(palette[idx])
    return block

def decode_cmpr(raw, width, height):
    pixels = bytearray(width * height * 4)
    tile_w = (width  + 7) // 8
    tile_h = (height + 7) // 8
    offset = 0
    rlen   = len(raw)
    sub_offsets = [(0,0),(4,0),(0,4),(4,4)]
    for ty in range(tile_h):
        for tx in range(tile_w):
            for (bx, by) in sub_offsets:
                if offset + 8 > rlen:
                    offset += 8; continue
                block = _dxt1_block(raw, offset)
                offset += 8
                for py in range(4):
                    for px in range(4):
                        x = tx*8 + bx + px
                        y = ty*8 + by + py
                        if x < width and y < height:
                            r,g,b,a = block[py*4+px]
                            i = (y*width+x)*4
                            pixels[i]=r; pixels[i+1]=g; pixels[i+2]=b; pixels[i+3]=a
    return pixels

def decode_rgba8(raw, width, height):
    pixels = bytearray(width * height * 4)
    tile_w = (width  + 3) // 4
    tile_h = (height + 3) // 4
    rlen   = len(raw)
    src    = 0
    for ty in range(tile_h):
        for tx in range(tile_w):
            ar  = raw[src     : src + 32] if src + 32 <= rlen else raw[src:rlen]
            gb  = raw[src + 32: src + 64] if src + 64 <= rlen else raw[src+32:rlen]
            src = min(src + 64, rlen)
            for py in range(4):
                for px in range(4):
                    pi = py * 4 + px
                    x  = tx * 4 + px
                    y  = ty * 4 + py
                    if x < width and y < height:
                        k2 = pi * 2
                        a  = ar[k2]     if k2     < len(ar) else 255
                        r  = ar[k2 + 1] if k2 + 1 < len(ar) else 0
                        g  = gb[k2]     if k2     < len(gb) else 0
                        b  = gb[k2 + 1] if k2 + 1 < len(gb) else 0
                        i  = (y * width + x) * 4
                        pixels[i] = r; pixels[i+1] = g; pixels[i+2] = b; pixels[i+3] = a
    return pixels


# ---- MTX parser ----
# Returns list of bpy.types.Image in MTX order; index = texture slot number.

def parse_mtx(data, image_registry=None):
    """Parse an MTX texture set into a list of bpy Images (index = slot).

    image_registry: optional dict shared across multiple MTX loads in one
    import session, keyed by content hash so identical textures are
    deduplicated into a single bpy Image.
    """
    if data[:4] != b'TSET':
        return []
    magic, size, u1, u2 = read_hdr(data, 0)
    off = 16
    end = 16 + size
    images = []
    seen_names = {}

    while off + 16 <= end:
        m, sz, uu1, uu2 = read_hdr(data, off)
        if not is_printable(m):
            break
        if m.decode('ascii').strip() == 'TXTR':
            inner = off + 16
            inner_end = off + 16 + sz
            name = None; fmt = None; w = h = None; imag_data = None
            while inner + 16 <= inner_end:
                m2, s2, i1, i2 = read_hdr(data, inner)
                if not is_printable(m2):
                    break
                ms2 = m2.decode('ascii').strip()
                do2 = inner + 16
                if ms2 == 'NAME':
                    name = data[do2:do2+s2].rstrip(b'\x00').decode('ascii', errors='replace')
                elif ms2 == 'FMT':
                    fmt = data[do2]
                elif ms2 == 'SIZE':
                    w, h = struct.unpack_from('>II', data, do2)
                elif ms2 == 'IMAG':
                    imag_data = data[do2:do2+s2]
                adv = 16 + s2; adv = (adv + 15) & ~15
                inner += adv

            img = None
            if name and fmt is not None and w and imag_data:
                reg_key = None
                if image_registry is not None:
                    reg_key = (name, fmt, w, h, hashlib.sha1(imag_data).hexdigest())
                if name in seen_names:
                    img = seen_names[name]
                elif reg_key is not None and reg_key in image_registry:
                    img = image_registry[reg_key]
                    seen_names[name] = img
                else:
                    try:
                        if fmt == 0x00:
                            pixels = decode_rgba8(imag_data, w, h)
                        elif fmt == 0x06:
                            pixels = decode_cmpr(imag_data, w, h)
                        else:
                            pixels = None

                        if pixels is not None:
                            img = bpy.data.images.new(name, width=w, height=h, alpha=True)
                            float_px = [0.0] * (w * h * 4)
                            for y in range(h):
                                src_row = h - 1 - y
                                for x in range(w):
                                    si = (src_row * w + x) * 4
                                    di = (y * w + x) * 4
                                    float_px[di]   = pixels[si]   / 255.0
                                    float_px[di+1] = pixels[si+1] / 255.0
                                    float_px[di+2] = pixels[si+2] / 255.0
                                    float_px[di+3] = pixels[si+3] / 255.0
                            img.pixels = float_px
                            img.pack()
                            seen_names[name] = img
                            if reg_key is not None:
                                image_registry[reg_key] = img
                    except Exception:
                        img = None

            images.append(img)

        adv = 16 + sz; adv = (adv + 15) & ~15
        off += adv

    return images


# ---- VSET parser ----

def parse_vset(data, do, size):
    """Collect raw VSET attribute chunk bytes; decoding is deferred to
    finalize_vset() since NORM/UV element format can't be determined
    without the DSET's index usage (see FFCC_FORMAT_SPEC.md 2.1)."""
    raw = {'VERT': b'', 'NORM': b'', 'UV': b'', 'COLR': b''}
    offset = do
    end = do + size
    if offset + 4 <= len(data) and not is_printable(data[offset:offset+4]):
        offset += 16
    while offset + 16 <= end:
        magic, sz, u1, u2 = read_hdr(data, offset)
        if not is_printable(magic):
            break
        ms = magic.decode('ascii').strip()
        if ms in raw:
            raw[ms] = data[offset+16 : offset+16+sz]
        adv = 16 + sz; adv = (adv + 15) & ~15
        offset += adv
    return raw


def finalize_vset(raw, groups):
    """Decode raw VSET arrays, auto-detecting float-vs-fixed-point NORM/UV
    by checking which interpretation covers the indices actually
    referenced by the display lists (see FFCC_FORMAT_SPEC.md 2.1)."""
    result = {'verts': [], 'norms': [], 'uvs': [], 'colrs': []}

    vert_raw = raw['VERT']
    for i in range(0, len(vert_raw) - 11, 12):
        result['verts'].append(struct.unpack_from('>fff', vert_raw, i))

    colr_raw = raw['COLR']
    for i in range(0, len(colr_raw) - 3, 4):
        result['colrs'].append((
            colr_raw[i]   / 255.0,
            colr_raw[i+1] / 255.0,
            colr_raw[i+2] / 255.0,
            colr_raw[i+3] / 255.0,
        ))

    max_ni = max_ui = -1
    for faces, tex_idx in groups:
        for tri in faces:
            for v in tri:
                if v[1] > max_ni: max_ni = v[1]
                if v[3] > max_ui: max_ui = v[3]

    uv_raw = raw['UV']
    n_float_uv = len(uv_raw) // 8
    uv_is_float = (len(uv_raw) % 8 == 0) and (max_ui < n_float_uv)
    if uv_is_float:
        for i in range(0, len(uv_raw) - 7, 8):
            result['uvs'].append(struct.unpack_from('>ff', uv_raw, i))
    else:
        for i in range(0, len(uv_raw) - 3, 4):
            u, v = struct.unpack_from('>hh', uv_raw, i)
            result['uvs'].append((u / 1024.0, v / 1024.0))

    norm_raw = raw['NORM']
    n_float_n = len(norm_raw) // 12
    if (len(norm_raw) % 12 == 0) and (max_ni < n_float_n):
        for i in range(0, len(norm_raw) - 11, 12):
            result['norms'].append(struct.unpack_from('>fff', norm_raw, i))
    else:
        for i in range(0, len(norm_raw) - 5, 6):
            x, y, z = struct.unpack_from('>hhh', norm_raw, i)
            result['norms'].append((x / 16384.0, y / 16384.0, z / 16384.0))

    return result


# ---- GX display list parser ----

GX_QUADS          = 0x80
GX_TRIANGLES      = 0x90
GX_TRIANGLE_STRIP = 0x98
GX_TRIANGLE_FAN   = 0xA0

_GX_PRIM_OPS = (GX_QUADS, GX_TRIANGLES, GX_TRIANGLE_STRIP, GX_TRIANGLE_FAN,
                0xA8, 0xB0, 0xB8)


def gx_stream_fits(gx_data, stride):
    """Whether the GX display list parses to completion at this vertex
    stride (8 or 10 bytes, see FFCC_FORMAT_SPEC.md 1.1)."""
    pos = 0
    sz = len(gx_data)
    while pos + 3 <= sz:
        op = gx_data[pos]
        if op == 0:
            return True
        if (op & 0xF8) not in _GX_PRIM_OPS:
            return False
        count = struct.unpack_from('>H', gx_data, pos + 1)[0]
        if pos + 3 + count * stride > sz:
            return False
        pos += 3 + count * stride
    return True


def parse_gx(gx_data, stride=8):
    faces = []
    pos = 0
    sz = len(gx_data)
    while pos + 3 <= sz:
        op = gx_data[pos]
        if op == 0:
            break
        base = op & 0xF8
        if base not in _GX_PRIM_OPS:
            break
        count = struct.unpack_from('>H', gx_data, pos + 1)[0]
        vstart = pos + 3
        if vstart + count * stride > sz:
            break
        verts = []
        for i in range(count):
            pi, ni, ci, ui = struct.unpack_from('>HHHH', gx_data, vstart + i * stride)
            # Dual-texture materials (stride 10) carry a second UV/texcoord
            # index for GX_VA_TEX1 (a second texture layer, e.g. an eye or
            # mouth decal blended onto a base skin texture) — read it when
            # present, otherwise mirror the first UV index so 5-tuples are
            # always safe to index uniformly downstream.
            ui2 = struct.unpack_from('>H', gx_data, vstart + i * stride + 8)[0] if stride >= 10 else ui
            verts.append((pi, ni, ci, ui, ui2))
        if base == GX_QUADS:
            for i in range(0, len(verts) - 3, 4):
                faces.append((verts[i], verts[i+1], verts[i+2]))
                faces.append((verts[i], verts[i+2], verts[i+3]))
        elif base == GX_TRIANGLES:
            for i in range(0, len(verts) - 2, 3):
                faces.append((verts[i], verts[i+1], verts[i+2]))
        elif base == GX_TRIANGLE_STRIP:
            for i in range(len(verts) - 2):
                if i % 2 == 0:
                    faces.append((verts[i], verts[i+1], verts[i+2]))
                else:
                    faces.append((verts[i+1], verts[i], verts[i+2]))
        elif base == GX_TRIANGLE_FAN:
            for i in range(1, len(verts) - 1):
                faces.append((verts[0], verts[i], verts[i+1]))
        pos += 3 + count * stride
    return faces


# ---- DSET parser ----
# Returns list of (faces, tex_idx) — one entry per DLST.

def parse_dset(data, do, size, materials_table=None):
    groups = []
    offset = do
    end = do + size
    if offset + 4 <= len(data) and not is_printable(data[offset:offset+4]):
        offset += 16
    while offset + 16 <= end:
        magic, sz, u1, u2 = read_hdr(data, offset)
        if not is_printable(magic):
            break
        ms = magic.decode('ascii').strip()
        chunk_do = offset + 16
        if ms == 'DLHD':
            dl_off = chunk_do
            dl_end = chunk_do + sz
            while dl_off + 16 <= dl_end:
                m2, s2, uu1, uu2 = read_hdr(data, dl_off)
                if not is_printable(m2):
                    break
                if m2.decode('ascii').strip() == 'DLST':
                    gcx_size = uu1
                    sub_hdr_size = s2 - gcx_size
                    sub_hdr = data[dl_off+16 : dl_off+16+sub_hdr_size]
                    gx_do = dl_off + 16 + sub_hdr_size
                    gx_data = data[gx_do:gx_do + gcx_size]
                    tex_idx = struct.unpack_from('>H', sub_hdr, 0)[0] if sub_hdr_size >= 2 else 0

                    stride = 8
                    if materials_table and tex_idx < len(materials_table):
                        if len(materials_table[tex_idx]['tex_indices']) >= 2:
                            stride = 10
                    if not gx_stream_fits(gx_data, stride):
                        alt = 10 if stride == 8 else 8
                        if gx_stream_fits(gx_data, alt):
                            stride = alt

                    faces = parse_gx(gx_data, stride)
                    if faces:
                        groups.append((faces, tex_idx))
                adv = 16 + s2; adv = (adv + 15) & ~15
                dl_off += adv
        adv = 16 + sz; adv = (adv + 15) & ~15
        offset += adv
    return groups


# ---- MPL file parser ----
# Returns list of (vset, groups) where groups = [(faces, tex_idx), ...]

def parse_mpl(data, materials_table=None):
    mesh_sz = struct.unpack_from('>I', data, 4)[0]
    end = 16 + mesh_sz
    off = 16
    result = []
    pending_raw = None
    while off + 16 <= end:
        magic, size, u1, u2 = read_hdr(data, off)
        if not is_printable(magic):
            break
        ms = magic.decode('ascii').strip()
        do = off + 16
        if ms == 'VSET':
            pending_raw = parse_vset(data, do, size)
        elif ms == 'DSET' and pending_raw is not None:
            groups = parse_dset(data, do, size, materials_table)
            vset = finalize_vset(pending_raw, groups)
            result.append((vset, groups))
            pending_raw = None
        adv = 16 + size; adv = (adv + 15) & ~15
        off += adv
    return result


# ---- OTM scene-graph helpers ----

def _euler_to_matrix(rx_deg, ry_deg, rz_deg):
    """Extrinsic XYZ euler (degrees) -> 3x3 rotation matrix (R = Rz.Ry.Rx)."""
    rx, ry, rz = math.radians(rx_deg), math.radians(ry_deg), math.radians(rz_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return (
        (cy*cz,          cz*sx*sy - cx*sz,  cx*cz*sy + sx*sz),
        (cy*sz,          cx*cz + sx*sy*sz,  cx*sy*sz - cz*sx),
        (-sy,            cy*sx,             cx*cy),
    )


def _is_identity(T, R_mat, S, eps=1e-5):
    return (abs(T[0]) < eps and abs(T[1]) < eps and abs(T[2]) < eps
            and abs(S[0] - 1.0) < eps and abs(S[1] - 1.0) < eps and abs(S[2] - 1.0) < eps
            and abs(R_mat[0][0] - 1.0) < eps and abs(R_mat[1][1] - 1.0) < eps
            and abs(R_mat[2][2] - 1.0) < eps
            and abs(R_mat[0][1]) < eps and abs(R_mat[0][2]) < eps
            and abs(R_mat[1][0]) < eps and abs(R_mat[1][2]) < eps
            and abs(R_mat[2][0]) < eps and abs(R_mat[2][1]) < eps)


def _apply_trs(x, y, z, T, R_mat, S):
    """Scale → Rotate → Translate a point in GC space."""
    xs, ys, zs = x * S[0], y * S[1], z * S[2]
    xr = R_mat[0][0]*xs + R_mat[0][1]*ys + R_mat[0][2]*zs
    yr = R_mat[1][0]*xs + R_mat[1][1]*ys + R_mat[1][2]*zs
    zr = R_mat[2][0]*xs + R_mat[2][1]*ys + R_mat[2][2]*zs
    return xr + T[0], yr + T[1], zr + T[2]


def _chunk_advance(sz):
    return (16 + sz + 15) & ~15


def parse_otm(otm_path):
    """Parse OTM SCEN section.
    Returns {pair_idx: [(T, R_mat, S), ...]} — one entry per scene-graph instance."""
    data = open(otm_path, 'rb').read()
    _, sz0, _, _ = read_hdr(data, 0)
    off = 16; end = 16 + sz0

    scen_off = scen_sz = None
    while off + 16 <= end:
        m, sz, _, _ = read_hdr(data, off)
        if not is_printable(m):
            break
        if m.decode('ascii').strip() == 'SCEN':
            scen_off = off + 16; scen_sz = sz; break
        off += _chunk_advance(sz)

    if scen_off is None:
        return {}

    instances = {}
    off = scen_off; end = scen_off + scen_sz
    while off + 16 <= end:
        m, sz, _, _ = read_hdr(data, off)
        if not is_printable(m):
            break
        if m.decode('ascii').strip() == 'NODE':
            inn = off + 16; inn_end = off + 16 + sz
            pidx_data = tfrm_data = None
            while inn + 16 <= inn_end:
                m2, s2, _, _ = read_hdr(data, inn)
                if not is_printable(m2):
                    break
                ms2 = m2.decode('ascii').strip()
                do2 = inn + 16
                if ms2 == 'PIDX':
                    pidx_data = data[do2:do2+s2]
                elif ms2 == 'TFRM':
                    tfrm_data = data[do2:do2+s2]
                inn += _chunk_advance(s2)

            if pidx_data and len(pidx_data) >= 4 and tfrm_data and len(tfrm_data) >= 36:
                pair_idx = struct.unpack_from('>H', pidx_data, 2)[0]
                if pair_idx != 0xffff:
                    f = [struct.unpack_from('>f', tfrm_data, i*4)[0] for i in range(9)]
                    T = (f[0], f[1], f[2])
                    R_mat = _euler_to_matrix(f[3], f[4], f[5])
                    S = (f[6], f[7], f[8])
                    instances.setdefault(pair_idx, []).append((T, R_mat, S))

        off += _chunk_advance(sz)

    return instances


def parse_otm_materials(otm_path):
    """Parse the OTM SCEN -> MSET material table (see FFCC_FORMAT_SPEC.md
    2.3). Returns a list of {'name', 'tex_indices', 'blend'} dicts, or []
    if no MSET is present."""
    data = open(otm_path, 'rb').read()
    _, sz0, _, _ = read_hdr(data, 0)

    def find_chunk(start, end, target):
        off = start
        while off + 16 <= end:
            m, sz, _, _ = read_hdr(data, off)
            if not is_printable(m):
                return None
            if m.decode('ascii').strip() == target:
                return off + 16, sz
            off += _chunk_advance(sz)
        return None

    scen = find_chunk(16, 16 + sz0, 'SCEN')
    if not scen:
        return []
    mset = find_chunk(scen[0], scen[0] + scen[1], 'MSET')
    if not mset:
        return []

    materials = []
    off = mset[0]; end = mset[0] + mset[1]
    while off + 16 <= end:
        m, sz, _, _ = read_hdr(data, off)
        if not is_printable(m):
            break
        if m.decode('ascii').strip() == 'MATL':
            entry = {'name': '', 'tex_indices': [], 'blend': 4}
            inn = off + 16; inn_end = off + 16 + sz
            while inn + 16 <= inn_end:
                m2, s2, f1, _ = read_hdr(data, inn)
                if not is_printable(m2):
                    break
                ms2 = m2.decode('ascii').strip()
                do2 = inn + 16
                if ms2 == 'TIDX':
                    # f1 = number of texture indices (u32 BE each)
                    count = min(f1, s2 // 4)
                    entry['tex_indices'] = [
                        struct.unpack_from('>I', data, do2 + i * 4)[0]
                        for i in range(count)
                    ]
                elif ms2 == 'NAME':
                    entry['name'] = data[do2:do2+s2].split(b'\0')[0].decode('ascii', 'replace')
                elif ms2 == 'ATRB' and s2 >= 5:
                    entry['blend'] = data[do2 + 4]
                inn += _chunk_advance(s2)
            materials.append(entry)
        off += _chunk_advance(sz)

    return materials


def _dedup_instances(pair_insts):
    """Remove exact-duplicate transforms (e.g. two identity OTM nodes)."""
    seen = set()
    out = []
    for T, R_mat, S in pair_insts:
        key = (round(T[0],3), round(T[1],3), round(T[2],3),
               round(S[0],3), round(S[1],3), round(S[2],3),
               round(R_mat[0][0],4), round(R_mat[1][1],4), round(R_mat[2][2],4),
               round(R_mat[0][1],4), round(R_mat[0][2],4))
        if key not in seen:
            seen.add(key)
            out.append((T, R_mat, S))
    return out


# ---- Material helpers ----

_mat_cache = {}

VCOLOR_LAYER_NAME = "Col"


def _get_material(name, image, blend=None, blend_vcolors=False, fullbright=False, image2=None):
    # Merge only true duplicates (same name/image/blend/settings); Blender
    # suffixes differing same-named materials (.001 etc.) automatically.
    key = (name, image.name if image is not None else None, blend, blend_vcolors, fullbright,
           image2.name if image2 is not None else None)
    if key in _mat_cache:
        return _mat_cache[key]
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out  = nodes.new('ShaderNodeOutputMaterial')
    tex  = nodes.new('ShaderNodeTexImage')
    tex.image = image
    tex.label = "Albedo"
    uv1 = nodes.new('ShaderNodeUVMap')
    uv1.uv_map = "UVMap"
    links.new(uv1.outputs['UV'], tex.inputs['Vector'])
    uv1.location = (-1050, 150)

    color_socket = tex.outputs['Color']
    if image2 is not None:
        uv2 = nodes.new('ShaderNodeUVMap')
        uv2.uv_map = "UVMap2"
        tex2 = nodes.new('ShaderNodeTexImage')
        tex2.image = image2
        tex2.label = "Decal"
        links.new(uv2.outputs['UV'], tex2.inputs['Vector'])
        decal_mix = nodes.new('ShaderNodeMixRGB')
        decal_mix.inputs['Fac'].default_value = 1.0
        links.new(tex2.outputs['Alpha'], decal_mix.inputs['Fac'])
        links.new(color_socket, decal_mix.inputs['Color1'])
        links.new(tex2.outputs['Color'], decal_mix.inputs['Color2'])
        color_socket = decal_mix.outputs['Color']
        decal_mix.location = (-450, -150)
        uv2.location = (uv1.location.x, -350)
        tex2.location = (decal_mix.location.x - 200, tex.location.y - 450)
    if blend_vcolors:
        # Multiply the baked per-vertex shading onto the albedo.
        vcol = nodes.new('ShaderNodeVertexColor')
        vcol.layer_name = VCOLOR_LAYER_NAME
        mix = nodes.new('ShaderNodeMixRGB')
        mix.blend_type = 'MULTIPLY'
        mix.inputs['Fac'].default_value = 1.0
        links.new(tex.outputs['Color'], mix.inputs['Color1'])
        links.new(vcol.outputs['Color'], mix.inputs['Color2'])
        vcol.location = (-500, -200)
        mix.location  = (-200, 0)
        color_socket = mix.outputs['Color']

    if fullbright:
        # Bypass lighting via Emission, mixed with Transparent by alpha.
        emission = nodes.new('ShaderNodeEmission')
        links.new(color_socket, emission.inputs['Color'])
        transp = nodes.new('ShaderNodeBsdfTransparent')
        mix_shader = nodes.new('ShaderNodeMixShader')
        links.new(tex.outputs['Alpha'], mix_shader.inputs['Fac'])
        links.new(transp.outputs['BSDF'], mix_shader.inputs[1])
        links.new(emission.outputs['Emission'], mix_shader.inputs[2])
        links.new(mix_shader.outputs['Shader'], out.inputs['Surface'])
        emission.location   = (0, 100)
        transp.location      = (0, -100)
        mix_shader.location  = (300, 0)
        out.location         = (600, 0)
    else:
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        # The game has no PBR roughness data; Principled BSDF's own default
        # (0.5) reads as an unintended glossy/plastic look on every
        # texture. Flatten to fully rough — matte is a much closer match
        # to the game's actual (unlit-ish) shading, and a minority of
        # materials that do want gloss can have it dialed back manually.
        bsdf.inputs['Roughness'].default_value = 1.0
        links.new(color_socket, bsdf.inputs['Base Color'])
        links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
        links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
        bsdf.location = (0, 0)
        out.location  = (300, 0)

    # ATRB byte 4 (FFCC_FORMAT_SPEC.md 2.3): 0x04 opaque, else blend/clip.
    if blend == 4:
        mat.blend_method = 'OPAQUE'
    elif blend in (0, 1, 3):
        mat.blend_method = 'BLEND'
    else:
        mat.blend_method = 'CLIP'
    tex.location = (-800, 100) if blend_vcolors else (-300, 0)

    _mat_cache[key] = mat
    return mat


# ---- Blender mesh builder ----
# One mesh per VSET, with one material slot per unique tex_idx.

def build_blender_mesh(name, vset, groups, scale=0.01, images_by_index=None,
                       materials_table=None, blend_vcolors=False, fullbright=False,
                       debug=False):
    if images_by_index is None:
        images_by_index = []

    raw_verts = vset['verts']
    raw_uvs   = vset['uvs']
    raw_colrs = vset['colrs']
    n_verts = len(raw_verts)
    n_uvs   = len(raw_uvs)
    n_colrs = len(raw_colrs)

    if debug:
        print(f"[FFCC] {name}: n_verts={n_verts}, n_dlsts={len(groups)}, tex_idxs={sorted(set(t for _,t in groups))}")

    # Build ordered list of unique tex_idxs (preserving first-seen order)
    seen = {}
    tex_slot_order = []
    for faces, tex_idx in groups:
        if tex_idx not in seen:
            seen[tex_idx] = len(tex_slot_order)
            tex_slot_order.append(tex_idx)

    # Collect all valid faces tagged with their material slot index.
    # Filter degenerate triangles (any two vertices sharing a position index).
    all_face_data = []  # (v0, v1, v2, mat_slot)
    for faces, tex_idx in groups:
        mat_slot = seen[tex_idx]
        for tri in faces:
            v0, v1, v2 = tri
            p0, p1, p2 = v0[0], v1[0], v2[0]
            # Skip out-of-range or degenerate (zero-area) triangles
            if p0 >= n_verts or p1 >= n_verts or p2 >= n_verts:
                continue
            if p0 == p1 or p1 == p2 or p0 == p2:
                continue
            all_face_data.append((v0, v1, v2, mat_slot))

    if not all_face_data:
        if debug:
            print(f"[FFCC] {name}: SKIPPED (no valid faces)")
        return None

    mesh = bpy.data.meshes.new(name)
    obj  = bpy.data.objects.new(name, mesh)

    bl_verts   = [(-v[0]*scale, v[2]*scale, v[1]*scale) for v in raw_verts]
    poly_verts = [[fd[0][0], fd[1][0], fd[2][0]] for fd in all_face_data]

    mesh.from_pydata(bl_verts, [], poly_verts)
    mesh.update()

    # Assign material slots.
    # DLST tex_idx is an index into the OTM MSET material table; the MATL
    # record's TIDX then remaps to the actual MTX texture slot. Fall back to
    # treating tex_idx as a direct texture index when no table is available.
    has_dual_tex = False
    for tex_idx in tex_slot_order:
        if materials_table:
            if tex_idx < len(materials_table):
                matl = materials_table[tex_idx]
                real_tex = matl['tex_indices'][0] if matl['tex_indices'] else None
                image = (images_by_index[real_tex]
                         if real_tex is not None and real_tex < len(images_by_index)
                         else None)
                image2 = None
                if len(matl['tex_indices']) >= 2:
                    real_tex2 = matl['tex_indices'][1]
                    if real_tex2 < len(images_by_index):
                        image2 = images_by_index[real_tex2]
                        has_dual_tex = True
                mat_name = matl['name'] or (image.name if image else f"matl{tex_idx}")
                mat = _get_material(mat_name, image, blend=matl['blend'],
                                    blend_vcolors=blend_vcolors, fullbright=fullbright, image2=image2)
            else:
                # tex_idx beyond the MSET table: the game's material lookup
                # fails for these DLSTs (unused/leftover data), so give them
                # an untextured placeholder rather than a random texture.
                if debug:
                    print(f"[FFCC] {name}: tex_idx {tex_idx} beyond material table "
                          f"({len(materials_table)} entries) — placeholder")
                mat = _get_material(f"matl{tex_idx}_missing", None,
                                    blend_vcolors=blend_vcolors, fullbright=fullbright)
        else:
            image = images_by_index[tex_idx] if tex_idx < len(images_by_index) else None
            mat_name = image.name if image else f"unknown_tex{tex_idx}"
            mat = _get_material(mat_name, image, blend_vcolors=blend_vcolors, fullbright=fullbright)
        mesh.materials.append(mat)

    # Assign per-polygon material index
    for poly_idx, fd in enumerate(all_face_data):
        mesh.polygons[poly_idx].material_index = fd[3]

    # UV layer
    if raw_uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uv_layer2 = mesh.uv_layers.new(name="UVMap2") if has_dual_tex else None
        for poly_idx, fd in enumerate(all_face_data):
            for vi, v in enumerate(fd[:3]):
                loop_idx = mesh.polygons[poly_idx].loop_start + vi
                ui = v[3]
                if ui < n_uvs:
                    u, vc = raw_uvs[ui]
                    uv_layer.data[loop_idx].uv = (u, 1.0 - vc)
                if uv_layer2 is not None:
                    ui2 = v[4] if len(v) > 4 else ui
                    if ui2 < n_uvs:
                        u2, vc2 = raw_uvs[ui2]
                        uv_layer2.data[loop_idx].uv = (u2, 1.0 - vc2)

    # Vertex color layer — always imported (per-corner, byte color) even
    # when blend_vcolors is off, so the data is available in Blender's
    # Color Attributes for manual use regardless of the material setup.
    if raw_colrs:
        color_attr = mesh.color_attributes.new(
            name=VCOLOR_LAYER_NAME, type='BYTE_COLOR', domain='CORNER')
        for poly_idx, fd in enumerate(all_face_data):
            for vi, v in enumerate(fd[:3]):
                loop_idx = mesh.polygons[poly_idx].loop_start + vi
                ci = v[2]
                if ci < n_colrs:
                    color_attr.data[loop_idx].color = raw_colrs[ci]
                else:
                    color_attr.data[loop_idx].color = (1.0, 1.0, 1.0, 1.0)

    # The game stores real per-vertex normals (NORM); use them as custom
    # split normals rather than Blender's auto-calculated flat/smooth
    # normals. Falls back to plain shade_smooth() only for the rare mesh
    # with no NORM data at all.
    n_norms = len(vset['norms'])
    if n_norms:
        mesh.polygons.foreach_set('use_smooth', [True] * len(mesh.polygons))
        loop_normals = [(0.0, 0.0, 1.0)] * len(mesh.loops)
        for poly_idx, fd in enumerate(all_face_data):
            for vi, v in enumerate(fd[:3]):
                loop_idx = mesh.polygons[poly_idx].loop_start + vi
                ni = v[1]
                if ni < n_norms:
                    nx, ny, nz = vset['norms'][ni]
                    loop_normals[loop_idx] = (-nx, nz, ny)
        mesh.normals_split_custom_set(loop_normals)
    else:
        mesh.polygons.foreach_set('use_smooth', [True] * len(mesh.polygons))

    return obj


# ==========================================================================
# ---- CHM (character) PARSER ----
# Format documented in FFCC_FORMAT_SPEC.md section 3 (modern/legacy
# sub-formats, NSET skeleton, MESH/SKIN weight tables).
# ==========================================================================

def iter_chunks(data, start, end):
    off = start
    while off + 16 <= end:
        magic, sz, u1, u2 = read_hdr(data, off)
        if not is_printable(magic):
            break
        yield magic, sz, u1, u2, off + 16
        off += _chunk_advance(sz)

def find_chunk(data, start, end, target):
    for m, sz, u1, u2, ds in iter_chunks(data, start, end):
        if m == target:
            return sz, u1, u2, ds
    return None


def parse_tex(data):
    """Parse a .tex file (TEX -> SCEN -> TSET -> TXTR*) into a list of
    (bpy.types.Image or None, fmt) tuples, index = texture slot. Byte-for-
    byte the same TXTR sub-format as the map's .mtx files (parse_mtx)."""
    if data[:4] != b'TEX ':
        return []
    _, sz, _, _ = read_hdr(data, 0)
    scen = find_chunk(data, 16, 16+sz, b'SCEN')
    if scen is None:
        return []
    scen_sz, _, _, scen_ds = scen
    tset = find_chunk(data, scen_ds, scen_ds+scen_sz, b'TSET')
    if tset is None:
        return []
    tset_sz, _, _, tset_ds = tset

    images = []
    seen_names = {}
    for magic, sz2, u1, u2, ds2 in iter_chunks(data, tset_ds, tset_ds+tset_sz):
        if magic != b'TXTR':
            continue
        name = None; fmt = None; w = h = None; imag_data = None
        for m3, s3, i1, i2, ds3 in iter_chunks(data, ds2, ds2+sz2):
            if m3 == b'NAME':
                name = data[ds3:ds3+s3].rstrip(b'\x00').decode('ascii', errors='replace')
            elif m3 == b'FMT ':
                fmt = data[ds3]
            elif m3 == b'SIZE':
                w, h = struct.unpack_from('>II', data, ds3)
            elif m3 == b'IMAG':
                imag_data = data[ds3:ds3+s3]

        img = None
        if name and fmt is not None and w and imag_data:
            if name in seen_names:
                img = seen_names[name]
            else:
                try:
                    if fmt == 0x00:
                        pixels = decode_rgba8(imag_data, w, h)
                    elif fmt == 0x06:
                        pixels = decode_cmpr(imag_data, w, h)
                    else:
                        pixels = None
                    if pixels is not None:
                        img = bpy.data.images.new(name, width=w, height=h, alpha=True)
                        float_px = [0.0] * (w * h * 4)
                        for y in range(h):
                            src_row = h - 1 - y
                            for x in range(w):
                                si = (src_row * w + x) * 4
                                di = (y * w + x) * 4
                                float_px[di]   = pixels[si]   / 255.0
                                float_px[di+1] = pixels[si+1] / 255.0
                                float_px[di+2] = pixels[si+2] / 255.0
                                float_px[di+3] = pixels[si+3] / 255.0
                        img.pixels = float_px
                        img.pack()
                        seen_names[name] = img
                except Exception:
                    img = None
        images.append((img, fmt))
    return images


def _parse_mset_modern(data, ds, sz):
    """MSET with MATL{TIDX,NAME,ATRB} — same layout as the map OTM format."""
    materials = []
    for m3, s3, a3, b3, ds3 in iter_chunks(data, ds, ds+sz):
        if m3 != b'MATL':
            continue
        entry = {'tex_indices': [], 'name': '', 'blend': 4}
        for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
            if m4 == b'TIDX':
                count = min(a4, s4 // 4)
                entry['tex_indices'] = [struct.unpack_from('>I', data, ds4+i*4)[0]
                                        for i in range(count)]
            elif m4 == b'NAME':
                entry['name'] = data[ds4:ds4+s4].split(b'\x00')[0].decode('ascii', 'replace')
            elif m4 == b'ATRB' and s4 >= 5:
                entry['blend'] = data[ds4+4]
        materials.append(entry)
    return materials


def _parse_mset_legacy_char(data, ds, sz):
    """MSET with MATL{TIDX} only — no NAME/ATRB (legacy SCEN-format CHM)."""
    materials = []
    for m3, s3, a3, b3, ds3 in iter_chunks(data, ds, ds+sz):
        if m3 != b'MATL':
            continue
        tex_idx = None
        for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
            if m4 == b'TIDX' and s4 >= 4:
                tex_idx = struct.unpack_from('>i', data, ds4)[0]
        materials.append({'tex_indices': [tex_idx] if tex_idx is not None else [],
                          'name': '', 'blend': 4})
    return materials


def _parse_skin_weights(data, skin_sections, bone_refs):
    """Decode SKIN's ONE/TWO/RMIN weight tables (FFCC_FORMAT_SPEC.md
    3.1.3). Returns {vert_idx: [(skin_list_idx, weight), ...]} or None if
    any section fails to parse to an exact byte boundary (falls back to
    the nearest-bone heuristic)."""
    weights = {}
    n_refs = len(bone_refs)

    sec = skin_sections.get(b'ONE ')
    if sec:
        po, psz = sec
        off, end = po, po + psz
        while off + 6 <= end:
            s, v, c = struct.unpack_from('>HHH', data, off)
            if s >= n_refs:
                return None
            weights.setdefault(v, []).append((s, 1.0))
            off += 6 + 2 * c
        if off != end:
            return None

    sec = skin_sections.get(b'TWO ')
    if sec:
        po, psz = sec
        off, end = po, po + psz
        while off + 12 <= end:
            sa, sb, wa, wb, v, c = struct.unpack_from('>HHHHHH', data, off)
            if sa >= n_refs or sb >= n_refs:
                return None
            lst = weights.setdefault(v, [])
            lst.append((sa, wa / 4096.0))
            lst.append((sb, wb / 4096.0))
            off += 12 + 2 * c
        if off != end:
            return None

    sec = skin_sections.get(b'RMIN')
    if sec:
        po, psz = sec
        off, end = po, po + psz
        while off + 8 <= end:
            s, w, v, c = struct.unpack_from('>HHHH', data, off)
            if s >= n_refs:
                return None
            weights.setdefault(v, []).append((s, w / 4096.0))
            off += 8 + 2 * c
        if off != end:
            return None

    return weights if weights else None


def _parse_mesh_modern(data, ds, sz, materials):
    """MSST/MESH chunk: INFO(attach idx), MNAM, VERT/NORM/UV (s16 fixed
    point), COLR, SKIN(bone refs), DLHD->DLST (GX display lists)."""
    attach_idx = None
    mesh_name = ''
    verts = norms = uvs = []
    bone_refs = []
    groups = []  # (faces, tex_idx)
    vert_weights = None  # {vert_idx: [(skin_list_idx, weight), ...]} or None

    for m3, s3, a3, b3, ds3 in iter_chunks(data, ds, ds+sz):
        if m3 == b'INFO' and s3 >= 4:
            attach_idx = struct.unpack_from('>i', data, ds3)[0]
        elif m3 == b'MNAM':
            mesh_name = data[ds3:ds3+s3].split(b'\x00')[0].decode('ascii', 'replace')
        elif m3 == b'VERT':
            # Fixed-point divisors per FFCC_FORMAT_SPEC.md 3.1.2 — differ
            # from the map format's own VERT/NORM/UV conventions.
            verts = [tuple(c / 256.0 for c in struct.unpack_from('>hhh', data, ds3+i*6))
                     for i in range(s3 // 6)]
        elif m3 == b'NORM':
            norms = [tuple(c / 4096.0 for c in struct.unpack_from('>hhh', data, ds3+i*6))
                     for i in range(s3 // 6)]
        elif m3 == b'UV  ' or m3 == b'UV':
            uvs = [tuple(c / 4096.0 for c in struct.unpack_from('>hh', data, ds3+i*4))
                   for i in range(s3 // 4)]
        elif m3 == b'SKIN':
            skin_sections = {}
            for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
                if m4 == b'NODE' and s4 >= 4:
                    bone_refs.append(struct.unpack_from('>I', data, ds4)[0])
                elif m4 in (b'ONE ', b'TWO ', b'RMIN'):
                    skin_sections[m4] = (ds4, s4)
            vert_weights = _parse_skin_weights(data, skin_sections, bone_refs)
        elif m3 == b'DLHD':
            for m4, s4, gxsz, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
                if m4 != b'DLST':
                    continue
                sub_hdr_size = s4 - gxsz
                sub_hdr = data[ds4:ds4+sub_hdr_size]
                gx_data = data[ds4+sub_hdr_size:ds4+s4]
                tex_idx = struct.unpack_from('>H', sub_hdr, 0)[0] if sub_hdr_size >= 2 else 0

                stride = 8
                if materials and tex_idx < len(materials):
                    if len(materials[tex_idx]['tex_indices']) >= 2:
                        stride = 10
                if not gx_stream_fits(gx_data, stride):
                    alt = 10 if stride == 8 else 8
                    if gx_stream_fits(gx_data, alt):
                        stride = alt

                faces = parse_gx(gx_data, stride)
                if faces:
                    groups.append((faces, tex_idx))

    return {
        'attach_idx': attach_idx, 'name': mesh_name,
        'verts': verts, 'norms': norms, 'uvs': uvs,
        'bone_refs': bone_refs, 'groups': groups,
        'vert_weights': vert_weights,
    }


def _parse_mesh_legacy_char(data, mesh_node_chunk_ds, mesh_node_chunk_sz, midx):
    """Legacy SCEN/NODE mesh data (FFCC_FORMAT_SPEC.md 3.2). FACE is a flat
    array of 14x uint16 (28-byte) triangle records: a 2-short header
    (purpose not required for geometry; usually 0, occasionally 1) followed
    by 3 corners of (pos_idx, norm_idx, unused, uv_idx)."""
    verts = norms = uvs = []
    faces_raw = []
    bone_refs = []
    vert_weights = None

    for m3, s3, a3, b3, ds3 in iter_chunks(data, mesh_node_chunk_ds, mesh_node_chunk_ds+mesh_node_chunk_sz):
        if m3 == b'VERT':
            verts = [struct.unpack_from('>fff', data, ds3+i*12) for i in range(s3 // 12)]
        elif m3 == b'NORM':
            norms = [struct.unpack_from('>fff', data, ds3+i*12) for i in range(s3 // 12)]
        elif m3 == b'UV  ' or m3 == b'UV':
            uvs = [struct.unpack_from('>ff', data, ds3+i*8) for i in range(s3 // 8)]
        elif m3 == b'FACE':
            n_records = (s3 // 2) // 14
            vals = struct.unpack_from(f'>{n_records * 14}H', data, ds3)
            for r in range(n_records):
                base = r * 14 + 2  # skip the 2-short record header
                c0 = vals[base:base+4]
                c1 = vals[base+4:base+8]
                c2 = vals[base+8:base+12]
                faces_raw.append((c0, c1, c2))
        elif m3 == b'SKIN':
            skin_sections = {}
            for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
                if m4 == b'NODE' and s4 >= 4:
                    bone_refs.append(struct.unpack_from('>I', data, ds4)[0])
                elif m4 in (b'ONE ', b'TWO ', b'RMIN') and s4 > 0:
                    skin_sections[m4] = (ds4, s4)
            vert_weights = _parse_skin_weights_legacy(data, skin_sections, bone_refs)

    # Normalize to the same (faces, tex_idx) shape as the modern format:
    # v = (pos_idx, norm_idx, color_idx, uv_idx). Legacy has no per-vertex
    # COLR array (just a single default color), so color_idx is always 0.
    faces = [((c0[0], c0[1], 0, c0[3]), (c1[0], c1[1], 0, c1[3]), (c2[0], c2[1], 0, c2[3]))
             for c0, c1, c2 in faces_raw]
    groups = [(faces, midx)] if faces else []

    return {'verts': verts, 'norms': norms, 'uvs': uvs, 'bone_refs': bone_refs,
           'groups': groups, 'vert_weights': vert_weights}


def _parse_skin_weights_legacy(data, skin_sections, bone_refs):
    """Legacy SKIN weight tables: same ONE/TWO/RMIN record semantics as the
    modern format (_parse_skin_weights) but with uint32 fields instead of
    uint16 (FFCC_FORMAT_SPEC.md 3.2 — confirmed by exact byte fit:
    ONE size == (3*n_verts + n_norms)*4 with every vertex covered once).
    Weight quantization for TWO/RMIN is assumed u32/4096 by analogy; every
    known legacy sample has empty TWO/RMIN sections, so only ONE (rigid,
    weight 1.0) is exercised in practice."""
    weights = {}
    n_refs = len(bone_refs)

    sec = skin_sections.get(b'ONE ')
    if sec:
        po, psz = sec
        off, end = po, po + psz
        while off + 12 <= end:
            s, v, c = struct.unpack_from('>III', data, off)
            if s >= n_refs:
                return None
            weights.setdefault(v, []).append((s, 1.0))
            off += 12 + 4 * c
        if off != end:
            return None

    sec = skin_sections.get(b'TWO ')
    if sec:
        po, psz = sec
        off, end = po, po + psz
        while off + 24 <= end:
            sa, sb, wa, wb, v, c = struct.unpack_from('>IIIIII', data, off)
            if sa >= n_refs or sb >= n_refs:
                return None
            lst = weights.setdefault(v, [])
            lst.append((sa, wa / 4096.0))
            lst.append((sb, wb / 4096.0))
            off += 24 + 4 * c
        if off != end:
            return None

    sec = skin_sections.get(b'RMIN')
    if sec:
        po, psz = sec
        off, end = po, po + psz
        while off + 16 <= end:
            s, w, v, c = struct.unpack_from('>IIII', data, off)
            if s >= n_refs:
                return None
            weights.setdefault(v, []).append((s, w / 4096.0))
            off += 16 + 4 * c
        if off != end:
            return None

    return weights if weights else None


def euler_deg_to_matrix4(rx_deg, ry_deg, rz_deg):
    """Same convention as _euler_to_matrix, as a 4x4 mathutils.Matrix."""
    rx, ry, rz = math.radians(rx_deg), math.radians(ry_deg), math.radians(rz_deg)
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return Matrix((
        (cy*cz,          cz*sx*sy - cx*sz,  cx*cz*sy + sx*sz, 0.0),
        (cy*sz,          cx*cz + sx*sy*sz,  cx*sy*sz - cz*sx, 0.0),
        (-sy,            cy*sx,             cx*cy,            0.0),
        (0.0,            0.0,               0.0,              1.0),
    ))

def _gc_to_blender_vec(v):
    """FFCC (Y-up) -> Blender (Z-up) axis remap: (-x, z, y)."""
    return Vector((-v[0], v[2], v[1]))


# Rotation-only change-of-basis matching _gc_to_blender_vec (det=+1,
# self-inverse). Full GC-space transform matrices must be conjugated by
# this (see FFCC_FORMAT_SPEC.md section 5), not just have their
# translation remapped, or rotations end up around the wrong local axis.
GC_TO_BL_MATRIX = Matrix((
    (-1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
))

def _gc_to_blender_matrix(m):
    """Conjugate a GC-space affine matrix into Blender's axis convention."""
    return GC_TO_BL_MATRIX @ m @ GC_TO_BL_MATRIX


# Characters otherwise import facing -Y. Baked permanently into the data
# (not an armature object rotation) as a 180-degree yaw applied AFTER
# _gc_to_blender_vec/matrix — a global left-multiply, not a conjugation,
# so it cancels out of parent-relative math (bind_local, deltas) and only
# shifts each root's absolute orientation.
FACING_FIX_MATRIX = Matrix((
    (-1.0, 0.0, 0.0, 0.0),
    (0.0, -1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
))

def _facing_fix_vec(v):
    return Vector((-v[0], -v[1], v[2]))


def parse_chm(data):
    """Parse a .chm file into (materials, nodes, mesh_format).

    materials: list of {'tex_indices': [...], 'name': str, 'blend': int}
    nodes: list of dicts, index = flat node id:
        {'name', 'pidx', 'local_matrix' (mathutils.Matrix, GC-space),
         'mesh': None or {'verts','norms','uvs','bone_refs','groups'} }
    mesh_format: 'modern' or 'legacy'
    """
    if data[:4] != b'CHM ':
        return [], [], None
    _, sz, _, _ = read_hdr(data, 0)

    first = find_chunk(data, 16, 16+sz, b'SCEN')
    if first is not None:
        return _parse_chm_legacy(data, first)
    return _parse_chm_modern(data, sz)


def _parse_chm_modern(data, sz):
    materials = []
    node_by_attach = {}

    mset = find_chunk(data, 16, 16+sz, b'MSET')
    if mset:
        mset_sz, _, _, mset_ds = mset
        materials = _parse_mset_modern(data, mset_ds, mset_sz)

    nset = find_chunk(data, 16, 16+sz, b'NSET')
    bones = []
    if nset:
        nset_sz, _, _, nset_ds = nset
        for m3, s3, a3, b3, ds3 in iter_chunks(data, nset_ds, nset_ds+nset_sz):
            if m3 != b'NODE':
                continue
            pidx = -1; name = ''; mat3x4 = None
            for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
                if m4 == b'INFO' and s4 >= 4:
                    pidx = struct.unpack_from('>i', data, ds4)[0]
                elif m4 == b'NAME':
                    name = data[ds4:ds4+s4].split(b'\x00')[0].decode('ascii', 'replace')
                elif m4 == b'TFRM' and s4 >= 48:
                    mat3x4 = struct.unpack_from('>12f', data, ds4)
            if mat3x4 is None:
                mat3x4 = (1,0,0,0, 0,1,0,0, 0,0,1,0)
            r = mat3x4
            local = Matrix((
                (r[0], r[1], r[2], r[3]),
                (r[4], r[5], r[6], r[7]),
                (r[8], r[9], r[10], r[11]),
                (0.0, 0.0, 0.0, 1.0),
            ))
            bones.append({'name': name, 'pidx': pidx, 'local_matrix': local, 'mesh': None})

    msst = find_chunk(data, 16, 16+sz, b'MSST')
    if msst:
        msst_sz, _, _, msst_ds = msst
        for m3, s3, a3, b3, ds3 in iter_chunks(data, msst_ds, msst_ds+msst_sz):
            if m3 != b'MESH':
                continue
            mesh = _parse_mesh_modern(data, ds3, s3, materials)
            attach = mesh['attach_idx']
            if attach is not None and 0 <= attach < len(bones):
                bones[attach]['mesh'] = mesh

    return materials, bones, 'modern'


def _parse_chm_legacy(data, scen_info):
    scen_sz, _, _, scen_ds = scen_info
    materials = []
    nodes = []

    mset = find_chunk(data, scen_ds, scen_ds+scen_sz, b'MSET')
    if mset:
        mset_sz, _, _, mset_ds = mset
        materials = _parse_mset_legacy_char(data, mset_ds, mset_sz)

    for magic, sz2, u1, u2, ds2 in iter_chunks(data, scen_ds, scen_ds+scen_sz):
        if magic != b'NODE':
            continue
        pidx = -1; name = ''; T = (0.0,0.0,0.0); R = (0.0,0.0,0.0); S = (1.0,1.0,1.0)
        midx = None
        is_mesh = False
        for m3, s3, a3, b3, ds3 in iter_chunks(data, ds2, ds2+sz2):
            if m3 == b'PIDX' and s3 >= 4:
                pidx = struct.unpack_from('>i', data, ds3)[0]
            elif m3 == b'NAME':
                name = data[ds3:ds3+s3].split(b'\x00')[0].decode('ascii', 'replace')
            elif m3 == b'TFRM' and s3 >= 36:
                f = struct.unpack_from('>9f', data, ds3)
                T, R, S = f[0:3], f[3:6], f[6:9]
            elif m3 == b'MIDX' and s3 >= 4:
                midx = struct.unpack_from('>i', data, ds3)[0]
            elif m3 == b'VERT':
                is_mesh = True

        Rm = euler_deg_to_matrix4(*R)
        Tm = Matrix.Translation(Vector(T))
        Sm = Matrix.Diagonal(Vector((S[0], S[1], S[2], 1.0)))
        local = Tm @ Rm @ Sm

        node = {'name': name, 'pidx': pidx, 'local_matrix': local, 'mesh': None}
        if is_mesh:
            node['mesh'] = _parse_mesh_legacy_char(data, ds2, sz2, midx if midx is not None else 0)
        nodes.append(node)

    return materials, nodes, 'legacy'


# Character-to-Blender-meters conversion, measured empirically (game units
# use a different implicit scale than map units). The operator's own
# "Scale" property defaults to 1.0 as a multiplier on top of this.
CHAR_BASE_SCALE = 0.01 * 10.8


def compute_char_world_matrices(nodes, scale):
    scale_m = Matrix.Diagonal(Vector((scale, scale, scale, 1.0)))
    world = [None] * len(nodes)
    def get_world(i):
        if world[i] is not None:
            return world[i]
        node = nodes[i]
        lm = node['local_matrix']
        if node['pidx'] < 0 or node['pidx'] >= len(nodes):
            wm = scale_m @ lm
        else:
            wm = get_world(node['pidx']) @ lm
        world[i] = wm
        return wm
    for i in range(len(nodes)):
        get_world(i)
    return world


def _get_character_material(name, image, blend, fmt, image2=None):
    """image2: a second texture layer sampled from its own UV map ("UVMap2")
    and alpha-blended over the base texture — used for dual-TIDX materials
    (e.g. an eye or mouth decal composited onto a base skin texture; see
    FFCC_FORMAT_SPEC.md 1.1's dual-texture vertex stride note). Without
    this, those materials rendered only the base texture and the decal
    region (eyes/mouth) was invisible."""
    key = (name, image.name if image is not None else None, blend, fmt,
           image2.name if image2 is not None else None)
    if key in _mat_cache:
        return _mat_cache[key]
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    out  = nodes.new('ShaderNodeOutputMaterial')
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    # See _get_material's identical note: flatten the default 0.5
    # roughness to fully matte, since the game has no PBR roughness data
    # and 0.5 reads as an unintended glossy/plastic look.
    bsdf.inputs['Roughness'].default_value = 1.0
    tex  = nodes.new('ShaderNodeTexImage')
    tex.image = image
    tex.label = "Albedo"
    uv1 = nodes.new('ShaderNodeUVMap')
    uv1.uv_map = "UVMap"
    links.new(uv1.outputs['UV'], tex.inputs['Vector'])
    uv1.location = (-960, 150)

    color_socket = tex.outputs['Color']
    alpha_socket = tex.outputs['Alpha']
    if image2 is not None:
        uv2 = nodes.new('ShaderNodeUVMap')
        uv2.uv_map = "UVMap2"
        tex2 = nodes.new('ShaderNodeTexImage')
        tex2.image = image2
        tex2.label = "Decal"
        links.new(uv2.outputs['UV'], tex2.inputs['Vector'])
        mix = nodes.new('ShaderNodeMixRGB')
        mix.inputs['Fac'].default_value = 1.0
        links.new(tex2.outputs['Alpha'], mix.inputs['Fac'])
        links.new(color_socket, mix.inputs['Color1'])
        links.new(tex2.outputs['Color'], mix.inputs['Color2'])
        color_socket = mix.outputs['Color']
        uv2.location = (-960, -250)
        tex2.location = (-760, -250)
        mix.location = (-450, 100)
    links.new(color_socket, bsdf.inputs['Base Color'])
    links.new(alpha_socket, bsdf.inputs['Alpha'])
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    if blend is not None:
        if blend == 4:
            mat.blend_method = 'OPAQUE'
        elif blend in (0, 1, 3):
            mat.blend_method = 'BLEND'
        else:
            mat.blend_method = 'CLIP'
    else:
        # Legacy format has no ATRB — fall back to the texture's own FFCC
        # format code (0x00 commonly carries real alpha, 0x06/CMPR doesn't).
        mat.blend_method = 'CLIP' if fmt == 0x00 else 'OPAQUE'
    out.location  = (50, 0)
    bsdf.location = (-250, 0)
    tex.location  = (-760, 100)
    _mat_cache[key] = mat
    return mat


def _point_segment_dist_sq(p, a, b):
    """Squared distance from point p to the segment a-b (clamped)."""
    ab = b - a
    ab_len_sq = ab.length_squared
    if ab_len_sq < 1e-12:
        return (p - a).length_squared
    t = (p - a).dot(ab) / ab_len_sq
    t = max(0.0, min(1.0, t))
    closest = a + ab * t
    return (p - closest).length_squared


def build_character_mesh_object(basename, node_idx, node, world_mat, images,
                                materials, bone_names, bone_world_pos, bone_tail_pos):
    mesh_data = node['mesh']
    raw_verts = mesh_data['verts']
    raw_uvs = mesh_data['uvs']
    groups = mesh_data['groups']
    n_verts = len(raw_verts)
    n_uvs = len(raw_uvs)
    if not groups or not raw_verts:
        return None

    seen = {}
    tex_slot_order = []
    for faces, tex_idx in groups:
        if tex_idx not in seen:
            seen[tex_idx] = len(tex_slot_order)
            tex_slot_order.append(tex_idx)

    all_face_data = []  # (v0, v1, v2, mat_slot) where v = (pos,norm,color,uv)
    for faces, tex_idx in groups:
        mat_slot = seen[tex_idx]
        for tri in faces:
            v0, v1, v2 = tri
            p0, p1, p2 = v0[0], v1[0], v2[0]
            if p0 >= n_verts or p1 >= n_verts or p2 >= n_verts:
                continue
            if p0 == p1 or p1 == p2 or p0 == p2:
                continue
            all_face_data.append((v0, v1, v2, mat_slot))
    if not all_face_data:
        return None

    name = f"{basename}_{node['name'] or node_idx}"
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)

    bl_verts = [_facing_fix_vec(_gc_to_blender_vec(world_mat @ Vector(v))) for v in raw_verts]
    poly_verts = [[fd[0][0], fd[1][0], fd[2][0]] for fd in all_face_data]
    mesh.from_pydata(bl_verts, [], poly_verts)
    mesh.update()

    has_dual_tex = False
    for tex_idx in tex_slot_order:
        matl = materials[tex_idx] if 0 <= tex_idx < len(materials) else None
        image = fmt = image2 = None
        if matl and matl['tex_indices']:
            real_tex = matl['tex_indices'][0]
            if 0 <= real_tex < len(images):
                image, fmt = images[real_tex]
            if len(matl['tex_indices']) >= 2:
                real_tex2 = matl['tex_indices'][1]
                if 0 <= real_tex2 < len(images):
                    image2, _ = images[real_tex2]
                    has_dual_tex = True
        mat_name = (matl['name'] if matl and matl['name'] else
                   (image.name if image else f"matl{tex_idx}"))
        mesh.materials.append(_get_character_material(mat_name, image,
                                                       matl['blend'] if matl else None, fmt, image2))

    for poly_idx, fd in enumerate(all_face_data):
        mesh.polygons[poly_idx].material_index = fd[3]

    if raw_uvs:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uv_layer2 = mesh.uv_layers.new(name="UVMap2") if has_dual_tex else None
        for poly_idx, fd in enumerate(all_face_data):
            for vi, v in enumerate(fd[:3]):
                loop_idx = mesh.polygons[poly_idx].loop_start + vi
                ui = v[3]
                if ui < n_uvs:
                    u, vc = raw_uvs[ui]
                    uv_layer.data[loop_idx].uv = (u, 1.0 - vc)
                if uv_layer2 is not None:
                    ui2 = v[4] if len(v) > 4 else ui
                    if ui2 < n_uvs:
                        u2, vc2 = raw_uvs[ui2]
                        uv_layer2.data[loop_idx].uv = (u2, 1.0 - vc2)

    bone_refs = mesh_data['bone_refs']
    vert_weights = mesh_data.get('vert_weights')
    if vert_weights:
        # Real per-vertex weights from the SKIN chunk's ONE/TWO/RMIN tables
        # (decoded from the game's own skinning kernel — see
        # _parse_skin_weights). skin_list_idx -> bone_refs -> global node.
        vgroups = {}
        for vi, influences in vert_weights.items():
            if vi >= n_verts:
                continue
            for skin_idx, w in influences:
                if skin_idx >= len(bone_refs) or w <= 0.0:
                    continue
                b = bone_refs[skin_idx]
                if b not in bone_names:
                    continue
                if b not in vgroups:
                    vgroups[b] = obj.vertex_groups.new(name=bone_names[b])
                vgroups[b].add([vi], w, 'ADD')
    else:
        # Fallback (legacy format, or the rare CHM whose weight tables fail
        # to parse): nearest bone by distance to the head-to-tail SEGMENT.
        candidate_bones = [b for b in bone_refs if b in bone_names]
        if candidate_bones:
            vgroups = {b: obj.vertex_groups.new(name=bone_names[b]) for b in candidate_bones}
            cand_seg = [(b, bone_world_pos[b], bone_tail_pos[b]) for b in candidate_bones]
            for vi, v in enumerate(bl_verts):
                best_b, best_d = None, None
                for b, head, tail in cand_seg:
                    d = _point_segment_dist_sq(v, head, tail)
                    if best_d is None or d < best_d:
                        best_d, best_b = d, b
                vgroups[best_b].add([vi], 1.0, 'REPLACE')

    # The game stores real per-vertex normals (NORM); use them as custom
    # split normals instead of Blender's auto-calculated ones. Direction
    # only (world_mat's rotation, no translation) then the same GC->Blender
    # axis remap used for vertex positions.
    raw_norms = mesh_data['norms']
    n_norms = len(raw_norms)
    world_rot = world_mat.to_3x3()
    if n_norms:
        mesh.polygons.foreach_set('use_smooth', [True] * len(mesh.polygons))
        loop_normals = [(0.0, 0.0, 1.0)] * len(mesh.loops)
        for poly_idx, fd in enumerate(all_face_data):
            for vi, v in enumerate(fd[:3]):
                loop_idx = mesh.polygons[poly_idx].loop_start + vi
                ni = v[1]
                if ni < n_norms:
                    n = (world_rot @ Vector(raw_norms[ni])).normalized()
                    loop_normals[loop_idx] = _facing_fix_vec(_gc_to_blender_vec(n))
        mesh.normals_split_custom_set(loop_normals)
    else:
        mesh.polygons.foreach_set('use_smooth', [True] * len(mesh.polygons))

    return obj


# ---- Operator ----

class ImportFFCCMap(Operator, ImportHelper):
    bl_idname = "import_scene.ffcc_mpl"
    bl_label = "Import FFCC Map (.mpl)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".mpl"
    filter_glob: StringProperty(default="*.mpl", options={'HIDDEN'})

    # Multi-file selection support
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    scale: bpy.props.FloatProperty(
        name="Scale",
        description="Scale factor (game units to Blender meters)",
        default=0.1,
        min=0.0001,
        max=100.0,
    )

    load_textures: BoolProperty(
        name="Load Textures",
        description="Load matching .mtx texture file if present",
        default=True,
    )

    apply_otm: BoolProperty(
        name="Apply Scene Transforms (OTM)",
        description="Load the .otm scene graph and place instanced pairs at correct world positions",
        default=True,
    )

    blend_vcolors: BoolProperty(
        name="Blend Vertex Colors",
        description="Multiply the baked per-vertex shading onto the albedo in each material. "
                    "Vertex colors are always imported as a Color Attribute regardless of this setting",
        default=True,
    )

    fullbright: BoolProperty(
        name="Fullbright",
        description="Use an Emission shader instead of Principled BSDF for every material, "
                    "so textures render unaffected by scene lighting",
        default=False,
    )

    mtx_source: EnumProperty(
        name="Texture Source",
        description="Which MTX file to use for texture lookup",
        items=[
            ('AUTO',   'Auto (game-accurate)', 'Concatenate all mapNNN_K.mtx in order, like the game does — recommended'),
            ('EXACT',  'Per-file only',        'Only use the exact per-file MTX (e.g. map002_0.mtx) — debugging'),
            ('SHARED', 'Stage-shared only',    'Only use the plain mapNNN.mtx (MID object set) — debugging'),
        ],
        default='AUTO',
    )

    perfile_mtx: StringProperty(
        name="Per-file MTX",
        description="Override path for the per-file MTX (leave blank to auto-detect from mpl directory)",
        default="",
        subtype='FILE_PATH',
    )

    shared_mtx: StringProperty(
        name="Stage-shared MTX",
        description="Override path for the stage-shared MTX (leave blank to auto-detect from mpl directory)",
        default="",
        subtype='FILE_PATH',
    )

    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description="Set the scene's Color Management view transform to 'Standard' (sRGB) instead "
                    "of Filmic/AgX, so imported colors match how they look in-game",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "scale")
        layout.prop(self, "load_textures")
        if self.load_textures:
            layout.prop(self, "mtx_source")
            layout.label(text="Override MTX paths (optional):")
            layout.prop(self, "perfile_mtx")
            layout.prop(self, "shared_mtx")
        layout.prop(self, "apply_otm")
        layout.prop(self, "blend_vcolors")
        layout.prop(self, "fullbright")
        layout.prop(self, "disable_color_correction")

    def execute(self, context):
        global _mat_cache
        _mat_cache = {}

        # Collect all selected files (multi-select in the file browser);
        # fall back to the single filepath.
        paths = []
        if self.files and self.directory:
            for f in self.files:
                if f.name:
                    paths.append(os.path.join(self.directory, f.name))
        if not paths:
            paths = [self.filepath]

        # Caches shared across the whole (possibly multi-file) import:
        # - per map prefix (folder, mapNNN): texture set + material table,
        #   so map000_0.mpl and map000_1.mpl reuse the same bpy Images.
        # - image registry: content-hash dedup of identical textures across
        #   different map prefixes.
        self._prefix_cache = {}
        self._image_registry = {}

        imported = 0
        errors = []
        for path in paths:
            result = self._import_one(context, path)
            if result:
                imported += 1
            else:
                errors.append(os.path.basename(path))

        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = 'Standard'
            except (AttributeError, TypeError) as e:
                print(f"[FFCC] Could not set view_transform to Standard: {e}")

        # Eevee shadow rendering noticeably lags the viewport on scenes this
        # size; turn it off after every import regardless of the currently
        # active render engine (the eevee settings block exists on the
        # scene independent of whether Eevee or Cycles is selected).
        try:
            context.scene.eevee.use_shadows = False
        except (AttributeError, TypeError) as e:
            print(f"[FFCC] Could not disable Eevee shadows: {e}")

        if imported == 0:
            self.report({'ERROR'}, f"No files imported ({', '.join(errors)})")
            return {'CANCELLED'}
        if errors:
            self.report({'WARNING'}, f"Imported {imported}/{len(paths)} files; failed: {', '.join(errors)}")
        else:
            self.report({'INFO'}, f"Imported {imported} file(s)")
        return {'FINISHED'}

    def _import_one(self, context, path):
        try:
            data = open(path, 'rb').read()
        except OSError as e:
            print(f"[FFCC] Cannot open {path}: {e}")
            return False

        if data[:4] != b'MESH':
            print(f"[FFCC] {path}: not a valid MPL file (expected MESH magic)")
            return False

        basename = os.path.splitext(os.path.basename(path))[0]
        folder = os.path.dirname(path)
        map_m0 = re.match(r'^(map\d+)', basename, re.IGNORECASE)
        prefix_key = (folder, map_m0.group(1).lower() if map_m0 else basename)

        if prefix_key in self._prefix_cache:
            images_by_index, cached_mats = self._prefix_cache[prefix_key]
        else:
            images_by_index = []
            if self.load_textures:
                images_by_index = self._load_textures(path)
            cached_mats = None
            self._prefix_cache[prefix_key] = (images_by_index, cached_mats)

        # Locate the map's OTM (needed for scene transforms, the MSET
        # material table that DLST tex_idx values index into, and the
        # per-DLST vertex stride: dual-texture materials add a TEX1 index).
        otm_path = None
        map_m = re.match(r'^(map\d+)', basename, re.IGNORECASE)
        if map_m:
            candidate = os.path.join(os.path.dirname(path),
                                     map_m.group(1) + '.otm')
            if os.path.exists(candidate):
                otm_path = candidate
            else:
                print(f"[FFCC] No OTM found at {candidate}")

        # Material table: tex_idx -> MATL {name, tex_indices, blend}
        # (cached per map prefix so all mapNNN_K.mpl files share it)
        if cached_mats is not None:
            materials_table = cached_mats
        else:
            materials_table = []
            if otm_path:
                try:
                    materials_table = parse_otm_materials(otm_path)
                    print(f"[FFCC] OTM material table: {len(materials_table)} MATL entries")
                except Exception as e:
                    print(f"[FFCC] OTM material table load failed: {e}")
            if not materials_table:
                print("[FFCC] No material table — falling back to direct texture indexing")
            self._prefix_cache[prefix_key] = (images_by_index, materials_table)

        mesh_data = parse_mpl(data, materials_table)
        if not mesh_data:
            print(f"[FFCC] {basename}: no mesh data found")
            return False

        collection = bpy.data.collections.new(basename)
        context.scene.collection.children.link(collection)

        # Load OTM scene transforms if available
        otm_instances = {}
        if self.apply_otm and otm_path:
            try:
                otm_instances = parse_otm(otm_path)
                total_inst = sum(len(v) for v in otm_instances.values())
                print(f"[FFCC] OTM loaded: {len(otm_instances)} pairs, "
                      f"{total_inst} total instances")
            except Exception as e:
                print(f"[FFCC] OTM load failed: {e}")

        _identity_R = _euler_to_matrix(0, 0, 0)
        _identity_inst = ((0.0, 0.0, 0.0), _identity_R, (1.0, 1.0, 1.0))

        print(f"[FFCC] Starting import: {basename}, {len(mesh_data)} VSET/DSET pairs")
        created = 0
        for pair_idx, (vset, groups) in enumerate(mesh_data):
            # Get OTM instances for this pair; fall back to a single identity instance
            if otm_instances and pair_idx in otm_instances:
                pair_insts = _dedup_instances(otm_instances[pair_idx])
            else:
                pair_insts = [_identity_inst]

            for inst_idx, (T, R_mat, S) in enumerate(pair_insts):
                # Transform vertex positions into world space (GC coordinate space)
                if _is_identity(T, R_mat, S):
                    inst_vset = vset
                else:
                    world_verts = [_apply_trs(x, y, z, T, R_mat, S)
                                   for x, y, z in vset['verts']]
                    inst_vset = dict(vset)
                    inst_vset['verts'] = world_verts

                suffix = f"_{inst_idx:02d}" if len(pair_insts) > 1 else ""
                obj_name = f"{basename}_{pair_idx:02d}{suffix}"
                obj = build_blender_mesh(obj_name, inst_vset, groups,
                                         scale=self.scale,
                                         images_by_index=images_by_index,
                                         materials_table=materials_table,
                                         blend_vcolors=self.blend_vcolors,
                                         fullbright=self.fullbright,
                                         debug=(inst_idx == 0))
                if obj is not None:
                    collection.objects.link(obj)
                    created += 1

        print(f"[FFCC] Done: {created} meshes created from {basename}")
        return created > 0

    def _load_textures(self, mpl_path):
        """Return merged images_by_index list.

        Texture index space: two strategies auto-selected in AUTO mode.
          Strategy A (stage-shared present): stage-shared as base, per-file overrides low indices.
          Strategy B (no stage-shared, e.g. stg000): concatenate all mapNNN_K.mtx files in order.
        User may override paths explicitly via the importer UI.
        """
        base   = os.path.splitext(mpl_path)[0]
        folder = os.path.dirname(mpl_path)

        def resolve_mtx_path(raw, fallback):
            p = raw.strip()
            if not p:
                return fallback
            if not os.path.isabs(p):
                p = os.path.join(folder, p)
            return os.path.normpath(p)

        # Per-file MTX: use explicit override if set, else auto-detect
        exact = resolve_mtx_path(self.perfile_mtx, base + '.mtx')

        # Stage-shared MTX: use explicit override if set, else auto-detect
        if self.shared_mtx.strip():
            stage_shared = resolve_mtx_path(self.shared_mtx, None)
        else:
            stage_shared = None
            try:
                for fname in sorted(os.listdir(folder)):
                    if re.match(r'^map\d+\.mtx$', fname, re.IGNORECASE):
                        stage_shared = os.path.join(folder, fname)
                        break
            except OSError:
                pass

        print(f"[FFCC] MTX source={self.mtx_source}")
        print(f"[FFCC]   exact={exact!r} exists={os.path.exists(exact)}")
        print(f"[FFCC]   stage_shared={stage_shared!r} exists={os.path.exists(stage_shared) if stage_shared else 'N/A'}")

        images = []

        if self.mtx_source == 'EXACT':
            # Only use the per-file MTX, no fallback
            if os.path.exists(exact):
                try:
                    images = parse_mtx(open(exact, 'rb').read(), self._image_registry)
                    self.report({'INFO'}, f"Loaded {sum(1 for x in images if x)} textures from {os.path.basename(exact)}")
                except Exception as e:
                    self.report({'WARNING'}, f"Could not load {os.path.basename(exact)}: {e}")
            return images

        if self.mtx_source == 'SHARED':
            # Only use the stage-shared MTX
            if stage_shared and os.path.exists(stage_shared):
                try:
                    images = parse_mtx(open(stage_shared, 'rb').read(), self._image_registry)
                    self.report({'INFO'}, f"Loaded {sum(1 for x in images if x)} textures from {os.path.basename(stage_shared)}")
                except Exception as e:
                    self.report({'WARNING'}, f"Could not load stage-shared MTX: {e}")
            return images

        # AUTO: replicate the game's Map_LoadMTXFiles (DOL 0x22318) exactly.
        # The runtime texture set for map geometry is the concatenation of
        # mapNNN_0.mtx, mapNNN_1.mtx, ... in order (each file's TSET f1
        # holds the total count, the last file's TSET f1 == 1). The plain
        # mapNNN.mtx (no _K suffix) belongs to the MID object models, NOT
        # the map's index space — never merge it in.
        map_m = re.match(r'^(map\d+)', os.path.basename(mpl_path), re.IGNORECASE)
        if map_m:
            map_prefix = map_m.group(1).lower()
            component_files = []
            try:
                for fname in sorted(os.listdir(folder)):
                    if re.match(rf'^{re.escape(map_prefix)}_\d+\.mtx$', fname, re.IGNORECASE):
                        component_files.append(os.path.join(folder, fname))
            except OSError:
                pass

            print(f"[FFCC] AUTO concat: {[os.path.basename(p) for p in component_files]}")
            for mtx_path in component_files:
                try:
                    block = parse_mtx(open(mtx_path, 'rb').read(), self._image_registry)
                    images.extend(block)
                    print(f"[FFCC]   +{os.path.basename(mtx_path)}: {len(block)} entries (total {len(images)})")
                except Exception as e:
                    self.report({'WARNING'}, f"Could not load {os.path.basename(mtx_path)}: {e}")

        if images:
            self.report({'INFO'}, f"Loaded {sum(1 for x in images if x)} textures ({len(images)} slots)")
        else:
            self.report({'WARNING'}, "No MTX files found — no textures loaded")

        return images


# ==========================================================================
# ---- CHA (animation) PARSER ----
# Format documented in FFCC_FORMAT_SPEC.md section 4 (legacy/modern
# sub-formats, INFO quantization shifts, absolute-translation/delta-
# rotation channel semantics, and the _chn pivot-node caveat).
# ==========================================================================

def parse_cha(data):
    """Parse a .cha file into a dict:
        {'format': 'legacy'|'modern', 'num_frames': int,
         'tracks': {bone_name: {'T': [(frame,x,y,z)...] or None,
                                'R': [...] or None, 'S': [...] or None}}}
    Frame numbers are as stored (legacy: explicit, 1-based; modern: dense,
    0-based key index == frame index). Values are physical units (radians/
    game units), already divided by their per-file quantization shift.
    """
    if data[:4] != b'CHA ':
        return None
    _, sz, _, _ = read_hdr(data, 0)

    scen = find_chunk(data, 16, 16+sz, b'SCEN')
    if scen is not None:
        return _parse_cha_legacy(data, scen)

    anim = find_chunk(data, 16, 16+sz, b'ANIM')
    if anim is not None:
        return _parse_cha_modern(data, anim)
    return None


def _parse_cha_legacy(data, scen_info):
    scen_sz, _, _, scen_ds = scen_info
    anim = find_chunk(data, scen_ds, scen_ds+scen_sz, b'ANIM')
    if anim is None:
        return None
    anim_sz, _, _, anim_ds = anim

    num_frames = 0
    tracks = {}
    for m3, s3, a3, b3, ds3 in iter_chunks(data, anim_ds, anim_ds+anim_sz):
        if m3 == b'FRAM' and s3 >= 8:
            num_frames = struct.unpack_from('>I', data, ds3+4)[0]
        elif m3 == b'NODE':
            name = None
            track = {'T': None, 'R': None, 'S': None}
            for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
                if m4 == b'NAME':
                    name = data[ds4:ds4+s4].split(b'\x00')[0].decode('ascii', 'replace')
                elif m4 in (b'TRAN', b'ROT ', b'SCAL'):
                    key = {b'TRAN': 'T', b'ROT ': 'R', b'SCAL': 'S'}[m4]
                    n = s4 // 16
                    track[key] = [struct.unpack_from('>Ifff', data, ds4+i*16) for i in range(n)]
            if name:
                tracks[name] = track

    return {'format': 'legacy', 'num_frames': num_frames, 'tracks': tracks}


def _parse_cha_modern(data, anim_info):
    anim_sz, _, _, anim_ds = anim_info
    num_frames = 0
    tracks = {}
    bank_payload = None

    # BANK can appear anywhere among ANIM's children but is always last in
    # practice; resolve it before using any DATA offsets (two-pass below).
    # Per-channel-group quantization shifts (FFCC_FORMAT_SPEC.md 4.2);
    # defaults match observed pc/c1xx files in case INFO is ever absent.
    t_shift, r_shift, s_shift = 11, 13, 10
    for m3, s3, a3, b3, ds3 in iter_chunks(data, anim_ds, anim_ds+anim_sz):
        if m3 == b'BANK':
            bank_payload = ds3
        elif m3 == b'INFO' and s3 >= 12:
            t_shift, r_shift, s_shift = struct.unpack_from('>III', data, ds3)

    group_div = [float(1 << t_shift)] * 3 + [float(1 << r_shift)] * 3 + [float(1 << s_shift)] * 3

    for m3, s3, a3, b3, ds3 in iter_chunks(data, anim_ds, anim_ds+anim_sz):
        if m3 == b'FRAM' and s3 >= 4:
            num_frames = struct.unpack_from('>I', data, ds3)[0]
        elif m3 == b'NODE':
            name = None
            data_ds = data_sz = None
            for m4, s4, a4, b4, ds4 in iter_chunks(data, ds3, ds3+s3):
                if m4 == b'NAME':
                    name = data[ds4:ds4+s4].split(b'\x00')[0].decode('ascii', 'replace')
                elif m4 == b'DATA':
                    data_ds, data_sz = ds4, s4
            if not name or data_ds is None or bank_payload is None:
                continue

            n_pairs = data_sz // 8
            pairs = [struct.unpack_from('>II', data, data_ds+i*8) for i in range(n_pairs)]
            channels = []
            for ch_idx, (count, offset) in enumerate(pairs):
                if count == 0:
                    channels.append(None)
                    continue
                s16 = struct.unpack_from(f'>{count}h', data, bank_payload+offset)
                div = group_div[ch_idx] if ch_idx < 9 else 16384.0
                channels.append([v / div for v in s16])

            def track_from(idx0, idx1, idx2):
                cx, cy, cz = (channels[idx0] if idx0 < len(channels) else None,
                              channels[idx1] if idx1 < len(channels) else None,
                              channels[idx2] if idx2 < len(channels) else None)
                if cx is None and cy is None and cz is None:
                    return None
                n = max(len(c) for c in (cx, cy, cz) if c is not None)
                def at(c, i):
                    # None (channel absent for this axis) is a distinct
                    # signal from "animated to 0.0" — callers must fall back
                    # to the bind pose's own value for that axis, not zero.
                    if c is None:
                        return None
                    return c[i] if i < len(c) else c[-1]
                return [(i, at(cx, i), at(cy, i), at(cz, i)) for i in range(n)]

            tracks[name] = {
                'T': track_from(0, 1, 2),
                'R': track_from(3, 4, 5),
                'S': track_from(6, 7, 8),
            }

    return {'format': 'modern', 'num_frames': num_frames, 'tracks': tracks}


def _bone_depth(nodes, i):
    """Hierarchy depth (root=0), walking pidx upward. Used to process bones
    parent-before-child each frame — required for the armature-space pose
    approach below, since a child's PoseBone.matrix must be set after its
    parent's for the same frame."""
    depth = 0
    seen = set()
    while nodes[i]['pidx'] >= 0 and i not in seen:
        seen.add(i)
        i = nodes[i]['pidx']
        depth += 1
    return depth


def apply_cha_animation(arm_obj, bone_names_by_node_idx, nodes, world_bl, cha, action_name):
    """Create a Blender Action from a parsed .cha and keyframe the
    armature's pose bones (see FFCC_FORMAT_SPEC.md section 4.3 for the
    pose-composition math this implements).

    world_bl: {node_idx: Matrix} rest pose in Blender's axis convention and
    scale (armature space), for every bone — the SAME matrices used to build
    the edit bones (see _import_one). Needed here to compute each bone's
    parent-relative rest ("bind_local") for hierarchical pose composition.

    Returns the created Action, or None if nothing could be applied.
    """
    if cha is None or not cha['tracks']:
        return None

    bone_indices = list(bone_names_by_node_idx.keys())
    by_name = {}
    for i in bone_indices:
        by_name[nodes[i]['name'] or bone_names_by_node_idx[i]] = i

    bind_local = {}
    for i in bone_indices:
        p = nodes[i]['pidx']
        bind_local[i] = (world_bl[p].inverted() @ world_bl[i]) if p in world_bl else world_bl[i]

    # Per-bone, per-frame LOCAL delta matrix, bone-local relative to this
    # bone's own rest orientation (channel semantics: FFCC_FORMAT_SPEC.md 4.2).
    per_bone_deltas = {}
    for track_name, track in cha['tracks'].items():
        if cha['format'] == 'modern' and track_name.endswith('_chn'):
            continue  # _chn pivot nodes: see FFCC_FORMAT_SPEC.md 4.2.3
        if track_name not in by_name:
            continue
        node_idx = by_name[track_name]
        deltas = {}

        if cha['format'] == 'modern':
            rest_matrix_gc = nodes[node_idx]['local_matrix']
            bind_trans_gc = rest_matrix_gc.translation
            rest_rot_only_gc = rest_matrix_gc.to_3x3().to_4x4()
            t_data = track.get('T')
            r_data = track.get('R')
            def sample_group(data, frame):
                if not data:
                    return (None, None, None)
                return data[frame][1:4] if frame < len(data) else data[-1][1:4]
            n_frames = max(len(t_data) if t_data else 0, len(r_data) if r_data else 0)
            for frame in range(n_frames):
                T = sample_group(t_data, frame)
                R = sample_group(r_data, frame)
                T_abs_gc = Vector((bind_trans_gc[k] if T[k] is None else T[k] for k in range(3)))
                R_deg = tuple(0.0 if v is None else math.degrees(v) for v in R)
                local_animated_gc = (Matrix.Translation(T_abs_gc)
                                     @ rest_rot_only_gc
                                     @ euler_deg_to_matrix4(*R_deg))
                full_animated_bl = _gc_to_blender_matrix(local_animated_gc)
                # bind_local[] carries CHAR_BASE_SCALE in its translation
                # (see world_bl construction below); match that convention.
                full_animated_bl.translation = full_animated_bl.translation * CHAR_BASE_SCALE
                deltas[frame + 1] = bind_local[node_idx].inverted() @ full_animated_bl
        else:
            # Legacy channels are ABSOLUTE local T/R(euler deg)/S, fully
            # replacing the bind TFRM (FFCC_FORMAT_SPEC.md 4.1) — rotation
            # included, using the same extrinsic-XYZ convention as the
            # TFRM itself. Static (single-record) channels hold their value
            # at every frame; a channel missing entirely falls back to the
            # bind TFRM's own component.
            rest_matrix = nodes[node_idx]['local_matrix']
            rest_rot_only = rest_matrix.to_3x3().to_4x4()
            rest_trans = rest_matrix.translation
            frames = set()
            for chan in ('T', 'R', 'S'):
                if track[chan]:
                    frames.update(r[0] for r in track[chan])
            def sample(chan, frame):
                # Hold the most recent record at or before this frame
                # (records are sorted, usually dense from frame 1).
                data = track[chan]
                if not data:
                    return None
                prev = None
                for r in data:
                    if r[0] <= frame:
                        prev = r
                    else:
                        break
                return (prev or data[0])[1:4]
            for frame in frames:
                T = sample('T', frame)
                R = sample('R', frame)
                S = sample('S', frame) or (1.0, 1.0, 1.0)
                t_m = Matrix.Translation(Vector(T)) if T else Matrix.Translation(rest_trans)
                r_m = euler_deg_to_matrix4(*R) if R else rest_rot_only
                animated = t_m @ r_m @ Matrix.Diagonal(Vector((S[0], S[1], S[2], 1.0)))
                full_animated_bl = _gc_to_blender_matrix(animated)
                # Same unit fix as the modern branch: bind_local carries
                # CHAR_BASE_SCALE in its translation, so the animated local
                # matrix (raw game units) must be scaled to match.
                full_animated_bl.translation = full_animated_bl.translation * CHAR_BASE_SCALE
                deltas[frame] = bind_local[node_idx].inverted() @ full_animated_bl

        if deltas:
            per_bone_deltas[node_idx] = deltas

    if not per_bone_deltas:
        return None

    all_frames = sorted(set().union(*(d.keys() for d in per_bone_deltas.values())))
    order = sorted(bone_indices, key=lambda i: _bone_depth(nodes, i))

    if not arm_obj.animation_data:
        arm_obj.animation_data_create()
    action = bpy.data.actions.new(action_name)
    arm_obj.animation_data.action = action

    # Keyframe via matrix_basis computed analytically (never read/assign
    # PoseBone.matrix in a loop — reads return stale depsgraph state).
    # Blender deforms meshes/draws bones using its OWN head/tail-derived
    # rest frame (bone.matrix_local, ML), not the raw TFRM frame, so the
    # per-bone delta must be re-expressed as a similarity transform in
    # Blender's rest frame: basis(i) = C_i @ delta(i) @ C_i^-1, where
    # C_i = ML(i)^-1 @ world_bind(i). This is the general solution derived
    # in FFCC_FORMAT_SPEC.md section 4.3's "fixed bone rest frame" note —
    # it guarantees an identity delta produces an exact identity pose
    # regardless of how Blender's rest frame differs from the TFRM frame.
    conj = {}
    for i in bone_indices:
        ml = arm_obj.data.bones[bone_names_by_node_idx[i]].matrix_local
        conj[i] = ml.inverted() @ world_bl[i]

    identity = Matrix()
    # Hold state: a bone with no delta stored for this frame keeps its most
    # recent one (bones whose channels are all static only produce a delta
    # at their single record's frame, but that pose applies to EVERY frame
    # — falling back to identity/rest instead was wrong whenever a static
    # value differs from the bind pose, e.g. a held hip lean).
    held = {i: identity for i in bone_indices}
    applied = 0
    for frame in all_frames:
        # Bake the WHOLE skeleton at this frame (not just animated bones) so
        # every bone gets an explicit keyframe — avoids interpolation gaps
        # and keeps every child's parent chain fully defined per frame.
        for i in order:
            d = per_bone_deltas.get(i, {}).get(frame)
            if d is not None:
                held[i] = d
            pbone = arm_obj.pose.bones[bone_names_by_node_idx[i]]
            pbone.matrix_basis = conj[i] @ held[i] @ conj[i].inverted()
            pbone.keyframe_insert(data_path="location", frame=frame)
            pbone.keyframe_insert(data_path="rotation_euler", frame=frame)
            applied += 1

    if applied == 0:
        bpy.data.actions.remove(action)
        return None
    return action


# ---- Character Operator ----

class ImportFFCCCharacter(Operator, ImportHelper):
    bl_idname = "import_scene.ffcc_chm"
    bl_label = "Import FFCC Character (.chm)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".chm"
    filter_glob: StringProperty(default="*.chm", options={'HIDDEN'})

    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    scale: bpy.props.FloatProperty(
        name="Scale",
        description="Scale multiplier on top of the built-in character-to-Blender-meters "
                    "conversion (CHAR_BASE_SCALE) — leave at 1.0 for correct real-world size",
        default=1.0,
        min=0.0001,
        max=100.0,
    )

    load_textures: BoolProperty(
        name="Load Textures",
        description="Load the matching .tex texture set if present",
        default=True,
    )

    import_animations: BoolProperty(
        name="Import Animations",
        description="Import every matching .cha animation file in the same folder as a "
                    "separate Action on the armature. See FFCC_FORMAT_SPEC.md section 4 "
                    "for format notes and known limitations",
        default=True,
    )

    disable_color_correction: BoolProperty(
        name="Disable Color Correction",
        description="Set the scene's Color Management view transform to 'Standard' (sRGB) instead "
                    "of Filmic/AgX, so imported colors match how they look in-game",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "scale")
        layout.prop(self, "load_textures")
        layout.prop(self, "import_animations")
        layout.prop(self, "disable_color_correction")

    def execute(self, context):
        global _mat_cache
        _mat_cache = {}

        paths = []
        if self.files and self.directory:
            for f in self.files:
                if f.name:
                    paths.append(os.path.join(self.directory, f.name))
        if not paths:
            paths = [self.filepath]

        imported = 0
        errors = []
        for path in paths:
            if self._import_one(context, path):
                imported += 1
            else:
                errors.append(os.path.basename(path))

        if self.disable_color_correction:
            try:
                context.scene.view_settings.view_transform = 'Standard'
            except (AttributeError, TypeError) as e:
                print(f"[FFCC-CHAR] Could not set view_transform to Standard: {e}")

        if imported == 0:
            self.report({'ERROR'}, f"No files imported ({', '.join(errors)})")
            return {'CANCELLED'}
        if errors:
            self.report({'WARNING'}, f"Imported {imported}/{len(paths)} files; failed: {', '.join(errors)}")
        else:
            self.report({'INFO'}, f"Imported {imported} file(s)")
        return {'FINISHED'}

    def _import_one(self, context, path):
        try:
            data = open(path, 'rb').read()
        except OSError as e:
            print(f"[FFCC-CHAR] Cannot open {path}: {e}")
            return False
        if data[:4] != b'CHM ':
            print(f"[FFCC-CHAR] {path}: not a valid CHM file (expected CHM magic)")
            return False

        basename = os.path.splitext(os.path.basename(path))[0]
        folder = os.path.dirname(path)

        materials, nodes, mesh_format = parse_chm(data)
        if not nodes:
            print(f"[FFCC-CHAR] {basename}: no nodes found")
            return False
        n_mesh = sum(1 for n in nodes if n['mesh'] is not None)
        print(f"[FFCC-CHAR] {basename}: format={mesh_format} nodes={len(nodes)} "
              f"materials={len(materials)} mesh_attachments={n_mesh}")

        images = []
        if self.load_textures:
            tex_path = os.path.join(folder, basename + '.tex')
            if os.path.exists(tex_path):
                try:
                    images = parse_tex(open(tex_path, 'rb').read())
                    print(f"[FFCC-CHAR] Loaded {len(images)} textures from {os.path.basename(tex_path)}")
                except Exception as e:
                    print(f"[FFCC-CHAR] Texture load failed: {e}")
            else:
                print(f"[FFCC-CHAR] No matching texture set at {tex_path}")

        world = compute_char_world_matrices(nodes, self.scale * CHAR_BASE_SCALE)

        collection = bpy.data.collections.new(basename)
        context.scene.collection.children.link(collection)

        arm_data = bpy.data.armatures.new(f"{basename}_Armature")
        arm_obj = bpy.data.objects.new(f"{basename}_Armature", arm_data)
        arm_obj.show_in_front = True
        arm_data.display_type = 'STICK'
        collection.objects.link(arm_obj)

        # world_bl: rest matrices in Blender's axis convention, rotation
        # taken from an UNSCALED matrix chain and translation from the
        # CHAR_BASE_SCALE-scaled chain — world[]'s rotation submatrix is
        # non-orthonormal (scale baked in via ordinary multiplication), and
        # EditBone.matrix derives bone length from basis-vector magnitude,
        # so feeding it a scaled rotation directly would balloon bone length.
        world_unscaled = compute_char_world_matrices(nodes, 1.0)
        world_bl = {}
        for i in range(len(nodes)):
            rot_bl = _gc_to_blender_matrix(world_unscaled[i])
            rot_bl.translation = _gc_to_blender_matrix(world[i]).translation
            world_bl[i] = FACING_FIX_MATRIX @ rot_bl

        bpy.context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode='EDIT')
        edit_bones = arm_data.edit_bones
        bone_names = {}
        for i, node in enumerate(nodes):
            if node['mesh'] is not None:
                continue
            bname = node['name'] or f"bone{i}"
            base, n = bname, 1
            while bname in edit_bones:
                bname = f"{base}.{n:03d}"; n += 1
            eb = edit_bones.new(bname)
            head = world_bl[i].translation
            eb.head = head
            eb.tail = head + Vector((0.0, 0.01, 0.0))  # placeholder, fixed up below
            bone_names[i] = bname

        children = {}
        for i, node in enumerate(nodes):
            if node['mesh'] is None and node['pidx'] in bone_names:
                children.setdefault(node['pidx'], []).append(i)
        bone_world_pos = {}
        bone_tail_pos = {}
        for i, node in enumerate(nodes):
            if node['mesh'] is not None:
                continue
            eb = edit_bones[bone_names[i]]
            if node['pidx'] in bone_names:
                eb.parent = edit_bones[bone_names[node['pidx']]]

            # Bone shape (head/tail/roll) is cosmetic w.r.t. the animation
            # math (apply_cha_animation re-derives its own rest-frame
            # conjugation from bone.matrix_local, whatever that ends up
            # being) — so point head->tail at the actual child for correct
            # display/IK, rather than using the TFRM rotation's own Y axis
            # (which is not guaranteed to be the point-to-child direction).
            head = world_bl[i].translation
            eb.head = head
            kids = children.get(i, [])
            if kids:
                tail = world_bl[kids[0]].translation
                if (tail - head).length < 1e-6:
                    tail = head + world_bl[i].to_3x3().col[1].normalized() * 0.05
            else:
                tail = head + world_bl[i].to_3x3().col[1].normalized() * 0.05
            eb.tail = tail
            # Preserve the TFRM's own twist via its Z axis as a roll reference.
            up_ref = world_bl[i].to_3x3().col[2]
            if up_ref.length > 1e-6:
                eb.align_roll(up_ref)

            bone_world_pos[i] = eb.head.copy()
            bone_tail_pos[i] = eb.tail.copy()

        # use_connect is intentionally never set: a connected bone's
        # Location channel is ignored by Blender, but this format's
        # translation channels carry genuine per-pose data (not a fixed
        # rest length) with no rigid/non-rigid flag to distinguish bones
        # that would be safe to connect.

        bpy.ops.object.mode_set(mode='OBJECT')

        created = 0
        for i, node in enumerate(nodes):
            if node['mesh'] is None:
                continue
            obj = build_character_mesh_object(basename, i, node, world[i], images,
                                              materials, bone_names, bone_world_pos, bone_tail_pos)
            if obj is not None:
                collection.objects.link(obj)
                obj.parent = arm_obj
                mod = obj.modifiers.new("Armature", 'ARMATURE')
                mod.object = arm_obj
                created += 1

        print(f"[FFCC-CHAR] {basename}: created armature ({len(bone_names)} bones) "
              f"+ {created} mesh object(s)")

        if self.import_animations:
            # matrix_basis decomposes into location/rotation/scale according
            # to rotation_mode; force XYZ euler so apply_cha_animation's
            # rotation_euler keyframes actually take effect (pose bones
            # default to quaternion rotation).
            for pbone in arm_obj.pose.bones:
                pbone.rotation_mode = 'XYZ'

            cha_paths = sorted(
                os.path.join(folder, f) for f in os.listdir(folder)
                if f.lower().endswith('.cha')
            )
            n_anims = 0
            for cha_path in cha_paths:
                try:
                    cha = parse_cha(open(cha_path, 'rb').read())
                    action_name = os.path.splitext(os.path.basename(cha_path))[0]
                    action = apply_cha_animation(arm_obj, bone_names, nodes, world_bl, cha, action_name)
                    if action is not None:
                        n_anims += 1
                        action.use_fake_user = True
                except Exception as e:
                    print(f"[FFCC-CHAR] {os.path.basename(cha_path)}: animation import failed: {e}")
            if arm_obj.animation_data is not None:
                arm_obj.animation_data.action = None
            # Detaching the action does NOT reset the pose — each pose bone
            # still holds whatever location/rotation was last written into
            # it by the final apply_cha_animation() call (baking every
            # animation leaves the armature sitting at that action's last
            # keyframed frame). Explicitly clear every bone back to rest.
            for pbone in arm_obj.pose.bones:
                pbone.location = (0.0, 0.0, 0.0)
                pbone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
                pbone.rotation_euler = (0.0, 0.0, 0.0)
                pbone.scale = (1.0, 1.0, 1.0)
            print(f"[FFCC-CHAR] {basename}: imported {n_anims}/{len(cha_paths)} animation(s) as Actions")

        return True


# ---- Registration ----

def menu_func_import(self, context):
    self.layout.operator(ImportFFCCMap.bl_idname, text="FFCC Map (.mpl)")
    self.layout.operator(ImportFFCCCharacter.bl_idname, text="FFCC Character (.chm)")


def register():
    bpy.utils.register_class(ImportFFCCMap)
    bpy.utils.register_class(ImportFFCCCharacter)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.utils.unregister_class(ImportFFCCCharacter)
    bpy.utils.unregister_class(ImportFFCCMap)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)


if __name__ == "__main__":
    register()
