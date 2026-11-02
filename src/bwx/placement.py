"""CPU/NUMA placement: who runs where, disjointly, and provably.

Three guarantees the experiment depends on:

1. **Disjoint CPU sets.** Every VM gets `vcpus + 1` CPUs (the extra one is the
   Firecracker helper: main thread, API/logger threads). Overlapping CPU sets
   would turn a memory-bandwidth measurement into a CPU-contention measurement.
2. **Node-local memory.** Each VM is bound to one NUMA node's CPUs *and* that
   node's memory (`numactl --membind`). Bandwidth caps are per MB domain, and the
   ddrc counters are per node, so a VM straddling nodes cannot be attributed.
3. **SMT-aware selection.** Only one thread per physical core is handed out by
   default; SMT siblings sharing a core would contend for the same execution
   resources and pollute per-core bandwidth numbers.

Unlike the upstream implementation, this allocator does *not* scan /proc for other
users' firecracker processes to avoid their CPUs. On a shared host that scan is a
TOCTOU race (a VM can start between the scan and our launch). Instead the caller
declares the CPU budget per node, and concurrent VMs launched by the same driver
are assigned from a single allocation plan, which is structurally disjoint.
"""
import os
import re
from pathlib import Path

from .bandwidth import expand_cpulist, format_cpulist
from .util import Error, read_json, write_json

_NODE_DIR = Path('/sys/devices/system/node')
_CPU_DIR = Path('/sys/devices/system/cpu')


class Placement:
    """CPU/NUMA placement for one VM."""

    def __init__(self, node, cpus, *, helper_mode='dedicated'):
        self.node = int(node)
        self.cpus = [int(c) for c in cpus]
        self.helper_mode = helper_mode
        if not self.cpus:
            raise Error('placement needs at least one CPU')
        if helper_mode == 'dedicated' and len(self.cpus) < 2:
            raise Error('dedicated helper mode needs vcpus+1 CPUs')

    @property
    def vcpu_cpus(self):
        """One CPU per vCPU; the last CPU is reserved for helper threads."""
        return self.cpus[:-1] if self.helper_mode == 'dedicated' and len(self.cpus) > 1 \
            else self.cpus

    @property
    def helper_cpu(self):
        return self.cpus[-1]

    @property
    def physcpubind(self):
        return ','.join(str(c) for c in self.cpus)

    def as_dict(self):
        return {'node': self.node, 'cpus': self.cpus, 'helper_mode': self.helper_mode}


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
    """How many VMs a node can host given vcpus+1 CPUs each."""
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

    def __init__(self, nodes, vcpus, *, smt=True, mem_mib=4096, helper_mode='dedicated',
                 reserve_gib=2):
        self.vcpus = int(vcpus)
        self.smt = smt
        self.mem_mib = int(mem_mib)
        self.helper_mode = helper_mode
        self.per_vm = vcpus + (1 if helper_mode == 'dedicated' else 0)
        self.pools = {}
        for node in nodes:
            cpus = select_cores(node_cpus(node), smt=smt)
            if len(cpus) < self.per_vm:
                raise Error('node %d has only %d usable CPUs, need %d per VM '
                            '(vcpus=%d + helper)' % (node, len(cpus), self.per_vm, vcpus))
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
        return Placement(node, cpus, helper_mode=self.helper_mode)

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
        """Assert disjointness, node membership and SMT safety. Raises Error."""
        seen = {}
        for index, placement in enumerate(placements):
            node_cpus_set = set(node_cpus(placement.node))
            for cpu in placement.cpus:
                if cpu in seen:
                    raise Error('CPU %d assigned to both VM %d and VM %d'
                                % (cpu, seen[cpu], index))
                seen[cpu] = index
                if cpu not in node_cpus_set:
                    raise Error('CPU %d is not on node %d' % (cpu, placement.node))
            conflicts = smt_sibling_conflicts(placement.cpus)
            if conflicts:
                raise Error('VM %d got SMT siblings on one core: %s' % (index, conflicts))
        return True

    def as_dict(self):
        return {'vcpus': self.vcpus, 'per_vm': self.per_vm, 'smt': self.smt,
                'mem_mib': self.mem_mib, 'helper_mode': self.helper_mode,
                'capacity': self.capacity()[0],
                'pools': {str(n): {'total': len(p['cpus']), 'used': p['cursor'],
                                   'max_vms': p['max_vms']}
                          for n, p in self.pools.items()}}


def plan_from_json(path):
    """Load an explicit {node: [cpus]} plan (for reproducing a past layout)."""
    doc = read_json(path)
    return {int(node): [int(c) for c in cpus] for node, cpus in doc.items()}


def write_plan(path, placements):
    write_json(path, {'placements': [p.as_dict() for p in placements]})
