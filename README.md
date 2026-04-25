# Image Scraper Service

REST API for scraping images from Google, Bing, and Flickr. Used by the VidRush n8n pipeline as a Tier 2 fallback for stock image sourcing.

## Endpoints

- `POST /v1/images/search` — Search images by keyword
- `GET /v1/health` — Health check

## Usage

```bash
curl -X POST http://localhost:8080/v1/images/search \
  -H "Content-Type: application/json" \
  -d '{"query": "worried families", "max_results": 5, "sources": ["google","bing","flickr"], "license": "cc"}'
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| PORT | 8080 | Listen port |
| MAX_RESULTS | 10 | Max results per request |
| TIMEOUT | 30 | Request timeout (seconds) |