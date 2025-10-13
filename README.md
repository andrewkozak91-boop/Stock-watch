# Stock-watch (Render Free friendly)

**Endpoints**
- `GET /`        → health
- `GET /universe?limit=250` → build auto universe from Finnhub (US common shares)
- `GET /scan?max=150&offset=0` → run 7.5 scan on current universe (chunkable)
- `GET /board`   → view ranked Near-Trigger Board (from last scan)
- `POST /reset`  → clear universe & board

**Env var (required)**
- `FINNHUB_API_KEY` → your Finnhub key (free key works; mind rate limits)

**7.5 Gates**
- price < \$30
- float < 150M unless **Real** catalyst
- avoid pharma FDA binaries implicitly (by catalyst detection; nothing custom here)
- ADRs allowed but Tier-2+ only
- volume gate: last 15m bar >= 2,000,000 shares OR >= 0.75% of float
- Real catalysts (headlines): earnings/M&A/13D/13G/insider/buyback/contract/partnership
- Speculative keywords → Tier-3 note

**First run**
1. `GET /universe?limit=250`
2. `GET /scan?max=150` (repeat with `offset=150` if you want more)
3. `GET /board`
