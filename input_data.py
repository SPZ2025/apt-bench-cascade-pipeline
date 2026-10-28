"""Portable JSONL input adapter. Original text is never normalized."""
from pathlib import Path
import json

SOURCE_FILE = None


def load_units():
    if SOURCE_FILE is None:
        raise ValueError('Set a source JSONL using --source or an existing run.')
    result, documents = {}, {}
    for number, line in enumerate(Path(SOURCE_FILE).read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        fields = ('scholar_id', 'document_id', 'document_title', 'unit_id', 'unit_title', 'text')
        if any(not isinstance(row.get(k), str) or not row[k].strip() for k in fields):
            raise ValueError(f'Invalid source record on line {number}; required nonempty strings: {fields}')
        uid = row['unit_id']
        if uid in result:
            raise ValueError('Duplicate unit_id: ' + uid)
        doc = {'scholar_id': row['scholar_id'], 'document_id': row['document_id'], 'title': row['document_title']}
        if row['document_id'] in documents and documents[row['document_id']] != doc:
            raise ValueError('Inconsistent document metadata: ' + row['document_id'])
        documents[row['document_id']] = doc
        result[uid] = (doc, row)
    if not result:
        raise ValueError('Empty source file')
    return result


def import_bundle(folder, output, author=None):
    """Convert existing APT exports_json documents to the standalone input format."""
    files = sorted(Path(folder).glob('exports_json/*.json'))
    if not files:
        raise ValueError('No exports_json/*.json in the supplied bundle')
    result = []
    for path in files:
        doc = json.loads(path.read_text(encoding='utf-8-sig'))
        if author is not None and doc['scholar_id'] != author:
            continue
        for unit in doc['units']:
            result.append({**unit, 'scholar_id': doc['scholar_id'], 'document_id': doc['document_id'],
                           'document_title': doc['title']})
    if not result:
        raise ValueError('No matching author in bundle')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as f:
        for row in result:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return len(result)
