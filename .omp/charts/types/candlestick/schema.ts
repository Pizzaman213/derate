// Same input as ohlc: rows of { date, open, high, low, close, volume? }, validated in
// types/_shared/TrendB.ts (high must cover open/close, low must sit under them).
export { ohlcInput as input, ohlcOptions as options, type OhlcRow as CandleRow, type OhlcOptions as CandleOptions } from '../_shared/TrendB'
