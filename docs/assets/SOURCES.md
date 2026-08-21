# Asset sources

The public ContextForge image assets are tracked here so their origin and
release treatment remain reviewable.

| Asset | Source | Date | Maintainer treatment |
| --- | --- | --- | --- |
| `contextforge-icon.png` | Generated with OpenAI image generation | 2026-08-02 | Selected and curated by `waterflane`; mechanically resized to 512×512 and stripped of embedded metadata on 2026-08-12 |
| `contextforge-banner.png` | Generated with OpenAI image generation | 2026-08-02 | Selected and curated by `waterflane`; mechanically resized to 1600×500 and stripped of embedded metadata on 2026-08-12 |
| `contextforge-social-preview.png` | Derived from `contextforge-banner.png` | 2026-08-12 | Mechanically resized and padded to 1280×640 for GitHub Social Preview |

The original generated files contained C2PA provenance identifying the OpenAI
Media Service API and trained-algorithmic media. The manifest also carried an
OpenAI provenance icon as metadata; it was not part of the visible ContextForge
design and was removed during release optimization. No prompt text, local path,
username, third-party source image, or third-party logo was intentionally
supplied or incorporated into the visible design.

To the extent the ContextForge maintainer owns rights in these assets, those
rights are made available with the project under Apache License 2.0. The project
name and visual identity are not a grant of trademark rights beyond the license
terms.
