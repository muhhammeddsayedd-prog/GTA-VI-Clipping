# Fonts

| File | Font | Source | Licence |
|---|---|---|---|
| `Montserrat-Black.ttf` | Montserrat Black 9.000 (subtitles, hook, watermark, end card: the `factory.font` setting) | https://github.com/JulietaUla/Montserrat, commit `f1f4e9b630b6ac52f1c55c572b29d2a5c3535139`, `fonts/ttf/Montserrat-Black.ttf` | SIL Open Font License 1.1, see `OFL-Montserrat.txt` |
| `NotoEmoji.ttf` | Noto Emoji (variable weight, monochrome; draws the emoji in hooks and the camera in the creator credit) | https://github.com/google/fonts, `ofl/notoemoji/NotoEmoji[wght].ttf` | SIL Open Font License 1.1, see `OFL-NotoEmoji.txt` |
| `Anton-Regular.ttf` | Anton (heavy condensed capitals; the captions of the `factory.style` look, setting `factory.style.caption_font`) | https://github.com/google/fonts, `ofl/anton/Anton-Regular.ttf` | SIL Open Font License 1.1, see `OFL-Anton.txt` |
| `TikTokSans-Variable.ttf` | TikTok Sans (variable; drawn at weight 420 for the docs/27 title bar, the look of TikTok's own text box) | https://github.com/google/fonts, `ofl/tiktoksans/TikTokSans[opsz,slnt,wdth,wght].ttf` | SIL Open Font License 1.1, see `OFL-TikTokSans.txt` |
| `Inter-Variable.ttf` | Inter (variable; drawn at weight 600 for the cover engine's TikTok-native text box, docs/29a section 6.4) | https://github.com/google/fonts, `ofl/inter/Inter[opsz,wght].ttf` (sha256 `29160a80…c559031`) | SIL Open Font License 1.1, see `OFL-Inter.txt` |

The title bar's colour emoji come from a system font, not from this directory: Debian's `fonts-noto-color-emoji` (installed in `docker/Dockerfile`, SIL OFL 1.1) or Segoe UI Emoji on Windows; without one the monochrome `NotoEmoji.ttf` is used.

libass gets this directory through `ass=fontsdir=`, so neither font has to be installed on the machine.
