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
"""Exercise actual Ninja interruption and checkpoint restoration."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import apk_checkpoint


class CheckpointTest(unittest.TestCase):
  """Test the state Ninja needs to avoid redoing completed compilation."""

  def setUp(self):
    # Cleanup is registered with unittest so assertion failures also remove it.
    self.temporary = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
    self.addCleanup(self.temporary.cleanup)
    self.workspace = Path(self.temporary.name)
    self.source = self.workspace / 'cobalt/src'
    self.output = self.source / 'out/android-arm64_qa'
    self.output.mkdir(parents=True)
    self.key = self.workspace / '.android/debug.keystore'
    self.key.parent.mkdir()
    self.key.write_bytes(b'synthetic-development-key')
    self.archive = self.workspace / 'checkpoint.tar.zst'
    self.platform = 'android-arm64'
    self.revision = 'test-revision'
    self.environment = mock.patch.dict(os.environ, {
        'ImageOS': 'ubuntu24',
        'ImageVersion': 'test-image'
    })
    self.environment.start()
    self.addCleanup(self.environment.stop)

  def pack(self):
    """Create a checkpoint with the fixture identity."""
    apk_checkpoint.pack(self.workspace, self.platform, self.revision,
                        self.archive)

  def restore(self):
    """Restore a checkpoint with the fixture identity."""
    apk_checkpoint.restore(self.workspace, self.platform, self.revision,
                           self.archive)

  def test_real_ninja_resume(self):
    """An interrupted GCC build keeps object/dependency state across jobs."""
    (self.source / 'header.h').write_text('#define NUMBER 7\n')
    (self.source /
     'a.cc').write_text('#include "header.h"\nint value() { return NUMBER; }\n')
    (self.source / 'b.cc'
    ).write_text('extern int value(); int main() { return value() != 7; }\n')
    apk_checkpoint.normalize_sources(self.workspace)
    (self.output / 'build.ninja').write_text('''rule compile
  command = g++ -MMD -MF $out.d -c $in -o $out
  depfile = $out.d
  deps = gcc
rule slow_compile
  command = sleep 2; g++ -MMD -MF $out.d -c $in -o $out
  depfile = $out.d
  deps = gcc
rule link
  command = g++ a.o b.o -o app
build a.o: compile ../../a.cc
build b.o: slow_compile ../../b.cc | a.o
build app: link a.o b.o
''')
    interrupted = subprocess.run([
        'timeout', '--signal=INT', '--kill-after=2s', '1s', 'ninja', '-C',
        str(self.output), '-j', '1', 'app'
    ],
                                 capture_output=True,
                                 text=True,
                                 check=False)
    self.assertEqual(interrupted.returncode, 124, interrupted.stdout)
    self.assertTrue((self.output / 'a.o').exists())
    self.assertFalse((self.output / 'b.o').exists())
    object_time = (self.output / 'a.o').stat().st_mtime_ns
    self.assertTrue((self.output / '.ninja_deps').exists())
    self.pack()
    shutil.rmtree(self.output)
    self.key.unlink()
    # A fresh checkout changes input mtimes; normalization must undo that.
    os.utime(self.source / 'a.cc', None)
    os.utime(self.source / 'header.h', None)
    apk_checkpoint.normalize_sources(self.workspace)
    self.restore()
    self.assertEqual((self.output / 'a.o').stat().st_mtime_ns, object_time)
    resumed = subprocess.run(
        ['ninja', '-C',
         str(self.output), '-d', 'explain', '-j', '1', 'app'],
        capture_output=True,
        text=True,
        check=True)
    self.assertNotIn(' -c ../../a.cc ', resumed.stdout)
    self.assertEqual((self.output / 'a.o').stat().st_mtime_ns, object_time)
    self.assertEqual(self.key.read_bytes(), b'synthetic-development-key')
    subprocess.run([str(self.output / 'app')], check=True)
    unchanged = subprocess.run(
        ['ninja', '-C', str(self.output), 'app'],
        capture_output=True,
        text=True,
        check=True)
    self.assertIn('no work to do', unchanged.stdout)

  def test_exact_timestamps_and_permissions(self):
    """PAX timestamps must survive without float rounding or lost modes."""
    output = self.output / 'tool'
    output.write_text('generated')
    output.chmod(0o755)
    expected = 1700000000123456789
    os.utime(output, ns=(expected, expected))
    (self.output / 'link').symlink_to('tool')
    self.pack()
    shutil.rmtree(self.output)
    self.restore()
    self.assertEqual(output.stat().st_mtime_ns, expected)
    self.assertTrue(os.access(output, os.X_OK))
    self.assertEqual((self.output / 'link').read_text(), 'generated')

  def test_reject_oversized_checkpoint(self):
    """Do not silently rely on cache entries exceeding the storage budget."""

    def oversized_archive(*_args, **_kwargs):
      with self.archive.open('wb') as stream:
        stream.truncate(8 * 1024**3 + 1)

    with mock.patch(
        'apk_checkpoint.subprocess.run', side_effect=oversized_archive):
      with self.assertRaisesRegex(ValueError, 'exceeds 8 GiB'):
        self.pack()

  def test_identity_mismatch_rejected_before_extraction(self):
    """Different source/platform/image/path must not reuse object files."""
    (self.output / 'object.o').write_text('object')
    self.pack()
    shutil.rmtree(self.output)
    for attribute, value in [('revision', 'different'),
                             ('platform', 'android-arm')]:
      with self.subTest(attribute=attribute):
        original = getattr(self, attribute)
        setattr(self, attribute, value)
        with self.assertRaisesRegex(ValueError, 'differs'):
          self.restore()
        setattr(self, attribute, original)
    with mock.patch.dict(os.environ, {'ImageVersion': 'new-image'}):
      with self.assertRaisesRegex(ValueError, 'differs'):
        self.restore()
    with self.assertRaisesRegex(ValueError, 'differs'):
      apk_checkpoint.restore(self.workspace / 'elsewhere', self.platform,
                             self.revision, self.archive)
    self.assertFalse(self.output.exists())

  def test_normalization_excludes_outputs_and_git(self):
    """Normalize source, including deps, while retaining output/git mtimes."""
    source_file = self.source / 'a.cc'
    source_file.touch()
    git = self.source / '.git'
    git.mkdir()
    git_file = git / 'index'
    git_file.touch()
    output_file = self.output / 'object.o'
    output_file.touch()
    times = [p.stat().st_mtime_ns for p in (git_file, output_file)]
    apk_checkpoint.normalize_sources(self.workspace)
    self.assertEqual(source_file.stat().st_mtime_ns, 946684800000000000)
    self.assertEqual(times,
                     [p.stat().st_mtime_ns for p in (git_file, output_file)])

  def test_reject_source_overwrite_and_traversal(self):
    """An artifact must not overwrite checkout files or escape workspace."""
    for member_name in ('cobalt/src/a.cc', '../escape',
                        'cobalt/src/out/android-arm64_qa/../../escape'):
      with self.subTest(member=member_name):
        raw_archive = self.workspace / 'malicious.tar'
        with tarfile.open(raw_archive, 'w') as contents:
          identity = json.dumps(
              apk_checkpoint.checkpoint_identity(self.workspace, self.platform,
                                                 self.revision)).encode()
          for name, data in [('apk-checkpoint.json', identity),
                             (member_name, b'unexpected')]:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            contents.addfile(entry, io.BytesIO(data))
        subprocess.run(
            ['zstd', '-qf',
             str(raw_archive), '-o',
             str(self.archive)],
            check=True)
        with self.assertRaises((ValueError, tarfile.FilterError)):
          self.restore()
        self.assertFalse((self.source / 'a.cc').exists())


if __name__ == '__main__':
  unittest.main()
