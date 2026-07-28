## Added

- Added a persistent local GGUF directory picker shared by first-run setup,
  the Models page, and local model choices in Chat.

## Changed

- Managed setup downloads Qwen3 into the selected directory and enables llama.cpp model hot-swapping.

## Fixed

- Kept offline runtime planning tests isolated from machine-local executables.
- Accepted shallow system Python installation paths during setup discovery.
- Installed-model names, details, and paths stay inside their dashboard cards.
