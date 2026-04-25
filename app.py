"""Image scraper service with API-based sources and ddgs web search.

Sources:
  - ddgs (DuckDuckGo/Bing/Google image search via proxy)
  - Pixabay API (free, datacenter-safe)
  - Unsplash API (free, datacenter-safe)
  - Flickr API (free, CC-only filtering)

Called by n8n's Image Sourcing subworkflow as Tier 2 fallback.

Endpoints:
  POST /v1/images/search  — search images by keyword
  GET  /v1/health         — health check

Environment variables:
  PIXABAY_API_KEY   — Pixabay API key (free: https://pixabay.com/api/docs/)
  UNSPLASH_ACCESS_KEY — Unsplash API key (free: https://unsplash.com/developers)
  FLICKR_API_KEY    — Flickr API key (free: https://www.flickr.com/services/api/misc.api_keys.html)
  PROXY             — Proxy URL for ddgs (e.g. socks5://user:pass@host:port or http://host:port)
  MAX_RESULTS       — Max results per source (default: 10)
  TIMEOUT           — Request timeout in seconds (default: 30)
"""

import os
import base64
import hashlib
import sys
from flask import Flask, request, jsonify

sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)

MAX_RESULTS = int(os.environ.get('MAX_RESULTS', 10))
TIMEOUT = int(os.environ.get('TIMEOUT', 30))
PROXY = os.environ.get('PROXY', '')

PIXABAY_API_KEY = os.environ.get('PIXABAY_API_KEY', '')
UNSPLASH_ACCESS_KEY = os.environ.get('UNSPLASH_ACCESS_KEY', '')
FLICKR_API_KEY = os.environ.get('FLICKR_API_KEY', '')

FLICKR_CC_LICENSES = '1,2,3,4,5,6,9,10'


def deduplicate_results(results):
    """Remove duplicates by URL."""
    seen = set()
    deduped = []
    for r in results:
        url = r.get('url', '')
        if url and url not in seen:
            seen.add(url)
            deduped.append(r)
    return deduped


def scrape_ddgs(query, max_results, source='bing'):
    """Use ddgs library for Google/Bing/DuckDuckGo image search with proxy support."""
    try:
        from ddgs import DDGS
        kwargs = {}
        if PROXY:
            kwargs['proxy'] = PROXY

        results = []
        with DDGS(**kwargs) as ddgs:
            backend_map = {
                'google': 'google',
                'bing': 'bing',
                'duckduckgo': 'duckduckgo',
            }
            backend = backend_map.get(source, 'bing')
            r = ddgs.images(query, region='wt-wt', max_results=max_results, backend=backend)
            for item in r:
                results.append({
                    'url': item.get('image', item.get('thumbnail', '')),
                    'thumbnail': item.get('thumbnail', item.get('thumbnail_src', '')),
                    'source': source,
                    'title': item.get('title', ''),
                    'width': item.get('image_width', 0),
                    'height': item.get('image_height', 0),
                    'license': 'unknown',
                })
        return results
    except Exception as e:
        app.logger.warning(f"ddgs {source} failed: {e}")
        return []


def scrape_pixabay(query, max_results):
    """Search Pixabay via official API. Free, datacenter-safe."""
    import requests as req_lib
    if not PIXABAY_API_KEY:
        return []
    try:
        url = (
            f"https://pixabay.com/api/"
            f"?key={PIXABAY_API_KEY}"
            f"&q={query}"
            f"&per_page={max_results}"
            f"&image_type=photo"
            f"&orientation=horizontal"
            f"&min_width=1280"
            f"&safesearch=true"
        )
        resp = req_lib.get(url, timeout=TIMEOUT)
        data = resp.json()
        results = []
        for hit in data.get('hits', [])[:max_results]:
            results.append({
                'url': hit.get('largeImageURL', hit.get('webformatURL', '')),
                'thumbnail': hit.get('previewURL', ''),
                'source': 'pixabay',
                'title': hit.get('tags', ''),
                'width': hit.get('imageWidth', 0),
                'height': hit.get('imageHeight', 0),
                'license': 'pixabay',  # Pixabay License = free for commercial use
            })
        return results
    except Exception as e:
        app.logger.warning(f"Pixabay failed: {e}")
        return []


def scrape_unsplash(query, max_results):
    """Search Unsplash via official API. Free, datacenter-safe."""
    import requests as req_lib
    if not UNSPLASH_ACCESS_KEY:
        return []
    try:
        url = (
            f"https://api.unsplash.com/search/photos"
            f"?query={query}"
            f"&per_page={max_results}"
            f"&orientation=landscape"
        )
        headers = {'Authorization': f'Client-ID {UNSPLASH_ACCESS_KEY}'}
        resp = req_lib.get(url, headers=headers, timeout=TIMEOUT)
        data = resp.json()
        results = []
        for item in data.get('results', [])[:max_results]:
            results.append({
                'url': item.get('urls', {}).get('regular', ''),
                'thumbnail': item.get('urls', {}).get('thumb', ''),
                'source': 'unsplash',
                'title': item.get('alt_description', item.get('description', '')),
                'width': item.get('width', 0),
                'height': item.get('height', 0),
                'license': 'unsplash',  # Unsplash License = free for commercial use
            })
        return results
    except Exception as e:
        app.logger.warning(f"Unsplash failed: {e}")
        return []


def scrape_flickr(query, max_results, license_filter='cc'):
    """Search Flickr via official API with CC license filtering."""
    import requests as req_lib
    if not FLICKR_API_KEY:
        return []
    try:
        url = (
            f"https://www.flickr.com/services/rest/"
            f"?method=flickr.photos.search"
            f"&api_key={FLICKR_API_KEY}"
            f"&text={query}"
            f"&per_page={max_results}"
            f"&format=json&nojsoncallback=1"
            f"&sort=relevance"
            f"&content_type=1"
        )
        if license_filter == 'cc':
            url += f"&license={FLICKR_CC_LICENSES}"

        resp = req_lib.get(url, timeout=TIMEOUT)
        data = resp.json()
        results = []
        for photo in data.get('photos', {}).get('photo', [])[:max_results]:
            img_url = (
                f"https://farm{photo['farm']}.staticflickr.com/"
                f"{photo['server']}/{photo['id']}_{photo['secret']}_z.jpg"
            )
            results.append({
                'url': img_url,
                'thumbnail': img_url.replace('_z.jpg', '_q.jpg'),
                'source': 'flickr',
                'title': photo.get('title', ''),
                'width': 0,
                'height': 0,
                'license': 'cc' if license_filter == 'cc' else 'unknown',
                'photo_id': photo['id'],
                'owner': photo.get('owner', ''),
            })
        return results
    except Exception as e:
        app.logger.warning(f"Flickr failed: {e}")
        return []


SOURCE_HANDLERS = {
    'google': lambda q, m, l: scrape_ddgs(q, m, 'google'),
    'bing': lambda q, m, l: scrape_ddgs(q, m, 'bing'),
    'duckduckgo': lambda q, m, l: scrape_ddgs(q, m, 'duckduckgo'),
    'pixabay': lambda q, m, l: scrape_pixabay(q, m),
    'unsplash': lambda q, m, l: scrape_unsplash(q, m),
    'flickr': lambda q, m, l: scrape_flickr(q, m, l),
}


@app.route('/v1/images/search', methods=['POST'])
def search():
    """Search for images across multiple sources with dedup and license filtering.

    Request body:
      {
        "query": "worried families grocery prices",
        "max_results": 5,
        "sources": ["google", "bing", "pixabay", "unsplash", "flickr"],
        "license": "cc"
      }

    Response:
      {
        "results": [...],
        "count": 5,
        "query": "worried families grocery prices"
      }
    """
    data = request.get_json(force=True)
    query = data.get('query', '')
    max_results = min(int(data.get('max_results', 5)), MAX_RESULTS)
    sources = data.get('sources', ['pixabay', 'unsplash', 'flickr'])
    license_filter = data.get('license', 'cc')

    if not query:
        return jsonify({'error': 'query is required'}), 400

    all_results = []
    for source in sources:
        handler = SOURCE_HANDLERS.get(source)
        if handler:
            try:
                results = handler(query, max_results, license_filter)
                all_results.extend(results)
                app.logger.info(f"{source}: returned {len(results)} results")
            except Exception as e:
                app.logger.warning(f"{source} handler error: {e}")

    all_results = deduplicate_results(all_results)

    return jsonify({
        'results': all_results[:max_results],
        'count': len(all_results[:max_results]),
        'query': query,
        'license_filter': license_filter
    })


@app.route('/v1/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'service': 'image-scraper',
        'sources': list(SOURCE_HANDLERS.keys()),
        'proxy_configured': bool(PROXY),
        'api_keys': {
            'pixabay': bool(PIXABAY_API_KEY),
            'unsplash': bool(UNSPLASH_ACCESS_KEY),
            'flickr': bool(FLICKR_API_KEY),
        }
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, threaded=True)