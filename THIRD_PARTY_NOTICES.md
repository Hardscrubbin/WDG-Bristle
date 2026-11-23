# Third-party notices and media attribution

## Watch Dogs Go by LOCOSP

This fork builds on [LOCOSP's Watch Dogs Go](https://github.com/LOCOSP). Retain the original project [LICENSE](LICENSE), including its **Copyright (c) 2025-2026 LOCOSP** and MIT permission/disclaimer text. The original game, menus and upstream functionality must remain credited to LOCOSP; fork/integration and demo-editing credits are separate.

The software's MIT notice does not automatically relicense third-party fonts, music, artwork or dependencies.

Parts of this build come from [KiloGramowy's WDG Wars Go edition](https://github.com/KiloGramowy/WDG-Wars-Go-2.0-KG-Edition).

## Dark3D C5 and Piglet Collection compatibility

Dark3D's existing firmware is maintained by
[Dark3D](https://github.com/dark3d/ESP32DualBandWardriver), based on
[ESP32DualBandWardriver by Just Call Me Koko](https://github.com/justcallmekoko/ESP32DualBandWardriver).
The uConsole receiver integration is separate work. CSV interoperability
and a shared Collection receiver do not transfer authorship of Dark3D's existing
functions or transfer private source/build rights.

The official **`v2.3.0+dark3d.13`** is the CSV compatibility baseline.
Modified Dark3D Collection sender firmware awaits its own separately approved
release. No Dark3D source, build environment, ELF/MAP or image is redistributed
by this game candidate. The receiver is independently implemented format and
protocol interoperability code; it does not grant firmware redistribution rights.


## Spleen 5x8 font — BSD 2-Clause

`assets/spleen-5x8.bdf` contains the Spleen font by Frederic Cambus and its copyright/SPDX header. Preserve that header. The following full notice was checked against the [primary Spleen license source](https://github.com/fcambus/spleen/blob/5116f8eac7f65da2cfa169626ec43b5d041665fe/LICENSE) on 2026-11-01:

```text
Copyright (c) 2018-2026, Frederic Cambus
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  * Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

  * Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in the
    documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS
BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

## Other upstream assets

`assets/font_8x8.bdf`, the character/radio sprites, sound effects and existing landing-page assets were inherited from upstream. Do not describe them as newly authored by this fork. No separate license/provenance declaration for the 8x8 font was established in this preparation; no replacement license is invented here.

Upstream artwork attribution and any applicable third-party rights remain relevant. Its presence in the original repository is not a new claim that all depicted characters or external artwork are universally cleared. Record verified provenance before adding or redistributing additional artwork/media.

## Offline geographic data

The radio geography catalog is derived from [Natural Earth](https://www.naturalearthdata.com/about/terms-of-use/), whose data is public domain. Selected enclave policy centers are derived from [Wikidata's CC0 structured data](https://www.wikidata.org/wiki/Wikidata:Licensing). Retain the source URLs, pinned revisions, scope and hashes in `watchdogs/radio_countries.json`. These data sources are not measurements of the user's location or legal/survey boundary guarantees.


## Dependencies

**The entire WDG game remains open source.** Installed third-party Python/system packages retain their own licenses and notices. Scapy is GPL-2.0-only; its separately installed source package and notices retain those terms. This source release neither embeds Scapy into a closed executable nor relicenses it as MIT. Listing or installing a dependency does not place it under this project's MIT license. Keep applicable notices when building a bundled distribution rather than a source-only checkout.

## Default downloaded street basemap

The existing tile downloader uses **Alidade Smooth Dark** from Stadia Maps.

**Default street-basemap credits:** © [Stadia Maps](https://stadiamaps.com/attribution/) · © [OpenMapTiles](https://openmaptiles.org/) · © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).

See [Stadia Maps attribution requirements](https://docs.stadiamaps.com/attribution/). These provider/data credits are separate from the game's MIT license. They describe the default tile service, not verified input provenance for every owner-supplied illustration.
