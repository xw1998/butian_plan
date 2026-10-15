"""Read-only CSV inspection preserving identifier text and leading zeroes."""
import csv
import io
import json
import sys


def run(data):
    text = data['csv_text']
    if not isinstance(text, str):
        raise ValueError('csv_text_must_be_string')
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError('empty_table')
    width = len(rows[0])
    mismatched = [i + 1 for i, row in enumerate(rows) if len(row) != width]
    protected = [{'row': i + 1, 'column': j + 1, 'value': value}
                 for i, row in enumerate(rows) for j, value in enumerate(row)
                 if value.isdigit() and (len(value) > 15 or (len(value) > 1 and value.startswith('0')))]
    formulas = [{'row': i + 1, 'column': j + 1} for i, row in enumerate(rows) for j, value in enumerate(row) if value.startswith(('=', '+', '-', '@'))]
    return {'exit_code': 0, 'rows': len(rows), 'columns': width, 'ragged_rows': mismatched,
            'keep_as_text': protected, 'formula_like_cells': formulas,
            'limit': 'No numeric coercion or formula execution; CSV does not preserve workbook features.'}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
    except (ValueError, KeyError, TypeError) as error:
        print(json.dumps({'exit_code': 1, 'error': str(error)}))
        raise SystemExit(1)
