"""Command line interface: `python -m bwx <command> ...`.

Commands map 1:1 onto the experiment phases in docs/EXPERIMENT-PLAN.md:

    doctor     verify host prerequisites (tools, KVM, resctrl, ddrc PMU)
    calibrate  Phase 0: measure ddrc scale, MB domain <-> node mapping, level snap
    convert    build base ext4 images from podman (idempotent)
    replay     single-VM replay (optionally under an MPAM cap)
    sweep      Phase 1: case x level x repeat sensitivity matrix (serial or N-VM)
    alloc      Phase 3: concurrent multi-case runs under allocation strategies A-D
    analyze    Phase 2/4: degradation, classification, knee, N*, strategy comparison
    list       show converted cases
    cleanup    remove resctrl groups / scratch dirs left by an interrupted run
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .util import Error


def build_parser():
    parser = argparse.ArgumentParser(
        prog='bwx', description='MPAM memory-bandwidth sandbox experiments',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--version', action='version', version='bwx %s' % __version__)
    sub = parser.add_subparsers(dest='command', required=True)

    from . import config as config_mod
    from .commands import analyze, alloc, calibrate, cleanup, convert, doctor, list_, replay, sweep

    for module in (doctor, calibrate, convert, replay, sweep, alloc, analyze, list_, cleanup):
        module_parser = sub.add_parser(module.COMMAND, help=module.HELP,
                                       description=module.HELP,
                                       formatter_class=argparse.RawDescriptionHelpFormatter)
        config_mod.add_arguments(module_parser)
        module.add_arguments(module_parser)
        module_parser.set_defaults(func=module.run)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except Error as exc:
        print('bwx: %s' % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('bwx: interrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
