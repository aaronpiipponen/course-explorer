# Model presets

A model preset is one tracked set of inference and translation model values that the worker install writes into two working files.

## What a preset holds

Each preset is one directory under `examples/config/presets/` that holds an `inference.toml` and a `translation.toml` fragment. The install offers a preset when its directory holds both files.

The inference fragment holds one key, `INFERENCE_LOCAL_VARIANTS`, with two local keys in order: the main key and the fallback key. Each key names a source model repository and revision, the model artifact's repository, revision, file and SHA-256 digest, and its context size. The two keys of a preset name different model artifacts, and the fallback sets the larger context size.

The translation fragment names the translation model's alias, repository and revision, its model artifact's repository, revision, file, path and SHA-256 digest, and `TRANSLATION_CONTEXT_SIZE`.

## The three presets

The `8gb` preset targets an 8 GB card, the `12gb` preset a 12 GB card and the `16gb` preset a 16 GB card. Each preset names two local keys and one translation artifact.

| Preset | Key | Source model | Model artifact | Context |
|---|---|---|---|---|
| `8gb` | Main | `google/gemma-4-12B-it` | `gemma-4-12b-it-Q3_K_M.gguf` | 131072 tokens |
| `8gb` | Fallback | `google/gemma-4-12B-it` | `gemma-4-12b-it-Q3_K_S.gguf` | 262144 tokens |
| `12gb` | Main | `Qwen/Qwen3.8-27B` | `Qwen3.8-27B-UD-IQ2_S.gguf` | 131072 tokens |
| `12gb` | Fallback | `google/gemma-4-12B-it` | `gemma-4-12b-it-Q3_K_M.gguf` | 262144 tokens |
| `16gb` | Main | `Qwen/Qwen3.8-27B-FP8` | `Qwen3.8-27B-IQ3-MIX.gguf` | 131072 tokens |
| `16gb` | Fallback | `Qwen/Qwen3.8-27B` | `Qwen3.8-27B-UD-IQ2_S.gguf` | 262144 tokens |

The Gemma 4 artifacts are published in `unsloth/gemma-4-12b-it-GGUF`, the Qwen3.8-27B UD-IQ2_S artifact in `unsloth/Qwen3.8-27B-GGUF`, and the IQ3-MIX artifact in `vmarcelo/Qwen3.8-27B-MIX_GGUF`.

The 12 GB preset's fallback is the 8 GB preset's main artifact, and the 16 GB preset's fallback is the 12 GB preset's main artifact, so the install downloads a shared model artifact once. The 8 GB preset's two keys name one model, `google/gemma-4-12B-it`, in two artifacts: both name the artifact repository `unsloth/gemma-4-12b-it-GGUF`, and the main key names `gemma-4-12b-it-Q3_K_M.gguf` and the fallback key `gemma-4-12b-it-Q3_K_S.gguf`. The 16 GB preset's main key is the only key with MTP speculation.

The translation artifact is `translategemma-12b-it.Q2_K.gguf` from `mradermacher/translategemma-12b-it-GGUF` at 16384 tokens for the `8gb` preset, and `translategemma-12b-it.Q5_K_M.gguf` from the same repository at the same context for the `12gb` and `16gb` presets. Both files are conversions of `google/translategemma-12b-it`.

## How the install applies a preset

The install asks the preset question last among its startup questions, which [Installing a worker](/docs/getting-started/worker-install.md#the-install-prompts) lists. The offered names are the tracked directories that hold both fragments, in sorted order, and `keep` accepts the values the working files already hold. An answer outside that list refuses the run with `unknown model preset: <name>` before any package is installed and before any working file is written.

A chosen preset writes the `INFERENCE_LOCAL_VARIANTS` array into `config/inference.toml` and the translation values of its fragment into `config/translation.toml`, and the install prints one line that names the applied preset, in the form `model preset: applied 16gb`. The answer `keep` writes neither file and prints `model preset: kept the current configuration values`.

Both fragments are validated against the working files before either file is written. The install replaces the value of every key its fragment names and leaves every other key and comment in the two files as it is. A key a fragment names must be present in the working file: a working `config/translation.toml` that names a local translation conversion omits the three artifact source keys, and the install refuses the preset there rather than adding them. A refusal names the preset and stops before any generated password, worker identity value or preset value is written.

The tracked `examples/config/inference.toml` and `examples/config/translation.toml` hold the 16 GB preset's values, so the working files of a checkout that materializes the templates and answers `keep` hold the 16 GB pair.

The install downloads an absent model artifact from the address derived from its repository, revision and file, and verifies the file against the preset's digest.
