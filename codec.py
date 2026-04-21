# -*- coding: utf-8 -*-
"""
gn_toolkit.codec — Serialization / deserialization of Python values to/from
JSON-safe representations.

Contains ``clean_value`` (Python → JSON-safe) and ``unclean_value``
(JSON-safe → Python/Blender value).
"""

from __future__ import annotations

import bpy

# Attempt to import mathutils types at module level (with fallback guard).
try:
    from mathutils import Vector, Color, Euler
    _HAS_MATHUTILS = True
except ImportError:
    _HAS_MATHUTILS = False


def clean_value(val):
    """Convert a Python/Blender value into a JSON-safe representation."""
    if val is None:
        return None
    val_type = type(val).__name__.lower()
    if "array" in val_type or "collection" in val_type or val_type in ("idpropertyarray", "idpropertygroup"):
        if hasattr(val, "items") and callable(getattr(val, "items")):
            return {str(k): clean_value(v) for k, v in val.items()}
        return [clean_value(v) for v in val]

    if isinstance(val, float):
        return round(val, 6)
    if isinstance(val, (int, str, bool)):
        return val

    if _HAS_MATHUTILS:
        if isinstance(val, Vector):
            return [round(v, 6) for v in val]
        if isinstance(val, Color):
            return [round(val.r, 6), round(val.g, 6), round(val.b, 6)]
        if isinstance(val, Euler):
            return [round(v, 6) for v in val]

    if isinstance(val, dict):
        return {str(k): clean_value(v) for k, v in val.items()}

    if hasattr(val, '__iter__') and not isinstance(val, str):
        return [clean_value(v) for v in val]

    if isinstance(val, bpy.types.ID):
        return {
            "type": val.__class__.__name__,
            "name": val.name,
            "library": val.library.name if val.library else None,
        }

    return str(val)


# ---------------------------------------------------------------------------
# Helpers for unclean_value
# ---------------------------------------------------------------------------

def _is_vector_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a vector-like socket (2D or 3D)."""
    et = expected_type.upper()
    return ("VECTOR" in et or "EULER" in et or "ROTATION" in et
            or et in ("NODESOCKETVECTOR", "NODESOCKETROTATION",
                      "NODESOCKETVECTORTRANSLATION", "NODESOCKETVECTORDIRECTION",
                      "NODESOCKETVECTORACCELERATION", "NODESOCKETVELOCITY",
                      "NODESOCKETVECTOR2D", "NODESOCKETVECTORTRANSLATION2D"))


def _is_2d_vector_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a 2D vector socket (needs 2 components)."""
    et = expected_type.upper()
    return "2D" in et or et in ("NODESOCKETVECTOR2D", "NODESOCKETVECTORTRANSLATION2D")


def _is_color_type(expected_type: str) -> bool:
    """Return True if expected_type denotes an RGBA color socket."""
    et = expected_type.upper()
    return ("RGBA" in et or "COLOR" in et
            or et in ("NODESOCKETCOLOR",))


def _is_float_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a float/value socket."""
    et = expected_type.upper()
    return ("FLOAT" in et or "VALUE" in et
            or et in ("NODESOCKETFLOAT", "NODESOCKETFLOATANGLE",
                      "NODESOCKETFLOATDISTANCE", "NODESOCKETFLOATFACTOR",
                      "NODESOCKETFLOATPERCENTAGE", "NODESOCKETUNSIGNED"))


def _is_int_type(expected_type: str) -> bool:
    """Return True if expected_type denotes an integer socket."""
    et = expected_type.upper()
    return ("INT" in et or et in ("NODESOCKETINT", "NODESOCKETINTUNSIGNED"))


def _is_bool_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a boolean socket."""
    et = expected_type.upper()
    return "BOOL" in et or et == "NODESOCKETBOOL"


def _is_string_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a string socket."""
    et = expected_type.upper()
    return "STRING" in et or et == "NODESOCKETSTRING"


def _is_geometry_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a geometry socket."""
    et = expected_type.upper()
    return "GEOMETRY" in et or et == "NODESOCKETGEOMETRY"


def _is_object_type(expected_type: str) -> bool:
    """Return True if expected_type denotes an object socket."""
    et = expected_type.upper()
    return "OBJECT" in et or et == "NODESOCKETOBJECT"


def _is_non_scalar_type(expected_type: str) -> bool:
    """Return True if expected_type denotes a socket that has no meaningful
    default_value (geometry, object, material, collection, texture, image,
    matrix, closure, etc.).  Attempting to assign a scalar default to these
    will always fail.
    """
    et = expected_type.upper()
    return any(kw in et for kw in (
        "GEOMETRY", "OBJECT", "MATERIAL", "COLLECTION", "TEXTURE", "IMAGE",
        "MATRIX", "CLOSURE", "MENU",
    ))


def unclean_value(val, expected_type=None, context=None):
    """Convert a JSON-safe value back into a Python/Blender value.

    When *expected_type* indicates a vector or color socket but *val* is a
    scalar (or None), this function promotes the value to the correct
    sequence type.  This handles the common case where Blender expects a
    ``[x, y, z]`` vector but the JSON stored ``0.0`` or ``null`` because
    that was the default for a disconnected input.

    Additionally, when the target socket type is int/bool/float/string but
    the value is of a different scalar type, this function coerces it
    (e.g. ``0.5`` → ``0`` for an INT socket, ``1.0`` → ``True`` for a
    BOOL socket).

    When *context* is provided (e.g. ``"Node 'Foo' socket 'Bar'"``), a
    ``[DEFAULT_VALUE]`` warning is printed to the console whenever a
    value fallback or potentially lossy conversion is used.  Safe
    coercions (e.g. 0→0.0, 1→True) are logged at ``[COERCE]`` level
    to reduce noise while still being traceable.
    """
    def _warn(msg):
        """Print a [DEFAULT_VALUE] warning with optional context."""
        prefix = "[DEFAULT_VALUE]"
        if context:
            print(f"{prefix} {context}: {msg}")
        else:
            print(f"{prefix} {msg}")

    def _coerce(msg):
        """Print a [COERCE] info message with optional context.

        Used for safe type coercions where no data is lost
        (e.g. 0→0.0, 1→True, float 1.0→int 1).

        NOTE: _coerce messages for zero-equivalent values (0, 0.0,
        False, [0,0,0], etc.) are suppressed because they are
        functionally harmless — the Blender default for that socket
        type is also zero, so the coercion has no effect on the
        final result.  Only non-trivial coercions are logged.
        """
        prefix = "[COERCE]"
        if context:
            print(f"{prefix} {context}: {msg}")
        else:
            print(f"{prefix} {msg}")

    def _is_trivial_coerce(val, result):
        """Return True if the coercion is trivial (zero→zero-equivalent).

        When the original value and the coerced result are both
        zero-equivalent, the coercion has no practical effect and
        should not be logged to reduce noise.
        """
        def _is_zero(v):
            if v is None:
                return True
            if isinstance(v, bool):
                return not v
            if isinstance(v, (int, float)):
                return v == 0
            if isinstance(v, (list, tuple)):
                return all(_is_zero(x) for x in v)
            if isinstance(v, str):
                return v == ""
            return False
        return _is_zero(val) and _is_zero(result)

    # Pre-compute type desires (only if expected_type is provided)
    wants_vector = bool(expected_type) and _is_vector_type(expected_type)
    wants_2d_vector = bool(expected_type) and _is_2d_vector_type(expected_type)
    wants_color = bool(expected_type) and _is_color_type(expected_type)
    wants_int = bool(expected_type) and _is_int_type(expected_type)
    wants_bool = bool(expected_type) and _is_bool_type(expected_type)
    wants_float = bool(expected_type) and _is_float_type(expected_type)
    wants_string = bool(expected_type) and _is_string_type(expected_type)
    wants_geometry = bool(expected_type) and _is_geometry_type(expected_type)
    wants_object = bool(expected_type) and _is_object_type(expected_type)

    # Number of components for vector sockets (2 or 3)
    vec_len = 2 if wants_2d_vector else 3

    if val is None:
        # Promote None to the correct zero-default for the socket type
        if expected_type:
            if wants_vector:
                zero_vec = [0.0] * vec_len
                _warn(f"None promoted to zero-vector {zero_vec} for type '{expected_type}'")
                return zero_vec
            if wants_color:
                _warn(f"None promoted to zero-color [0,0,0,1] for type '{expected_type}'")
                return [0.0, 0.0, 0.0, 1.0]
            if wants_float:
                _warn(f"None promoted to 0.0 for type '{expected_type}'")
                return 0.0
            if wants_int:
                _warn(f"None promoted to 0 for type '{expected_type}'")
                return 0
            if wants_bool:
                _warn(f"None promoted to False for type '{expected_type}'")
                return False
            if wants_geometry:
                # Geometry sockets have no default_value, return None silently
                return None
            if wants_object:
                return None
        return None

    # --- Bool input ---
    if isinstance(val, bool):
        if wants_vector:
            f = float(val)
            vec = [f] * vec_len
            if not _is_trivial_coerce(val, vec):
                _coerce(f"bool {val} promoted to vector {vec} for type '{expected_type}'")
            return vec
        if wants_color:
            result = [float(val), float(val), float(val), 1.0]
            if not _is_trivial_coerce(val, result):
                _coerce(f"bool {val} promoted to color {result} for type '{expected_type}'")
            return result
        if wants_int:
            result = int(val)
            if not _is_trivial_coerce(val, result):
                _coerce(f"bool {val} → int {result} for type '{expected_type}'")
            return result
        # bool is already correct for BOOL, and compatible with FLOAT
        return val

    # --- Int/Float input ---
    if isinstance(val, (int, float)):
        if wants_vector:
            f = float(val)
            vec = [f] * vec_len
            if not _is_trivial_coerce(val, vec):
                _coerce(f"scalar {val} promoted to vector {vec} for type '{expected_type}'")
            return vec
        if wants_color:
            result = [float(val), float(val), float(val), 1.0]
            if not _is_trivial_coerce(val, result):
                _coerce(f"scalar {val} promoted to color {result} for type '{expected_type}'")
            return result
        # Coerce between scalar types to match the socket's expected type
        if wants_int:
            if isinstance(val, float):
                result = int(val)
                if not _is_trivial_coerce(val, result):
                    _coerce(f"float {val} → int {result} for type '{expected_type}'")
                return result
            return val
        if wants_bool:
            result = bool(val)
            if not _is_trivial_coerce(val, result):
                _coerce(f"numeric {val} → bool {result} for type '{expected_type}'")
            return result
        if wants_float:
            if isinstance(val, int):
                result = float(val)
                if not _is_trivial_coerce(val, result):
                    _coerce(f"int {val} → float {result} for type '{expected_type}'")
                return result
            return val
        return val

    # --- String input ---
    if isinstance(val, str):
        if wants_vector:
            zero_vec = [0.0] * vec_len
            _warn(f"string '{val}' replaced with zero-vector {zero_vec} for type '{expected_type}'")
            return zero_vec
        if wants_color:
            _warn(f"string '{val}' replaced with zero-color [0,0,0,1] for type '{expected_type}'")
            return [0.0, 0.0, 0.0, 1.0]
        if wants_int:
            try:
                return int(val)
            except (ValueError, TypeError):
                _warn(f"string '{val}' cannot be converted to int for type '{expected_type}'")
                return 0
        if wants_float:
            try:
                return float(val)
            except (ValueError, TypeError):
                _warn(f"string '{val}' cannot be converted to float for type '{expected_type}'")
                return 0.0
        if wants_bool:
            low = val.lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
            _warn(f"string '{val}' cannot be converted to bool for type '{expected_type}'")
            return False
        return val

    # --- List input ---
    if isinstance(val, list):
        if wants_vector:
            if len(val) == vec_len:
                return val
            elif len(val) > vec_len:
                _warn(f"vector truncated from {len(val)} to {vec_len} components for type '{expected_type}'")
                return val[:vec_len]
            else:
                _warn(f"vector padded from {len(val)} to {vec_len} components with zeros for type '{expected_type}'")
                return val + [0.0] * (vec_len - len(val))
        if wants_color:
            if len(val) == 3:
                _coerce(f"3-component color promoted to RGBA with alpha=1.0 for type '{expected_type}'")
                return val + [1.0]
            if len(val) == 4:
                return val
            if len(val) > 4:
                _warn(f"color truncated from {len(val)} to 4 components for type '{expected_type}'")
                return val[:4]
            _warn(f"color list {val} replaced with zero-color [0,0,0,1] for type '{expected_type}'")
            return [0.0, 0.0, 0.0, 1.0]
        # List assigned to a scalar socket: try to extract single value
        if len(val) == 1:
            if not _is_trivial_coerce(val, val[0]):
                _coerce(f"list {val} with single element unwrapped for scalar type '{expected_type}'")
            return unclean_value(val[0], expected_type, context)
        # Multi-component list → scalar: extract first component
        # This handles the common case where a vector [x,y,z] is
        # assigned to a float/int/bool socket due to a type mismatch.
        if wants_int:
            first = val[0] if val else 0
            if isinstance(first, (int, float, bool)):
                result = int(first)
                if not _is_trivial_coerce(val, result):
                    _coerce(f"list {val} first component → int {result} for type '{expected_type}'")
                return result
            _warn(f"list {val} replaced with 0 for int type '{expected_type}'")
            return 0
        if wants_float:
            first = val[0] if val else 0.0
            if isinstance(first, (int, float, bool)):
                result = float(first)
                if not _is_trivial_coerce(val, result):
                    _coerce(f"list {val} first component → float {result} for type '{expected_type}'")
                return result
            _warn(f"list {val} replaced with 0.0 for float type '{expected_type}'")
            return 0.0
        if wants_bool:
            first = val[0] if val else False
            if isinstance(first, (int, float, bool)):
                result = bool(first)
                if not _is_trivial_coerce(val, result):
                    _coerce(f"list {val} first component → bool {result} for type '{expected_type}'")
                return result
            _warn(f"list {val} replaced with False for bool type '{expected_type}'")
            return False
        return val

    # --- Dict input (Blender data-block reference) ---
    if isinstance(val, dict) and "name" in val:
        try:
            coll_name = val["type"].lower() + "s"
            if hasattr(bpy.data, coll_name):
                result = getattr(bpy.data, coll_name).get(val["name"])
                if result is None:
                    _warn(f"data-block '{val['type']}' named '{val['name']}' not found")
                return result
            # Fallback: try alternative collection names for unusual types
            alt_names = {
                "vectorfont": "vectorfonts",
                "curve": "curves",
                "brush": "brushes",
                "palettes": "palettes",
                "action": "actions",
                "armature": "armatures",
                "camera": "cameras",
                "light": "lights",
                "mesh": "meshes",
                "sound": "sounds",
                "screen": "screens",
            }
            alt = alt_names.get(val["type"].lower())
            if alt and hasattr(bpy.data, alt):
                result = getattr(bpy.data, alt).get(val["name"])
                if result is None:
                    _warn(f"data-block '{val['type']}' named '{val['name']}' not found in {alt}")
                return result
        except (TypeError, AttributeError):
            _warn(f"could not resolve data-block reference: {val}")
            return None
    return val
