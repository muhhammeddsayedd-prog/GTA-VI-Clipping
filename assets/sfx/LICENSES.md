# Sound effects: licences

| File | Source | Licence |
|---|---|---|
| `pop.wav` | Made for this project by a script: a sine sweep from 1150 Hz down to 280 Hz, 90 ms long, with a 2 ms attack and an exponential decay, 48 kHz mono 16-bit. No recording or sample from anyone else is in it. | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain dedication) |
| `whoosh.wav` | Made for this project with ffmpeg `aevalsrc`: 0.45 s of white noise whose level rises with the square of time to a peak at 0.3 s and then decays, band-passed around 1.4 kHz, limited at -1 dBFS, 48 kHz stereo 16-bit. | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `impact.wav` | Made for this project with ffmpeg `aevalsrc`: a 75 Hz sine falling from 235 Hz plus its octave and a 30 ms noise burst, soft-clipped for harmonics, 0.6 s, 48 kHz stereo 16-bit. | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |
| `boom.wav` | Made for this project with ffmpeg `aevalsrc`: a 62 Hz sine falling from 200 Hz plus its octave, decaying over 1.6 s, soft-clipped so the harmonics carry on phone speakers, 48 kHz stereo 16-bit. | [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) |

No recording or sample from anyone else is in any of these files.

The factory mixes `pop.wav` in when a subtitle keyword is spoken (`docs/09-clip-factory.md`, section 5). With the `factory.style` setting (docs/27), `whoosh.wav` leads into each punch-in, `impact.wav` lands on its first frame and `boom.wav` on the payoff; all effects play at `factory.style.sfx_gain_db`.
