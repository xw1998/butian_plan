"""Read-only PDF metadata and text inspection, learned from PDF tool workflows."""
import json
import sys
from pathlib import Path


def run(data):
    import pdfplumber
    path = Path(data['path'])
    if not path.is_file():
        raise ValueError('pdf_file_not_found')
    with pdfplumber.open(path) as document:
        pages = data.get('pages', list(range(1, len(document.pages) + 1)))
        if not isinstance(pages, list) or not pages or any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= len(document.pages) for p in pages):
            raise ValueError('invalid_pdf_page_numbers')
        result = []
        for number in pages:
            page = document.pages[number - 1]
            text = page.extract_text() or ''
            result.append({'page': number, 'width': page.width, 'height': page.height,
                           'text': text, 'text_layer_empty': not bool(text.strip())})
        return {'exit_code': 0, 'page_count': len(document.pages), 'pages': result,
                'limit': 'Empty text layer requires visual/OCR review; extraction order is not layout proof.'}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'exit_code': 1, 'error': type(error).__name__ + ': ' + str(error)}))
        raise SystemExit(1)
