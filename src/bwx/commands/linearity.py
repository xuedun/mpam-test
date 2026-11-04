"""`bwx linearity` -- concurrency-scaling sweep over `bwx replay` (fill mode).

Measures how each case's execution time grows with concurrency. Two nested
loops, both driven entirely by inputs (there is no built-in concurrency
ceiling; the CPU budget is the caller's responsibility via --cpu-start /
--cpu-step, and a pool that is too small or straddles NUMA nodes fails fast
in the preflight):

    for level in levels (ascending):
        shuffle the case list once (seed derived from the run id and the
        level, logged under n<level>/seed.txt -- reproducible; disable with
        --no-shuffle to group in the given order instead)
        slice the shuffled list into consecutive groups of `level` cases
        for each group (serially):
            bwx replay --cases <group> --concurrency <level> --fill

Serial capture is forwarded per group via --console off|markers|full
(default: the config's setting, normally `markers`): the protocol lines
(BOOT/READY/STEP/CASE_DONE) land in n<level>/g<idx>/real/vm-*/console-*.log
so execution success is checkable by eye; --console off drops them.

Tail groups: when the last group has fewer cases than `level`, replay's
expand_cases round-robin replicates it to exactly `level` real VMs, so every
group runs at full level pressure and tail-group samples stay valid (the CSV
cell then averages multiple samples).

Resumable: a group is marked with `.done` after processing, whatever the
outcome (vm-failed / error / timeout all leave the measured-or-missing data
on disk). Re-running the same --out skips marked groups. Interrupting with
Ctrl-C forwards SIGINT to the in-flight replay, leaves its group unmarked,
and exits 130; `bwx cleanup` removes any scratch left behind.

Fill mode is always on (the point of the sweep is sustained N-VM pressure);
filler records are load bookkeeping and never enter the measurement.

Outputs (under --out):
    n<level>/g<idx>/          one replay run directory (result.json, real/,
                              filler/, bandwidth-*, bwx.log)
    linearity.csv             rows = cases, cols = levels, mean case_wall_s
                              (seconds; guest clock CASE_START..CASE_DONE,
                              boot/teardown excluded)
    linearity_ratio.csv       same shape, seconds relative to the lowest
                              level with data for that case
    linearity_detail.csv      long form: every real record with its status
"""
import argparse
import hashlib
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

from .. import cases as cases_mod
from ..util import Error, read_json, require_root, utc_stamp, write_csv, write_json
from .replay import _collect_case_ids

COMMAND = 'linearity'
HELP = 'concurrency-scaling sweep: replay every case at each level, ' \
       'summarize case_wall_s into CSVs'

DEFAULT_LEVELS = '1,2,4,8,16,24,32,48'
DEFAULT_GROUP_TIMEOUT = 2400          # boot(120) + case_timeout(1800) + slack
DONE_MARKER = '.done'
STATUS_FILE = '.status'

# status values counted as "the VM finished its (possibly truncated) work";
# mirrors the replay exit-code contract.
_OK_STATUSES = ('completed', 'budget_exhausted')


def add_arguments(parser):
    # -- case input (same merge semantics as `bwx replay`) ------------------
    parser.add_argument('--case', action='append', default=[], metavar='ID',
                        help='one case id (repeatable)')
    parser.add_argument('--cases', metavar='LIST',
                        help='comma-separated case ids')
    parser.add_argument('--case-file', type=Path, metavar='FILE',
                        help='case ids from a file: .txt or .json (same '
                             'formats as replay)')

    # -- sweep shape --------------------------------------------------------
    parser.add_argument('--levels', metavar='LIST',
                        help='comma-separated concurrency levels, mixing '
                             'integers and inclusive slices start:stop[:step] '
                             '(e.g. "1,2,4:8,24:48:8"; default: %s)'
                            % DEFAULT_LEVELS)
    parser.add_argument('--no-shuffle', dest='shuffle', action='store_false',
                        default=True,
                        help='group cases in the given order instead of '
                             'shuffling per level (default: shuffle once per '
                             'level, seed logged under n<level>/seed.txt)')
    parser.add_argument('--out', type=Path,
                        help='sweep root dir (default: '
                             '<root>/logs/linearity/<stamp>); re-run against '
                             'an existing dir to resume')
    parser.add_argument('--group-timeout', type=float, metavar='SEC',
                        default=DEFAULT_GROUP_TIMEOUT,
                        help='wall-clock cap per group, SIGINT then SIGKILL '
                             'on expiry (default %d, 0 disables)'
                             % DEFAULT_GROUP_TIMEOUT)
    parser.add_argument('--summarize-only', action='store_true',
                        help='only recompute the CSVs from an existing sweep '
                             'dir; no VMs are launched')

    # -- cpu binding (forwarded to replay; default: auto numa allocation) --
    parser.add_argument('--cpu-start', type=int, metavar='CPU',
                        help='generate cpus as start + i*step (with --cpu-step)')
    parser.add_argument('--cpu-step', type=int, metavar='N',
                        help='step for --cpu-start (default 1)')

    # -- forwarded replay tuning -------------------------------------------
    parser.add_argument('--vcpus', type=int, help='vCPU count (default from config)')
    parser.add_argument('--mem-mib', type=int, help='guest MiB (default from config)')
    parser.add_argument('--numa-node', type=int, help='NUMA node (default from config)')
    parser.add_argument('--case-timeout', type=float)
    parser.add_argument('--boot-timeout', type=float)
    parser.add_argument('--action-timeout', type=float)
    parser.add_argument('--bandwidth-interval', type=float)
    parser.add_argument('--no-tools-disk', action='store_true')
    parser.add_argument('--discard-work', action='store_true',
                        help='drop per-VM work dirs after extraction '
                             '(default: keep for post-mortem)')


def run(args):
    from .. import config as config_mod
    config = config_mod.load(args.config, {key: getattr(args, key, None)
                                           for key in ('vcpus', 'mem_mib',
                                                       'numa_node')})
    config_mod.validate_runtime(config)
    require_root('linearity (drives bwx replay: KVM, firecracker)')

    out = (args.out if args.out is not None
           else config.logs_dir / ('linearity/' + utc_stamp())).resolve()

    if args.summarize_only:
        if args.out is None:
            raise Error('--summarize-only needs an explicit --out')
        run_doc = read_json(out / 'run.json', default={})
        if not run_doc.get('cases') or not run_doc.get('levels'):
            raise Error('%s: no run.json with cases/levels (not a sweep dir?)'
                        % (out / 'run.json'))
        print('summarize only: %s' % out)
        _summarize(out, list(run_doc['cases']),
                   [int(level) for level in run_doc['levels']])
        return 0

    ids = _collect_case_ids(args)
    levels = _parse_levels(args.levels, default=DEFAULT_LEVELS)
    run_doc = _load_or_create_run(out, ids, levels, args)
    sweep_ids = run_doc['cases']
    sweep_levels = [int(level) for level in run_doc['levels']]

    _preflight(config, args, out, sweep_ids, sweep_levels)
    failures = _sweep(out, sweep_ids, sweep_levels, args)
    _summarize(out, sweep_ids, sweep_levels)
    return 0 if not failures else 1


# ------------------------------------------------------------- run metadata
def _load_or_create_run(out, ids, levels, args):
    """Create (or load) the run manifest that pins the shuffle seed.

    The run id is random on first use and persisted, so a resumed sweep
    shuffles exactly like the original run would have.
    """
    path = out / 'run.json'
    doc = read_json(path, default={})
    if not doc:
        out.mkdir(parents=True, exist_ok=True)
        doc = {
            'run_id': '%s-%04x' % (utc_stamp(), random.getrandbits(16)),
            'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'cases': list(ids),
            'levels': list(levels),
            'shuffle': bool(getattr(args, 'shuffle', True)),
            'cpu_start': args.cpu_start,
            'cpu_step': args.cpu_step or 1,
        }
        write_json(path, doc)
    else:
        for key, value in (('cases', list(ids)), ('levels', list(levels))):
            if list(doc.get(key) or []) != value:
                print('warning: %s changed since this sweep dir was created; '
                      'the recorded %s is used for the summary'
                      % (key, key))
        recorded = doc.get('shuffle')
        current = bool(getattr(args, 'shuffle', True))
        if recorded is not None and bool(recorded) != current:
            print('warning: shuffle changed since this sweep dir was created; '
                  'already-done groups keep their old grouping, new groups '
                  'follow --%s' % ('shuffle' if current else 'no-shuffle'))
    if not doc.get('run_id'):
        raise Error('%s: missing run_id' % path)
    return doc


# ------------------------------------------------------------- preflight
def _preflight(config, args, out, ids, levels):
    """Fail fast before hours of scheduled work: resolve every case and
    dry-run the largest level so pool size / NUMA membership is validated."""
    for iid in ids:
        cases_mod.load_case(config, iid)
    preflight_dir = out / 'preflight'
    cmd = _replay_cmd(args, [ids[0]], levels[-1], preflight_dir)
    cmd.append('--dry-run')
    preflight_dir.mkdir(parents=True, exist_ok=True)
    log = preflight_dir / 'dry-run.log'
    print('preflight: %d case(s), max level %d -> dry-run (log: %s)'
          % (len(ids), levels[-1], log))
    with log.open('wb') as stream:
        rc = subprocess.call(cmd, stdout=stream, stderr=subprocess.STDOUT,
                             env=_subprocess_env())
    if rc != 0:
        tail = log.read_text(errors='replace').splitlines()[-15:]
        raise Error('preflight failed (rc=%d):\n  %s' % (rc, '\n  '.join(tail)))


# ------------------------------------------------------------- sweep loops
def _sweep(out, ids, levels, args):
    """Run every (level, group). Returns the list of failed group labels."""
    failures = []
    total_groups = sum(_chunk_count(len(ids), level) for level in levels)
    print('sweep: %d case(s) x %d level(s) = %d group(s), out=%s'
          % (len(ids), len(levels), total_groups, out))

    for level in levels:
        level_dir = out / ('n%d' % level)
        seed = _seed_for(out, level)
        shuffle = bool(getattr(args, 'shuffle', True))
        order = _shuffled(ids, seed) if shuffle else list(ids)
        groups = _chunks(order, level)
        _write_seed(level_dir, seed, order, groups, shuffle=shuffle)
        pending = [gdir for _, gdir in _group_dirs(level_dir, groups)
                   if not (gdir / DONE_MARKER).exists()]
        print('[n%d] %d group(s), %d already done, %s 0x%016x'
              % (level, len(groups), len(groups) - len(pending),
                 'seed' if shuffle else 'order', seed))

        for index, (group, gdir) in enumerate(_group_dirs(level_dir, groups)):
            if not (gdir / DONE_MARKER).exists():
                label = 'n%d/g%02d' % (level, index)
                outcome = _run_group(_replay_cmd(args, group, level, gdir),
                                     gdir / 'bwx.log', args.group_timeout, label)
                _mark_done(gdir, outcome)
                if outcome['status'] != 'ok':
                    failures.append('%s %s' % (label, outcome['status']))
    return failures


def _group_dirs(level_dir, groups):
    """(group cases, group dir) pairs in execution order."""
    return [(group, level_dir / ('g%02d' % index))
            for index, group in enumerate(groups)]


def _run_group(cmd, log_path, timeout, label):
    """One `bwx replay` subprocess; returns {status, rc, wall_s, tail}.

    Ctrl-C during a group forwards SIGINT into the replay process group
    (its per-VM finally blocks tear firecracker down) and re-raises, so the
    group stays unmarked and is retried on resume.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    outcome = {'status': 'error', 'rc': None, 'wall_s': 0.0, 'tail': ''}
    with log_path.open('wb') as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, env=_subprocess_env())
        try:
            outcome['rc'] = proc.wait(timeout=timeout or None)
        except subprocess.TimeoutExpired:
            _signal_group(proc, 'group timeout')
            outcome['status'] = 'timeout'
        except KeyboardInterrupt:
            _signal_group(proc, 'interrupt')
            raise
    outcome['wall_s'] = round(time.monotonic() - started, 1)
    if outcome['rc'] is not None:
        outcome['status'] = 'ok' if outcome['rc'] == 0 else (
            'vm-failed' if outcome['rc'] == 1 else 'error')
    tail = log_path.read_text(errors='replace').splitlines()[-6:]
    outcome['tail'] = ' | '.join(line.strip() for line in tail if line.strip())
    _print_group(label, outcome, log_path)
    return outcome


def _signal_group(proc, reason):
    """SIGINT (graceful, firecracker teardown) then SIGKILL, best effort."""
    for sig, grace in ((signal.SIGINT, 20), (signal.SIGKILL, 5)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _print_group(label, outcome, log_path):
    note = ''
    if outcome['status'] == 'ok':
        note = ''
    elif outcome['status'] == 'timeout':
        note = ' (killed; see %s)' % log_path
    elif outcome['rc'] not in (None, 0, 1):
        note = ' (%s)' % outcome['tail']
    print('  [%s] wall=%ss rc=%s %s%s'
          % (label, outcome['wall_s'], outcome['rc'], outcome['status'], note))


def _mark_done(gdir, outcome):
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / DONE_MARKER).write_text('')
    (gdir / STATUS_FILE).write_text(
        '%s rc=%s wall=%ss\n' % (outcome['status'], outcome['rc'],
                                 outcome['wall_s']))


# ------------------------------------------------------------- subprocess cmd
def _replay_cmd(args, group, level, out_dir):
    """argv for one `bwx replay` (fill mode is part of the sweep contract)."""
    cmd = [sys.executable, '-B', '-m', 'bwx', 'replay',
           '--cases', ','.join(group),
           '--concurrency', str(level),
           '--fill',
           '--out', str(out_dir)]
    if args.cpu_start is not None:
        cmd += ['--cpu-start', str(args.cpu_start)]
        if args.cpu_step is not None:
            cmd += ['--cpu-step', str(args.cpu_step)]
    for attr, flag in (('vcpus', '--vcpus'), ('mem_mib', '--mem-mib'),
                       ('numa_node', '--numa-node'),
                       ('case_timeout', '--case-timeout'),
                       ('boot_timeout', '--boot-timeout'),
                       ('action_timeout', '--action-timeout'),
                       ('bandwidth_interval', '--bandwidth-interval')):
        value = getattr(args, attr, None)
        if value is not None:
            cmd += [flag, str(value)]
    if args.no_tools_disk:
        cmd.append('--no-tools-disk')
    if args.discard_work:
        cmd.append('--discard-work')
    console = getattr(args, 'console', None)
    if console is not None:
        # Serial capture forwarded per group: `markers` keeps the protocol
        # lines (BOOT/READY/STEP/CASE_DONE) so success is checkable in
        # n<level>/g<idx>/real/vm-*/console-markers.log; `full` adds all
        # console noise; `off` discards.
        cmd += ['--console', console]
    if getattr(args, 'console_verify', True) is False:
        cmd.append('--no-console-verify')
    if getattr(args, 'config', None) is not None:
        cmd += ['--config', str(args.config)]
    return cmd


def _subprocess_env():
    env = os.environ.copy()
    repo_src = str(Path(__file__).resolve().parents[2])
    existing = env.get('PYTHONPATH')
    env['PYTHONPATH'] = repo_src + ((':' + existing) if existing else '')
    return env


# ------------------------------------------------------------- shuffling
def _parse_levels(text, default=None):
    """Levels syntax: comma-separated segments that mix freely.

    Each segment is either a bare integer or an inclusive slice
    `start:stop[:step]` (bash `{1..6}` style -- endpoints INCLUDED):

        '1,2,4:8,24:48:8'  ->  [1, 2, 4, 5, 6, 7, 8, 24, 32, 40, 48]

    The union is deduplicated and sorted ascending; empty slices (stop <
    start), steps < 1, and non-integer parts are rejected on the spot.
    """
    raw = text if text is not None else default
    if not raw:
        raise Error('no concurrency level given: pass --levels 1,2,4,8,... '
                    '(slices like 1:6 or 1:48:8 also work)')
    levels = []
    for part in str(raw).split(','):
        part = part.strip()
        if part:
            levels.extend(_expand_level_segment(part))
    levels = sorted(set(levels))
    if not levels or levels[0] < 1:
        raise Error('--levels must contain positive integers, got %r' % raw)
    return levels


def _expand_level_segment(part):
    """One comma segment: '7' or '1:6' or '1:6:2' (inclusive endpoints)."""
    if ':' not in part:
        try:
            return [int(part)]
        except ValueError:
            raise Error('bad level %r (integer or start:stop[:step])' % part)
    pieces = part.split(':')
    if len(pieces) > 3 or not all(piece.strip() for piece in pieces):
        raise Error('bad level slice %r (want start:stop[:step])' % part)
    try:
        start, stop = int(pieces[0]), int(pieces[1])
        step = int(pieces[2]) if len(pieces) == 3 else 1
    except ValueError:
        raise Error('bad level slice %r (non-integer part)' % part)
    if step < 1:
        raise Error('level slice %r: step must be >= 1' % part)
    if start < 1:
        raise Error('level slice %r: levels start at 1' % part)
    if stop < start:
        raise Error('level slice %r: stop below start (empty range)' % part)
    return list(range(start, stop + 1, step))


def _seed_for(out, level):
    """Stable per-(run, level) seed, recorded on disk for reproducibility."""
    path = out / ('n%d' % level) / 'seed.txt'
    if path.is_file():
        try:
            return int(path.read_text().strip(), 16)
        except ValueError:
            pass
    run_doc = read_json(out / 'run.json', default={})
    run_id = run_doc.get('run_id') or 'anonymous'
    digest = hashlib.sha256(('%s:%d' % (run_id, level)).encode()).hexdigest()
    return int(digest[:16], 16)


def _shuffled(ids, seed):
    order = list(ids)
    random.Random(seed).shuffle(order)
    return order


def _chunks(order, size):
    return [order[i:i + size] for i in range(0, len(order), size)]


def _chunk_count(total, size):
    return (total + size - 1) // size


def _write_seed(level_dir, seed, order, groups, *, shuffle=True):
    level_dir.mkdir(parents=True, exist_ok=True)
    path = level_dir / 'seed.txt'
    if not path.exists():
        path.write_text(('0x%016x\n' % seed) if shuffle else 'off\n')
    (level_dir / 'plan.txt').write_text(
        '\n'.join('g%02d: %s' % (index, ','.join(group))
                  for index, group in enumerate(groups)) + '\n')


# ------------------------------------------------------------- summary
def _collect_samples(out, levels):
    """Every real record from every group, tagged with its level and group."""
    samples = []
    for level in levels:
        level_dir = out / ('n%d' % level)
        if not level_dir.is_dir():
            continue
        for gdir in sorted(level_dir.glob('g*')):
            doc = read_json(gdir / 'result.json', default={})
            if not doc:
                continue
            for record in doc.get('real') or []:
                samples.append({
                    'level': level,
                    'group': gdir.name,
                    'slot': record.get('slot'),
                    'case': record.get('case'),
                    'status': record.get('status'),
                    'verified': (record.get('verification') or {}).get('ok'),
                    'case_wall_s': record.get('case_wall_s'),
                    'vmm_wall_s': record.get('vmm_wall_s'),
                    'label': record.get('label'),
                })
    return samples


def _valid(sample):
    return (sample['status'] in _OK_STATUSES
            and sample['verified'] is True
            and isinstance(sample['case_wall_s'], (int, float)))


def _aggregate(samples, levels):
    """{(case, level): (mean case_wall_s, ok_count, fail_count)}."""
    buckets = {}
    for sample in samples:
        key = (sample['case'], sample['level'])
        ok, fail, total, wall = buckets.get(key, (0, 0, 0, 0.0))
        total += 1
        if _valid(sample):
            ok += 1
            wall += sample['case_wall_s']
        else:
            fail += 1
        buckets[key] = (ok, fail, total, wall)
    return {key: (wall / ok if ok else None, ok, fail)
            for key, (ok, fail, total, wall) in buckets.items()}


def _summarize(out, ids, levels):
    samples = _collect_samples(out, levels)
    if not samples:
        print('no result.json under %s: nothing to summarize' % out)
        return
    agg = _aggregate(samples, levels)
    cases = list(ids) + [case for case in sorted({s['case'] for s in samples
                                                  if s['case'] is not None})
                         if case not in ids]

    _write_wide(out / 'linearity.csv', cases, levels, agg, seconds=True)
    _write_wide(out / 'linearity_ratio.csv', cases, levels, agg, seconds=False)
    write_csv(out / 'linearity_detail.csv', samples, [
        'level', 'group', 'slot', 'case', 'status', 'verified',
        'case_wall_s', 'vmm_wall_s', 'label'])

    print('\nlevel  groups  samples  ok  failed  mean(case_wall_s)')
    for level in levels:
        level_samples = [s for s in samples if s['level'] == level]
        ok = [s for s in level_samples if _valid(s)]
        groups = len({s['group'] for s in level_samples})
        mean = (sum(s['case_wall_s'] for s in ok) / len(ok)) if ok else None
        print('n%-5d %-7d %-8d %-3d %-7d %s'
              % (level, groups, len(level_samples), len(ok),
                 len(level_samples) - len(ok),
                 ('%.3f' % mean) if mean is not None else '-'))
    print('\nwrote: %s, %s, %s (%d samples)'
          % (out / 'linearity.csv', out / 'linearity_ratio.csv',
             out / 'linearity_detail.csv', len(samples)))


def _write_wide(path, cases, levels, agg, seconds):
    """One row per case, one column per level: mean seconds, or the ratio
    against the case's lowest level with data."""
    rows = []
    for case in cases:
        row = {'case': case}
        values = {}
        for level in levels:
            mean, ok, fail = agg.get((case, level), (None, 0, 0))
            if ok:
                values[level] = mean
        base = values[min(values)] if values else None
        for level in levels:
            mean = values.get(level)
            if mean is None:
                row['n%d' % level] = ''
            elif seconds:
                row['n%d' % level] = '%.3f' % mean
            else:
                row['n%d' % level] = '%.3f' % (mean / base) if base else ''
        rows.append(row)
    write_csv(path, rows, ['case'] + ['n%d' % level for level in levels])
