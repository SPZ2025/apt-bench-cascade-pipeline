"""Synthetic fixtures test mechanics only; they are never real model approvals."""
from pathlib import Path
from tempfile import TemporaryDirectory as SystemTemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline as p
import input_data


def TemporaryDirectory():
    # Keep test writes and automatic cleanup inside the checked-out project.
    root = Path(__file__).resolve().parents[1] / '.test_tmp'
    root.mkdir(exist_ok=True)
    return SystemTemporaryDirectory(dir=root)


def target(text, start=0, candidate='CP00001', unit='u1'):
    return {'candidate_id': candidate, 'scholar_id': 'a', 'document_id': 'd', 'unit_id': unit,
            'chapter_id': 'c', 'chapter_title': 'Chapter', 'text': text, 'token_count': len(text.split()),
            'start': start, 'end': start + len(text), 'clean_start': start, 'clean_end': start + len(text),
            'source_spans': [[start, start + len(text)]]}


class SelectionPolicyTests(TestCase):
    def test_all_passing_passages_retained_even_above_half(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            values = [target('Distinct argument ' + str(i), candidate=f'CP{i}', unit=f'u{i}') for i in range(4)]
            p.write_rows(run / 'passage_partition.jsonl', values)
            reviews = {r['candidate_id']: {'decision': 'keep' if i < 3 else 'drop'} for i, r in enumerate(values)}
            with patch.object(p, 'stage_results', return_value=reviews):
                p.select_passages(run)
            self.assertEqual(len(p.rows(run / 'passages.jsonl')), 3)
            self.assertEqual(p.rows(run / 'remaining_passages.jsonl')[0]['candidate_id'], 'CP3')

    def test_need_more_is_not_a_passing_passage(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            p.write_rows(run / 'passage_partition.jsonl', [target('Needs context')])
            with patch.object(p, 'stage_results', return_value={'CP00001': {'decision': 'need_more'}}):
                p.select_passages(run)
            self.assertEqual(p.rows(run / 'passages.jsonl'), [])
            self.assertEqual(len(p.rows(run / 'remaining_passages.jsonl')), 1)

    def test_duplicate_passing_passages_not_silently_dropped(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            values = [target('Same text', candidate=f'CP{i}', unit=f'u{i}') for i in range(2)]
            p.write_rows(run / 'passage_partition.jsonl', values)
            with patch.object(p, 'stage_results', return_value={f'CP{i}': {'decision': 'keep'} for i in range(2)}):
                with self.assertRaisesRegex(AssertionError, 'Passing passages conflict'):
                    p.select_passages(run)

    def test_uniform_200_and_shortfall(self):
        values = [target(str(i), start=i, candidate=f'CS{i:06}') for i in range(400)]
        selected = p.choose_sentences(list(reversed(values)), 200)
        self.assertEqual(len(selected), 200)
        self.assertEqual([r['start'] for r in selected], list(range(1, 400, 2)))
        self.assertEqual(p.choose_sentences(values[:7], 200), values[:7])
        self.assertEqual(p.choose_sentences([], 200), [])

    def test_paragraph_parent_must_not_have_passed(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            parent = target('Some text') | {'boundary_review': {'decision': 'keep'}}
            p.write_rows(run / 'remaining_passages.jsonl', [parent])
            with patch.object(p, 'stage_results', return_value={}):
                with self.assertRaises(AssertionError):
                    p.select_paragraphs(run)


class ReviewValidationTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name)
        text = ' '.join(f'word{i}' for i in range(110)) + '.'
        self.parent = target(text)
        options = []
        for i in range(2):
            options.append({**self.parent, 'option_id': f'PQ{i}', 'passage_id': 'CP00001',
                            'block_id': 'NB1', 'complete_natural_paragraph': True})
        p.write_rows(self.run / 'paragraph_options.jsonl', options)
        p.write_rows(self.run / 'remaining_passages.jsonl', [self.parent])
        group = {'candidate_id': 'CP00001', 'context_blocks': [{'block_id': 'NB1', 'separator_before': '',
            'segments': [{'sentence_id': 'CS1', 'text': text}],
            'options': [{'option_id': o['option_id'], 'kind': 'whole', 'token_count': o['token_count']} for o in options]}],
            'trailing_separator': ''}
        self.protocol = {'prompts': {'paragraph': 'test-prompt'}}
        group['input_hash'] = p.digest({'stage': 'paragraph', 'prompt': 'test-prompt', 'input': group})
        self.group = group
        self.output = self.run / 'results/paragraph/paragraph_001.json'
        p.write(self.run / 'jobs/paragraph/paragraph_001.json', {'job_id': 'paragraph_001', 'prompt_sha256': 'test-prompt',
            'groups': [group], 'output_path': str(self.output)})

    def response(self, selections):
        p.write(self.output, {'results': [{'candidate_id': 'CP00001', 'input_hash': self.group['input_hash'],
            'selections': [{'option_id': s, 'reason': 'Synthetic fixture'} for s in selections], 'reason': 'Synthetic fixture'}]})

    def validate(self):
        with patch.object(p, 'verify_protocol', return_value=self.protocol), patch.object(p, 'count', side_effect=lambda t: len(t.split())):
            return p.stage_results(self.run, 'paragraph')

    def test_zero_or_one_is_accepted(self):
        for selected in ([], ['PQ0']):
            self.response(selected)
            self.assertEqual(len(self.validate()['CP00001']['selections']), len(selected))

    def test_two_is_rejected_before_export(self):
        self.response(['PQ0', 'PQ1'])
        with self.assertRaisesRegex(AssertionError, 'At most one paragraph'):
            self.validate()

    def test_unknown_option_is_rejected(self):
        self.response(['invented'])
        with self.assertRaises(AssertionError):
            self.validate()

    def test_stale_hash_is_rejected(self):
        self.response(['PQ0'])
        obj = p.read(self.output)
        obj['results'][0]['input_hash'] = 'stale'
        p.write(self.output, obj)
        with self.assertRaises(AssertionError):
            self.validate()

    def test_changed_text_is_rejected(self):
        self.response(['PQ0'])
        options = p.rows(self.run / 'paragraph_options.jsonl')
        options[0]['text'] = 'Changed source text'
        p.write_rows(self.run / 'paragraph_options.jsonl', options)
        with self.assertRaises(AssertionError):
            self.validate()


class SourceAndRemainderTests(TestCase):
    def test_finalize_exports_200_actual_source_sentences(self):
        # This is a synthetic mechanics fixture, not model-generated quality evidence.
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            pieces = [f'Synthetic fixture statement number {i:03d}.' for i in range(250)]
            source = ' '.join(pieces)
            candidates, cursor = [], 0
            for i, text in enumerate(pieces):
                r = target(text, cursor, candidate=f'CS{i:06}')
                r.update(paragraph_id='NB1', paragraph=source, paragraph_source_spans=[[0, len(source)]])
                candidates.append(r)
                cursor += len(text) + 1
            p.write_rows(run / 'sentence_candidates.jsonl', candidates)
            for name in ('passages', 'paragraphs', 'remaining_passages', 'split_status'):
                p.write_rows(run / f'{name}.jsonl', [])
            p.write_rows(run / 'chapters.jsonl', [{'unit_id': 'u1'}])
            p.write(run / 'passage_selection.json', {'N': 0, 'K': 0})
            p.write(run / 'paragraph_selection.json', {'whole_natural_paragraphs': 0, 'preferred_100_200': 0})
            p.write(run / 'protocol.json', {'fixture': True})
            review = {'selected_ids': [r['candidate_id'] for r in candidates], 'job_id': 'fixture',
                      'input_hash': 'fixture', 'prompt_sha256': 'fixture', 'reviewer': 'synthetic_fixture', 'reason': 'Synthetic only'}
            doc = {'scholar_id': 'a', 'document_id': 'd', 'title': 'Synthetic'}
            unit = {'unit_id': 'u1', 'unit_title': 'Synthetic chapter', 'text': source}
            with patch.object(p, 'stage_results', side_effect=lambda run, stage: {'NB1': review} if stage == 'sentence' else {}), \
                    patch.object(p, 'verify_protocol', return_value={'sentence_target': 200, 'author': 'a'}), \
                    patch.object(p, 'load_units', return_value={'u1': (doc, unit)}):
                p.finalize(run)
            self.assertEqual(len(p.rows(run / 'sentences.jsonl')), 200)
            self.assertEqual(p.read(run / 'sentence_selection.json')['shortfall'], 0)
            self.assertEqual(p.read(run / 'verification.json')['target_conflicts'], 0)
            from zipfile import ZipFile
            with ZipFile(run / 'cascade_dataset.zip') as z:
                self.assertIsNone(z.testzip())
                self.assertIn('source_units.jsonl', z.namelist())

    def test_sentence_targets_exclude_occupied_text_but_keep_full_background(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            source = 'Library access expands. Lending rules explain returns. Readers share resources.'
            spans = p.sentence_ranges(source)
            chapter = target(source)
            chapter['source_spans'] = [[0, len(source)]]
            p.write_rows(run / 'chapters.jsonl', [chapter])
            occupied = target(source[:spans[0][1]])
            paragraph = target(source[spans[1][0]:spans[1][1]], start=spans[1][0])
            p.write_rows(run / 'passages.jsonl', [occupied])
            p.write_rows(run / 'paragraphs.jsonl', [paragraph])
            with patch.object(p, 'count', side_effect=lambda t: len(t.split())), patch.object(p, 'make_jobs'):
                p.sentence_jobs(run)
            candidates = p.rows(run / 'sentence_candidates.jsonl')
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]['text'], 'Readers share resources.')
            self.assertEqual(candidates[0]['paragraph'], source)
            self.assertEqual(p.restore(source, candidates[0]['source_spans']), candidates[0]['text'])

    def test_mapping_across_removed_heading_and_adjacent_intervals(self):
        source = 'abcHEADINGdef'
        spans = [[0, 3], [10, 13]]
        self.assertEqual(p.restore(source, p.map_spans(spans, 1, 5)), 'bcde')
        self.assertFalse(p.overlap([[0, 3]], [[3, 6]]))

    def test_abbreviations_decimals_and_chinese_quotes(self):
        source = 'Mr. Keynes paid 2.5 pounds. This is the next sentence.'
        self.assertEqual(len(p.sentence_ranges(source)), 2)
        source = '作者说“规则有效。”随后说明理由。'
        ss = p.sentence_ranges(source, True)
        self.assertEqual([source[a:b] for a, b in ss], ['作者说“规则有效。”', '随后说明理由。'])

    def test_input_duplicate_units_rejected(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.jsonl'
            row = {'scholar_id': 'a', 'document_id': 'd', 'document_title': 'Title', 'unit_id': 'u', 'unit_title': 'Chapter', 'text': 'Original.'}
            p.write_rows(source, [row, row])
            with patch.object(input_data, 'SOURCE_FILE', source):
                with self.assertRaisesRegex(ValueError, 'Duplicate unit_id'):
                    input_data.load_units()


if __name__ == '__main__':
    main()
