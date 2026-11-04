"""Command line interface: `python -m bwx <command> ...`.

Commands map 1:1 onto the experiment phases in docs/EXPERIMENT-PLAN.md:

    doctor     verify host prerequisites (tools, KVM, ddrc PMU)
    convert    build base ext4 images from podman (idempotent)
    replay     concurrent multi-VM trajectory replay (filler mode supported)
    linearity  concurrency-scaling sweep, CSV summary per case
    case-list  extract the replayable case ids from a case directory
    list       show converted cases (image bytes, steps, sha)
    cleanup    remove scratch dirs left by an interrupted run
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
        prog='bwx', description='Firecracker sandbox trajectory replay toolkit',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--version', action='version', version='bwx %s' % __version__)
    sub = parser.add_subparsers(dest='command', required=True)

    from . import config as config_mod
    from .commands import (case_list, cleanup, convert, doctor, linearity,
                           list_, replay)

    for module in (doctor, convert, replay, linearity, case_list, list_,
                   cleanup):
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
