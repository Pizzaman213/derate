# Candlestick chart (`candlestick`)

Open, high, low and close per session as candles, with optional volume bars.

## When to use
- Price action of one instrument over days to years.

## When not to use
- Only the close matters: `line`.
- Hiding the colour must still read: candles are hollow when up and filled when down; `ohlc` encodes direction in tick positions.

## Input
Rows of `{ date, open, high, low, close, volume? }`. Validation rejects `high < max(open, close)` and `low > min(open, close)` with the row number ("field 'high': must be at least max(open, close) = 12, got 11 at row 3").
```json
[{ "date": "2026-01-05", "open": 100, "high": 103.6, "low": 99.9, "close": 102.4, "volume": 1200000 }]
```
Also accepted: CSV (a typical export with extra columns such as `Adj Close`), column arrays, and a field mapping: `{ fields: { date: 'Date', open: 'Open', ... } }`.

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
chart('candlestick', el, csv, { fields: { date: 'Date', open: 'Open', high: 'High', low: 'Low', close: 'Close', volume: 'Volume' }, valueFormat: 'currency', zoom: true })
```

## Edge cases
Up = close ≥ open: hollow, positive colour; down: filled, negative colour (pattern 0 with `patterns`). Sessions are sorted by instant (unsorted and mixed-offset inputs). Repeated dates are kept and noted. Zeros → flat dashes, no volume. Negative prices draw below zero. Missing volume → no bar. 5,020 sessions → canvas, candles at least 1px.
