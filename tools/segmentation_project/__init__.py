"""Project derivative of frozen upstream splitter, using approved 1200..3000 bounds.

Upstream originals remain in ../segmentation_split_toll. Only bounds, associated
messages, backend import and profile differ; source-text slicing/DP are unchanged.
"""
from .segmenter import LargeTextSegmenter
