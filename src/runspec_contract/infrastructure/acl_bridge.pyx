# cython: language_level=3
"""Cython-side ACL bridge stub for the native data-plane handoff.

This file mirrors the design intent from ``specs/run_spec/design.md``:
the Python layer validates and times the call, while the Cython layer owns the
actual ``nogil`` handoff into the native runtime.

The project does not yet ship compiled native bindings, so this module is a
stub with two explicit goals:

1. document what the future Cython boundary must look like
2. keep the interface small and predictable for later wiring
"""

from libc.stddef cimport size_t


cdef int native_execute(
    const unsigned char* data,
    size_t length,
    const char* rootfs,
) noexcept nogil:
    # This symbol is the future seam to the real C/C++ runtime.
    #
    # The implementation currently returns ``-4`` so the Python layer can
    # exercise deterministic error translation even before the native data
    # plane exists in the repository.
    # Replace this stub with the linked C/C++ data-plane symbol.
    return -4


cpdef int execute_native(bytes serialized, str rootfs_path):
    """Invoke the native entrypoint while releasing the GIL.

    Step by step, this function prepares the call in the most explicit way:

    1. capture the payload length as a C ``size_t``
    2. encode ``rootfs_path`` to stable UTF-8 bytes
    3. take raw pointers to both byte buffers
    4. release the GIL for the native call
    5. return the integer status code to the Python layer

    Args:
        serialized: Serialized RunSpec envelope already validated in Python.
        rootfs_path: Root filesystem path that the native layer expects as
            UTF-8 bytes.

    Returns:
        Native integer status code. Non-negative values represent success,
        while negative values are translated by the Python bridge.
    """

    # Cython converts the Python ``bytes`` length into the exact C integer type
    # expected by the future native function signature.
    cdef size_t length = len(serialized)

    # The native side expects a NUL-terminated UTF-8 path. Keeping a reference
    # to ``rootfs_bytes`` ensures the memory remains valid for the whole call.
    cdef bytes rootfs_bytes = rootfs_path.encode("utf-8")

    # These casts expose raw contiguous buffers to the native function. The
    # Python objects stay alive in this scope, so the pointers remain valid
    # while ``native_execute`` runs.
    cdef const unsigned char* data = <const unsigned char*> serialized
    cdef const char* rootfs = rootfs_bytes
    cdef int result

    # Only the actual native traversal runs without the GIL. Validation and
    # error translation stay on the Python side, which keeps the boundary clear.
    with nogil:
        result = native_execute(data, length, rootfs)

    return result
