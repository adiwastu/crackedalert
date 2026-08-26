# FROM_TRADINGVIEW_COPY

> **VERIFIED 2026-08-26** against a live Risk/Reward copy pasted from
> TradingView (web). The original draft had the fields at the wrong
> nesting level — the corrected schema below is what TradingView
> actually produces, and what `frontend/tv.html` parses.

## How TradingView Stores Clipboard Data

When you copy a drawing object in TradingView, the clipboard carries two formats:

- `text/plain` → just the label text e.g. `"short position"`
- `text/html` → the full data inside a `data-tradingview-clip` attribute on a `<span>` element

The price data lives in `text/html`.

---

## Verified Clipboard JSON (Risk/Reward tool)

```json
{
  "sources": [{
    "type": "drawing",
    "geometry": [
      {"x": -0.047, "y": 0.370},
      {"x": 0.584, "y": 0.370},
      {"x": -0.044, "y": 0.370},
      {"x": 0.507, "y": 0.249}
    ],
    "source": {
      "type": "LineToolRiskRewardShort",
      "id": "5YDOPM",
      "state": {
        "title": "",
        "interval": "5",
        "symbol": "OANDA:XAUUSD",
        "stopLevel": 4010,
        "profitLevel": 13512,
        "qty": 12.4688,
        "amountStop": 4950,
        "amountTarget": 5168.48
      },
      "points": [
        {"time_t": 1787735700, "offset": 0, "price": 4624.21, "interval": "5"},
        {"time_t": 1787746800, "offset": 1, "price": 4624.21, "interval": "5"},
        {"time_t": 1787735700, "offset": 0, "price": 4624.21, "interval": "5"}
      ]
    }
  }],
  "title": "short position"
}
```

## Field Map (verified — note the nesting!)

| Field | Path in the clip | Example |
|---|---|---|
| Tool / direction | `sources[0].source.type` (`...Short` / `...Long`) | `LineToolRiskRewardShort` |
| Label | `title` (clip level) | `"short position"` |
| Symbol | `sources[0].source.state.symbol` | `OANDA:XAUUSD` → strip prefix → `XAUUSD` |
| Interval | `sources[0].source.state.interval` (minutes as string) | `"5"` = 5 min |
| Entry price | `sources[0].source.points[0].price` | `4624.21` |
| Stop loss | `sources[0].source.state.stopLevel` | `4010` |
| Take profit | `sources[0].source.state.profitLevel` | `13512` |
| Position size | `sources[0].source.state.qty` | `12.47` |
| Risk amount | `sources[0].source.state.amountStop` | `4950` |
| Target amount | `sources[0].source.state.amountTarget` | `5168.48` |

- `geometry` holds **normalized chart coordinates** (0..1 fractions), NOT prices — ignore it.
- `qty` / `amountStop` / `amountTarget` come from **TradingView's** account settings — informational only, never authoritative for cTrader sizing.
- `state` also carries cosmetic keys (colors, `infoBlocks`, transparency) — irrelevant.


---

## How To Extract It

### Step 1 — Listen for the paste event

```javascript
document.addEventListener('paste', (e) => {
  const html = e.clipboardData.getData('text/html');
  const data = parseTradingViewClip(html);
  console.log(data);
});
```

### Step 2 — Parse the HTML and extract the attribute

```javascript
function parseTradingViewClip(html) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(html, 'text/html');
  const span = doc.querySelector('[data-tradingview-clip]');

  if (!span) return null;

  const raw = span.getAttribute('data-tradingview-clip');
  return JSON.parse(raw);
}
```

### Step 3 — Pull the fields you need (verified paths)

```javascript
function extractTradeData(clip) {
  const src  = clip.sources[0].source;     // the drawing tool
  const st   = src.state || {};            // settings: symbol/interval/levels

  return {
    title:       clip.title,
    symbol:      (st.symbol || '').split(':').pop(),   // OANDA:XAUUSD -> XAUUSD
    interval:    st.interval,
    direction:   src.type === 'LineToolRiskRewardShort' ? 'short' : 'long',
    entry:       src.points[0].price,      // points[] lives on source, NOT state
    stopLoss:    st.stopLevel,
    takeProfit:  st.profitLevel,
    qty:         st.qty,
    amountStop:  st.amountStop,
    amountTarget: st.amountTarget,
  };
}
```

---

## Output Shape

```json
{
  "title": "short position",
  "symbol": "XAUUSD",
  "interval": "5",
  "direction": "short",
  "entry": 4624.21,
  "stopLoss": 4010,
  "takeProfit": 13512,
  "qty": 12.47,
  "amountStop": 4950,
  "amountTarget": 5168.48
}
```

---

## Direction Detection

| `sources[0].source.type` value | Direction |
|---|---|
| `LineToolRiskRewardShort` | short |
| `LineToolRiskRewardLong` | long |

---

## Notes

- `interval` is the timeframe in minutes as a string e.g. `"5"` = 5 min, `"60"` = 1 hour
- `stopLevel` and `profitLevel` are absolute price values
- `points[0].price` is the entry price (the `points` array is at `source` level, the levels at `state`)
- `qty` is the calculated position size based on account settings in TradingView
- `amountStop` and `amountTarget` are in the account currency
- Live implementation: `frontend/tv.html` (`parseTradingViewClip` + `extractTrade`), served at `/tv.html`
