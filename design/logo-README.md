# Keel Logo

## Files

| File | Description |
|------|-------------|
| `keel-logo.svg` | Primary Cd mark. Split blade keel with restrained teal core signal. Use on light backgrounds. |
| `keel-logo-light.svg` | Inverse Cd mark. White split blades + teal core signal. Use on dark backgrounds. |
| `keel-logo-favicon.svg` | Simplified Ca mark. Pure split blades with no teal accent. Use at 16-32px sizes. |
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
| Primary | `#1a2332` | Split blade fill, wordmark text |
| Accent | `#0ea5a0` | Restrained core signal |
| Surface (light) | `#ffffff` | Background, inverse blade fill |
| Surface (dark) | `#1a2332` | Background for inverse version |

## Design Rationale

- **Split blade mark**: Two asymmetric blade forms converge into a compact coordination mark. Sharp outer cuts and lightly curved inner edges keep it structural without becoming a literal boat.
- **Core signal**: A short teal pulse sits inside the split. It adds a live coordination cue while staying secondary to the dark blade silhouette.
- **Responsive system**: Cd is the primary mark for product and documentation contexts; Ca removes the teal signal for favicon, monochrome, and very small sizes.
- **Wordmark lockup**: The mark is reduced to roughly 1.3x the Syne cap height and spaced to sit with the wordmark instead of reading as a separate icon.

## Size Guidelines

- **Full mark (keel-logo.svg)**: 32px and above as a standalone mark. In compact navigation lockups, it can sit at 28px when paired with the Keel word.
- **Favicon (keel-logo-favicon.svg)**: 16-32px. The simplified Ca shape holds better at small sizes and avoids subpixel accent noise.
- **Wordmark (keel-logo-wordmark.svg)**: Headers, documentation, README. Minimum display width ~200px.

## Usage Notes

- The mark is designed for flat backgrounds. Avoid placing on gradient or image backgrounds.
- Minimum clear space around the mark: 1/4 of the mark height on all sides.
- For the wordmark, the text has been converted to vector paths (Syne 800). The SVG is self-contained with no font dependency.
