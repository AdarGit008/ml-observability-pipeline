# ML Observability Pipeline — Visual Set

A set of four precise architecture diagrams plus one AI hero-image prompt, for the
README, portfolio, and slide decks. Diagrams are hand-built SVG (accurate, editable,
infinitely scalable). The hero image is generated separately — it's decorative cover
art, not a technical reference.

## Diagrams (SVG)

| File | Shows |
|------|-------|
| `01-overview.svg` | The big picture: one shared scoring brain, two runtimes (local $0 / AWS ephemeral). |
| `02-components.svg` | Component catalog grouped by layer: simulator, shared contract, local runtime, 4 Lambdas, AWS infra. |
| `03-features.svg` | Eight capabilities: scoring, PSI drift, warmup gate, fleet-PSI, edge alerts, hot+cold paths, parity, cost. |
| `04-data-flow.svg` | End-to-end AWS data flow with color-coded hot / cold / fleet-drift / visualization paths. |

### Exporting to PNG (for slides / social)

SVGs render crisply in browsers and GitHub. For raster exports:

```bash
# requires: npm i -g sharp-cli   (or use Inkscape / rsvg-convert)
sharp -i 01-overview.svg -o 01-overview.png resize 2040   # 3x for retina
```

Or open the `.svg` in a browser and screenshot, or drag into Figma/Inkscape.

---

## AI hero image — prompt

Use for a stylized cover image only (top of README, title slide). It will **not**
contain accurate labels or flow — that's what the SVGs are for. Aim for a wide
banner: **21:9** or **16:9**.

### Primary prompt (Midjourney / DALL·E / Ideogram)

> Clean, modern editorial tech illustration of an industrial **pump fleet** monitored
> by a machine-learning observability system. A row of stylized centrifugal pumps on
> the left, each emitting thin glowing telemetry streams that flow rightward and
> converge into a single luminous central "scoring core." From the core, the streams
> fan out into a calm analytics dashboard with sparklines and a subtle drift wave.
> Flat vector aesthetic, generous negative space, soft paper-white background.
> Restrained palette: indigo, teal, and warm amber accents on off-white. Minimal,
> sophisticated, precise — like a high-end developer-tooling brand. Isometric, gentle
> depth, no text, no logos. --ar 16:9 --style raw --v 6

### Variant A — "two runtimes" motif

> Same industrial-pump-fleet ML observability scene, but the central scoring core
> visibly splits into **two mirrored runtime paths** — one labeled-feeling as a small
> local laptop cluster, one as a soft cloud — to express "identical logic, two
> environments." Symmetrical composition, flat vector, indigo/teal/amber on warm white,
> lots of whitespace, no text. --ar 21:9 --style raw --v 6

### Variant B — "drift detection" motif

> Minimalist abstract data-art: a fleet of small pump icons feeding a smooth baseline
> distribution curve that gradually warps into a drifted curve, with one amber alert
> pulse rising above the rest. Quiet, scientific, elegant. Flat illustration, indigo
> and teal with a single amber highlight, off-white background, ample negative space,
> no text. --ar 16:9 --style raw --v 6

### Negative prompt (Stable Diffusion / tools that support it)

> text, words, letters, labels, logos, watermark, UI chrome, cluttered, busy,
> photorealistic, 3d render noise, neon cyberpunk, dark background, lens flare, clip art

### Tips
- AI generators garble text — keep `no text` in the prompt and add titles yourself.
- Generate 4, pick one, upscale. Re-roll rather than fighting a bad composition.
- Match the SVG palette (indigo `#7F77DD`, teal `#1D9E75`, amber `#EF9F27`, ink `#2C2C2A`
  on warm white `#FCFBF8`) so the cover and diagrams feel like one set.
