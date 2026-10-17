# Third-party assets

The code in this repository is MIT (see `LICENSE`). The files below are not ours
and keep their own license; they are listed here rather than inside `LICENSE`
because inserting anything into the MIT text stops GitHub recognising the license,
and an unrecognised license reads as "all rights reserved".

## Fonts — SIL Open Font License 1.1

`src/chrono_handoff/statics/fonts/`:

| File | Typeface |
|---|---|
| `inter-latin.woff2`, `inter-latin-ext.woff2` | Inter — (c) The Inter Project Authors |
| `jbmono-latin.woff2` | JetBrains Mono — (c) JetBrains s.r.o. |

Both are redistributed under the SIL Open Font License 1.1, which permits
redistribution and bundling: <https://openfontlicense.org/>

They are bundled rather than fetched from a CDN so that the cue panel works with
no network at all — see `docs/component.md`.
