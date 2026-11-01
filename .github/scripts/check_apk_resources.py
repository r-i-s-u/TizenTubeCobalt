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
"""Run Chromium's shell resource processor before expensive APK compilation."""

import ast
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def main():
  """Pack the resources declared by the literal GN sources list."""
  root = Path(__file__).resolve().parents[2]
  target = root / 'cobalt/shell/android'
  match = re.search(
      r'android_resources\("cobalt_shell_java_resources"\) \{\s*'
      r'sources = (\[[\s\S]*?\])', (target / 'BUILD.gn').read_text())
  if match is None:
    raise ValueError('Cannot find the shell resource sources list in BUILD.gn')
  sources = ast.literal_eval(match.group(1))
  with tempfile.TemporaryDirectory(prefix='apk-resource-check-') as temporary:
    output = Path(temporary)
    manifest = output / 'resources.sources'
    manifest.write_text(''.join(str(target / name) + '\n' for name in sources))
    subprocess.run([
        sys.executable, 'build/android/gyp/prepare_resources.py',
        '--res-sources-path',
        str(manifest), '--resource-zip-out',
        str(output / 'resources.zip'), '--r-text-out',
        str(output / 'R.txt')
    ],
                   cwd=root,
                   check=True)
  print('Shell Android resources packed successfully, including translations.')


if __name__ == '__main__':
  main()
