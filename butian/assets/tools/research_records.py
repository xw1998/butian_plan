"""Offline bibliography normalization; no invented metadata or network access."""
import json
import sys
import unicodedata


def run(data):
    records = data.get('records')
    if not isinstance(records, list):
        raise ValueError('records_must_be_list')
    seen, unique, duplicates, incomplete = {}, [], [], []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError('record_must_be_object')
        title = record.get('title', '')
        doi = record.get('doi', '')
        if not isinstance(title, str) or not isinstance(doi, str):
            raise ValueError('title_and_doi_must_be_strings')
        doi = doi.strip().lower().removeprefix('https://doi.org/').removeprefix('doi:').strip()
        if doi and not doi.startswith('10.'):
            raise ValueError('invalid_doi_prefix')
        if not title.strip():
            incomplete.append(index)
        # Only a shared DOI is sufficient for automatic duplicate removal.
        if doi and doi in seen:
            duplicates.append({'index': index, 'same_doi_as': seen[doi]})
        else:
            if doi:
                seen[doi] = index
            unique.append({**record, 'doi': doi, 'title': ' '.join(unicodedata.normalize('NFKC', title).split())})
    return {'exit_code': 0, 'unique': unique, 'duplicates': duplicates, 'missing_title': incomplete,
            'limit': 'Offline normalization only; DOI existence, citation claims and title matches are not verified.'}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
    except (ValueError, TypeError, AttributeError) as error:
        print(json.dumps({'exit_code': 1, 'error': str(error)}))
        raise SystemExit(1)
