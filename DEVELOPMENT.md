# Developing WDG-Bristle

The complete game is open source. LOCOSP's game is under LOCOSP's [MIT licence](LICENSE); the
additions of this repository are under [LICENSE-HARDSCRUBBIN](LICENSE-HARDSCRUBBIN). Keep both,
the attribution and the [third-party notices](THIRD_PARTY_NOTICES.md).
Use a separate clone and synthetic player data for development; never run the
suite against a live player profile, radio or upload account.

```sh
python -B -m unittest discover -s tests -v
python -B -m unittest test_meshtastic_rx_probe test_spi_mutation_regression
```

Optional private replay/archive fixtures are supplied outside Git, or their
checks skip. The 30/100 MiB upload-capacity cases are opt-in synthetic HTTP tests;
see their test file for environment variables and memory needs. A test pass does not
establish fresh RF, uConsole, Windows or flash compatibility.

Keep runtime data, GPS traces, keys, credentials, capture files, build caches and
backup archives out of this repository. Preserve protocols, durable ACKs, XP rules and
existing user data. This build has no in-game updater: see [updating](docs/UPDATING.md).
