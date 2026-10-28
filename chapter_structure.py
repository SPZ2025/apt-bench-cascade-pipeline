"""Existing chapter-first rules and corpus-specific adapters, unchanged."""
import re
from source_utils import paragraphs, trim, merge_ranges, clean_heading, restore

IDENT = r"(?:[IVXLCDM]+|\d+|One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Thirteen|Fourteen)"
CHAPTER = re.compile(r"^(?:CHAPTER|CHAP\.)\s+" + IDENT + r"(?=[\s.:—~-]|$)", re.I)
TAIL = re.compile(r"^(?:CONTENTS|TABLE OF CONTENTS|INDEX|FOOTNOTES?|BIBLIOGRAPH(?:Y|ICAL NOTE)|TRANSCRIBER[’']?S NOTES?)\s*[:.]?$", re.I)
ROMAN_BARE = re.compile(r"^[IVXLCDM]+[.]?$")


def subtract(a, b, cuts):
    spans = []
    cursor = a
    for x, y in merge_ranges(cuts):
        if y <= a or x >= b:
            continue
        x, y = max(a, x), min(b, y)
        if cursor < x:
            spans.append((cursor, x))
        cursor = max(cursor, y)
    if cursor < b:
        spans.append((cursor, b))
    return spans


def excerpt(text, spans):
    return "".join(text[a:b] for a, b in spans)


def short_blocks(text):
    return [(a, b, clean_heading(text[a:b])) for a, b in paragraphs(text)
            if b-a <= 500 and len(text[a:b].splitlines()) <= 7]


def structure(unit, docid):
    text = unit['text']
    blocks = short_blocks(text)
    # True chapter headings, not generic caps/number formatting.
    tops = [(a,b,s) for a,b,s in blocks if CHAPTER.match(s)]
    if docid == 'scholar_003_doc_001':
        tops = [(a,b,s) for a,b,s in blocks if
                re.match(r"^[IVXLCDM]+\.\s+[A-Z]", s) and s.isupper()
                or s == 'ZARATHUSTRA’S PROLOGUE.']
    elif docid == 'scholar_005_doc_003':
        tops = [(a,b,s) for a,b,s in blocks if re.match(r'^\d+\.\s+[A-Z]',s)
                and s.isupper() and len(s.split()) >= 4]
    # The original unit remains a parent when there are no explicit inner chapters.
    starts = [a for a,b,s in tops] if tops else [0]
    if tops and starts[0] > 0:
        prefix = text[:starts[0]]
        # Book/section title + epigraph preceding the first chapter is front matter.
        if not re.search(r'\b(?:BOOK|SECTION|PART)\b', prefix, re.I) and len(prefix.strip()) > 500:
            starts.insert(0,0)
    result=[]
    for i,a in enumerate(starts):
        b = starts[i+1] if i+1<len(starts) else len(text)
        title = next((s for x,y,s in tops if x==a), unit['unit_title'])
        result.append((a,b,title))
    return result, blocks


def clean_chapter(text,a,b,title,docid,blocks):
    cuts=[]; removed=[]; flags=[]; explicit_subs=[]
    first_blocks=[(x,y,s) for x,y,s in blocks if a<=x<y<=b]
    # Remove initial display title(s), not interior all-caps prose.
    cursor=a
    for x,y,s in first_blocks:
        if text[cursor:x].strip():
            break
        initial_title = (CHAPTER.match(s) or s.isupper() or
                         (docid=='scholar_002_doc_001' and re.match(r'^(?:Of |On |That )',s) and len(s)<250) or
                         (docid=='scholar_002_doc_001' and len(s)<250 and len(s.split())<=25 and s.istitle()) or
                         (docid=='scholar_006_doc_004' and text[x:y].strip().startswith('_Of ')) or
                         re.match(r'^(?:BOOK|PART|SECTION|LECTURE)\s+',s,re.I))
        if not initial_title:
            break
        cuts.append((x,y));cursor=y
    for x,y,s in first_blocks:
        if TAIL.fullmatch(s):
            cuts.append((x,b));break
        if docid in {'scholar_001_doc_003','scholar_001_doc_005'} and ROMAN_BARE.fullmatch(s) and not text[y:b].strip():
            cuts.append((x,y))
        # Only known document-specific subsection formats, never any bare numbering.
        sub=False
        if docid.startswith('scholar_003_') and docid!='scholar_003_doc_001':
            sub=bool(re.fullmatch(r'\d+[.]?',s) or (docid=='scholar_003_doc_005' and ROMAN_BARE.fullmatch(s)))
        if docid in {'scholar_004_doc_005','scholar_005_doc_001','scholar_005_doc_003','scholar_005_doc_004','scholar_005_doc_005'}:
            sub=sub or bool(ROMAN_BARE.fullmatch(s) or
                           (text[x:y].startswith('#') and re.match(r'^(?:[IVXLCDM]+\.|\([ivxlcdm]+\)|\([AB]\)|\d+\.)\s',s)))
        if docid=='scholar_001_doc_003':
            sub=sub or bool(ROMAN_BARE.fullmatch(s) or (re.match(r'^[IVXLCDM]+\.\s+',s) and s.isupper()))
        if docid=='scholar_005_doc_003':
            sub=sub or bool(re.match(r'^\([ivxlcdm]+\)\s+[A-Z]',s) and len(s)<200 and
                           ('\n' not in s or re.search(r'\n\([A-Z][a-z]+ \d{1,2}, \d{4}\)$',s)) and not s.endswith(('.',',',';')))
        if docid=='scholar_006_doc_003':
            sub=sub or bool(re.match(r'^\[(?:§\s*\d+\.|DIVISION\s+[IVXLCDM]+\.)',s))
        if sub:
            cuts.append((x,y));removed.append({'start':x,'end':y,'title':s});explicit_subs.append(x)
        # Explicit notes, but no general quotation or indentation masks.
        if re.match(r'^(?:\[\d+\]|\[?Footnote\s+\d+\s*:|\$\s*\^\{\d+\}\$|[¹²³⁴⁵⁶⁷⁸⁹⁰]+\s)',s,re.I):
            if not s.lower().startswith('[footnote'):
                cuts.append((x,y))
    # Section labels attached to prose: remove just the label.
    if docid=='scholar_002_doc_001':
        matches=re.finditer(r'(?m)^[ \t]*§\s*\d+\s*\.\s*',text[a:b])
    elif docid in {'scholar_005_doc_002','scholar_003_doc_002'}:
        matches=re.finditer(r'''(?m)^\d+[A-Z]?\.\s+(?=["“‘'_*(]*[A-Z])''',text[a:b])
    else:
        matches=[]
    for m in matches:
        x,y=a+m.start(),a+m.end();cuts.append((x,y))
        removed.append({'start':x,'end':y,'title':text[x:y].strip()});explicit_subs.append(x)
    # Bracketed footnotes may contain nested brackets and multiple paragraphs.
    for m in re.finditer(r'\[Footnote\s+\d+\s*:',text[a:b],re.I):
        x=a+m.start();depth=0;end=None
        for j in range(x,b):
            if text[j]=='[':depth+=1
            elif text[j]==']':
                depth-=1
                if depth==0:end=j+1;break
        if end is not None:cuts.append((x,end))
        else:flags.append('unclosed_footnote')
    for m in re.finditer(r'\[\d+\]|\$\s*\^\{\d+\}\$',text[a:b]):
        cuts.append((a+m.start(),a+m.end()))
    # Confirmed explanatory interpolation in the Wealth of Nations export.
    if docid=='scholar_006_doc_001':
        for m in re.finditer(r'\{this was\s+the current designation for steam engines\}',text[a:b]):
            cuts.append((a+m.start(),a+m.end()))
    # Repeated running heads matching the parent chapter title are layout, not chapters.
    normalized=re.sub(r'[^a-z]','',title.lower())
    for x,y,s in first_blocks:
        key=re.sub(r'[^a-z]','',s.lower())
        if len(key)>5 and key in normalized and text[x:y].startswith('#') and not CHAPTER.match(s):
            cuts.append((x,y));flags.append('running_head_removed')
        elif x>a+500 and s.isupper() and len(s.split())>=3 and not any(c<=x<d for c,d in cuts):
            flags.append('interior_title_needs_review')
    spans=subtract(a,b,cuts)
    # Trim only outer whitespace; keep inner whitespace exactly from the source.
    while spans:
        x,y=spans[0]
        while x<y and text[x].isspace():x+=1
        if x<y:spans[0]=(x,y);break
        spans.pop(0)
    while spans:
        x,y=spans[-1]
        while y>x and text[y-1].isspace():y-=1
        if x<y:spans[-1]=(x,y);break
        spans.pop()
    cleaned=excerpt(text,spans)
    if any(len(q.split())>=8 for q in re.findall(r'“[^”]+”|"[^"\n]+"',cleaned)):
        flags.append('quotation_needs_review')
    if re.search(r'(?:—|--)\s*TR\.|\(\d+\)',cleaned):flags.append('notes_needs_review')
    if docid=='scholar_006_doc_004':flags.append('essay_collection_structure_needs_review')
    if docid=='scholar_003_doc_006' and 'Excellent Books' in title:
        flags.append('nested_book_titles_needs_review')
    return spans,removed,sorted(set(flags))
