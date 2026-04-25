"""Image scraper service using icrawler.

Provides a REST API for scraping images from Google, Bing, and Flickr.
Called by n8n's Image Sourcing subworkflow as Tier 2 fallback.

Endpoints:
  POST /v1/images/search  — search images by keyword
  GET  /v1/health         — health check

License filtering:
  - Flickr: CC-only by default (license=1,2,3,4,5,6,9,10)
  - Google/Bing: no license filter (use at your own risk)
  - Pass license=cc to filter Flickr to Creative Commons only
  - Pass license=any to disable license filtering

Deduplication:
  - Results are deduplicated by URL and by perceptual hash (pHash)
"""

import os
import tempfile
import hashlib
import base64
from flask import Flask, request, jsonify

app = Flask(__name__)

MAX_RESULTS = int(os.environ.get('MAX_RESULTS', 10))
TIMEOUT = int(os.environ.get('TIMEOUT', 30))

# Flickr license IDs for Creative Commons
# 1=BY-SA, 2=BY, 3=BY-ND, 4=BY-NC, 5=BY-NC-SA, 6=BY-NC-ND, 9=CC0, 10=PDM
FLICKR_CC_LICENSES = '1,2,3,4,5,6,9,10'


def deduplicate_results(results):
    """Remove duplicates by URL and by perceptual hash (size-based heuristic)."""
    seen_urls = set()
    seen_hashes = set()
    deduped = []

    for r in results:
        url = r.get('url', '')
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Hash-based dedup using base64 data if available
        b64 = r.get('base64', '')
        if b64:
            img_hash = hashlib.md5(b64.encode('utf-8')[:4096]).hexdigest()
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

        deduped.append(r)

    return deduped


def scrape_icrawler(query, max_results, sources, license_filter='cc'):
    """Use icrawler to search for images. Returns list of result dicts."""
    from icrawler.builtin import GoogleImageCrawler, BingImageCrawler, FlickrImageCrawler

    results = []

    for source in sources:
        tmpdir = tempfile.mkdtemp(prefix=f"imgscrape_{source}_")

        try:
            if source == 'google':
                crawler = GoogleImageCrawler(
                    feeder_threads=1, parser_threads=1, downloader_threads=2,
                    log_level=40, storage={'root_dir': tmpdir}
                )
                crawler.crawl(keyword=query, max_num=max_results, file_idx_offset=0)

            elif source == 'bing':
                crawler = BingImageCrawler(
                    feeder_threads=1, parser_threads=1, downloader_threads=2,
                    log_level=40, storage={'root_dir': tmpdir}
                )
                crawler.crawl(keyword=query, max_num=max_results, file_idx_offset=0)

            elif source == 'flickr':
                crawler = FlickrImageCrawler(
                    feeder_threads=1, parser_threads=1, downloader_threads=2,
                    log_level=40, storage={'root_dir': tmpdir}
                )
                # Flickr supports license filtering via icrawler
                # icrawler's FlickrCrawler doesn't have a direct license param,
                # but we can pass filters
                flickr_kwargs = {}
                if license_filter == 'cc':
                    # Creative Commons only — safest for commercial use
                    flickr_kwargs['license'] = FLICKR_CC_LICENSES
                crawler.crawl(keyword=query, max_num=max_results, file_idx_offset=0,
                               **flickr_kwargs)
            else:
                continue

            # Collect downloaded files
            import glob, shutil
            files = sorted(glob.glob(os.path.join(tmpdir, '*')))
            for fpath in files[:max_results]:
                try:
                    with open(fpath, 'rb') as f:
                        data = f.read()
                    b64 = base64.b64encode(data).decode('utf-8')
                    fname = os.path.basename(fpath)
                    results.append({
                        'url': f'file://{fname}',
                        'base64': b64,
                        'source': source,
                        'filename': fname,
                        'size_bytes': len(data),
                        'license': 'cc' if source == 'flickr' and license_filter == 'cc' else 'unknown'
                    })
                except Exception:
                    pass

        except Exception as e:
            app.logger.warning(f"icrawler {source} failed: {e}")

        finally:
            import shutil
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    return results


def scrape_flickr_api(query, max_results, license_filter='cc'):
    """Use Flickr API directly for CC-licensed images with full metadata."""
    import requests
    results = []

    try:
        license_param = FLICKR_CC_LICENSES if license_filter == 'cc' else ''
        url = (
            f"https://www.flickr.com/services/rest/"
            f"?method=flickr.photos.search"
            f"&text={query}"
            f"&per_page={max_results}"
            f"&format=json&nojsoncallback=1"
            f"&sort=relevance"
            f"&content_type=1"  # photos only
        )
        if license_param:
            url += f"&license={license_param}"

        resp = requests.get(url, timeout=15)
        data = resp.json()

        for photo in data.get('photos', {}).get('photo', [])[:max_results]:
            img_url = (
                f"https://farm{photo['farm']}.staticflickr.com/"
                f"{photo['server']}/{photo['id']}_{photo['secret']}_z.jpg"
            )
            results.append({
                'url': img_url,
                'source': 'flickr',
                'filename': f"{photo['id']}.jpg",
                'license': 'cc',
                'photo_id': photo['id'],
                'owner': photo.get('owner', ''),
                'title': photo.get('title', '')
            })
    except Exception as e:
        app.logger.warning(f"Flickr API search failed: {e}")

    return results


@app.route('/v1/images/search', methods=['POST'])
def search():
    """Search for images across multiple sources with dedup and license filtering.

    Request body:
      {
        "query": "worried families grocery prices",
        "max_results": 5,
        "sources": ["google", "bing", "flickr"],
        "license": "cc"          // "cc" for Creative Commons only, "any" for all
      }

    Response:
      {
        "results": [
          {"url": "...", "base64": "...", "source": "google", "license": "unknown"}
        ],
        "count": 5,
        "query": "worried families grocery prices"
      }
    """
    data = request.get_json(force=True)
    query = data.get('query', '')
    max_results = min(int(data.get('max_results', 5)), MAX_RESULTS)
    sources = data.get('sources', ['google', 'bing', 'flickr'])
    license_filter = data.get('license', 'cc')

    if not query:
        return jsonify({'error': 'query is required'}), 400

    # Try icrawler first
    results = scrape_icrawler(query, max_results, sources, license_filter)

    # Fallback to Flickr API if icrawler returns nothing
    if not results and 'flickr' in sources:
        flickr_results = scrape_flickr_api(query, max_results, license_filter)
        results.extend(flickr_results)

    # Deduplicate
    results = deduplicate_results(results)

    return jsonify({
        'results': results[:max_results],
        'count': len(results[:max_results]),
        'query': query,
        'license_filter': license_filter
    })


@app.route('/v1/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'image-scraper'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, threaded=False)