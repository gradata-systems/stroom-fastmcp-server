"""Release: refuse while the docs are behind the code, test, bump the version everywhere it is, tag and push.

    uv run python dev/release.py                    # the next patch version (0.16.33 -> 0.16.34)
    uv run python dev/release.py 0.17.0             # a version of your own
    uv run python dev/release.py --dry-run          # every check, nothing changed
    uv run python dev/release.py --wait             # and wait for CI on the tag

The docs are written by hand (what changed and why is a judgement), but a release can't go out without them:

1. On master, with nothing uncommitted but the IDE's own files.
2. Docs: when code changed since the last tag (tools, utils, security, config, the server's resources, the chart's
   templates), the docs must have too: docs/, knowledge/ (what the server gives agents), README.md or
   dev/eval/README.md. Otherwise it stops and lists the code changes. --docs-unchanged "why" overrides it, for a
   release with nothing a reader would see (a dependency bump, say), and the reason goes in the release commit.
3. Tests: the unit tests, which check the docs list every tool, e2e suite, evaluation case and workflow
   (tests/test_knowledge.py), and every evaluation case's reference checked offline (dev/eval/offline.py).
4. The version in README.md, docs/DEPLOYMENT.md, charts/stroom-mcp/Chart.yaml (version and appVersion),
   pyproject.toml and uv.lock; a "Release X" commit, the vX tag, both pushed. CI builds the image and the chart.

--co-author adds a Co-Authored-By trailer to the release commit, for a release an agent makes.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = 'gradata-systems/stroom-fastmcp-server'
VERSIONED = ['README.md', 'docs/DEPLOYMENT.md', 'charts/stroom-mcp/Chart.yaml', 'pyproject.toml']
# What a release changes for its users: code the server runs and what it ships.
CODE = ('tools/', 'utils/', 'security/', 'conventions/', 'charts/stroom-mcp/templates/', 'charts/stroom-mcp/values.yaml',
        'config.py', 'main.py', 'main_tools.py', 'access_policy.yaml', 'error_rules.yaml', 'Dockerfile')
# Where it is written up.
DOCS = ('docs/', 'knowledge/', 'README.md', 'dev/eval/README.md')
# Never part of a release, and never a reason to stop one.
IGNORED = ('.idea/', '.claude/')


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    if check and done.returncode:
        raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.rstrip()     # not strip(): a status line starts with a space


def stop(message: str) -> None:
    raise SystemExit(f"Not released: {message}")


def current_version() -> str:
    found = re.search(r'^version = "([^"]+)"', (ROOT / 'pyproject.toml').read_text(encoding='utf-8'), re.M)
    return found.group(1)


def next_patch(version: str) -> str:
    major, minor, patch = (int(p) for p in version.split('.'))
    return f'{major}.{minor}.{patch + 1}'


def check_tree() -> None:
    if git('rev-parse', '--abbrev-ref', 'HEAD') != 'master':
        stop('not on master')
    dirty = [line[3:] for line in git('status', '--porcelain').splitlines()
             if not line[3:].startswith(IGNORED)]
    if dirty:
        stop(f"uncommitted changes: {dirty[:10]}. Commit them (by path) first.")


def check_docs(last_tag: str, unchanged_reason: str | None) -> list[str]:
    """The code files changed since the last tag; stops when the docs weren't, unless a reason is given."""
    changed = git('diff', '--name-only', f'{last_tag}..HEAD').splitlines()
    code = [f for f in changed if f.startswith(CODE)]
    docs = [f for f in changed if f.startswith(DOCS)]
    if code and not docs and not unchanged_reason:
        stop(f"the code changed since {last_tag} and the docs didn't: {code[:15]}{' ...' if len(code) > 15 else ''}. "
             f"Write up what changed first: docs/FINDINGS.md (what was found and done), docs/DESIGN.md (the tool "
             f"catalogue and e2e tables, the workflows), knowledge/guides (what agents read) and the tools' own "
             f"descriptions. Or --docs-unchanged \"why\" when nothing a reader sees changed.")
    return code


def run_tests() -> None:
    for name, command in (('unit tests', [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider']),
                          ('evaluation references, offline', [sys.executable, 'dev/eval/offline.py'])):
        print(f"  {name} ...", flush=True)
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if done.returncode:
            stop(f"{name} failed:\n{(done.stdout + done.stderr)[-3000:]}")
        print(f"    {(done.stdout.strip().splitlines() or [''])[-1]}")


def bump(old: str, new: str) -> None:
    for name in VERSIONED:
        path = ROOT / name
        text = path.read_text(encoding='utf-8')
        if old not in text:
            stop(f"{name} doesn't hold {old}: the versions have drifted apart; fix them by hand")
        path.write_text(text.replace(old, new), encoding='utf-8')
    subprocess.run(['uv', 'lock', '-q'], cwd=ROOT, check=True)


def wait_for_ci(sha: str) -> None:
    url = f'https://api.github.com/repos/{REPO}/actions/runs?head_sha={sha}'
    for _ in range(40):
        with urllib.request.urlopen(url, timeout=30) as response:
            runs = json.load(response).get('workflow_runs') or []
        states = [(r['name'], r['status'], r.get('conclusion')) for r in runs]
        if runs and all(s == 'completed' for _, s, _ in states):
            failed = [n for n, _, c in states if c != 'success']
            print(f"CI: {'failed: ' + ', '.join(failed) if failed else 'every run passed'}")
            if failed:
                raise SystemExit(1)
            return
        time.sleep(60)
    raise SystemExit('CI still running after 40 minutes; check GitHub Actions')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('version', nargs='?', help="The new version; the next patch version if left out.")
    parser.add_argument('--docs-unchanged', metavar='WHY', help="Release though the docs didn't change: the reason.")
    parser.add_argument('--dry-run', action='store_true', help="Every check, nothing changed or pushed.")
    parser.add_argument('--wait', action='store_true', help="Wait for CI on the release and report it.")
    parser.add_argument('--co-author', metavar='NAME <EMAIL>', help="A Co-Authored-By trailer for the release commit.")
    args = parser.parse_args()

    old = current_version()
    new = args.version or next_patch(old)
    if not re.fullmatch(r'\d+\.\d+\.\d+', new):
        stop(f"{new!r} isn't a version (major.minor.patch)")
    if git('tag', '--list', f'v{new}'):
        stop(f"v{new} is tagged already")
    last_tag = git('describe', '--tags', '--abbrev=0')
    print(f"Release {old} -> {new} (changes since {last_tag})")
    check_tree()
    code = check_docs(last_tag, args.docs_unchanged)
    print(f"  docs: {'changed with the code' if code and not args.docs_unchanged else args.docs_unchanged or 'no code changed'}")
    run_tests()
    if args.dry_run:
        print("Dry run: every check passed; nothing changed.")
        return
    bump(old, new)
    message = f"Release {new}"
    if args.docs_unchanged:
        message += f"\n\nDocs unchanged: {args.docs_unchanged}"
    if args.co_author:
        message += f"\n\nCo-Authored-By: {args.co_author}"
    git('commit', '-q', *VERSIONED, 'uv.lock', '-m', message)
    git('tag', f'v{new}')
    git('push', 'origin', 'master', f'v{new}')
    sha = git('rev-parse', 'HEAD')
    print(f"Released {new}: {sha[:7]}, tag v{new}, pushed. CI builds the image and the chart.")
    if args.wait:
        wait_for_ci(sha)


if __name__ == '__main__':
    main()
