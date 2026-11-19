# OHLC chart (`ohlc`)

Open, high, low and close per session as tick bars, with optional volume bars.

## When to use
- Price action where the bar itself should show direction without colour (open tick left, close tick right).

## When not to use
- Only the close matters: `line`.
- Readers used to candles: `candlestick` (same input).

## Input
Same input and validation as `candlestick`: rows of `{ date, open, high, low, close, volume? }`; `high` must be at least `max(open, close)`, `low` at most `min(open, close)`.
```json
[{ "date": "2026-01-05", "open": 100, "high": 103.6, "low": 99.9, "close": 102.4 }]
```
Also accepted: CSV text, column arrays, a field mapping.

## Options
| Option | Type | Default | |
|---|---|---|---|
| `volume` | boolean | `true` | volume bars in the bottom fifth (when the data has volume) |
| `yTitle` | string | — | price axis title |

Zoom (`zoom: true`) and brush act on x.

Every chart also takes the base options (`valueFormat`, `locale`, `timeZone`, `theme`, `patterns`, `title`, ...; see `core/fields.ts`).

## Example
```ts
import { chart } from '.omp/charts'
chart('ohlc', el, rows, { zoom: true })
```

## Edge cases
Up bars in the positive colour, down bars in the negative one. Sessions are sorted by instant. Repeated dates are kept and noted. Missing volume → no bar. 5,020 sessions → canvas.
