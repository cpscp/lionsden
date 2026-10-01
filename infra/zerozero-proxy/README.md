# Lion's Den — Zerozero Article Proxy

Cloudflare Worker used by the Lions Den data pipeline.

It accepts a canonical Zerozero article URL, fetches it server-side using the
Portuguese host and regional Zerozero mirrors, and returns the raw HTML so the
existing Python parser can extract the article body.

Endpoint:
GET /?url=<encoded Zerozero article URL>

Only Zerozero article URLs are accepted.

Deploy with Wrangler. GitHub Actions deployment requires the
CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID repository secrets.
