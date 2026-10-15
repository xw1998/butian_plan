"""Offline HTML structure review; browser interaction remains a host capability."""
import json
import sys
from html.parser import HTMLParser


class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images_missing_alt = 0
        self.ids = set()
        self.duplicate_ids = []
        self.inputs_missing_names = 0
        self.lang = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        identity = values.get('id')
        if identity:
            if identity in self.ids:
                self.duplicate_ids.append(identity)
            self.ids.add(identity)
        if tag == 'html':
            self.lang = values.get('lang')
        if tag == 'img' and 'alt' not in values:
            self.images_missing_alt += 1
        if tag == 'input' and values.get('type') != 'hidden' and not any(values.get(k) for k in ('aria-label', 'aria-labelledby', 'id')):
            self.inputs_missing_names += 1


def run(data):
    if not isinstance(data.get('html'), str):
        raise ValueError('html_must_be_string')
    audit = Audit()
    audit.feed(data['html'])
    return {'exit_code': 0, 'missing_language': not bool(audit.lang),
            'images_missing_alt': audit.images_missing_alt, 'duplicate_ids': audit.duplicate_ids,
            'inputs_needing_label_review': audit.inputs_missing_names,
            'limit': 'Static hints only; does not prove accessible names, rendering, contrast or working interactions.'}


if __name__ == '__main__':
    try:
        print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
    except (ValueError, TypeError) as error:
        print(json.dumps({'exit_code': 1, 'error': str(error)}))
        raise SystemExit(1)
