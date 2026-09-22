# Third-party notices

Voucher Management is MIT-licensed. The components below retain their own
licenses. The exact license files distributed by the pinned packages are the
source of truth and must accompany the public Windows artifact where required.

## Runtime dependencies

| Component | Pinned version | Purpose | License / notice |
| --- | ---: | --- | --- |
| ReportLab | 4.5.1 | PDF generation | BSD license |
| Noto Sans | bundled static fonts | Unicode PDF text | SIL Open Font License 1.1 |
| Pillow | 12.3.0 | image and Windows print handling | MIT-CMU |
| pypdfium2 | 4.30.0 | embedded PDF rendering | Apache-2.0 or BSD-3-Clause |
| PDFium | wheel supplied with pypdfium2 | PDF engine | BSD-style plus bundled third-party licenses |
| pywin32 | 311 | Windows printer APIs | mixed licensing; packaged license files are authoritative |
| sv-ttk | 2.6.1 | ttk light/dark theme | MIT |
| darkdetect | 0.8.0 | Windows theme detection | BSD-3-Clause |
| cryptography | 50.0.1 | Scrypt KDF and AES-GCM backup encryption | Apache-2.0 or BSD-3-Clause |
| cffi | 2.1.1 | cryptography runtime dependency | MIT |
| pycparser | 3.0 | cffi runtime dependency | BSD-3-Clause |

### Noto Sans

The bundled Noto Sans Regular and Bold font files identify their copyright as
"Copyright 2015-2021 Google LLC."; the bundled Italic file identifies
"Copyright 2015-2022 Google LLC." They are distributed under the SIL Open Font
License 1.1. The upstream OFL text shipped with this project is in
`THIRD_PARTY_LICENSES/noto-sans/OFL.txt`; its license-header attribution names
"The Noto Project Authors".

### PDFium redistribution

pypdfium2 explicitly requires PDFium's license and the licenses of dependencies
bundled with PDFium to accompany binary redistributions. The release pipeline
collects the license material shipped by the pinned pypdfium2 wheel and also
packages the exact Apache-2.0 and BSD-3-Clause texts copied from the upstream
pypdfium2 4.30.0 source tag.

### pywin32

pywin32 states that it contains differently licensed code and that the license
files and per-file notices are authoritative. The release process must preserve
the applicable packaged notices.

## Build/test dependencies

| Component | Pinned version | Purpose | License |
| --- | ---: | --- | --- |
| pytest | 8.4.2 | automated tests | MIT |
| PyInstaller | 6.22.3 | Windows packaging | GPL-2.0-or-later with the PyInstaller bootloader exception |

The PyInstaller exception permits applications packaged with the bootloader to
be distributed under the application's own license, subject to the exception's
terms.

## Release rule

Before a signed public release:

1. install only the pinned dependency set in the declared GitHub Actions job;
2. generate an inventory/SBOM from that build;
3. collect the exact license files from the installed distributions/wheels;
4. include mandatory notices with the downloadable artifact;
5. rerun this review whenever any pinned dependency changes.

This file is an inventory and does not replace upstream license texts.
