# Third-party notices

Imperium release bundles contain or can optionally redistribute the following
third-party components. They are not authored by the Imperium project and
remain subject to their own licenses.

## Python

The private Python runtime is distributed by the Python Software Foundation.
Its license is included as `runtime/python/LICENSE.txt` in the Windows bundle.

- Source: https://www.python.org/
- License: PSF License Agreement

## llama.cpp

Managed and offline runtime assets are official llama.cpp release archives.
Their upstream license remains inside the archive and this notice accompanies
the bundle.

- Source: https://github.com/ggml-org/llama.cpp
- License: MIT

## Qwen3 1.7B GGUF

The optional offline first-run model is `Qwen3-1.7B-Q8_0.gguf`, downloaded from
the revision and checksum pinned in `model_catalog.json`.

- Source: https://huggingface.co/Qwen/Qwen3-1.7B-GGUF
- License: Apache License 2.0
- License text: `licenses/Apache-2.0.txt`

Python packages installed in the private runtime include their own package
metadata and license files under `runtime/python/Lib/site-packages`.
