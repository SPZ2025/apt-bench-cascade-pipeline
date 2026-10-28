"""APT cascade v2: all passing passages, <=1 paragraph/failed passage, ~200 sentences."""
from pathlib import Path
from functools import lru_cache
from collections import Counter
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import re
import sys
import input_data

from source_utils import paragraphs, trim, clean_heading, map_spans, restore, overlap, text_key
from chapter_structure import structure, clean_chapter, subtract
from sentence_utils import sentence_spans
from input_data import load_units

ROOT = Path(__file__).resolve().parent
PROMPTS = {kind: ROOT / f'prompts/cascade_{kind}.md' for kind in ('boundary', 'paragraph', 'sentence')}
COUNTER = None
REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
TOKENIZER = Path.home() / '.cache/huggingface/hub/models--intfloat--multilingual-e5-small/snapshots' / REVISION / 'tokenizer.json'
SENTENCE_TARGET = 200
POLICY = {'version': 2, 'passage_selection': 'all_keep', 'paragraph_limit_per_parent': 1,
          'paragraph_pool': 'all_not_passed', 'sentence_scope': 'all_source_minus_selected_targets'}


def read(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON key: ' + key)
            value[key] = item
        return value
    return json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=unique)


def rows(path):
    return [json.loads(s) for s in Path(path).read_text(encoding='utf-8-sig').splitlines() if s.strip()]


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def write_rows(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(''.join(json.dumps(v, ensure_ascii=False) + '\n' for v in values), encoding='utf-8')
    temp.replace(path)


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def digest(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


@lru_cache(maxsize=80000)
def count(text):
    global COUNTER
    if COUNTER is None:
        from tokenizers import Tokenizer
        COUNTER = Tokenizer.from_file(str(TOKENIZER))
    return len(COUNTER.encode(text, add_special_tokens=False).ids)


def verify_protocol(run):
    protocol = read(run / 'protocol.json')
    assert protocol['policy'] == POLICY, 'Policy changed; use a new run.'
    for kind, path in PROMPTS.items():
        assert sha(path.read_text(encoding='utf-8')) == protocol['prompts'][kind], 'Prompt changed; use a new run.'
    assert hashlib.sha256(input_data.SOURCE_FILE.read_bytes()).hexdigest() == protocol['source_sha256'], 'Source changed.'
    assert hashlib.sha256(TOKENIZER.read_bytes()).hexdigest() == protocol['tokenizer_sha256'], 'Tokenizer changed.'
    for relative, expected in protocol['code_sha256'].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, 'Code changed; use a new run.'
    return protocol


def source_row(chapter, start, end, kind):
    start, end = trim(chapter['text'], start, end)
    assert start < end
    spans = map_spans(chapter['source_spans'], start, end)
    return {k: chapter[k] for k in ('scholar_id', 'document_id', 'unit_id', 'chapter_id', 'chapter_title')} | {
        'source_spans': spans, 'start': spans[0][0], 'end': spans[-1][1],
        'text': chapter['text'][start:end], 'token_count': count(chapter['text'][start:end]),
        'clean_start': start, 'clean_end': end, 'source_kind': kind,
    }


@lru_cache(maxsize=20000)
def normalized(text):
    return text_key(text)


def conflicts(left, right):
    if left['unit_id'] == right['unit_id'] and overlap(left['source_spans'], right['source_spans']):
        return True
    a, b = normalized(left['text']), normalized(right['text'])
    return a in b or b in a


def sentence_ranges(text, chinese=False):
    if chinese:
        from sentence_utils import global_sentences, quote_ranges
        return global_sentences(text, quote_ranges(text))
    return sentence_spans(text)


def prepare(run, author):
    if run.exists():
        protocol = verify_protocol(run)
        assert protocol['author'] == author
    else:
        run.mkdir(parents=True)
        assert any(d['scholar_id'] == author for d, u in load_units().values()), 'Unknown author'
        code_files = sorted(ROOT.glob('*.py')) + sorted((ROOT / 'tools').rglob('*.py')) + sorted((ROOT / 'tools').rglob('*.json'))
        write(run / 'protocol.json', {'author': author, 'policy': POLICY,
            'source_path': str(input_data.SOURCE_FILE), 'source_sha256': hashlib.sha256(input_data.SOURCE_FILE.read_bytes()).hexdigest(),
            'model_path': str(TOKENIZER.parent), 'tokenizer_sha256': hashlib.sha256(TOKENIZER.read_bytes()).hexdigest(),
            'code_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files},
            'prompts': {k: sha(p.read_text(encoding='utf-8')) for k, p in PROMPTS.items()},
            'passage_tokens': [1200, 3000], 'paragraph_tokens': [100, 300],
            'paragraph_preferred_tokens': [100, 200], 'sentence_target': SENTENCE_TARGET,
            'uniform_method': 'midpoint quantiles in source order among agent-approved unique sentences',
            'created_at': datetime.now(timezone.utc).isoformat()})
    registry = {uid: value for uid, value in load_units().items() if value[0]['scholar_id'] == author}
    chapters = []
    for uid, (doc, unit) in sorted(registry.items()):
        if doc['document_id'] == 'scholar_032_doc_004' or 'prelude in rhyme' in unit['unit_title'].lower():
            continue
        source = unit['text']
        if author in ('scholar_031', 'scholar_032'):
            from sentence_utils import exclusion, heading
            raw = paragraphs(source)
            tops = [(a, b, source[a:b].lstrip('# ').strip()) for a, b in raw if re.match(r'^#\s+\S', source[a:b])]
            starts = sorted(set([0] + [a for a, b, s in tops]))
            for i, a in enumerate(starts):
                b = starts[i + 1] if i + 1 < len(starts) else len(source)
                cuts = [(x, y) for x, y in raw if a <= x < y <= b and (heading(source[x:y]) or exclusion(source[x:y]))]
                spans = subtract(a, b, cuts)
                text = restore(source, spans)
                if not text.strip():
                    continue
                ca, cb = trim(text, 0, len(text)); spans = map_spans(spans, ca, cb)
                title = next((s for x, y, s in tops if x == a), unit['unit_title'])
                chapters.append({'scholar_id': author, 'document_id': doc['document_id'], 'unit_id': uid,
                    'chapter_id': f'{uid}@{a}:{b}', 'chapter_title': title, 'chapter_start': a, 'chapter_end': b,
                    'source_spans': spans, 'text': restore(source, spans), 'removed_subheadings': [], 'review_flags': []})
        else:
            bounds, blocks = structure(unit, doc['document_id'])
            if bounds and bounds[0][0] > 0:
                stop = bounds[0][0]
                if any(y - x > 250 and re.search(r'[.!?]', source[x:y]) and not source[x:y].startswith('#')
                       for x, y in paragraphs(source[:stop])):
                    bounds.insert(0, (0, stop, unit['unit_title']))
            for a, b, title in bounds:
                spans, removed, flags = clean_chapter(source, a, b, title, doc['document_id'], blocks)
                # Confirmed Keynes display headings and running heads, not numeric data rows.
                additional = []
                if author == 'scholar_005':
                    for x, y in paragraphs(source):
                        value = source[x:y]
                        if not a <= x < y <= b:
                            continue
                        if x == a and clean_heading(value) == title:
                            additional.append((x, y))
                        if not value.startswith('#'):
                            continue
                        s = re.sub(r'^#+\s*', '', value)
                        if len(s) < 250 and (re.search(r'[A-Za-z]', s) or re.fullmatch(r'π|11|1', s)):
                            additional.append((x, y))
                            if not any(v['start'] == x for v in removed):
                                removed.append({'start': x, 'end': y, 'title': s})
                kept = [piece for x, y in spans for piece in subtract(x, y, additional)]
                if not kept:
                    continue
                value = restore(source, kept); ca, cb = trim(value, 0, len(value))
                spans = map_spans(kept, ca, cb)
                if not spans:
                    continue
                chapters.append({'scholar_id': author, 'document_id': doc['document_id'], 'unit_id': uid,
                    'chapter_id': f'{uid}@{a}:{b}', 'chapter_title': title, 'chapter_start': a, 'chapter_end': b,
                    'source_spans': spans, 'text': restore(source, spans), 'removed_subheadings': removed, 'review_flags': flags})
    for c in chapters:
        c['token_count'] = count(c['text'])
        assert restore(registry[c['unit_id']][1]['text'], c['source_spans']) == c['text']
    path = run / 'chapters.jsonl'
    if path.exists():
        if rows(path) != chapters:
            assert not (run / 'jobs').exists(), 'Prepared chapters changed after reviews; use a new run.'
            write_rows(path, chapters)
    else:
        write_rows(path, chapters)
    sys.path.insert(0, str(ROOT / 'tools'))
    from segmentation_project import LargeTextSegmenter
    import torch
    torch.set_num_threads(4)
    splitter = LargeTextSegmenter(model_path=TOKENIZER.parent, local_files_only=True, device='cpu', batch_size=16)
    candidates, status = [], []
    cache_index = {read(p)['input_hash']: p for p in (run / 'split_cache').glob('*.json')}
    for i, c in enumerate(chapters, 1):
        binding = digest({'chapter': c, 'method': 'existing_E5_DP_1200_3000'})
        cache = cache_index.get(binding, run / 'split_cache' / (binding + '.json'))
        if cache.exists():
            result = read(cache); assert result['input_hash'] == binding
        else:
            print(f"SPLIT {i}/{len(chapters)} {c['chapter_id']} {c['token_count']}", flush=True)
            try:
                split = splitter.segment(c['text'], unit_id=c['chapter_id'])
                assert ''.join(f.text for f in split.fragments) == c['text']
                result = {'input_hash': binding, 'status': split.status, 'refined': split.refined_candidates,
                          'parts': [[f.start, f.end] for f in split.fragments]}
            except Exception as error:
                result = {'input_hash': binding, 'status': 'failed', 'error': f'{type(error).__name__}: {error}', 'parts': []}
            write(cache, result)
        status.append({'chapter_id': c['chapter_id'], 'token_count': c['token_count'], **result})
        for a, b in result['parts']:
            r = source_row(c, a, b, 'whole_chapter' if len(result['parts']) == 1 else 'semantic_split')
            if not 1200 <= r['token_count'] <= 3000:
                assert result['status'] == 'below_minimum'
                continue
            r['candidate_id'] = f"CP{len(candidates) + 1:05d}"
            r['refined_candidates'] = result.get('refined', False)
            r['crosses_subsections'] = any(r['start'] < h['start'] < r['end'] for h in c['removed_subheadings'])
            r['removed_subheadings'] = [h for h in c['removed_subheadings'] if r['start'] < h['start'] < r['end']]
            candidates.append(r)
    candidates.sort(key=lambda p: (p['document_id'], p['unit_id'], p['start']))
    for i, p in enumerate(candidates, 1):
        p['candidate_id'] = f'CP{i:05d}'
    write_rows(run / 'passage_partition.jsonl', candidates)
    write_rows(run / 'split_status.jsonl', status)
    print(json.dumps({'chapters': len(chapters), 'passages': len(candidates), 'split_status': dict(Counter(s['status'] for s in status))}), flush=True)


def make_jobs(run, stage, groups, char_budget=54000):
    prompt = PROMPTS[stage].read_text(encoding='utf-8')
    batches, batch, size = [], [], 0
    for group in groups:
        group['input_hash'] = digest({'stage': stage, 'prompt': sha(prompt), 'input': group})
        n = len(json.dumps(group, ensure_ascii=False))
        if batch and size + n > char_budget:
            batches.append(batch); batch, size = [], 0
        batch.append(group); size += n
    if batch:
        batches.append(batch)
    (run / 'jobs' / stage).mkdir(parents=True, exist_ok=True)
    (run / 'results' / stage).mkdir(parents=True, exist_ok=True)
    expected_names = {f'{stage}_{i:03d}.json' for i in range(1, len(batches) + 1)}
    assert {p.name for p in (run / 'jobs' / stage).glob('*.json')} <= expected_names, 'Unexpected old job files'
    for i, batch in enumerate(batches, 1):
        jobid = f'{stage}_{i:03d}'
        path = run / 'jobs' / stage / f'{jobid}.json'
        value = {
            'job_id': jobid, 'stage': stage, 'prompt': prompt, 'prompt_sha256': sha(prompt), 'groups': batch,
            'output_path': str(run / 'results' / stage / f'{jobid}.json'),
            'execution_rules': ['Read only this assignment. No web, no source modifications.',
                'Use the existing Python interpreter with -B; no installations or downloads.',
                'Write complete UTF-8 JSON to output_path. Reply only job_id.']}
        if path.exists():
            assert read(path) == value, 'Existing job differs; do not invalidate completed reviews.'
        else:
            write_rows(path, [value])
    print({'stage': stage, 'groups': len(groups), 'jobs': len(batches)})


def boundary_jobs(run):
    cs = {c['chapter_id']: c for c in rows(run / 'chapters.jsonl')}
    groups = []
    for p in rows(run / 'passage_partition.jsonl'):
        c = cs[p['chapter_id']]; a, b = p['clean_start'], p['clean_end']
        groups.append({'candidate_id': p['candidate_id'], 'chapter_title': p['chapter_title'],
            'head': p['text'][:1000], 'tail': p['text'][-900:],
            'before': c['text'][max(0, a - 500):a], 'after': c['text'][b:b + 500],
            'refined_candidates': p['refined_candidates'], 'token_count': p['token_count']})
    make_jobs(run, 'boundary', groups)


def stage_results(run, stage, require_all=True):
    output = {}
    paragraph_options = {r['option_id']: r for r in rows(run / 'paragraph_options.jsonl')} if stage == 'paragraph' and (run / 'paragraph_options.jsonl').exists() else {}
    paragraph_parents = {r['candidate_id']: r for r in rows(run / 'remaining_passages.jsonl')} if stage == 'paragraph' and (run / 'remaining_passages.jsonl').exists() else {}
    sentence_options = {r['candidate_id']: r for r in rows(run / 'sentence_candidates.jsonl')} if stage == 'sentence' and (run / 'sentence_candidates.jsonl').exists() else {}
    for path in sorted((run / 'jobs' / stage).glob('*.json')):
        job = read(path)
        assert job['prompt_sha256'] == verify_protocol(run)['prompts'][stage]
        answer_path = Path(job['output_path'])
        if not answer_path.exists():
            if require_all:
                raise ValueError('Missing real agent result: ' + job['job_id'])
            continue
        response = read(answer_path)
        assert set(response) == {'results'}
        index = {g['candidate_id']: g for g in job['groups']}
        assert len(response['results']) == len(index)
        assert {r['candidate_id'] for r in response['results']} == set(index)
        for r in response['results']:
            g = index[r['candidate_id']]
            original = {k: v for k, v in g.items() if k != 'input_hash'}
            assert g['input_hash'] == digest({'stage': stage, 'prompt': job['prompt_sha256'], 'input': original})
            assert r['input_hash'] == g['input_hash'] and r['candidate_id'] not in output
            assert isinstance(r['reason'], str) and r['reason'].strip()
            if stage == 'boundary':
                assert set(r) == {'candidate_id', 'input_hash', 'decision', 'reason'}
                assert r['decision'] in ('keep', 'drop', 'need_more')
            elif stage == 'paragraph':
                assert set(r) == {'candidate_id', 'input_hash', 'selections', 'reason'}
                assert isinstance(r['selections'], list)
                assert len(r['selections']) <= 1, 'At most one paragraph per failed passage; return to the agent.'
                options = {o['option_id'] for block in g['context_blocks'] for o in block['options']}
                described = {o['option_id']: (block, o) for block in g['context_blocks'] for o in block['options']}
                reviewed_context = ''.join(block['separator_before'] + ''.join(s['text'] for s in block['segments']) for block in g['context_blocks']) + g['trailing_separator']
                assert paragraph_parents[r['candidate_id']]['text'] == reviewed_context
                chosen = [s['option_id'] for s in r['selections']]
                assert len(chosen) == len(set(chosen)) and set(chosen) <= options
                assert all(set(s) == {'option_id', 'reason'} and isinstance(s['reason'], str) and s['reason'].strip() for s in r['selections'])
                for i, option_id in enumerate(chosen):
                    left = paragraph_options[option_id]
                    assert left['passage_id'] == r['candidate_id']
                    block, description = described[option_id]
                    assert left['block_id'] == block['block_id'] and left['token_count'] == description['token_count']
                    if description['kind'] == 'whole':
                        expected_text = ''.join(s['text'] for s in block['segments'])
                        assert left['complete_natural_paragraph']
                    else:
                        first = next(i for i, s in enumerate(block['segments']) if s['sentence_id'] == description['first_sentence'])
                        last = next(i for i, s in enumerate(block['segments']) if s['sentence_id'] == description['last_sentence'])
                        expected_text = ''.join(s['text'] for s in block['segments'][first:last + 1])
                    assert left['text'] == expected_text and 100 <= count(expected_text) <= 300
                    for other_id in chosen[i + 1:]:
                        assert not overlap(left['source_spans'], paragraph_options[other_id]['source_spans']), ('Overlapping agent choices', job['job_id'], option_id, other_id)
            else:
                assert set(r) == {'candidate_id', 'input_hash', 'selected_ids', 'reason'}
                assert isinstance(r['selected_ids'], list)
                choices = {s['candidate_id'] for s in g['segments'] if s['candidate_id'] is not None}
                assert len(r['selected_ids']) == len(set(r['selected_ids'])) and set(r['selected_ids']) <= choices
                described = {s['candidate_id']: s['text'] for s in g['segments'] if s['candidate_id'] is not None}
                for sid in r['selected_ids']:
                    assert sentence_options[sid]['text'] == described[sid]
                    assert sentence_options[sid]['paragraph_id'] == r['candidate_id']
                    assert sentence_options[sid]['paragraph'] == ''.join(s['text'] for s in g['segments'])
            output[r['candidate_id']] = {**r, 'job_id': job['job_id'], 'prompt_sha256': job['prompt_sha256'],
                                        'reviewer': 'agent_assignment:' + job['job_id']}
    return output


def uniform(values, limit):
    if len(values) <= limit:
        return values
    if limit == 0:
        return []
    return [values[((2 * i + 1) * len(values)) // (2 * limit)] for i in range(limit)]


def select_passages(run):
    reviews = stage_results(run, 'boundary')
    ps = rows(run / 'passage_partition.jsonl')
    assert set(reviews) == {p['candidate_id'] for p in ps}
    approved = [p for p in ps if reviews[p['candidate_id']]['decision'] == 'keep']
    # Never silently discard a passing passage to satisfy a quota or deduplication.
    for i, p in enumerate(approved):
        assert not any(conflicts(p, q) for q in approved[i + 1:]), 'Passing passages conflict; resolve source duplicates before export.'
    chosen = approved
    selected = [{**p, 'sample_id': p['candidate_id'], 'boundary_review': reviews[p['candidate_id']],
                 'coherence_reviewed': False, 'self_containment_reviewed': False} for p in chosen]
    ids = {p['candidate_id'] for p in chosen}
    write_rows(run / 'passages.jsonl', selected)
    write_rows(run / 'remaining_passages.jsonl', [{**p, 'boundary_review': reviews[p['candidate_id']]} for p in ps if p['candidate_id'] not in ids])
    write(run / 'passage_selection.json', {'N': len(ps), 'K': len(approved),
          'selection_policy': 'all_keep_no_cap', 'selected': len(chosen), 'remaining': len(ps) - len(chosen),
          'decisions': dict(Counter(r['decision'] for r in reviews.values()))})
    print(read(run / 'passage_selection.json'))


def natural_blocks(run):
    path = run / 'natural_blocks.jsonl'
    if path.exists():
        return rows(path)
    result = []
    sentence_index = 0
    for c in rows(run / 'chapters.jsonl'):
        for a, b in paragraphs(c['text']):
            r = source_row(c, a, b, 'natural_paragraph')
            r['block_id'] = f'NB{len(result) + 1:05d}'
            r['sentences'] = []
            for x, y in sentence_ranges(r['text'], r['scholar_id'] in ('scholar_031', 'scholar_032')):
                sentence_index += 1
                r['sentences'].append({'candidate_id': f'CS{sentence_index:06d}', 'start': a + x, 'end': a + y})
            result.append(r)
    write_rows(path, result)
    return result


def annotated_segments(chapter_text, block, a, b):
    pieces, cursor = [], a
    for s in block['sentences']:
        if not a <= s['start'] < s['end'] <= b:
            continue
        if cursor < s['start']:
            pieces.append({'sentence_id': None, 'text': chapter_text[cursor:s['start']]})
        pieces.append({'sentence_id': s['candidate_id'], 'text': chapter_text[s['start']:s['end']]})
        cursor = s['end']
    if cursor < b:
        pieces.append({'sentence_id': None, 'text': chapter_text[cursor:b]})
    assert ''.join(s['text'] for s in pieces) == chapter_text[a:b]
    return pieces


def paragraph_jobs(run):
    chapters = {c['chapter_id']: c for c in rows(run / 'chapters.jsonl')}
    blocks = natural_blocks(run)
    groups, options = [], []
    for p in rows(run / 'remaining_passages.jsonl'):
        assert p['boundary_review']['decision'] != 'keep', 'Paragraph parents must not have passed baseline one.'
        c = chapters[p['chapter_id']]
        context_blocks, cursor = [], p['clean_start']
        for block in blocks:
            if block['chapter_id'] != p['chapter_id']:
                continue
            a, b = max(p['clean_start'], block['clean_start']), min(p['clean_end'], block['clean_end'])
            if a >= b:
                continue
            complete = a == block['clean_start'] and b == block['clean_end']
            item = {'block_id': block['block_id'], 'complete_natural_paragraph': complete,
                    'token_count': count(c['text'][a:b]), 'separator_before': c['text'][cursor:a],
                    'segments': annotated_segments(c['text'], block, a, b), 'options': []}
            def option(x, y, kind, first=None, last=None):
                r = source_row(c, x, y, kind)
                r.update(option_id=f'PQ{len(options) + 1:06d}', passage_id=p['candidate_id'],
                         block_id=block['block_id'], complete_natural_paragraph=kind == 'whole_natural_paragraph')
                assert 100 <= r['token_count'] <= 300
                options.append(r)
                item['options'].append({'option_id': r['option_id'], 'kind': 'whole' if r['complete_natural_paragraph'] else 'excerpt',
                    'first_sentence': first, 'last_sentence': last, 'token_count': r['token_count']})
            if complete and 100 <= item['token_count'] <= 300:
                option(a, b, 'whole_natural_paragraph')
            ss = [s for s in block['sentences'] if a <= s['start'] < s['end'] <= b]
            for i, first in enumerate(ss):
                for last in ss[i:]:
                    n = count(c['text'][first['start']:last['end']])
                    if n > 320:
                        break
                    if not 100 <= n <= 300:
                        continue
                    if complete and first['start'] == a and last['end'] == b and 100 <= item['token_count'] <= 300:
                        continue
                    option(first['start'], last['end'], 'continuous_sentences_within_paragraph', first['candidate_id'], last['candidate_id'])
            context_blocks.append(item)
            cursor = b
        trailing = c['text'][cursor:p['clean_end']]
        assert ''.join(v['separator_before'] + ''.join(s['text'] for s in v['segments']) for v in context_blocks) + trailing == p['text']
        groups.append({'candidate_id': p['candidate_id'], 'chapter_title': p['chapter_title'],
                       'boundary_decision': p['boundary_review']['decision'], 'context_blocks': context_blocks,
                       'trailing_separator': trailing})
    write_rows(run / 'paragraph_options.jsonl', options)
    make_jobs(run, 'paragraph', groups, char_budget=90000)


def review_reference(review):
    return {key: review[key] for key in ('job_id', 'input_hash', 'prompt_sha256', 'reviewer', 'reason')}


def select_paragraphs(run):
    reviews = stage_results(run, 'paragraph')
    parents = {p['candidate_id']: p for p in rows(run / 'remaining_passages.jsonl')}
    assert all(p['boundary_review']['decision'] != 'keep' for p in parents.values())
    assert set(reviews) == set(parents)
    options = {r['option_id']: r for r in rows(run / 'paragraph_options.jsonl')}
    occupied = rows(run / 'passages.jsonl')
    chosen, deduplicated = [], []
    for pid, review in reviews.items():
        p = parents[pid]
        selected = [(options[s['option_id']], s['reason']) for s in review['selections']]
        assert len(selected) <= 1, 'At most one paragraph per failed passage.'
        for i, (left, why) in enumerate(selected):
            assert left['passage_id'] == pid
            for right, other_why in selected[i + 1:]:
                assert not overlap(left['source_spans'], right['source_spans']), ('Overlapping agent options', review['job_id'], left['option_id'], right['option_id'])
        for r, reason in sorted(selected, key=lambda v: v[0]['clean_start']):
            if any(conflicts(r, q) for q in occupied + chosen):
                deduplicated.append(r['option_id'])
                continue
            assert 100 <= count(r['text']) == r['token_count'] <= 300
            assert p['clean_start'] <= r['clean_start'] < r['clean_end'] <= p['clean_end']
            chosen.append({**r, 'sample_id': f'CF{len(chosen) + 1:06d}',
                'context': p['text'], 'context_source_spans': p['source_spans'],
                'parent_boundary_decision': p['boundary_review']['decision'],
                'paragraph_review': {**review_reference(review), 'group_id': pid, 'selection_reason': reason}})
    write_rows(run / 'paragraphs.jsonl', chosen)
    write(run / 'paragraph_selection.json', {'selected': len(chosen), 'deduplicated_options': deduplicated,
          'whole_natural_paragraphs': sum(r['complete_natural_paragraph'] for r in chosen),
          'preferred_100_200': sum(100 <= r['token_count'] <= 200 for r in chosen)})
    print(read(run / 'paragraph_selection.json'))


def sentence_jobs(run):
    chapters = {c['chapter_id']: c for c in rows(run / 'chapters.jsonl')}
    occupied = rows(run / 'passages.jsonl') + rows(run / 'paragraphs.jsonl')
    groups, candidates = [], []
    for block in natural_blocks(run):
        c = chapters[block['chapter_id']]
        eligible = set()
        for s in block['sentences']:
            r = source_row(c, s['start'], s['end'], 'sentence')
            if any(conflicts(r, q) for q in occupied):
                continue
            r.update(candidate_id=s['candidate_id'], paragraph_id=block['block_id'],
                     paragraph=block['text'], paragraph_source_spans=block['source_spans'])
            candidates.append(r); eligible.add(s['candidate_id'])
        if not eligible:
            continue
        segments = annotated_segments(c['text'], block, block['clean_start'], block['clean_end'])
        groups.append({'candidate_id': block['block_id'], 'chapter_title': block['chapter_title'],
                       'segments': [{'candidate_id': s['sentence_id'] if s['sentence_id'] in eligible else None,
                                     'text': s['text']} for s in segments]})
    write_rows(run / 'sentence_candidates.jsonl', candidates)
    make_jobs(run, 'sentence', groups, char_budget=90000)


def choose_sentences(candidates, limit):
    """Sample only after semantic approval and deduplication, in stable source order."""
    ordered = sorted(candidates, key=lambda r: (r['document_id'], r['unit_id'], r['start'], r['candidate_id']))
    return uniform(ordered, min(limit, len(ordered)))


def finalize(run):
    reviews = stage_results(run, 'sentence')
    candidates = {r['candidate_id']: r for r in rows(run / 'sentence_candidates.jsonl')}
    assert set(reviews) == {r['paragraph_id'] for r in candidates.values()}
    ps, fs = rows(run / 'passages.jsonl'), rows(run / 'paragraphs.jsonl')
    chosen, deduplicated = [], []
    for group_id, review in reviews.items():
        for sid in sorted(review['selected_ids']):
            r = candidates[sid]
            assert r['paragraph_id'] == group_id
            if any(conflicts(r, q) for q in ps + fs + chosen):
                deduplicated.append(sid)
                continue
            chosen.append({**r, 'sample_id': sid, 'sentence_review': review_reference(review) | {'group_id': group_id}})
    approved_count = len(chosen)
    chosen = choose_sentences(chosen, verify_protocol(run)['sentence_target'])
    sentence_stats = {'agent_approved_unique': approved_count, 'target': verify_protocol(run)['sentence_target'],
                      'selected': len(chosen), 'shortfall': max(0, verify_protocol(run)['sentence_target'] - len(chosen)),
                      'duplicates_removed': deduplicated, 'sampling': 'uniform_midpoint_quantiles_in_source_order'}
    write_rows(run / 'sentences.jsonl', chosen)
    write(run / 'sentence_selection.json', sentence_stats)
    registry = load_units()
    allrows = ps + fs + chosen
    assert len({r['sample_id'] for r in allrows}) == len(allrows), 'Repeated sample IDs'
    for r in allrows:
        assert registry[r['unit_id']][0]['scholar_id'] == r['scholar_id']
        assert registry[r['unit_id']][0]['document_id'] == r['document_id']
        source = registry[r['unit_id']][1]['text']
        assert r['text'] == restore(source, r['source_spans'])
        if 'context' in r:
            assert r['context'] == restore(source, r['context_source_spans'])
        if 'paragraph' in r:
            assert r['paragraph'] == restore(source, r['paragraph_source_spans']) and r['text'] in r['paragraph']
    for i, r in enumerate(allrows):
        assert not any(conflicts(r, q) for q in allrows[i + 1:]), 'Final target conflict'
    assert all(1200 <= count(p['text']) == p['token_count'] <= 3000 for p in ps)
    assert all(100 <= count(p['text']) == p['token_count'] <= 300 for p in fs)
    counts = {'passages': len(ps), 'paragraphs': len(fs), 'sentences': len(chosen)}
    passage_stats = read(run / 'passage_selection.json')
    paragraph_stats = read(run / 'paragraph_selection.json')
    assert counts['passages'] == passage_stats['K'], 'All passing passages must be kept.'
    parents = {p['candidate_id']: p for p in rows(run / 'remaining_passages.jsonl')}
    assert len({p['passage_id'] for p in fs}) == len(fs), 'More than one paragraph per parent.'
    for f in fs:
        assert parents[f['passage_id']]['boundary_review']['decision'] != 'keep'
        assert f['context'] == parents[f['passage_id']]['text']
    assert counts['sentences'] <= verify_protocol(run)['sentence_target']
    for stage in PROMPTS:
        write_rows(run / 'audit' / f'{stage}.jsonl', stage_results(run, stage).values())
    write(run / 'verification.json', {'counts': counts, 'passage_selection': passage_stats,
        'paragraph_selection': read(run / 'paragraph_selection.json'), 'sentence_selection': sentence_stats,
        'targets_source_verified': len(allrows), 'target_conflicts': 0,
        'passage_coherence_reviewed': False, 'passage_self_containment_reviewed': False,
        'split_status': dict(Counter(s['status'] for s in rows(run / 'split_status.jsonl'))),
        'citation_policy': 'Allow normal references, statutes, and short poetry quotations in discussion; exclude standalone poetry targets and substantial borrowed articles.',
        'pipeline_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_units_represented': len({c['unit_id'] for c in rows(run / 'chapters.jsonl')}),
        'environment': 'Existing Python -B, offline E5; no dependency modifications.'})
    names = ['# APT三层级联 v2', '', '| 类型 | 最终数量 |', '|---|---:|']
    names += [f'| {kind} | {n} |' for kind, n in counts.items()]
    names += ['', f"切分共{passage_stats['N']}条passage，基准一通过{passage_stats['K']}条，全部保留，无一半上限。",
        f"paragraph中{paragraph_stats['whole_natural_paragraphs']}条为完整自然段，{len(fs) - paragraph_stats['whole_natural_paragraphs']}条为段内连续句子截取；{paragraph_stats['preferred_100_200']}条处于优先的100–200 tokens范围。",
        'paragraph由agent直接从未通过基准一的passage中选择，每个父篇幅最多一条；100–300 E5 tokens，偏好100–200，完整自然段优先；context为所在完整passage。',
        'paragraph的passage_id对应remaining_passages.jsonl的candidate_id；它的父篇幅作为背景保留，不在最终passages.jsonl目标表中。',
        'sentence由agent从全部原文扣除已选passage和paragraph的位置选取，paragraph为包含目标句的完整自然段。',
        f"sentence在agent审核及去重后按原文顺序均匀选取，目标{sentence_stats['target']}条，合格不足时如实保留，缺口{sentence_stats['shortfall']}条。",
        '三类目标之间不重叠，不含规范化相同或包含文本，背景允许复用。',
        'source_spans为原unit.text的0基、左闭右开字符区间，顺序拼接可精确恢复原文。context_source_spans和paragraph_source_spans分别恢复两种背景。',
        '基准二、三未审核，不把基准一通过视为全文自足或连贯；paragraph有一轮选择审核，无独立二审。',
        '未生成Task。包内包含未通过passage、原文unit、split_status与分阶段审核汇总；完整候选、固定任务输入和原始agent响应另保留在本地运行目录。']
    (run / 'README.md').write_text('\n'.join(names) + '\n', encoding='utf-8')
    author = verify_protocol(run)['author']
    source_units = [{'document_id': d['document_id'], 'scholar_id': d['scholar_id'],
                    'document_title': d['title'], **u} for d, u in registry.values() if d['scholar_id'] == author]
    write_rows(run / 'source_units.jsonl', source_units)
    from zipfile import ZipFile, ZIP_DEFLATED
    archive = run / 'cascade_dataset.zip'
    assert not archive.exists(), 'Existing pilot archive must not be silently overwritten'
    files = ['passages.jsonl', 'remaining_passages.jsonl', 'paragraphs.jsonl', 'sentences.jsonl',
             'README.md', 'protocol.json', 'verification.json', 'source_units.jsonl', 'split_status.jsonl',
             'passage_selection.json', 'paragraph_selection.json', 'sentence_selection.json']
    with ZipFile(archive, 'x', ZIP_DEFLATED) as z:
        for name in files:
            z.write(run / name, name)
        for path in sorted((run / 'audit').glob('*.jsonl')):
            z.write(path, 'audit/' + path.name)
    with ZipFile(archive) as z:
        assert z.testzip() is None
    print(json.dumps({'counts': counts, 'archive': str(archive)}, ensure_ascii=True))


def status(run):
    output = {}
    for stage in PROMPTS:
        jobs = list((run / 'jobs' / stage).glob('*.json'))
        output[stage] = {'jobs': len(jobs), 'completed': sum(Path(read(p)['output_path']).exists() for p in jobs),
                         'validated_groups': len(stage_results(run, stage, False))}
    print(json.dumps(output))


def main():
    global TOKENIZER, SENTENCE_TARGET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('import-bundle', 'prepare', 'boundary-jobs', 'select-passages',
        'paragraph-jobs', 'select-paragraphs', 'sentence-jobs', 'finalize', 'status', 'pending', 'next'))
    parser.add_argument('--author', help='One author per run; required for a new run')
    parser.add_argument('--run', type=Path, default=ROOT / 'runs/default')
    parser.add_argument('--source', type=Path, help='Input source_units JSONL; bundle directory for import-bundle')
    parser.add_argument('--out', type=Path, help='New JSONL destination for import-bundle')
    parser.add_argument('--model-path', type=Path, help='Existing local multilingual-e5-small directory')
    parser.add_argument('--sentence-target', type=int, help='Default 200 per author; fixed at prepare')
    args = parser.parse_args()
    if not __debug__ or not sys.dont_write_bytecode:
        parser.error('Run the existing Python with -B and without -O; checks must remain enabled.')
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    if args.stage == 'import-bundle':
        if not args.source or not args.out:
            parser.error('import-bundle requires --source BUNDLE_DIR --out NEW.jsonl')
        print({'units': input_data.import_bundle(args.source, args.out, args.author), 'output': str(args.out)})
        return
    run = args.run.resolve()
    existing = read(run / 'protocol.json') if (run / 'protocol.json').exists() else None
    if existing:
        input_data.SOURCE_FILE = (args.source or Path(existing['source_path'])).resolve()
        TOKENIZER = (args.model_path or Path(existing['model_path'])).resolve() / 'tokenizer.json'
        SENTENCE_TARGET = existing['sentence_target']
        if args.author and args.author != existing['author']:
            parser.error('Author differs from the existing run')
        if args.sentence_target is not None and args.sentence_target != SENTENCE_TARGET:
            parser.error('Sentence target changed; use a new run')
        args.author = existing['author']
        verify_protocol(run)
        if (run / 'cascade_dataset.zip').exists() and args.stage not in ('status', 'pending', 'next'):
            parser.error('Run already finalized; keep its archive and use a new run for changes.')
    else:
        if args.stage != 'prepare' or not args.author or not args.source:
            parser.error('Start with prepare --author ID --source FILE.jsonl --run NEW_DIR')
        if run.exists():
            parser.error('New run directory already exists without a protocol; choose a new path')
        input_data.SOURCE_FILE = args.source.resolve()
        if args.model_path:
            TOKENIZER = args.model_path.resolve() / 'tokenizer.json'
        SENTENCE_TARGET = 200 if args.sentence_target is None else args.sentence_target
    if SENTENCE_TARGET < 1:
        parser.error('--sentence-target must be positive')
    if not TOKENIZER.is_file():
        parser.error('Local E5 tokenizer missing; supply --model-path. No download will be attempted.')
    if args.stage == 'prepare':
        prepare(run, args.author)
    else:
        stages = {'boundary-jobs': boundary_jobs, 'select-passages': select_passages,
                  'paragraph-jobs': paragraph_jobs, 'select-paragraphs': select_paragraphs,
                  'sentence-jobs': sentence_jobs, 'finalize': finalize,
                  'status': status, 'pending': pending, 'next': next_step}
        stages[args.stage](run)


def pending(run):
    """Return assignments needing actual semantic review; never synthesize approvals."""
    jobs = []
    for stage in PROMPTS:
        stage_results(run, stage, require_all=False)
        for path in sorted((run / 'jobs' / stage).glob('*.json')):
            if not Path(read(path)['output_path']).exists():
                jobs.append({'stage': stage, 'job_file': str(path.resolve()), 'job_id': read(path)['job_id']})
    print(json.dumps({'pending': jobs}, ensure_ascii=False, indent=2))


def next_step(run):
    """Tell an agent orchestrator the next dependency-safe action."""
    for stage, command, selected in (
            ('boundary', 'boundary-jobs', 'passages.jsonl'),
            ('paragraph', 'paragraph-jobs', 'paragraphs.jsonl'),
            ('sentence', 'sentence-jobs', 'cascade_dataset.zip')):
        folder = run / 'jobs' / stage
        if not folder.exists():
            print(json.dumps({'action': 'run_stage', 'stage': command}))
            return
        jobs = sorted(folder.glob('*.json'))
        stage_results(run, stage, require_all=False)
        missing = [str(p.resolve()) for p in jobs if not Path(read(p)['output_path']).exists()]
        if missing:
            print(json.dumps({'action': 'agent_review', 'stage': stage, 'job_files': missing}, ensure_ascii=False))
            return
        if not (run / selected).exists():
            print(json.dumps({'action': 'run_stage', 'stage': {'boundary': 'select-passages',
                'paragraph': 'select-paragraphs', 'sentence': 'finalize'}[stage]}))
            return
    print(json.dumps({'action': 'complete', 'archive': str(run / 'cascade_dataset.zip')}))


if __name__ == '__main__':
    main()
