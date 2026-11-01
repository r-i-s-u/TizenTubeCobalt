#!/usr/bin/env python3
# Copyright 2026 The Cobalt Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Preserve Ninja outputs and input timestamps between Android build jobs."""

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import tarfile

_MANIFEST = 'apk-checkpoint.json'
_SOURCE_TIME = 946684800  # Stable input mtimes: 2000-01-01, before any build.
_PLATFORMS = ('android-arm64', 'android-arm', 'android-x86')


def normalize_sources(workspace):
  """Give identical fresh checkouts the same mtimes without touching outputs."""
  source = workspace / 'cobalt/src'
  for directory, directories, files in os.walk(source, followlinks=False):
    directories[:] = [
        name for name in directories
        if name != '.git' and not (Path(directory) == source and name == 'out')
    ]
    for name in files + directories:
      path = Path(directory) / name
      if name != '.git':
        os.utime(path, (_SOURCE_TIME, _SOURCE_TIME), follow_symlinks=False)


def checkpoint_identity(workspace, platform, revision):
  """Restrict reuse to the same source, runner image and absolute build path."""
  return {
      'schema': 1,
      'revision': revision,
      'platform': platform,
      'workspace': str(workspace.resolve()),
      'image_os': os.environ.get('ImageOS', ''),
      'image_version': os.environ.get('ImageVersion', ''),
  }


def pack(workspace, platform, revision, archive):
  """Archive outputs, Ninja's hidden databases, and the development key."""
  manifest = workspace / _MANIFEST
  manifest.write_text(
      json.dumps(checkpoint_identity(workspace, platform, revision)))
  subprocess.run([
      'tar', '--format=posix', '-I', 'zstd -T2 -3', '-cf',
      str(archive), _MANIFEST, '.android/debug.keystore',
      f'cobalt/src/out/{platform}_qa'
  ],
                 cwd=workspace,
                 check=True)
  # Keep a single snapshot below the default 10 GiB repository cache allowance.
  if archive.stat().st_size > 8 * 1024**3:
    raise ValueError('Checkpoint exceeds 8 GiB; use a larger persistent runner')


def restore(workspace, platform, revision, archive):
  """Validate identity and restore exact output timestamps."""
  output_prefix = f'cobalt/src/out/{platform}_qa/'
  output_root = output_prefix.rstrip('/')

  def allowed_path(name):
    return name in ('.android/debug.keystore',
                    output_root) or name.startswith(output_prefix)

  def safe_member(member, destination):
    # The standard data filter rejects traversal and links outside workspace.
    member = tarfile.data_filter(member, destination)
    if not allowed_path(member.name):
      raise ValueError(f'Unexpected checkpoint path: {member.name}')
    resolved = (Path(destination) / member.name).resolve()
    if not allowed_path(str(resolved.relative_to(workspace.resolve()))):
      raise ValueError(f'Checkpoint would overwrite source: {member.name}')
    return member

  with subprocess.Popen(['zstd', '-dc', str(archive)],
                        stdout=subprocess.PIPE) as process:
    try:
      with tarfile.open(fileobj=process.stdout, mode='r|') as contents:
        first = contents.next()
        if first is None or first.name != _MANIFEST or not first.isfile():
          raise ValueError(
              'Checkpoint manifest must be the first archive member')
        with contents.extractfile(first) as manifest:
          identity = json.load(manifest)
        if identity != checkpoint_identity(workspace, platform, revision):
          raise ValueError(
              'Checkpoint source, platform, runner image or path differs')
        # Exclude the already-read manifest. Ninja needs both .ninja_log and
        # .ninja_deps, which are preserved inside the tar despite being hidden.
        while (member := contents.next()) is not None:
          contents.extract(member, path=workspace, filter=safe_member)
          # tarfile otherwise rounds PAX nanoseconds through a float. Ninja's
          # Ninja stores exact mtimes; rounding forces recompilation.
          timestamp = member.pax_headers.get('mtime', str(member.mtime))
          nanoseconds = int(Decimal(timestamp) * 1_000_000_000)
          os.utime(
              workspace / member.name,
              ns=(nanoseconds, nanoseconds),
              follow_symlinks=False)
      if process.wait() != 0:
        raise ValueError('Checkpoint decompression failed')
    finally:
      process.stdout.close()
      if process.poll() is None:
        process.kill()
      process.wait()


def main():
  """Run checkpoint operations in the GitHub workspace."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('operation', choices=('normalize', 'pack', 'restore'))
  parser.add_argument(
      '--workspace', type=Path, default=os.environ.get('GITHUB_WORKSPACE'))
  parser.add_argument(
      '--platform', choices=_PLATFORMS, default=os.environ.get('PLATFORM'))
  parser.add_argument('--revision', default=os.environ.get('GITHUB_SHA'))
  parser.add_argument('--archive', type=Path)
  args = parser.parse_args()
  if args.workspace is None:
    parser.error('--workspace or GITHUB_WORKSPACE is required')
  if args.operation == 'normalize':
    normalize_sources(args.workspace)
  else:
    if not args.platform or not args.revision or args.archive is None:
      parser.error('pack/restore require platform, revision and archive')
    operation = pack if args.operation == 'pack' else restore
    operation(args.workspace, args.platform, args.revision,
              args.archive.resolve())


if __name__ == '__main__':
  main()
