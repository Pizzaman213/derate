# Performance report

Generated 2026-11-19T18:44:10.135Z by `npm run charts:perf` — headless Chromium (Playwright 1.65.1), 960 px wide, animation off, one warm-up render per chart. Time is `chart()` to return: validation, layout and painting the SVG/canvas.

Budgets: **< 100 ms** for up to 1,000 SVG marks; **< 500 ms** on the canvas path (above 5,000 marks, tested at 50,000) and for every `edge-large` input (>5k rows even when they reduce to few marks, e.g. a KDE over 16k values); 250 ms in between.

Result: 18/18 within budget.

| Chart | Input | Marks | Renderer | First render (ms) | Budget (ms) | |
|---|---|---:|---|---:|---:|---|
| donut | basic | 5 | svg | 1.7 | 100 | ok |
| donut | realistic | 6 | svg | 3.4 | 100 | ok |
| donut | edge-large | 6 | svg | 5.8 | 500 | ok |
| grouped-bar | basic | 12 | svg | 1.9 | 100 | ok |
| grouped-bar | realistic | 18 | svg | 2.6 | 100 | ok |
| grouped-bar | edge-large | 5,200 | canvas | 48.7 | 500 | ok |
| multi-line | basic | 48 | svg | 3.4 | 100 | ok |
| multi-line | realistic | 260 | svg | 11.4 | 100 | ok |
| multi-line | edge-large | 5,040 | canvas | 62.7 | 500 | ok |
| pie | basic | 5 | svg | 1.6 | 100 | ok |
| pie | realistic | 6 | svg | 1.8 | 100 | ok |
| pie | edge-large | 6 | svg | 4.2 | 500 | ok |
| stacked-bar | basic | 12 | svg | 1.0 | 100 | ok |
| stacked-bar | realistic | 24 | svg | 1.8 | 100 | ok |
| stacked-bar | edge-large | 5,200 | canvas | 53.7 | 500 | ok |
| stacked-bar-100 | basic | 12 | svg | 1.4 | 100 | ok |
| stacked-bar-100 | realistic | 24 | svg | 2.9 | 100 | ok |
| stacked-bar-100 | edge-large | 5,200 | canvas | 74.5 | 500 | ok |
