# Keel Logo

## Files

| File | Description |
|------|-------------|
| `keel-logo.svg` | Primary mark. Twin pillars with hull curvature + exposed teal keel. Use on light backgrounds. |
| `keel-logo-light.svg` | Inverse mark. White pillars + teal keel. Use on dark backgrounds. |
| `keel-logo-favicon.svg` | Simplified monochrome mark (no teal accent). Use at 16-32px sizes. |
| `keel-logo-wordmark.svg` | Mark + "Keel" wordmark (Syne 800 vector paths), horizontal layout. Light backgrounds. |
| `keel-logo-wordmark-light.svg` | Mark + "Keel" wordmark (Syne 800 vector paths), horizontal layout. Dark backgrounds. |

## Typography

- **Wordmark font**: Syne ExtraBold (800), tracking -0.5px
- **Source**: [Google Fonts — Syne](https://fonts.google.com/specimen/Syne)
- The wordmark SVG files use vector paths (text → outlines) for production use — no external font dependency required.
- For web contexts where the font is already loaded, use: `font-family: 'Syne', system-ui, sans-serif; font-weight: 800; letter-spacing: -0.5px;`

## Colors

| Role | Hex | Usage |
|------|-----|-------|
| Primary | `#1a2332` | Pillar fill, wordmark text |
| Accent | `#0ea5a0` | Exposed keel line |
| Surface (light) | `#ffffff` | Background, inverse pillar fill |
| Surface (dark) | `#1a2332` | Background for inverse version |

## Design Rationale

- **Twin pillars**: Two vertical pillars with hull curvature on their outer edges. Represents the encapsulation work built on top of the keel — clean, solid, practical.
- **Exposed keel**: A teal line running through the gap between pillars, extending above them. Represents the structural backbone (keel) that underpins everything — visible, honest, foundational.
- **Hull curvature**: Outer edges of the pillars have a subtle outward curve, evoking a ship's hull. The encapsulation itself is the hull.
- **Favicon**: Drops the teal accent for reliability at small sizes. Pure twin-pillar shape.

## Size Guidelines

- **Full mark (keel-logo.svg)**: 32px and above. Teal keel becomes visible around 32px.
- **Favicon (keel-logo-favicon.svg)**: 16-32px. The simplified shape holds better at small sizes.
- **Wordmark (keel-logo-wordmark.svg)**: Headers, documentation, README. Minimum display width ~200px.

## Usage Notes

- The mark is designed for flat backgrounds. Avoid placing on gradient or image backgrounds.
- Minimum clear space around the mark: 1/4 of the mark height on all sides.
- For the wordmark, the text has been converted to vector paths (Syne 800). The SVG is self-contained with no font dependency.
