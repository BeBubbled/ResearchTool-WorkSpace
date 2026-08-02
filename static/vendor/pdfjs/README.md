# PDF.js vendored runtime

This directory contains the runtime subset of Mozilla PDF.js 6.1.200 from the
official `pdfjs-6.1.200-dist.zip` release.

Retained files:

- `build/pdf.mjs`
- `build/pdf.worker.mjs`
- character maps, ICC profiles, standard fonts, and WASM decoders under `web/`
- the upstream Apache 2.0 `LICENSE`

The reader loads these files locally so opening a non-OCR PDF does not require a
CDN or send the document to a third party.
