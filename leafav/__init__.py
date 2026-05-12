"""
pyLeafAV — Python library and tools to control Leaf HDMI matrix switches.

Supported models: LU642, LU642L, LU862, LU862D, LU1082
Verified with:    LU862
"""
from .client import LeafMatrix, LeafAVError
from .protocol import MODELS

__all__ = ["LeafMatrix", "LeafAVError", "MODELS"]
__version__ = "0.1.0"
