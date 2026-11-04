"""CPU/NUMA placement: disjoint, vcpu-only bindings.

Two guarantees the experiment depends on:

1. **Disjoint CPU sets.** No two VMs share a logical CPU: overlapping sets
   would turn a concurrency measurement into a scheduling artifact.
2. **vcpu-only binding.** Every VM gets exactly `vcpus` CPUs, one per vCPU
   thread (pinned individually after launch). Firecracker's control-plane
   threads (main/API/serial) are *not* given a dedicated CPU; they stay on
   the VM's own affinity mask and share it with the vCPUs.

Cross-NUMA pools are allowed: a VM whose CPUs span nodes is membound to the
set of those nodes, so its memory stays local to wherever its CPUs live.
`Placement.node` keeps the primary node (first CPU) for reporting and
bandwidth attribution.

SMT density is the caller's trade-off, expressed through the pool itself:
consecutive CPUs (e.g. `--cpu-step 1`) put SMT siblings in one VM (the two
vCPUs share a physical core), a step of 2 gives each vCPU its own physical
core. No SMT validation is imposed.

Unlike the upstream implementation, this allocator does *not* scan /proc for other
users' firecracker processes to avoid their CPUs. On a shared host that scan is a
TOCTOU race (a VM can start between the scan and our launch). Instead the caller
declares the CPU budget per node, and concurrent VMs launched by the same driver
are assigned from a single allocation plan, which is structurally disjoint.
"""
import os
import re
from pathlib import Path

from .bandwidth import expand_cpulist, format_cpulist, node_cpulists
from .util import Error, read_json, write_json

_NODE_DIR = Path('/sys/devices/system/node')
_CPU_DIR = Path('/sys/devices/system/cpu')


class Placement:
    """CPU/NUMA placement for one VM (vcpu-only: len(cpus) == vcpus)."""

    def __init__(self, node, cpus):
        self.node = int(node)
        self.cpus = [int(c) for c in cpus]
        if not self.cpus:
            raise Error('placement needs at least one CPU')

    @property
    def nodes(self):
        """All NUMA nodes this VM's CPUs live on (primary first)."""
        seen = []
        for cpu in self.cpus:
            node = node_of_cpu(cpu)
            if node is not None and node not in seen:
                seen.append(node)
        return seen

    @property
    def vcpu_cpus(self):
        """One CPU per vCPU (there is no separate helper CPU)."""
        return self.cpus

    @property
    def physcpubind(self):
        return ','.join(str(c) for c in self.cpus)

    def as_dict(self):
        return {'node': self.node, 'nodes': self.nodes, 'cpus': self.cpus}


def node_ids():
    """NUMA nodes that have both CPUs and memory."""
    nodes = []
    for entry in sorted(_NODE_DIR.glob('node[0-9]*'),
                        key=lambda p: int(p.name.removeprefix('node'))):
        node = int(entry.name.removeprefix('node'))
        cpulist = entry / 'cpulist'
        has_memory = entry / 'meminfo'
        if cpulist.is_file() and has_memory.is_file() and cpulist.read_text().strip():
            nodes.append(node)
    return nodes


def node_cpus(node, *, allowed_only=True):
    """Online CPUs of a node, intersected with our affinity mask."""
    path = _NODE_DIR / ('node%d' % node) / 'cpulist'
    if not path.is_file():
        raise Error('unknown NUMA node %d' % node)
    cpus = expand_cpulist(path.read_text())
    online = expand_cpulist((_CPU_DIR / 'online').read_text()) \
        if (_CPU_DIR / 'online').is_file() else cpus
    cpus &= online
    if allowed_only:
        cpus &= os.sched_getaffinity(0)
    return sorted(cpus)


def physical_core_key(cpu):
    """(package, core) so SMT siblings collapse to one key."""
    top = _CPU_DIR / ('cpu%d' % cpu) / 'topology'
    try:
        return ((top / 'physical_package_id').read_text().strip(),
                (top / 'core_id').read_text().strip())
    except OSError:
        return ('unknown', str(cpu))


def select_cores(cpus, *, smt=True):
    """One logical CPU per physical core (SMT-safe), preserving order.

    With smt=True (default) only the first thread of each core is returned, so no
    two VMs ever share a physical core. With smt=False all logical CPUs are
    eligible, which doubles capacity at the cost of intra-core contention.
    """
    if smt:
        seen, out = set(), []
        for cpu in cpus:
            key = physical_core_key(cpu)
            if key in seen:
                continue
            seen.add(key)
            out.append(cpu)
        return out
    return list(cpus)


def smt_sibling_conflicts(cpus):
    """Detect two CPUs that share a physical core (used for validation)."""
    groups = {}
    for cpu in cpus:
        groups.setdefault(physical_core_key(cpu), []).append(cpu)
    return {key: value for key, value in groups.items() if len(value) > 1}


def node_capacity(node, *, smt=True, per_vm=None):
    """How many VMs a node can host given vcpus CPUs each."""
    cpus = select_cores(node_cpus(node), smt=smt)
    if not per_vm:
        return len(cpus), cpus
    return len(cpus) // per_vm, cpus


def memory_available_kib(node):
    """Conservative node memory estimate.

    NUMA sysfs has no MemAvailable, so: MemFree + half of reclaimable
    file/slab, minus dirty/writeback, capped by the global MemAvailable.
    """
    node_info = (_NODE_DIR / ('node%d' % node) / 'meminfo').read_text()
    host_info = Path('/proc/meminfo').read_text()

    def value(text, key):
        match = re.search(re.escape(key) + r':\s+(\d+)', text)
        return int(match.group(1)) if match else 0

    reclaim = max(0, (value(node_info, 'Active(file)') + value(node_info, 'Inactive(file)')
                      + value(node_info, 'SReclaimable')) // 2
                  - value(node_info, 'Dirty') - value(node_info, 'Writeback'))
    return min(value(node_info, 'MemFree') + reclaim, value(host_info, 'MemAvailable'))


class Allocator:
    """Hands out disjoint CPU sets from a per-node pool.

    Usage::

        alloc = Allocator(nodes=[2], vcpus=2, smt=True)
        p1 = alloc.take()          # Placement for VM 1
        p2 = alloc.take()          # disjoint from p1
        alloc.validate([p1, p2])   # raises on any overlap
    """

    def __init__(self, nodes, vcpus, *, smt=True, mem_mib=4096,
                 reserve_gib=2):
        self.vcpus = int(vcpus)
        self.smt = smt
        self.mem_mib = int(mem_mib)
        self.per_vm = vcpus
        self.pools = {}
        for node in nodes:
            cpus = select_cores(node_cpus(node), smt=smt)
            if len(cpus) < self.per_vm:
                raise Error('node %d has only %d usable CPUs, need %d per VM '
                            '(vcpus=%d)' % (node, len(cpus), self.per_vm, vcpus))
            capacity = memory_available_kib(node)
            required = self.mem_mib * 1024 + reserve_gib * 1024 ** 2
            if capacity < required:
                raise Error('node %d has %d KiB available, need %d KiB '
                            '(mem_mib=%d + %d GiB reserve)'
                            % (node, capacity, required, self.mem_mib, reserve_gib))
            self.pools[node] = {'cpus': cpus, 'cursor': 0,
                                'max_vms': min(len(cpus) // self.per_vm,
                                               max(1, capacity // required))}
        self.nodes = list(self.pools)

    def capacity(self):
        """{node: max concurrent VMs} and the total."""
        per_node = {node: pool['max_vms'] for node, pool in self.pools.items()}
        return per_node, sum(per_node.values())

    def take(self, node=None):
        """Reserve the next disjoint CPU set. `node=None` = least-loaded node."""
        if node is None:
            node = min(self.pools, key=lambda n: self.pools[n]['cursor'])
        if node not in self.pools:
            raise Error('node %d not in the allocation plan (%s)' % (node, self.nodes))
        pool = self.pools[node]
        if pool['cursor'] + self.per_vm > len(pool['cpus']):
            raise Error('node %d CPU pool exhausted (%d VMs planned, %d CPUs)'
                        % (node, pool['cursor'] // self.per_vm, len(pool['cpus'])))
        cpus = pool['cpus'][pool['cursor']:pool['cursor'] + self.per_vm]
        pool['cursor'] += self.per_vm
        return Placement(node, cpus)

    def take_many(self, count, *, spread=False):
        """`count` disjoint placements. spread=True round-robins across nodes.

        spread=False packs each node before moving on (better for per-node
        bandwidth isolation studies); spread=True balances load (better for
        throughput).
        """
        if spread:
            order = [self.nodes[i % len(self.nodes)] for i in range(count)]
        else:
            order, remaining = [], dict((n, p['max_vms']) for n, p in self.pools.items())
            for _ in range(count):
                node = next((n for n in self.nodes if remaining.get(n, 0) > 0
                             and self.pools[n]['cursor'] + self.per_vm
                             <= len(self.pools[n]['cpus'])), None)
                if node is None:
                    raise Error('cannot place %d VMs: capacity exhausted '
                                '(pools: %s)' % (count, self.capacity()[0]))
                remaining[node] -= 1
                order.append(node)
        return [self.take(node) for node in order]

    @staticmethod
    def validate(placements):
        """Assert disjoint CPU sets and online CPUs. Raises Error.

        Cross-node placements are legal (each VM membinds to its own node
        set); SMT density is the caller's choice, not an error.
        """
        node_map = node_cpulists()
        known = set()
        for cpus in node_map.values():
            known |= cpus
        seen = {}
        for index, placement in enumerate(placements):
            for cpu in placement.cpus:
                if cpu in seen:
                    raise Error('CPU %d assigned to both VM %d and VM %d'
                                % (cpu, seen[cpu], index))
                if cpu not in known:
                    raise Error('CPU %d is not online on any NUMA node' % cpu)
                seen[cpu] = index
        return True

    def as_dict(self):
        return {'vcpus': self.vcpus, 'per_vm': self.per_vm, 'smt': self.smt,
                'mem_mib': self.mem_mib,
                'capacity': self.capacity()[0],
                'pools': {str(n): {'total': len(p['cpus']), 'used': p['cursor'],
                                   'max_vms': p['max_vms']}
                          for n, p in self.pools.items()}}


# ---------------------------------------------------------------- cpu pools
def parse_cpu_list(text):
    """'0,2,4-6' or '0 2 4' -> [0, 2, 4, 5, 6]. Order preserved, dups rejected."""
    tokens = text.replace(',', ' ').split()
    cpus = []
    for token in tokens:
        if '-' in token:
            low, _, high = token.partition('-')
            try:
                cpus.extend(range(int(low), int(high) + 1))
            except ValueError:
                raise Error('bad cpu range: %r' % token)
        else:
            try:
                cpus.append(int(token))
            except ValueError:
                raise Error('bad cpu number: %r' % token)
    if not cpus:
        raise Error('empty cpu list')
    if len(cpus) != len(set(cpus)):
        raise Error('duplicate cpus in list: %s' % cpus)
    return cpus


def read_cpu_pool(source):
    """A cpu-list file: text (commas/spaces/newlines, a-b ranges) or JSON array.

    JSON is accepted both as a bare array and as {"cpus": [...]}.
    """
    import json
    path = Path(source)
    if not path.is_file():
        raise Error('cpu list file not found: %s' % path)
    text = path.read_text(encoding='utf-8-sig')
    stripped = text.lstrip()
    if path.suffix.lower() == '.json' or stripped.startswith(('{', '[')):
        doc = json.loads(text)
        if isinstance(doc, dict):
            doc = doc.get('cpus')
        if not isinstance(doc, list) or any(not isinstance(c, int) for c in doc):
            raise Error('%s: expected a JSON array of cpu numbers' % path)
        if not doc:
            raise Error('%s: empty cpu array' % path)
        if len(doc) != len(set(doc)):
            raise Error('%s: duplicate cpus' % path)
        return list(doc)
    # Text: allow the list to span lines.
    return parse_cpu_list(text)


def placements_from_pool(cpus, vcpus, count):
    """Split a cpu pool into `count` disjoint placements, in pool order.

    Each VM consumes exactly `vcpus` cpus (vcpu-only binding; control-plane
    threads share the VM's mask). The pool may span NUMA nodes: a VM is
    membound to the nodes its cpus live on. Unknown/offline cpus are
    rejected.
    """
    per_vm = vcpus
    needed = per_vm * count
    if len(cpus) < needed:
        raise Error('cpu pool too small: %d cpu(s), need %d (%d VMs x %d)'
                    % (len(cpus), needed, count, per_vm))
    node_map = node_cpulists()
    known = set()
    for node_cpus_set in node_map.values():
        known |= node_cpus_set
    unknown = [c for c in cpus if c not in known]
    if unknown:
        raise Error('cpus %s are not online on any NUMA node' % unknown[:8])
    placements = [Placement(node_of_cpu(cpus[i * per_vm]),
                            cpus[i * per_vm:(i + 1) * per_vm])
                  for i in range(count)]
    Allocator.validate(placements)
    return placements


def placements_from_start_step(start, step, vcpus, count):
    """Generate cpu slots as start + i*step, per-VM chunks of `vcpus`.

    step=1 packs consecutive cpus (SMT siblings share a physical core);
    a larger step spreads vCPUs across physical cores. The generated cpus
    are validated for disjointness and liveness exactly like an explicit
    pool, and may cross NUMA nodes.
    """
    if step < 1:
        raise Error('--cpu-step must be >= 1')
    needed = vcpus * count
    cpus = [start + i * step for i in range(needed)]
    return placements_from_pool(cpus, vcpus, count)


def node_of_cpu(cpu):
    """Which NUMA node a cpu belongs to (None when it is offline/unknown)."""
    for node, cpus in node_cpulists().items():
        if int(cpu) in cpus:
            return node
    return None


def plan_from_json(path):
    """Load an explicit {node: [cpus]} plan (for reproducing a past layout)."""
    doc = read_json(path)
    return {int(node): [int(c) for c in cpus] for node, cpus in doc.items()}


def write_plan(path, placements):
    write_json(path, {'placements': [p.as_dict() for p in placements]})
