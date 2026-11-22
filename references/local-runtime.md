# Runtime environment

Use the actual installed Skill directory as `<skill-root>`. A source checkout is for maintenance and testing. `kb.py version` reports the real executable and configuration; it does not accept `--json` because its output is already JSON.

A local installation may replace this file with its verified host mapping, including its canonical WSL distribution and installed directory. Do not guess a path or copy a second data root. Windows callers with a configured WSL runtime invoke that same wrapper through `wsl.exe -d <distribution> --exec python3 <installed-wrapper> ...`.

Keep `config.json`, local credentials, retained files and backup configuration private. A portable release ships `config.example.json`; installing code must preserve the actual local configuration. Existing private test fixtures remain local unless independently reviewed for publication.
