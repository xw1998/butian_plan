"""Portable tool source bundles; no member database, execution history or grants."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

FORMAT = 'butian-tool-bundle-v1'
MAX_BYTES = 16 * 1024 * 1024
MANIFEST_FIELDS = {'input_contract', 'output_contract', 'permissions', 'failure_conditions', 'entrypoint', 'runtime', 'requires', 'environment', 'dependencies', 'license', 'rollback', 'impact_scope', 'write_targets', 'isolation', 'host_capabilities', 'business_assertions', 'risk_level', 'environment_lock', 'environment_matrix'}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def export_bundle(tools, owner: str, tool_id: str) -> dict[str, Any]:
    with tools.store._write_lock:
        tool = tools._tool(tool_id)
        if tool['owner_member_id'] != owner:
            raise ValueError('tool_access_denied')
        versions = []
        total = 0
        for row in tools.store.db.execute('SELECT * FROM tool_versions WHERE tool_id=? ORDER BY version', (tool_id,)):
            source = Path(row['source_path'])
            root = (tools._member_dir(owner) / tool_id / f"v{row['version']}").resolve()
            if source.is_symlink() or not source.resolve().is_relative_to(root):
                raise ValueError('tool_source_path_invalid')
            if source.stat().st_size > MAX_BYTES - total:
                raise ValueError('tool_bundle_size_limit')
            raw = source.read_bytes()
            total += len(raw)
            if total > MAX_BYTES:
                raise ValueError('tool_bundle_size_limit')
            if hashlib.sha256(raw).hexdigest() != row['content_hash']:
                raise ValueError('tool_bundle_source_checksum_mismatch')
            manifest = json.loads(row['manifest'])
            if digest({k: v for k, v in manifest.items() if k != 'manifest_hash'}) != manifest.get('manifest_hash'):
                raise ValueError('tool_bundle_manifest_checksum_mismatch')
            versions.append({'version': row['version'], 'content_base64': base64.b64encode(raw).decode('ascii'), 'content_sha256': row['content_hash'], 'manifest': {k: v for k, v in manifest.items() if k in MANIFEST_FIELDS}})
        if not versions:
            raise ValueError('tool_bundle_versions_required')
        bundle = {'format': FORMAT, 'name': tool['name'], 'description': tool['description'], 'versions': versions}
        bundle['bundle_sha256'] = digest(bundle)
        tools._event(tool_id, None, 'bundle_exported', {'bundle_sha256': bundle['bundle_sha256'], 'version_count': len(versions)}, 'tool-export:' + uuid.uuid4().hex)
        return bundle


def import_bundle(tools, owner: str, bundle: dict[str, Any], *, license: str, scan_status: str) -> dict[str, Any]:
    if not isinstance(license, str) or not license.strip() or scan_status != 'passed':
        raise ValueError('tool_supply_chain_unverified')
    if not isinstance(bundle, dict) or set(bundle) != {'format', 'name', 'description', 'versions', 'bundle_sha256'} or bundle.get('format') != FORMAT:
        raise ValueError('tool_bundle_format_invalid')
    if digest({k: v for k, v in bundle.items() if k != 'bundle_sha256'}) != bundle['bundle_sha256']:
        raise ValueError('tool_bundle_checksum_mismatch')
    if not all(isinstance(bundle[k], str) and bundle[k].strip() for k in ('name', 'description')):
        raise ValueError('tool_bundle_description_invalid')
    if not isinstance(bundle['versions'], list) or not 1 <= len(bundle['versions']) <= 1000:
        raise ValueError('tool_bundle_versions_invalid')
    validated = []
    total = 0
    for index, entry in enumerate(bundle['versions'], 1):
        if not isinstance(entry, dict) or set(entry) != {'version', 'content_base64', 'content_sha256', 'manifest'} or type(entry['version']) is not int or entry['version'] != index:
            raise ValueError('tool_bundle_version_invalid')
        if not isinstance(entry['content_base64'], str) or len(entry['content_base64']) > MAX_BYTES * 2:
            raise ValueError('tool_bundle_size_limit')
        try:
            raw = base64.b64decode(entry['content_base64'], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('tool_bundle_content_invalid') from exc
        total += len(raw)
        if not raw or total > MAX_BYTES:
            raise ValueError('tool_bundle_size_limit')
        if hashlib.sha256(raw).hexdigest() != entry['content_sha256']:
            raise ValueError('tool_bundle_content_checksum_mismatch')
        manifest = entry['manifest']
        if not isinstance(manifest, dict) or set(manifest) - MANIFEST_FIELDS:
            raise ValueError('tool_bundle_manifest_invalid')
        validated.append((raw, {**manifest, 'source_kind': 'imported', 'source': 'bundle-sha256:' + bundle['bundle_sha256'], 'license': license.strip(), 'scan_status': scan_status}))
    with tools.store._write_lock:
        savepoint = 'tool_import_' + uuid.uuid4().hex
        tools.store.db.execute('SAVEPOINT ' + savepoint)
        try:
            tools.store.db.execute('UPDATE tool_assets SET current_version=current_version WHERE 0')
            proposed = tools.propose(owner, bundle['name'], bundle['description'], reuse_lower=0, manual_cost_upper=0, call_cost_lower=0, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0)
            for index, (raw, manifest) in enumerate(validated, 1):
                # Migration preserves every version, including identical source revisions.
                manifest['migration_version'] = index
                tools.add_version(owner, proposed['tool_id'], raw, manifest)
            tools._event(proposed['tool_id'], None, 'bundle_imported', {'bundle_sha256': bundle['bundle_sha256'], 'requires_reverification': True}, 'tool-import:' + uuid.uuid4().hex)
            tools.store.db.execute('RELEASE ' + savepoint)
        except BaseException:
            tools.store.db.execute('ROLLBACK TO ' + savepoint)
            tools.store.db.execute('RELEASE ' + savepoint)
            raise
    return {'tool_id': proposed['tool_id'], 'version_count': len(validated), 'status': 'draft', 'requires_reverification': True, 'bundle_sha256': bundle['bundle_sha256']}
