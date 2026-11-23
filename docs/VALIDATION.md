# ✅ Validation and known limits

This page separates recorded software checks from real hardware evidence. Preparing documentation or a release branch does not itself repeat any radio, GPS, chat-delivery or server test.

## Prepared 0.9.18 — progress, BLE, mesh contacts and update notes

This combined candidate is **in preparation**, not a published update. It contains the LOCOSP format-1 loot migration fix, the Linux WiFi/BLE bridge completion fix, visible empty MeshCore/Meshtastic contacts and clearer update notes. The Deauth popup additions were removed at the owner's request; Deauth retains its earlier form and command handling. The public approved feed remains 0.9.17 until publication is authorized.

The final combined software suite after removing the Deauth additions passed on uConsole as root: **620/620 tests, no skips**. It includes **35 mesh UI tests** and **37 updater UI tests**. The preserved private Meshtastic capture was supplied outside Git for software replay; no packets were received or transmitted for those checks. The real updater and rollback also passed with synthetic player data, preserving newer progress. The earlier XP + BLE baseline passed 612 tests. These checks do not establish any Deauth RF behavior.

The **22 legacy-save cases** cover exact XP and level, explicitly saved zero, badges, contacts, discovery ledgers, removed session logs, byte-for-byte backups, restart, unsupported/corrupt/unreadable data, failed writes, conflicting or symbolic-link backups and the native startup failure path. The BLE bridge tests retain projectZero completion and verify save/STOP handover, adapter warnings and cancelled work. The reported AC1200 setup still needs its own hardware test.

An earlier Windows run exposed a missing test-environment dependency (`pyserial`), rather than a game assertion failure. The final uConsole run supplied the actual installed dependencies and all optional fixtures. Tests use temporary player data; the installed game and real progress were not replaced.


## Shutdown fix — 2026-11-01

The shutdown patch was tested before deployment in an isolated checkout and separate home/loot directories. It does not change RF presets, GPS policy, scoring rules, chat semantics or upload accounting.

| Check | Result |
| --- | --- |
| Windows main suite | 449 cases: **438 PASS / 11 SKIP**, no failures/errors. |
| uConsole main suite | 449 cases: **447 PASS / 2 SKIP**, no failures/errors. |
| Real uConsole normal quit | Real Meshtastic worker ready and ESP32 serial open; final STOP acknowledged, workers stopped, serial closed, managed LoRa/SDR/GPS/USB OFF. |
| Native Pyxel quit / window-close callback path | Same real hardware checks; Python atexit cleanup executed before native process exit. |
| Intentional isolated update-callback error | Cleanup completed before Pyxel's error exit; same resource and power checks passed. |
| Player data and installed source during staged tests | Unchanged; current data was backed up separately and fingerprinted. |

All four AIO rails were measured ON before each exit and OFF afterwards. GPS/SDR rail checks do not qualify GPS fixes or SDR reception. Real receiver readiness is not a new decoded-packet/reception claim. No test messages, WiFi/BLE attacks or local AUTO approvals were requested by the test harness.

The tests cover bounded shutdown STOP writes without tcdrain/flush, final versus preliminary firmware STOP responses, partial initialization, repeated cleanup, failed workers, callback errors, power ownership, queued scan cancellation and late auxiliary connector/process races. Optional platform/private-fixture skips remain explicit. Forced SIGKILL and power loss cannot execute cleanup; active flashing and unconfirmed worker release protect the affected power.

## Initial release-preparation checks — 2026-11-01

The initial documentation candidate `1304a5f` preserved the exact runtime, test and dependency-file blobs of `3481cf9`; that preparation changed documentation, packaging rules and reviewed media. Both earlier prototype and integration branch histories are included. The historical checks below ran in isolated checkouts without opening the physical radio. The later shutdown changes and their separate hardware checks are recorded above.

| Fresh software check | Cases | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| Windows main suite | 370 | 359 | 11 | 0 |
| uConsole main suite | 370 | 368 | 2 | 0 |
| uConsole standalone receiver/SPI regression suites | 49 | 49 | 0 | 0 |

The Windows standalone receiver/SPI run initially passed 47 of 49 cases with two missing-driver-source skips. Supplying the existing LoRaRF 1.4.0 source let both skipped adapter-contract cases pass separately. These exercise fake SPI/GPIO transports with real driver code; they remain software tests, not RF reception.

Platform/optional-fixture skips remain documented in the test output. Packaging checks cover local documentation links, image metadata, excluded runtime/credential files, source preservation, media hashes and decoder integrity. Public demo conversations and node information are intentionally visible; precise location and credentials are hidden.

## Historical software baseline

The recorded consolidated baseline is commit `3481cf9ea2486248d2710b8e19a29cb3bc1a6d08`, dated **2026-10-30**, on top of Watch Dogs Go 0.9.16.

| Platform | Tests run | Passed | Skipped | Failures/errors |
| --- | ---: | ---: | ---: | ---: |
| uConsole | 370 | 368 | 2 | 0 |
| Windows | 370 | 359 | 11 | 0 |

These are historical recorded runs, **not a new test result for this release-preparation branch**. Skips include optional/private replay or platform-dependent checks; they are not physical RF verification. Before a release, record the new candidate's exact commit, commands, results and skip reasons.

The regression coverage includes packet/CRC validation, declared NodeID parsing, deduplication and stored discovery awards, GPS freshness and RF policy, UI controls, scanner USB selection, STOP acknowledgements, alternating wardrive, CSV persistence failures, and upload receipt accounting including cooldown and pending-cache invalidation.

## Recorded hardware and server evidence

| Area | Recorded evidence | What it does not establish |
| --- | --- | --- |
| Meshtastic SFNarrow | Genuine natural packet reception on uConsole, including supported public messages and contact data. | Guaranteed coverage, direct range to a packet author, authenticated identities, or reception on every preset. |
| WiFi/BLE alternation | Seven complete WiFi and seven complete BLE phases were recorded with GPS and CSV output; LoRa remained active on its separate hardware. | Simultaneous WiFi and BLE scanning, unlimited throughput, or compatibility with every ESP32 firmware/USB device. |
| GPS/profile UI | Real GPS status and region resolution, manual controls and on-screen profile/assignment dialogs were checked. | A fresh qualification of every approved AUTO retune, country, border condition or RF preset on this candidate branch. |
| WDGWars upload | Complete application-level receipts and recorded DONE results were checked against submitted counts, including known and cooldown-handled observations. | New import credit for cooldown/known records, ownership transfer, or continued compatibility with an independently changed server. |
| Chat | Real public reception and a user-initiated local radio transmit completion were recorded. | End-to-end delivery, another device receiving the nickname, or a read receipt. |

## Important boundaries

- A passing test with a fake radio, injected failure or replayed frame is a software result. It must not be presented as new physical reception.
- A quiet RF session is inconclusive: it can reflect no traffic, different settings, range or a receiver problem. It is not proof that a radio is broken.
- MeshCore local reception on a newly selected Spanish preset has not been established by these results. The separately documented SF7 / CR4/6 variant is not implemented in the current catalog.
- US profiles and the installed RF path/antenna have not been physically qualified by this validation. Not every MeshCore preset has an on-air test.
- GPS recognition does not discover local RF settings. Existing user approvals and user-reported experience must not be replaced with invented approvals or inferred country-wide defaults during testing.
- HTTP 200, a cached duplicate notice, a UI ON label, a local XP award and a server discovery credit are different observations.

## Before calling a candidate validated

Use a separate checkout and preserve current player/configuration data. Run the relevant existing suites, document all skips, inspect the English dialogs without changing their layout, and distinguish synthetic fixtures from actual device results. Record remaining hardware gaps explicitly. A documentation-only preparation should remain labelled as such until those checks are performed.
