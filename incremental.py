"""Stop paragraph review at the first usable hit; replenish sentence batches."""
import math


def paragraph_order(option):
    return (not option['complete_natural_paragraph'], not 100 <= option['token_count'] <= 200,
            option['clean_start'], option['clean_end'], option['option_id'])


def next_round(e, run, stage):
    return 1 + max((e.read(p).get('round', 0) for p in (run / 'jobs' / stage).glob('*.json')), default=0)


def paragraph_progress(e, run):
    """Replay real responses in dispatch order; duplicates do not close a parent."""
    parents = {p['candidate_id']: p for p in e.rows(run / 'remaining_passages.jsonl')}
    assert all(p['boundary_review']['decision'] != 'keep' for p in parents.values())
    options = {r['option_id']: r for r in e.rows(run / 'paragraph_options.jsonl')}
    reviews = e.stage_results(run, 'paragraph')
    occupied = e.rows(run / 'passages.jsonl')
    chosen, duplicates = {}, []
    for oid, review in reviews.items():
        option = options[oid]
        pid = option['passage_id']
        assert pid not in chosen, 'A paragraph parent was reviewed again after its first accepted option.'
        if review['decision'] == 'keep':
            if any(e.conflicts(option, r[0]) for r in chosen.values()) or any(e.conflicts(option, r) for r in occupied):
                duplicates.append(oid)
            else:
                chosen[pid] = (option, review)
    pending = []
    for pid in parents:
        if pid in chosen:
            continue
        candidates = sorted((o for o in options.values() if o['passage_id'] == pid and o['option_id'] not in reviews),
                            key=paragraph_order)
        for option in candidates:
            if any(e.conflicts(option, r) for r in occupied) or any(e.conflicts(option, r[0]) for r in chosen.values()):
                continue
            pending.append(option)
            break
    return {'parents': parents, 'chosen': chosen, 'next_options': pending, 'reviews': reviews,
            'duplicates': duplicates, 'complete': not pending}


def paragraph_jobs(e, run):
    if not (run / 'paragraph_options.jsonl').exists():
        e.prepare_paragraph_options(run)
    state = paragraph_progress(e, run)  # Missing/invalid earlier responses block a new round.
    groups = [{'candidate_id': o['option_id'], 'passage_id': o['passage_id'],
               'text': o['text'], 'token_count': o['token_count'],
               'kind': 'whole' if o['complete_natural_paragraph'] else 'excerpt'} for o in state['next_options']]
    e.make_jobs(run, 'paragraph', groups, char_budget=30000, round_id=next_round(e, run, 'paragraph'))
    print({'paragraph_parents_done': len(state['chosen']), 'next_candidates': len(groups), 'complete': state['complete']})


def select_paragraphs(e, run):
    state = paragraph_progress(e, run)
    assert state['complete'], 'More paragraph candidates remain; run paragraph-jobs and review the next round.'
    chosen = []
    for pid, parent in state['parents'].items():
        if pid not in state['chosen']:
            continue
        option, review = state['chosen'][pid]
        assert 100 <= e.count(option['text']) == option['token_count'] <= 300
        assert parent['clean_start'] <= option['clean_start'] < option['clean_end'] <= parent['clean_end']
        chosen.append({**option, 'sample_id': f'CF{len(chosen) + 1:06d}', 'context': parent['text'],
            'context_source_spans': parent['source_spans'], 'parent_boundary_decision': parent['boundary_review']['decision'],
            'paragraph_review': {**e.review_reference(review), 'group_id': pid, 'selection_reason': review['reason']}})
    e.write_rows(run / 'paragraphs.jsonl', chosen)
    stats = {'selected': len(chosen), 'deduplicated_options': state['duplicates'],
             'whole_natural_paragraphs': sum(r['complete_natural_paragraph'] for r in chosen),
             'preferred_100_200': sum(100 <= r['token_count'] <= 200 for r in chosen),
             'options_reviewed': len(state['reviews']), 'options_available': len(e.rows(run / 'paragraph_options.jsonl')),
             'selection': 'first_acceptable_whole_before_excerpt'}
    e.write(run / 'paragraph_selection.json', stats)
    print(stats)


def sentence_progress(e, run):
    candidates = {r['candidate_id']: r for r in e.rows(run / 'sentence_candidates.jsonl')}
    occupied = e.rows(run / 'passages.jsonl') + e.rows(run / 'paragraphs.jsonl')
    reviews = e.stage_results(run, 'sentence')
    chosen, duplicates, seen = [], [], set()
    for group_id, review in reviews.items():
        reviewed = set(review['reviewed_ids'])
        assert not seen.intersection(reviewed), 'A sentence candidate was dispatched more than once.'
        seen.update(reviewed)
        for sid in sorted(review['selected_ids']):
            r = candidates[sid]
            if any(e.conflicts(r, q) for q in occupied + chosen):
                duplicates.append(sid)
                continue
            chosen.append({**r, 'sample_id': sid,
                           'sentence_review': e.review_reference(review) | {'group_id': group_id}})
    remaining = [r for sid, r in candidates.items() if sid not in seen
                 and not any(e.conflicts(r, q) for q in chosen)]
    target = e.verify_protocol(run)['sentence_target']
    return {'chosen': chosen, 'duplicates': duplicates, 'reviewed': len(seen), 'remaining': remaining,
            'complete': len(chosen) >= target or not remaining, 'target': target}


def sentence_batch_size(approved, reviewed, target):
    if approved >= target:
        return 0
    if not reviewed:
        return math.ceil(target * 1.5)
    gap = target - approved
    estimated = math.ceil(gap * reviewed / max(approved, 1))
    return min(100, max(gap, estimated))


def sentence_jobs(e, run):
    if not (run / 'sentence_candidates.jsonl').exists():
        e.prepare_sentence_candidates(run)
    state = sentence_progress(e, run)
    size = 0 if state['complete'] else sentence_batch_size(len(state['chosen']), state['reviewed'], state['target'])
    selected = e.choose_sentences(state['remaining'], size)
    selected_ids = {s['candidate_id'] for s in selected}
    block_ids = {s['paragraph_id'] for s in selected}
    chapters = {c['chapter_id']: c for c in e.rows(run / 'chapters.jsonl')}
    round_id = next_round(e, run, 'sentence')
    groups = []
    for block in e.natural_blocks(run):
        if block['block_id'] not in block_ids:
            continue
        chapter = chapters[block['chapter_id']]
        segments = e.annotated_segments(chapter['text'], block, block['clean_start'], block['clean_end'])
        groups.append({'candidate_id': f"{block['block_id']}@r{round_id:04d}", 'paragraph_id': block['block_id'],
            'segments': [{'candidate_id': s['sentence_id'] if s['sentence_id'] in selected_ids else None,
                          'text': s['text']} for s in segments]})
    e.make_jobs(run, 'sentence', groups, char_budget=60000, round_id=round_id)
    print({'sentences_approved': len(state['chosen']), 'sentences_reviewed': state['reviewed'],
           'new_candidates': len(selected), 'complete': state['complete']})


def next_step(e, run):
    stages = (('boundary', 'boundary-jobs', 'passages.jsonl'),
              ('paragraph', 'paragraph-jobs', 'paragraphs.jsonl'),
              ('sentence', 'sentence-jobs', 'cascade_dataset.zip'))
    for stage, command, selected in stages:
        folder = run / 'jobs' / stage
        if not folder.exists():
            return {'action': 'run_stage', 'stage': command}
        e.stage_results(run, stage, require_all=False)
        missing = [str(p.resolve()) for p in sorted(folder.glob('*.json')) if not e.Path(e.read(p)['output_path']).exists()]
        if missing:
            return {'action': 'agent_review', 'stage': stage, 'job_files': missing}
        if stage == 'paragraph' and not paragraph_progress(e, run)['complete']:
            return {'action': 'run_stage', 'stage': command}
        if stage == 'sentence' and not sentence_progress(e, run)['complete']:
            return {'action': 'run_stage', 'stage': command}
        if not (run / selected).exists():
            return {'action': 'run_stage', 'stage': {'boundary': 'select-passages',
                    'paragraph': 'select-paragraphs', 'sentence': 'finalize'}[stage]}
    return {'action': 'complete', 'archive': str(run / 'cascade_dataset.zip')}
