# System context

```mermaid
flowchart LR
    User((User)) -->|browser 127.0.0.1 / CLI| App[Bargain Agent<br/>local process]
    App -->|chat completions, JSON| LLM[(OpenAI-compatible LLM<br/>DeepSeek by default)]
    App -->|shopping search: find + monitoring| Serper[(Serper)]
    App -->|fallback search, product detail| Serp[(SerpApi)]
    App -->|extract| Tavily[(Tavily)]
    App -->|search, scrape| Fire[(Firecrawl)]
    App -->|fetch page, JSON-LD| ScrapeDo[(Scrape.do)]
    App -->|SMTP TLS/SSL| Mail[(Mail server)] --> Inbox((Recipient))
    App --- DB[(SQLite file)]
```

## Boundaries

| External system | What is sent | What comes back | Failure handling |
| --- | --- | --- | --- |
| LLM | prompt + structured input (descriptions, evidence summaries without links/tokens, page text) | JSON validated locally | retry once with the error, then deterministic fallback |
| Serper | search query, NZ locale, key in `X-API-KEY` header | Google Shopping listings (productId, merchant, price) | falls back to SerpApi; daily local cap |
| SerpApi | search query / page token, NZ locale, API key (query string) | listings, product pages (stores, shipping, store links) | typed errors; daily local cap; never logged with the key |
| Scrape.do / Firecrawl / Tavily | one public product URL (screened by `is_safe_public_url`) | JSON-LD price, schema-validated price or page text | next extractor in the chain; recorded as a source error |
| SMTP | one alert per deal episode | accepted / error | scheduler-owned attempt budget; permits prevent duplicates |

## Trust and safety rules

- The web server binds to 127.0.0.1 only and guards every POST with CSRF, Origin and Host checks.
- Model output is data: agents may only reference server-assigned ids; prices, thresholds and send decisions are code.
- Prices shown or compared come from Google Shopping structured data, a page's JSON-LD, or an AI extraction whose price is written on the page — never from model prose.
- No purchases, no account actions, no CAPTCHA or access-control bypass. Providers fetch pages; a blocked page is a recorded failure.
- Monitoring runs only while the process runs; nothing is scheduled outside it.
