# Performance report

Generated 2026-11-19T01:48:00.707Z by `npm run charts:perf` — headless Chromium (Playwright 1.65.1), 960 px wide, animation off, one warm-up render per chart. Time is `chart()` to return: validation, layout and painting the SVG/canvas.

Budgets: **< 100 ms** for up to 1,000 SVG marks; **< 500 ms** on the canvas path (above 5,000 marks, tested at 50,000) and for every `edge-large` input (>5k rows even when they reduce to few marks, e.g. a KDE over 16k values); 250 ms in between.

Result: 3/3 within budget.

| Chart | Input | Marks | Renderer | First render (ms) | Budget (ms) | |
|---|---|---:|---|---:|---:|---|
| donut | basic | 5 | svg | 2.2 | 100 | ok |
| donut | realistic | 6 | svg | 3.3 | 100 | ok |
| donut | edge-large | 6 | svg | 7.2 | 500 | ok |
