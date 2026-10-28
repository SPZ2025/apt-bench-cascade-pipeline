"""Exact source coordinates and input records; no model calls."""
from pathlib import Path
import json
import re

MIN_TOKENS, MAX_TOKENS = 1200, 3000

def trim(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end

def paragraphs(text):
    start = 0
    result = []
    for separator in re.finditer(r"\n[ \t\r]*\n(?:[ \t\r]*\n)*", text):
        a, b = trim(text, start, separator.start())
        if a < b:
            result.append((a, b))
        start = separator.end()
    a, b = trim(text, start, len(text))
    if a < b:
        result.append((a, b))
    return result

def merge_ranges(ranges):
    result = []
    for a, b in sorted(ranges):
        if a >= b:
            continue
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result

def clean_heading(text):
    return re.sub(r"^\s*#{1,6}\s*", "", text).strip().strip("_*")

def map_spans(spans, start, end):
    """Map cleaned [start,end) to retained source pieces, including deleted gaps."""
    if not 0 <= start <= end <= sum(b-a for a,b in spans):
        raise ValueError('Cleaned offsets outside mapped text')
    result, cursor = [], 0
    for a, b in spans:
        lo, hi = max(start, cursor), min(end, cursor+b-a)
        if lo < hi:
            result.append([a+lo-cursor, a+hi-cursor])
        cursor += b-a
    return result

def restore(source, spans):
    return ''.join(source[a:b] for a,b in spans)

def overlap(left, right):
    return any(max(a,c) < min(b,d) for a,b in left for c,d in right)

def text_key(text):
    return ' '.join(text.lower().split())
