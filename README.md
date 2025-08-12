# Fenix Startup Profiling

Automates the Android startup profiling workflow described in [MANUAL_WORKFLOW.md](MANUAL_WORKFLOW.md) for creating merged profiles from simpleperf and the Gecko Profiler.

Requires a Fenix build with the patch from [bug 1956859](https://bugzilla.mozilla.org/show_bug.cgi?id=1956859) applied, so that Gecko profiles can be captured automatically.

**Requires a rooted Android device**, `su` must be available.

This whole workflow is a workaround for the following facts:

- The Gecko Profiler's stack information is insufficient.
- Simpleperf's marker information is insufficient.

With markers from the Gecko Profiler and samples from simpleperf, we can approximate a more useful profiling infrastructure.

## Setup

Ensure you have [uv](https://docs.astral.sh/uv/) and [samply](https://github.com/mstange/samply) installed:

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install samply
cargo install --force --git https://github.com/mstange/samply samply
```

You also need node and adb.

## Usage

Run the script:

```bash
uv run main.py
```

This will set up startup profiling via adb, kill Fenix, start it with a VIEW intent and a default URL, wait 10 seconds, capture and merge the profiles, and then open a merged profile in your browser. Example: [https://share.firefox.dev/4lm0HAj](https://share.firefox.dev/4lm0HAj)

```bash
uv run main.py --java --url https://en.wikipedia.org/
```

Similar, but with Java stacks and a custom URL. Example: [https://share.firefox.dev/4fv4Lgg](https://share.firefox.dev/4fv4Lgg)

## Configuration

You can override all the default settings in `config.toml`. This is also where you add any extra symbol paths. For example, for local builds, you must have the dist/bin directory in `symbol_dirs`.

### CLI Examples

Arguments passed via CLI override those in the `config.toml`.

```bash
uv run main.py --package org.mozilla.fenix --duration 5 --frequency 2000
uv run main.py --config my-config.toml
uv run main.py --device emulator-5554
uv run main.py --java
uv run main.py --url "https://example.com/test-page"
uv run main.py --out ~/profiles/my-profile.json.gz
uv run main.py --log-level DEBUG
uv run main.py --device emulator-5554 --package org.mozilla.fenix --duration 20 --java
```

### Available CLI Arguments

- `--config`: Path to configuration file (default: config.toml)
- `--device`: Android device ID (required when multiple devices connected)
- `--java`: Use Java callgraph (-g) instead of native (--call-graph fp)
- `--package`: Override package name from config
- `--url`: Override startup URL from config  
- `--duration`: Override profiling duration in seconds from config
- `--frequency`: Override profiling frequency in Hz from config
- `--out`, `-o`: Output path for merged profile (if not specified, auto-loads with samply)
- `--log-level`: Set logging level (DEBUG, INFO, WARNING, ERROR; default: INFO)

## Development

### Available Make Commands

```bash
make help           # Show available commands
make install        # Install dependencies
make install-dev    # Install with dev dependencies
make format         # Format code
make lint           # Lint code
make lint-fix       # Lint and auto-fix issues
make typecheck      # Type check with mypy
make check          # Run all checks
make clean          # Clean temporary files
```