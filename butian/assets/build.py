"""Rebuild the portable knowledge database exclusively from public package assets.

Usage: python -m butian.assets.build ABSOLUTE_NEW_SQLITE_PATH
This is an interchange database, not a user runtime or authentication store.
"""
from __future__ import annotations
import hashlib
import json
import sqlite3
import sys
from importlib.resources import files
from pathlib import Path
from butian.runtime.personas import PERSONAS


def build(path: Path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('absolute_destination_required')
    with path.open('xb'):
        pass
    source = files('butian.assets')
    catalog = json.loads(source.joinpath('catalog.json').read_text(encoding='utf-8'))
    tools = json.loads(source.joinpath('professional_tools.json').read_text(encoding='utf-8'))
    db = sqlite3.connect(path)
    try:
        db.executescript('''
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE personas(member_id TEXT PRIMARY KEY, configuration TEXT NOT NULL);
        CREATE TABLE knowledge(member_id TEXT NOT NULL, asset_id TEXT PRIMARY KEY, content TEXT NOT NULL, sources TEXT NOT NULL, learning_state TEXT NOT NULL);
        CREATE TABLE tools(asset_id TEXT PRIMARY KEY, owner TEXT NOT NULL, manifest TEXT NOT NULL, source BLOB NOT NULL, sha256 TEXT NOT NULL);
        ''')
        db.execute('INSERT INTO metadata VALUES (?,?)', ('format', 'butian-public-initial-knowledge-v1'))
        db.execute('INSERT INTO metadata VALUES (?,?)', ('learning_state', 'unpractised'))
        for persona in PERSONAS:
            db.execute('INSERT INTO personas VALUES (?,?)', (persona['member_id'], json.dumps(persona, ensure_ascii=False, sort_keys=True)))
        for owner, cards in sorted(catalog['members'].items()):
            for card in sorted(cards, key=lambda card: card['id']):
                db.execute('INSERT INTO knowledge VALUES (?,?,?,?,?)', (owner, card['id'], card['content'], json.dumps(card['sources'], ensure_ascii=False, sort_keys=True), 'unpractised'))
        for tool in sorted(tools, key=lambda item: item['id']):
            raw = source.joinpath(tool['file']).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != tool['wrapper_sha256']:
                raise ValueError('initial_tool_hash_mismatch:' + tool['id'])
            db.execute('INSERT INTO tools VALUES (?,?,?,?,?)', (tool['id'], tool['owner'], json.dumps(tool, ensure_ascii=False, sort_keys=True), raw, digest))
        raw = source.joinpath('tool.py').read_bytes()
        for owner in sorted(catalog['members']):
            db.execute('INSERT INTO tools VALUES (?,?,?,?,?)', (owner + ':foundation', owner, json.dumps({'learning_state': 'unpractised', 'permissions': ['execute']}), raw, hashlib.sha256(raw).hexdigest()))
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('initial_database_integrity_failed')
        return {'personas': db.execute('SELECT COUNT(*) FROM personas').fetchone()[0], 'knowledge': db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0], 'tools': db.execute('SELECT COUNT(*) FROM tools').fetchone()[0]}
    finally:
        db.close()


if __name__ == '__main__':
    print(json.dumps(build(Path(sys.argv[1]))))
