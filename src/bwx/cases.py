"""Case model: resolve a case, its image, and its trajectory into a run spec.

A "case" is one SWE-bench instance: a podman image plus a recorded trajectory.
This module is the single place that turns a case id into everything a VM run
needs, so the converters and the replay drivers share one definition.
"""
import json
from pathlib import Path

from . import convert
from .util import Error, read_json, write_json


class Case:
    """One instance: identity + converted image + trajectory steps."""

    def __init__(self, instance_id, base_record, steps, trajectory_sha, dataset_row=None):
        self.instance_id = instance_id
        self.base = base_record
        self.steps = steps
        self.trajectory_sha = trajectory_sha
        self.row = dataset_row or {}

    @property
    def workdir(self):
        return self.base.get('working_dir') or self.row.get('workdir') or '/testbed'

    @property
    def language(self):
        return self.base.get('language') or self.row.get('language') or 'unclassified'

    @property
    def image_path(self):
        return Path(self.base['path'])

    @property
    def image_sha(self):
        return self.base.get('sha256')

    @property
    def step_count(self):
        return len(self.steps)

    @property
    def delay_sum_s(self):
        return round(sum(float(s.get('delay_time') or 0) for s in self.steps), 3)

    def as_dict(self):
        return {'instance_id': self.instance_id, 'language': self.language,
                'workdir': self.workdir, 'steps': self.step_count,
                'delay_sum_s': self.delay_sum_s, 'image_path': str(self.image_path),
                'image_sha256': self.image_sha, 'trajectory_sha256': self.trajectory_sha}


def load_case(config, instance_id, *, require_image=True):
    """Load a converted case. Raises if not converted and require_image is set."""
    workspace = config.case_workspace(instance_id)
    record_path = workspace / 'base-image.json'
    if not record_path.is_file():
        if require_image:
            raise Error('case %s is not converted; run: bwx convert --case %s'
                        % (instance_id, instance_id))
        record = {}
    else:
        record = read_json(record_path)

    replay_path = convert.find_replay(config.dataset, instance_id)
    if replay_path is None:
        raise Error('no trajectory for %s under %s' % (instance_id, config.dataset))
    steps, raw, sha = convert.load_trajectory(replay_path)

    row = convert.read_manifest(config.dataset).get(instance_id, {})
    case = Case(instance_id, record, steps, sha, row)
    if require_image:
        image = Path(record.get('path', ''))
        if not record.get('status') == 'converted' or not image.is_file():
            raise Error('case %s image missing or not converted: %s'
                        % (instance_id, image))
    return case


def parse_case_file(path):
    """Read a case selection file: .txt or .json, shared by every command.

    Accepted formats:

    * text: one instance_id per line, `#` starts a comment, blank lines skipped
    * JSON array: ``["id-a", "id-b"]``
    * JSON object with a list: ``{"cases": ["id-a", "id-b"]}``
    * JSON object of mixes (consumed by ``alloc``): ``{"mix-name": {"id-a":
      {"role": "hungry", "weight": 3}}}`` -- values may be null (role mixed,
      weight 1)

    Returns ``{'ids': [...], 'mixes': {name: [entries]} or None}``. Callers that
    only need ids (convert/sweep/replay-listing) ignore ``mixes``; ``alloc``
    uses ``mixes`` when present and builds a default mix from ``ids`` otherwise.
    """
    import json as _json

    path = Path(path)
    if not path.is_file():
        raise Error('case file not found: %s' % path)
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.lower() == '.json' or text.lstrip().startswith(('{', '[')):
        try:
            doc = _json.loads(text)
        except ValueError as exc:
            raise Error('invalid JSON in %s: %s' % (path, exc))
        return _normalize_json_selection(doc, path)

    ids = []
    for line in text.splitlines():
        token = line.split('#', 1)[0].strip()
        if token:
            ids.append(token)
    return {'ids': _dedupe(ids), 'mixes': None}


def _normalize_json_selection(doc, path):
    if isinstance(doc, list):
        if any(not isinstance(item, str) or not item.strip() for item in doc):
            raise Error('%s: JSON array must contain case id strings' % path)
        return {'ids': _dedupe([item.strip() for item in doc]), 'mixes': None}
    if isinstance(doc, dict):
        if 'cases' in doc and isinstance(doc['cases'], list):
            inner = doc['cases']
            if any(not isinstance(item, str) or not item.strip() for item in inner):
                raise Error('%s: "cases" must be a list of case id strings' % path)
            return {'ids': _dedupe([item.strip() for item in inner]), 'mixes': None}
        # Structured mixes: {mix: {case: {role, weight}}}
        mixes, ids = {}, []
        for mix_name, entries in doc.items():
            if not isinstance(entries, dict):
                raise Error('%s: mix %r must be an object of case entries' % (path, mix_name))
            items = []
            for case_id, meta in entries.items():
                meta = meta if isinstance(meta, dict) else {}
                try:
                    weight = float(meta.get('weight', 1))
                except (TypeError, ValueError):
                    weight = 1.0
                items.append({'case_id': str(case_id), 'role': str(meta.get('role', 'mixed')),
                              'weight': weight})
                ids.append(str(case_id))
            mixes[str(mix_name)] = items
        return {'ids': _dedupe(ids), 'mixes': mixes}
    raise Error('%s: unsupported case-file JSON shape (use an array, {"cases": [...]}, '
                'or {"mix": {"case": {...}}})' % path)


def _dedupe(ids):
    seen, ordered = set(), []
    for iid in ids:
        if iid not in seen:
            seen.add(iid)
            ordered.append(iid)
    return ordered


def expand_cases(ids, count):
    """Expand a case list to exactly `count` VM entries.

    The contract for concurrent replay: fewer entries than concurrency are
    replicated round-robin (same case runs in several VMs), more are truncated
    to the first `concurrency` entries. Order is preserved and deterministic.
    """
    if not ids:
        raise Error('no case given')
    if count < 1:
        raise Error('concurrency must be >= 1, got %d' % count)
    return [ids[index % len(ids)] for index in range(count)]


def select_cases(config, *, cases=None, case_file=None, all_converted=False,
                 language=None, require_image=True):
    """Resolve a case selection from CLI-style inputs, preserving order, deduped."""
    ids = []
    if cases:
        ids.extend(c.strip() for c in cases.split(',') if c.strip())
    if case_file:
        ids.extend(parse_case_file(case_file)['ids'])
    if all_converted:
        for workspace in sorted(config.cases_dir.glob('*')):
            record = workspace / 'base-image.json'
            if record.is_file() and read_json(record, {}).get('status') == 'converted':
                ids.append(workspace.name)
    # Deduplicate preserving order.
    seen, ordered = set(), []
    for iid in ids:
        if iid not in seen:
            seen.add(iid)
            ordered.append(iid)
    if not ordered:
        raise Error('no case selected (use --case/--case-file/--all)')
    resolved = [load_case(config, iid, require_image=require_image) for iid in ordered]
    if language:
        resolved = [c for c in resolved if c.language.lower() == language.lower()]
        if not resolved:
            raise Error('no converted case matches language %r' % language)
    return resolved


def catalog(config):
    """Every converted case, for `bwx list`."""
    out = []
    for workspace in sorted(config.cases_dir.glob('*')) if config.cases_dir.is_dir() else []:
        record_path = workspace / 'base-image.json'
        if not record_path.is_file():
            continue
        record = read_json(record_path, {})
        if record.get('status') != 'converted':
            continue
        replay = convert.find_replay(config.dataset, record.get('instance_id', workspace.name))
        steps = record.get('trajectory', {}).get('steps')
        out.append({'instance_id': record.get('instance_id', workspace.name),
                    'language': record.get('language', ''),
                    'image_bytes': record.get('bytes'),
                    'sha256': record.get('sha256', '')[:12],
                    'steps': steps, 'trajectory': str(replay) if replay else None})
    return out
