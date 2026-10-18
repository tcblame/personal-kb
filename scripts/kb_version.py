"""Read-only identity of the actual entry point and canonical storage layout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from kb_lib import skill_root_dir, storage_layout
from kb_release import _public_replacements


def build_identity() -> dict:
    root = skill_root_dir()
    digest = hashlib.sha256()
    portable = hashlib.sha256()
    for directory in ('scripts', 'backend'):
        for path in sorted((root / directory).glob('*.py')):
            if path.name.endswith('_test.py') or path.name.startswith('test_'):
                continue
            digest.update(str(path.relative_to(root)).encode())
            source = path.read_text(encoding='utf-8').replace('\r\n', '\n')
            digest.update(source.encode())
            portable.update(str(path.relative_to(root)).encode())
            portable.update(_public_replacements(source).encode())
    skill = root / 'SKILL.md'
    domain_model = root / 'references' / 'domain-model.json'
    return {'schema': 'personal-kb.build/v1', 'code_sha256': digest.hexdigest(),
            'portable_code_sha256': portable.hexdigest(),
            'entrypoint': str(root / 'scripts' / 'kb.py'),
            'skill_sha256': hashlib.sha256(skill.read_bytes()).hexdigest() if skill.exists() else None,
            'domain_model_sha256': hashlib.sha256(domain_model.read_bytes()).hexdigest() if domain_model.exists() else None,
            'config_path': str(Path(os.environ.get('PERSONAL_KB_CONFIG') or root / 'config.json')),
            'data_root': str(storage_layout().root),
            'capabilities': ['session-topic-identity', 'scope-anchor', 'exclusive-receipt',
                             'unresolved-attempt-closeout', 'own-rollout-identity',
                             'domain-model-v1', 'project-graph-retrieval', 'scoped-supersession']}


def main(argv: list[str]) -> int:
    if argv:
        raise ValueError('version accepts no arguments')
    print(json.dumps(build_identity(), ensure_ascii=False, indent=2))
    return 0
