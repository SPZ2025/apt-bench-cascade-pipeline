"""Conservative corpus flags and exact sentence offsets; no semantic/model checks.

Flags request inspection; they are not automatic evidence of an exclusion.
In particular, quoted terms and an author's fictional speech remain permissible.
"""
import re
from source_utils import trim
from statistics import median

_CLOSERS = '\"\u201d\u2019\u00bb) ]}_*'.replace(' ', '')
_TITLE_ABBR = {
    'mr', 'mrs', 'ms', 'dr', 'prof', 'rev', 'hon', 'sir', 'st', 'messrs',
    'mme', 'mlle', 'capt', 'col', 'gen', 'lt', 'sgt', 'fr', 'fig', 'figs',
    'vol', 'vols', 'ch', 'chap', 'bk', 'pp', 'p', 'no', 'nos', 'sec', 'art',
}
_OTHER_ABBR = {'etc', 'viz', 'cf', 'ibid', 'op', 'cit', 'al', 'vs'}


def _period_is_internal(text, pos):
    """Recognize protected periods without changing original text."""
    before, after = text[:pos], text[pos + 1:]
    if pos and text[pos - 1].isdigit() and after[:1].isdigit():
        return True
    if after.startswith('.') or before.endswith('.'):
        return True  # Never promote an ellipsis into a complete sentence.
    if after[:1].isalnum():
        return True  # URLs, decimals, and e.g./i.e. internal periods.
    word = re.search(r'([A-Za-z]+)$', before)
    token = word.group(1).lower() if word else ''
    next_word = re.match(r'[\s\"\u201c\u2018()\[\]_*]*([A-Za-z0-9]+)', after)
    following = next_word.group(1) if next_word else ''
    if (re.search(r'\d+\s*[sd]$',before,re.I) and following
            and (following[:1].isdigit() or following[:1].islower())):
        return True  # 18s. 6d.; 18s. per pound, not a completed sentence.
    if token in {'jan','feb','mar','apr','jun','jul','aug','sep','sept','oct','nov','dec'} and following[:1].isdigit():
        return True
    if token in _TITLE_ABBR and following:
        return True
    if token in _OTHER_ABBR and following and following[:1].islower():
        return True
    # A. Smith, J. S. Mill, and the final period in i.e./e.g.
    if word and len(word.group(1)) == 1 and word.group(1).isupper() and following:
        # A dotted abbreviation may itself finish a sentence: "the U.S. It ..."
        dotted = re.search(r'(?:[A-Za-z]\.)+[A-Za-z]$', before)
        if not dotted or following[:1].islower():
            return True
    if re.search(r'(?:e\.g|i\.e)$', before, re.I) and following:
        return True
    return False


def sentence_spans(text):
    """Return exact [start,end) pairs for complete, nonempty sentence candidates.

    Incomplete trailing material is omitted. Quotation-closing punctuation stays
    with its sentence. Uppercase is not required after a sentence (dialogue and
    old typography vary). Candidate quality is left to the reviewing agent.
    """
    result, start, i = [], 0, 0
    while i < len(text):
        if text[i] not in '.!?':
            i += 1
            continue
        if text[i] == '.' and _period_is_internal(text, i):
            i += 1
            continue
        end = i + 1
        while end < len(text) and text[end] in '.!?':
            end += 1
        while end < len(text) and text[end] in _CLOSERS:
            end += 1
        if end < len(text) and not text[end].isspace():
            i += 1
            continue
        left = start
        while left < end and text[left].isspace():
            left += 1
        if left < end and re.search(r'[A-Za-z]', text[left:end]):
            result.append((left, end))
        start, i = end, end
    return result



def quote_ranges(text):
    result=[];stack=[]
    for i,ch in enumerate(text):
        if ch=='“':stack.append(i)
        elif ch=='”' and stack:
            a=stack.pop()
            result.append((a,i+1))
    result.extend((m.start(),m.end()) for m in re.finditer(r'"[^"\n]+"',text))
    # An unmatched opener must not hide all later, correctly paired quotations.
    maximal=[]
    for a,z in sorted(result,key=lambda p:(p[0],-p[1])):
        if maximal and maximal[-1][0]<=a<z<=maximal[-1][1]:continue
        maximal.append((a,z))
    return maximal

def global_sentences(text,quotes):
    mask=bytearray(len(text))
    for a,z in quotes:mask[a:z]=b'1'*(z-a)
    ends={z for a,z in quotes if z-a>=3 and text[z-2] in '。！？!?' and not re.match(r'\s*[,，;；:：]',text[z:z+10])}
    for i,ch in enumerate(text):
        if ch in '。！？!?' and not mask[i]:
            z=i+1
            while z<len(text) and text[z] in '）)]】':z+=1
            ends.add(z)
    out=[];cursor=0
    for z in sorted(ends):
        a,z=trim(text,cursor,z)
        if a<z:out.append((a,z))
        cursor=z
    return out

def heading(text):
    s = text.strip()
    if s.startswith('#'): return True
    if re.fullmatch(r'[一二三四五六七八九十百〇零0-9]+', s): return True
    if len(s)<95 and '\n' not in s and not re.search(r'[。；;！？!?]$',s):
        if re.match(r'^(?:第[一二三四五六七八九十百0-9]+[章编节部分]|[一二三四五六七八九十]+[、．.]|[（(][一二三四五六七八九十0-9]+[)）]|\d+[.．、]\s*\D)',s): return True
    return False

def exclusion(text):
    s = text.strip()
    if '四、市场准入对于企业设立、变更、终止立法和执法的影响。' in s:return 'confirmed_display_heading'
    if re.match(r'^(?:\[\d+\]|[【\[]?(?:作者简介|内容提要|摘要|关键词|参考文献|注释|编者按|转录说明)|注[：:]|江\s*平$|高铭暄(?:\s*王秀梅)?$)',s): return 'front_matter_or_notes'
    if len(s)<120 and re.match(r'^[①②③④⑤⑥⑦⑧⑨⑩]',s) and re.search(r'出版社|出版|第.{0,6}页|《.+》.*卷',s): return 'footnote'
    if '\ufffd' in s or re.search(r'^\|.*\|$|!\[|^```',s,re.M): return 'damaged_or_nonprose'
    # 2026-10-27 user clarification: ordinary citations, short quotations and
    # statutory quotations are allowed. Quote length/names are not rejection rules.
    # A substantial passage borrowed from another article needs contextual review.
    return None
