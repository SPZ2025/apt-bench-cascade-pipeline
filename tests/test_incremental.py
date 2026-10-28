"""Synthetic review fixtures exercise dispatch/stop mechanics, not semantic quality."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from test_pipeline import TemporaryDirectory, target
import incremental as inc
import pipeline as p


class IncrementalFixture(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        self.protocol = {'sentence_target': 200, 'prompts': {k: p.sha(v.read_text(encoding='utf-8')) for k, v in p.PROMPTS.items()}}
        self.verifier = patch.object(p, 'verify_protocol', return_value=self.protocol)
        self.verifier.start()
        self.addCleanup(self.verifier.stop)
        self.counter = patch.object(p, 'count', side_effect=lambda s: len(s.split()))
        self.counter.start()
        self.addCleanup(self.counter.stop)
        p.write_rows(self.run / 'passages.jsonl', [])

    def answer(self, stage, take):
        """Write explicitly synthetic responses only into this temporary test run."""
        index = 0
        for path in sorted((self.run / 'jobs' / stage).glob('*.json')):
            job = p.read(path)
            if Path(job['output_path']).exists():
                continue
            results = []
            for group in job['groups']:
                result = {'candidate_id': group['candidate_id'], 'input_hash': group['input_hash'], 'reason': 'Synthetic fixture only'}
                if stage == 'paragraph':
                    result['decision'] = 'keep' if take(index) else 'drop'
                    index += 1
                else:
                    result['selected_ids'] = []
                    for segment in group['segments']:
                        if segment['candidate_id'] is not None:
                            if take(index):
                                result['selected_ids'].append(segment['candidate_id'])
                            index += 1
                results.append(result)
            p.write(job['output_path'], {'results': results})
        return index


class ParagraphStopTests(IncrementalFixture):
    def setUp(self):
        super().setUp()
        first = ' '.join(f'alpha{i}' for i in range(110)) + '.'
        second = ' '.join(f'beta{i}' for i in range(110)) + '. ' + ' '.join(f'gamma{i}' for i in range(110)) + '.'
        source = first + '\n\n' + second
        self.parent = target(source) | {'boundary_review': {'decision': 'drop'}}
        p.write_rows(self.run / 'remaining_passages.jsonl', [self.parent])
        values = []
        for oid, text, start, whole in (('PQ0', first, 0, True), ('PQ1', second, len(first) + 2, True),
                                      ('PQ2', second.split('. ')[0] + '.', len(first) + 2, False)):
            values.append(target(text, start) | {'option_id': oid, 'passage_id': 'CP00001',
                                                  'complete_natural_paragraph': whole})
        p.write_rows(self.run / 'paragraph_options.jsonl', values)

    def current_options(self):
        return [g['candidate_id'] for f in sorted((self.run / 'jobs/paragraph').glob('*.json'))
                if not Path(p.read(f)['output_path']).exists() for g in p.read(f)['groups']]

    def test_first_hit_stops_and_preserves_full_context(self):
        p.paragraph_jobs(self.run)
        self.assertEqual(self.current_options(), ['PQ0'])
        job = p.read(next((self.run / 'jobs/paragraph').glob('*.json')))
        self.assertEqual(set(job['groups'][0]) - {'input_hash'}, {'candidate_id', 'passage_id', 'text', 'token_count', 'kind'})
        self.answer('paragraph', lambda i: True)
        p.paragraph_jobs(self.run)
        self.assertEqual(self.current_options(), [])
        p.select_paragraphs(self.run)
        selected = p.rows(self.run / 'paragraphs.jsonl')
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['option_id'], 'PQ0')
        self.assertEqual(selected[0]['context'], self.parent['text'])
        self.assertEqual(p.read(self.run / 'paragraph_selection.json')['options_reviewed'], 1)

    def test_other_whole_before_excerpt_and_no_retry(self):
        expected = ['PQ0', 'PQ1', 'PQ2']
        for oid in expected:
            p.paragraph_jobs(self.run)
            self.assertEqual(self.current_options(), [oid])
            self.answer('paragraph', lambda i: False)
        self.assertTrue(inc.paragraph_progress(p, self.run)['complete'])
        p.select_paragraphs(self.run)
        self.assertEqual(p.rows(self.run / 'paragraphs.jsonl'), [])

    def test_pending_result_blocks_a_new_round(self):
        p.paragraph_jobs(self.run)
        with self.assertRaisesRegex(ValueError, 'Missing real agent result'):
            p.paragraph_jobs(self.run)
        self.assertEqual(len(list((self.run / 'jobs/paragraph').glob('*.json'))), 1)

    def test_next_tracks_rounds_until_paragraph_export(self):
        (self.run / 'jobs/boundary').mkdir(parents=True)
        self.assertEqual(inc.next_step(p, self.run)['stage'], 'paragraph-jobs')
        p.paragraph_jobs(self.run)
        self.assertEqual(inc.next_step(p, self.run)['action'], 'agent_review')
        self.answer('paragraph', lambda i: False)
        self.assertEqual(inc.next_step(p, self.run)['stage'], 'paragraph-jobs')
        p.paragraph_jobs(self.run)
        self.answer('paragraph', lambda i: True)
        self.assertEqual(inc.next_step(p, self.run)['stage'], 'select-paragraphs')
        p.select_paragraphs(self.run)
        self.assertEqual(inc.next_step(p, self.run)['stage'], 'sentence-jobs')

    def test_cannot_export_while_unreviewed_candidates_remain(self):
        p.paragraph_jobs(self.run)
        self.answer('paragraph', lambda i: False)
        with self.assertRaisesRegex(AssertionError, 'More paragraph candidates remain'):
            p.select_paragraphs(self.run)

    def test_duplicate_hit_does_not_close_other_parent(self):
        other = {**self.parent, 'candidate_id': 'CP00002', 'unit_id': 'u2'}
        p.write_rows(self.run / 'remaining_passages.jsonl', [self.parent, other])
        values = p.rows(self.run / 'paragraph_options.jsonl')
        values += [{**v, 'option_id': v['option_id'] + 'B', 'passage_id': 'CP00002', 'unit_id': 'u2'} for v in values[:2]]
        p.write_rows(self.run / 'paragraph_options.jsonl', values)
        p.paragraph_jobs(self.run)
        self.answer('paragraph', lambda i: True)
        state = inc.paragraph_progress(p, self.run)
        self.assertEqual(len(state['chosen']), 1)
        self.assertEqual(state['duplicates'], ['PQ0B'])
        p.paragraph_jobs(self.run)
        self.assertEqual(self.current_options(), ['PQ1B'])


class SentenceBatchTests(IncrementalFixture):
    def prepare_source(self, n=500):
        source = '\n\n'.join(f'Synthetic statement number {i:04d}.' for i in range(n))
        p.write_rows(self.run / 'chapters.jsonl', [target(source)])
        p.write_rows(self.run / 'paragraphs.jsonl', [])

    def dispatched_ids(self):
        return [s['candidate_id'] for f in sorted((self.run / 'jobs/sentence').glob('*.json'))
                for g in p.read(f)['groups'] for s in g['segments'] if s['candidate_id'] is not None]

    def test_first_batch_300_then_refill_only_gap_and_stop(self):
        self.prepare_source()
        p.sentence_jobs(self.run)
        first = self.dispatched_ids()
        self.assertEqual(len(first), 300)
        self.assertGreater(int(first[-1][2:]), 490)  # Source-wide spread, not just its beginning.
        self.answer('sentence', lambda i: i < 180)
        self.assertFalse(inc.sentence_progress(p, self.run)['complete'])
        p.sentence_jobs(self.run)
        all_ids = self.dispatched_ids()
        self.assertEqual(len(all_ids), 334)
        self.assertEqual(len(set(all_ids)), 334)
        self.answer('sentence', lambda i: i < 20)
        state = inc.sentence_progress(p, self.run)
        self.assertEqual(len(state['chosen']), 200)
        self.assertTrue(state['complete'])
        p.sentence_jobs(self.run)
        self.assertEqual(self.dispatched_ids(), all_ids)

    def test_enough_first_round_does_not_review_remaining_pool(self):
        self.prepare_source()
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: True)
        self.assertTrue(inc.sentence_progress(p, self.run)['complete'])
        p.sentence_jobs(self.run)
        self.assertEqual(len(self.dispatched_ids()), 300)

    def test_all_rejected_exhausts_pool_without_repeating(self):
        self.prepare_source()
        for expected in (300, 400, 500):
            p.sentence_jobs(self.run)
            self.assertEqual(len(self.dispatched_ids()), expected)
            self.answer('sentence', lambda i: False)
        state = inc.sentence_progress(p, self.run)
        self.assertTrue(state['complete'])
        self.assertEqual(len(state['chosen']), 0)
        self.assertEqual(len(set(self.dispatched_ids())), 500)

    def test_short_pool_retained_without_lowering_threshold(self):
        self.prepare_source(4)
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: i < 3)
        state = inc.sentence_progress(p, self.run)
        self.assertTrue(state['complete'])
        self.assertEqual(len(state['chosen']), 3)

    def test_same_paragraph_can_supply_different_sentences_in_later_round(self):
        source = ' '.join(f'Synthetic statement number {i:04d}.' for i in range(5))
        p.write_rows(self.run / 'chapters.jsonl', [target(source)])
        p.write_rows(self.run / 'paragraphs.jsonl', [])
        self.protocol['sentence_target'] = 2
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: i == 0)
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: True)
        state = inc.sentence_progress(p, self.run)
        self.assertTrue(state['complete'])
        self.assertEqual(state['reviewed'], 5)
        self.assertEqual(len(state['chosen']), 3)
        self.assertTrue(all(r['paragraph'] == source for r in state['chosen']))
        self.assertEqual(len(set(self.dispatched_ids())), 5)

    def test_duplicate_approvals_do_not_count_towards_200(self):
        texts = ['Repeated synthetic claim.'] * 400 + [f'Distinct synthetic claim number {i:04d}.' for i in range(100)]
        p.write_rows(self.run / 'chapters.jsonl', [target('\n\n'.join(texts))])
        p.write_rows(self.run / 'paragraphs.jsonl', [])
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: True)
        state = inc.sentence_progress(p, self.run)
        self.assertEqual(state['reviewed'], 300)
        self.assertLess(len(state['chosen']), 200)
        self.assertFalse(state['complete'])
        self.assertGreater(len(state['duplicates']), 0)
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: True)
        state = inc.sentence_progress(p, self.run)
        self.assertTrue(state['complete'])
        self.assertEqual(len(state['chosen']), 101)

    def test_finalize_blocks_if_target_not_met_and_pool_not_exhausted(self):
        self.prepare_source()
        p.sentence_jobs(self.run)
        self.answer('sentence', lambda i: i < 10)
        with self.assertRaisesRegex(AssertionError, 'Sentence target not met'):
            p.finalize(self.run)

    def test_unsubmitted_sentence_cannot_be_selected_as_context(self):
        # Two sentences per natural paragraph let a later round reuse the same background.
        self.prepare_source(2)
        self.protocol['sentence_target'] = 1
        p.sentence_jobs(self.run)
        path = next((self.run / 'jobs/sentence').glob('*.json'))
        job = p.read(path)
        self.answer('sentence', lambda i: True)
        response = p.read(job['output_path'])
        response['results'][0]['selected_ids'].append('CS999999')
        p.write(job['output_path'], response)
        with self.assertRaises(AssertionError):
            inc.sentence_progress(p, self.run)
