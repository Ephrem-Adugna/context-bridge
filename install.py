#!/usr/bin/env python3
"""Install the CLI and portable agent skill from this checkout. No administrator needed."""
import argparse
import os
from pathlib import Path
import shutil
import sys


def install():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    parser.add_argument('--skill-dir', action='append', type=Path, help='Skill parent directory; repeat for multiple agents')
    parser.add_argument('--name', help='Optional display name; otherwise the agent asks on first use')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or later is required.')
    source = Path(__file__).resolve().parent / 'skills' / 'context-bridge'
    runtime = args.prefix.expanduser() / 'share' / 'context-bridge'
    destinations = args.skill_dir or [Path.home() / '.agents' / 'skills', Path.home() / '.codex' / 'skills']
    for dest in [runtime] + [p.expanduser() / 'context-bridge' for p in destinations]:
        if dest.resolve() != source.resolve():
            shutil.copytree(source, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        print('Installed skill:', dest)
    binary = args.prefix.expanduser() / 'bin' / 'context-bridge'
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' + repr(str(runtime / 'scripts' / 'context_bridge.py')) + ', run_name="__main__")\n')
    binary.chmod(0o755)
    print('CLI:', binary)
    if str(binary.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('Add this directory to your PATH, or invoke the CLI at the absolute path above:', binary.parent)
    if args.name:
        import subprocess
        subprocess.run([sys.executable, str(binary), 'profile', '--name', args.name], check=True)
    else:
        print('On first use, your agent should ask your display name and run: context-bridge profile --name NAME')


if __name__ == '__main__':
    install()
