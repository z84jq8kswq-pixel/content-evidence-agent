"""Test package marker.

Without this file, ``python -m unittest discover -s tests`` run from the wrong
directory can resolve ``tests`` to an unrelated namespace package on sys.path
and fail with "expected str, bytes or os.PathLike object, not NoneType".
"""
