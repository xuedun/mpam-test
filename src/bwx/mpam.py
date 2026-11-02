"""resctrl (MPAM) control groups: per-VM bandwidth caps and group membership.

Semantics proven on this platform class (Kunpeng / openEuler MPAM):
* MB is a per-domain percentage cap. The domain IDs in the schemata
  (`MB:0=..;1=..`) are kernel resource-domain IDs. On this host there are 4 MB
  domains and 4 NUMA nodes and they line up 1:1, but that mapping is *not*
  guaranteed, so :func:`probe_platform` measures it instead of assuming it
  (`bwx calibrate` runs the measurement).
* Integer resources are zero-padded on readback (write 50 -> read "0000050"), so
  comparisons must be numeric, never string.
* The kernel snaps a requested percentage to a supported step (this host:
  bandwidth_gran=9, so 1..8 all land on 9). Every write is read back and the
  *applied* value is what gets recorded in results.
* Membership is inherited across fork/exec/clone. Writing our own PID into
  `<group>/tasks` before exec'ing the VMM is therefore enough to constrain every
  vCPU thread; no post-hoc thread migration is needed.
* Level 0 is never written. On this host a 0 cap with active tasks has hung the
  machine before, so 0 is rejected unconditionally.
"""
import os
import re
import sys
from pathlib import Path

from .util import Error, run, write_json

NUMERIC_RESOURCES = {'MB', 'MBHDL', 'MBPRI', 'MBMIN', 'L3PRI', 'L3MIN', 'L3MAX'}
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
MIN_SAFE_LEVEL = 1


def _check_level(platform, level):
    """Validate a requested percentage against the platform's limits.

    Level 0 is refused unconditionally: a 0 cap with active tasks has hung this
    class of machine before, so no code path may ever write it.
    """
    if not isinstance(level, int) or isinstance(level, bool):
        raise Error('bandwidth level must be an integer percent, got %r' % (level,))
    if level < MIN_SAFE_LEVEL:
        raise Error('bandwidth level %d refused: 0 has hung this machine before, '
                    'and the minimum safe level is %d' % (level, MIN_SAFE_LEVEL))
    if level > 100:
        raise Error('bandwidth level %d out of range 1..100' % level)
    minimum = getattr(platform, 'min_bandwidth', 0) or 0
    if level < minimum:
        raise Error('bandwidth level %d below info/MB/min_bandwidth=%d' % (level, minimum))
    return level


def _read_int(path):
    from pathlib import Path as _P
    try:
        return _P(path).read_text().strip()
    except OSError:
        return '?'


def parse_schemata(text):
    """`MB:0=100;1=100\\nL3:1=fff` -> {'MB': {'0': '100', ...}, 'L3': {...}}."""
    result = {}
    for line in text.splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        result[key.strip()] = dict(item.strip().split('=', 1)
                                   for item in value.split(';') if item.strip())
    return result


def normalize(parsed):
    """Strip the kernel's zero padding from integer resources."""
    return {resource: {domain: (str(int(value)) if resource in NUMERIC_RESOURCES else value)
                       for domain, value in domains.items()}
            for resource, domains in parsed.items()}


def mb_line(domains_values):
    """{0: 26, 1: 26} -> `MB:0=26;1=26`."""
    return 'MB:' + ';'.join('%s=%s' % (domain, value)
                            for domain, value in sorted(domains_values.items(),
                                                        key=lambda kv: int(kv[0])))


class Platform:
    """What this host's resctrl actually supports (measured, never assumed)."""

    def __init__(self, root):
        self.root = Path(root)
        info = self.root / 'info' / 'MB'
        if not (self.root / 'schemata').is_file():
            raise Error('resctrl is not mounted at %s' % self.root)
        if not info.is_dir():
            raise Error('resctrl exposes no MB (memory bandwidth) control at %s' % self.root)
        self.min_bandwidth = int((info / 'min_bandwidth').read_text().strip() or 0)
        self.granularity = int((info / 'bandwidth_gran').read_text().strip() or 1)
        self.num_closids = int((info / 'num_closids').read_text().strip() or 1)
        self.delay_linear = (info / 'delay_linear').read_text().strip() \
            if (info / 'delay_linear').is_file() else 'unknown'
        root_schema = parse_schemata((self.root / 'schemata').read_text())
        self.root_schema = root_schema
        self.domains = sorted((root_schema.get('MB') or {}), key=int)
        self.has_mbhdl = 'MBHDL' in root_schema
        self.has_mb_monitoring = (self.root / 'info' / 'MB_MON').is_dir()
        monitor_info = self.root / 'info' / 'L3_MON' / 'mon_features'
        self.monitor_features = monitor_info.read_text().split() if monitor_info.is_file() else []
        self.existing_groups = self.list_groups()

    def list_groups(self):
        skip = {'info', 'mon_data', 'mon_groups'}
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and p.name not in skip and (p / 'tasks').is_file())

    def check_level(self, level):
        """Validate a requested percentage; return it (int) or raise."""
        return _check_level(self, level)

    def level_map(self, levels, *, scratch_name=None):
        """{requested: applied} for a list of levels.

        Uses ONE scratch group whose schemata is rewritten per level, not one group
        per level. Creating and removing a group allocates a monitoring RMID, and on
        this platform `num_rmids=1`: rapid create/remove cycles exhaust the pool and
        the kernel then refuses every further mkdir with ENOSPC ("Out of RMIDs") --
        measured after a 100-level probe loop, with no recovery even 35 s later.
        """
        name = scratch_name or ('bwx-probe-%d' % os.getpid())
        group = self.root / name
        self._mkdir_with_retry(group)
        try:
            return {level: self._read_applied(group, level) for level in levels}
        finally:
            try:
                group.rmdir()
            except OSError:
                pass

    def _read_applied(self, group, requested):
        self._write(group, {domain: requested for domain in self.domains})
        readback = parse_schemata((group / 'schemata').read_text())
        values = {int(v) for v in (readback.get('MB') or {}).values()}
        if len(values) != 1:
            raise Error('schemata readback not uniform for level %d: %s'
                        % (requested, sorted(values)))
        return values.pop()

    def _mkdir_with_retry(self, group, attempts=4, delay=0.5):
        """mkdir a resctrl group, retrying on ENOSPC (RMID/CLOSID pressure)."""
        import time
        last = None
        for attempt in range(attempts):
            try:
                group.mkdir()
                return
            except OSError as exc:
                last = exc
                if exc.errno != 28:      # ENOSPC == out of RMIDs/CLOSIDs
                    raise Error('cannot create resctrl group %s: %s' % (group, exc))
                time.sleep(delay * (attempt + 1))
        raise Error('cannot create resctrl group %s: %s\n'
                    'This host reports num_rmids=%s, so its monitoring-ID pool is tiny; '
                    'a group leaked by an earlier run (or another tenant) blocks every '
                    'new group until resctrl is remounted. Check for stale groups under '
                    '%s and remove them with `bwx cleanup --groups --force`.'
                    % (group, last, _read_int(self.root / 'info/L3_MON/num_rmids'),
                       self.root))

    def group_capacity(self, limit=4):
        """How many groups this host can hold at once. Measured, not assumed.

        `limit` caps the probe: on a host whose monitoring pool is tiny, probing
        until failure is itself the failure mode being measured.
        """
        created = []
        try:
            while len(created) < min(limit, max(1, self.num_closids - 1)):
                path = self.root / ('bwx-cap-%d-%d' % (os.getpid(), len(created)))
                try:
                    path.mkdir()
                except OSError:
                    break
                created.append(path)
            return len(created)
        finally:
            for path in created:
                try:
                    path.rmdir()
                except OSError:
                    pass

    def monitoring_pool(self):
        """What limits group count on this host.

        CLOSIDs bound *control* groups; RMIDs bound *monitoring* allocations, and on
        this platform each control group consumes monitoring resources. Measured
        here: `num_closids=32` but `num_rmids=1` with `4 pmg`, and a 100-iteration
        create/destroy loop left the pool permanently exhausted ("Out of RMIDs",
        every later mkdir returning ENOSPC, no recovery after 5 minutes).
        """
        def read(path):
            try:
                return int(Path(path).read_text().strip())
            except (OSError, ValueError):
                return None
        return {'num_closids': self.num_closids,
                'num_rmids': read(self.root / 'info/L3_MON/num_rmids'),
                'monitor_features': self.monitor_features,
                'existing_groups': len(self.existing_groups),
                'note': 'group creation is a scarce resource on this host: reuse one '
                        'group per distinct cap and never create/destroy groups in a '
                        'loop'}

    def as_dict(self):
        return {'root': str(self.root), 'min_bandwidth': self.min_bandwidth,
                'bandwidth_granularity': self.granularity, 'num_closids': self.num_closids,
                'delay_linear': self.delay_linear, 'mb_domains': self.domains,
                'has_mbhdl': self.has_mbhdl, 'has_mb_monitoring': self.has_mb_monitoring,
                'monitor_features': self.monitor_features,
                'existing_groups': self.existing_groups,
                'min_safe_level': MIN_SAFE_LEVEL}

    def _write(self, group, domain_values, hard_limit=False):
        schema = {resource: dict(domains)
                  for resource, domains in parse_schemata((group / 'schemata').read_text()).items()}
        if 'MB' not in schema:
            raise Error('group %s exposes no MB resource' % group)
        schema['MB'] = {domain: str(value) for domain, value in domain_values.items()}
        with (group / 'schemata').open('w') as stream:
            stream.write(mb_line({int(d): v for d, v in schema['MB'].items()}) + '\n')
        if hard_limit:
            if not self.has_mbhdl:
                raise Error('MBHDL requested but not exposed by this kernel')
            with (group / 'schemata').open('w') as stream:
                stream.write('MBHDL:' + ';'.join('%s=1' % domain for domain in schema['MB']) + '\n')


class Group:
    """One resctrl control group at one MB cap, optionally per-domain.

    `domain_levels` maps MB domain id -> percent. Passing a single int applies it
    to every domain (the classic "cap the whole socket" behaviour). Passing a dict
    enables the differentiated allocation this experiment is about: a hungry VM on
    node 2 can get a high cap on domain 2 while light VMs get low caps elsewhere.
    """

    def __init__(self, platform, name, levels, *, hard_limit=False, output_dir=None):
        if not NAME_RE.match(name):
            raise Error('invalid resctrl group name: %r' % name)
        self.platform = platform
        self.name = name
        self.path = platform.root / name
        self.hard_limit = hard_limit
        self.output_dir = Path(output_dir) if output_dir else None
        self.requested = self._expand(levels)
        self.applied = None
        self.created = False
        self.adopted_root = False
        self.pre_existing = self.path.is_dir()

    def _expand(self, levels):
        domains = self.platform.domains
        if isinstance(levels, int):
            return {domain: _check_level(self.platform, levels) for domain in domains}
        expanded = {}
        for key, value in levels.items():
            key = str(key)
            if key not in domains:
                raise Error('MB domain %s not exposed by this host (have %s)'
                            % (key, domains))
            expanded[key] = _check_level(self.platform, int(value))
        missing = [domain for domain in domains if domain not in expanded]
        if missing:
            raise Error('per-domain levels must cover every MB domain; missing %s' % missing)
        return expanded

    # ---------------------------------------------------------------- lifecycle
    def create(self, *, allow_root=False):
        """Create (or adopt) the group and apply the caps. Returns self.

        `allow_root`: when group creation fails because the host's monitoring pool
        is exhausted (this platform leaks RMIDs on create/destroy cycles and even a
        resctrl remount does not recover), fall back to the ROOT group. The cap then
        applies to every task on the machine, not just ours, so it is recorded as
        `adopted_root: true` and only ever opt-in via config.
        """
        if self.pre_existing:
            readback = parse_schemata((self.path / 'schemata').read_text())
            self.applied = {domain: int(value)
                            for domain, value in (readback.get('MB') or {}).items()}
            return self
        if len(self.platform.existing_groups) + 1 >= self.platform.num_closids:
            raise Error('num_closids exhausted (%d groups exist, limit %d); remove unused '
                        'resctrl groups first' % (len(self.platform.existing_groups),
                                                  self.platform.num_closids))
        try:
            self.platform._mkdir_with_retry(self.path)
        except Error:
            if not allow_root or self.platform.existing_groups:
                raise
            # Last resort: use the root group. Machine-wide cap, opt-in only.
            self.path = self.platform.root
            self.adopted_root = True
            self.name = '(root)'
            self.pre_existing = True
            self.platform._write(self.path, self.requested, hard_limit=self.hard_limit)
            readback = parse_schemata((self.path / 'schemata').read_text())
            self.applied = {domain: int(value)
                            for domain, value in (readback.get('MB') or {}).items()}
            if self.output_dir:
                write_json(self.output_dir / ('group-%s.json' % self.name), self.as_dict())
            return self
        self.created = True
        before = normalize(parse_schemata((self.path / 'schemata').read_text()))
        root = normalize(self.platform.root_schema)
        # Only the resources we do NOT manage must match the root group. MB itself
        # is allowed to differ: a fresh group's MB defaults to the highest supported
        # step (97 here, where the root shows the requested-but-snapped 100), so
        # comparing it would reject every group on this platform.
        ours = {'MB', 'MBHDL'}
        foreign_before = {k: v for k, v in before.items() if k not in ours}
        foreign_root = {k: v for k, v in root.items() if k not in ours}
        if foreign_before != foreign_root:
            self.remove(force=True)
            raise Error('new group %s does not match the root schema on resources '
                        'this experiment does not own (%s vs %s); refusing to alter them'
                        % (self.name, foreign_before, foreign_root))
        self.platform._write(self.path, self.requested, hard_limit=self.hard_limit)
        readback = parse_schemata((self.path / 'schemata').read_text())
        self.applied = {domain: int(value)
                        for domain, value in (readback.get('MB') or {}).items()}
        expected = {int(domain): int(value) for domain, value in self.requested.items()}
        if len(self.platform.domains) > 1 and self.applied != expected:
            # Single-value requests are snapped by the kernel to a supported step,
            # which is expected and recorded rather than treated as an error.
            if len(set(expected.values())) > 1:
                self.remove(force=True)
                raise Error('MPAM readback mismatch for %s: requested %s applied %s '
                            '(check info/last_cmd_status)'
                            % (self.name, expected, self.applied))
        if self.output_dir:
            write_json(self.output_dir / ('group-%s.json' % self.name), self.as_dict())
        return self

    def snap(self):
        """Report the actually applied cap (uniform value or per-domain dict)."""
        values = set(self.applied.values()) if self.applied else set()
        return self.applied[list(self.applied)[0]] if len(values) == 1 else dict(self.applied or {})

    def join_argv(self, command):
        """argv that joins this group, verifies membership, then execs `command`.

        The launcher re-invokes this module with `--join <path> <command...>`, so
        the workload and every thread it ever creates inherit the CLOSID. This is
        strictly more reliable than migrating threads after the fact.
        """
        return [sys.executable, '-m', 'bwx.mpam', '--join', str(self.path),
                *[str(c) for c in command]]

    def members(self):
        try:
            return set((self.path / 'tasks').read_text().split())
        except OSError:
            return set()

    def check(self, pid):
        """Raise if the process's live threads have left this group."""
        task_dir = Path('/proc') / str(pid) / 'task'
        if not task_dir.is_dir():
            return
        tids = {p.name for p in task_dir.iterdir()}
        missing = tids - self.members()
        if not missing:
            return
        # The python launcher has not joined yet until it execs; only enforce once
        # the executable is no longer this interpreter.
        try:
            executable = os.readlink('/proc/%s/exe' % pid)
        except OSError:
            return
        if Path(executable).resolve() != Path(sys.executable).resolve():
            raise Error('owned threads left MPAM group %s: %s'
                        % (self.name, ','.join(sorted(missing)[:8])))

    def as_dict(self):
        return {'name': self.name, 'path': str(self.path), 'created_by_us': self.created,
                'pre_existing': self.pre_existing, 'adopted_root': self.adopted_root,
                'hard_limit': self.hard_limit,
                'requested': {k: v for k, v in self.requested.items()},
                'applied': self.applied}

    def remove(self, force=False):
        """rmdir the group. Refuses while tasks or CPU associations remain.

        The root group is never removed; when it was adopted as a fallback the
        schemata is restored to the full cap instead (leaving the machine capped
        would affect every other tenant).
        """
        errors = []
        try:
            if self.adopted_root:
                self.platform._write(self.path, {domain: 100 for domain in self.platform.domains})
                return errors
            if not force:
                if (self.path / 'tasks').read_text().strip():
                    raise Error('group still contains tasks')
                cpus = self.path / 'cpus_list'
                if cpus.is_file() and cpus.read_text().strip():
                    raise Error('group has CPU associations')
            self.path.rmdir()
        except Exception as exc:  # noqa: BLE001 - cleanup must report, not raise
            errors.append('%s: %s' % (self.path, exc))
        return errors


def join_and_exec(group_path, command):
    """`--join` entry point: write our PID into tasks, verify, exec the workload."""
    group_path = Path(group_path)
    tasks = group_path / 'tasks'
    tasks.write_text('%d\n' % os.getpid())
    if str(os.getpid()) not in tasks.read_text().split():
        raise Error('failed to join MPAM group %s' % group_path)
    os.execvp(command[0], command)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 2 and argv[0] == '--join':
        join_and_exec(argv[1], argv[2:])
        return 0
    sys.stderr.write('bwx.mpam is a library; the only direct entry point is '
                     '`--join <group-path> <command...>`\n')
    return 2


if __name__ == '__main__':
    sys.exit(main())
