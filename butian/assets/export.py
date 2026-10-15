"""Build initial databases in a new directory, never export a user's live store.

Usage: python -m butian.assets.export ABSOLUTE_NEW_DIRECTORY
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from butian.runtime.service import ButianService


def build_initial_database(destination: Path):
    destination = Path(destination)
    if not destination.is_absolute():
        raise ValueError('absolute_destination_required')
    destination.mkdir(parents=True, exist_ok=False)
    previous = os.environ.get('BUTIAN_MAINTENANCE_ENABLED')
    os.environ['BUTIAN_MAINTENANCE_ENABLED'] = '0'
    service = None
    try:
        service = ButianService(destination)
        summary = {'format': 'butian-initial-database-v1', 'members': [],
                   'initial_assets': service.initial_assets, 'learning_state': 'unpractised',
                   'contains_user_runtime': False}
        for row in service.store.db.execute('SELECT member_id FROM members ORDER BY member_id'):
            memory = service.memory(row[0])
            try:
                assert memory.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert memory.db.execute('SELECT COUNT(*) FROM practice_observations').fetchone()[0] == 0
                summary['members'].append({'member_id': row[0], 'memory_count': memory.db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]})
            finally:
                memory.close()
        (destination / 'initial-assets.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return summary
    finally:
        if service is not None:
            service.close()
        if previous is None:
            os.environ.pop('BUTIAN_MAINTENANCE_ENABLED', None)
        else:
            os.environ['BUTIAN_MAINTENANCE_ENABLED'] = previous


if __name__ == '__main__':
    print(json.dumps(build_initial_database(Path(sys.argv[1])), ensure_ascii=False))
