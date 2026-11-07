#!/usr/bin/env python
# License: GPLv3

"""
Springer metadata source plugin for calibre.

Metadata comes from the bibliographic records Springer Nature deposits with
Crossref (api.crossref.org, Crossref member 297 = Springer Science and
Business Media, which also covers Apress, Palgrave Macmillan, Birkhäuser,
Springer Singapore, ...). If a Springer Nature API key is configured, subject
tags and exact publication dates are added from the official Springer Nature
Meta API (api.springernature.com). Covers are downloaded from Springer's cover
image CDN (media.springernature.com).
"""

import hashlib
import json
import re
import threading
import time
from datetime import datetime
from queue import Empty, Queue
from urllib.parse import quote, urlencode

from calibre.ebooks.metadata.book.base import Metadata
from calibre.ebooks.metadata.sources.base import Option, Source

__license__ = 'GPL v3'

CROSSREF_API = 'https://api.crossref.org/works'
SPRINGER_META_API = 'https://api.springernature.com/meta/v2/json'
COVER_URL = 'https://media.springernature.com/full/springer-static/cover-hires/book/{}'
BOOK_URL = 'https://link.springer.com/book/{}'

# Crossref member id of Springer Science and Business Media LLC.
SPRINGER_MEMBER_ID = '297'
# DOI prefixes used by Springer Nature for books.
SPRINGER_DOI_PREFIXES = ('10.1007/', '10.1057/', '10.1365/')
BOOK_TYPES = ('book', 'monograph', 'edited-book', 'reference-book')

# media.springernature.com returns HTTP 200 with this generic "no cover"
# image (484x644, ~6 KB) for books it has no cover for.
PLACEHOLDER_COVER_SHA1 = {'604d60ab47ebb9c1f27e993841615a05063f2be7'}
MIN_COVER_BYTES = 10000

PLUGIN_VERSION = (1, 0, 0)

MIN_REQUEST_INTERVAL = 0.5  # seconds between API requests
MAX_RETRIES = 3
_request_lock = threading.Lock()
_last_request = [0.0]


def is_springer_doi(doi):
    return bool(doi) and doi.lower().startswith(SPRINGER_DOI_PREFIXES)


def book_doi(doi):
    """Strip a chapter suffix (e.g. 10.1007/978-3-319-09357-4_2)."""
    return re.sub(r'_\d+$', '', doi.strip())


def clean_isbn(val):
    return re.sub(r'[^0-9Xx]', '', val or '').upper()


def strip_html(text):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', text or '')).strip()


class Springer(Source):

    name = 'Springer'
    description = _(
        'Downloads metadata and covers for books published by Springer Nature '
        '(Springer, Apress, Palgrave Macmillan, Birkhäuser, ...). Metadata is '
        'taken from the records Springer deposits with Crossref; subject tags '
        'and exact publication dates need a free Springer Nature API key.')
    author = 'Springer metadata source contributors'
    version = PLUGIN_VERSION
    minimum_calibre_version = (6, 0, 0)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset([
        'title', 'authors', 'publisher', 'pubdate', 'series', 'languages',
        'comments', 'tags', 'identifier:isbn', 'identifier:doi',
    ])
    has_html_comments = True
    supports_gzip_transfer_encoding = True
    cached_cover_url_is_reliable = True
    prefer_results_with_isbn = True

    options = (
        Option('api_key', 'string', '', _('Springer Nature API key:'),
               _('Optional. A free key from https://dev.springernature.com '
                 'adds Springer subject classifications as tags and the exact '
                 'online publication date, from the Springer Nature Meta API '
                 '(one request per book; the free plan allows 500 per day).')),
        Option('max_results', 'number', 5, _('Maximum number of results:'),
               _('Maximum number of matches to return when searching by '
                 'title and author.')),
        Option('isbn_type', 'choices', 'electronic', _('Preferred ISBN:'),
               _('Which ISBN to store when a book has several and the search '
                 'was not made with an ISBN.'),
               {'electronic': _('eBook ISBN'), 'print': _('Print ISBN')}),
        Option('include_subtitle', 'bool', True, _('Add subtitle to title'),
               _('Append the subtitle to the title, separated by a colon.')),
        Option('include_series', 'bool', True, _('Set series'),
               _('Use the Springer book series (e.g. "Lecture Notes in '
                 'Computer Science") as the calibre series.')),
        Option('contact_email', 'string', '', _('Contact email for Crossref:'),
               _('Optional. Crossref routes requests that include a contact '
                 'address to its faster "polite" pool.')),
    )

    # Network helpers {{{

    def _user_agent(self):
        ua = 'calibre-springer-metadata/{} (calibre metadata source plugin)'.format(
            '.'.join(map(str, PLUGIN_VERSION)))
        email = (self.prefs['contact_email'] or '').strip()
        if email:
            ua += f'; mailto:{email}'
        return ua

    def _get_json(self, url, timeout, log):
        br = self.browser
        br.addheaders = [('User-Agent', self._user_agent()),
                         ('Accept', 'application/json')]
        log.info('Springer: fetching', url.replace(self.prefs['api_key'] or '\0', '***'))
        deadline = time.monotonic() + timeout
        for attempt in range(MAX_RETRIES + 1):
            # Crossref's public pool allows only a few requests per second and
            # little concurrency, so serialise and space out our requests.
            with _request_lock:
                wait = _last_request[0] + MIN_REQUEST_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    _last_request[0] = time.monotonic()
                    raw = br.open_novisit(url, timeout=timeout).read()
                    return json.loads(raw)
                except Exception as e:
                    code = getattr(e, 'code', None)
                    if code not in (429, 503) or attempt == MAX_RETRIES:
                        raise
                    try:
                        delay = float(e.hdrs.get('Retry-After'))
                    except Exception:
                        delay = 2 ** (attempt + 1)
                    delay = min(delay, 30)
                    if time.monotonic() + delay > deadline:
                        raise
                    log.warning(f'Springer: HTTP {code}, retrying in {delay:.0f}s')
                    time.sleep(delay)

    def _crossref_search(self, log, timeout, rows, isbn=None, title=None, authors=None):
        filters = [f'member:{SPRINGER_MEMBER_ID}'] + [f'type:{t}' for t in BOOK_TYPES]
        params = {'rows': str(rows)}
        if isbn:
            filters.append(f'isbn:{isbn}')
        else:
            if title:
                params['query.bibliographic'] = title
            if authors:
                params['query.author'] = ' '.join(authors)
        params['filter'] = ','.join(filters)
        data = self._get_json(CROSSREF_API + '?' + urlencode(params), timeout, log)
        return data.get('message', {}).get('items', [])

    def _crossref_doi(self, doi, log, timeout):
        data = self._get_json(CROSSREF_API + '/' + quote(doi, safe='/'), timeout, log)
        return data.get('message')

    # }}}

    # Identifiers and URLs {{{

    def get_book_url(self, identifiers):
        doi = identifiers.get('doi')
        if is_springer_doi(doi):
            doi = book_doi(doi)
            return ('doi', doi, BOOK_URL.format(doi))

    def get_book_url_name(self, idtype, idval, url):
        return 'SpringerLink'

    def id_from_url(self, url):
        m = re.search(r'link\.springer\.com/(?:book|chapter|referencework)/(10\.\d{4,}/[^?#\s]+)', url)
        if m is not None:
            return ('doi', book_doi(m.group(1)))

    def get_cached_cover_url(self, identifiers):
        doi = identifiers.get('doi')
        if not is_springer_doi(doi):
            isbn = identifiers.get('isbn')
            doi = self.cached_isbn_to_identifier(clean_isbn(isbn)) if isbn else None
        if is_springer_doi(doi):
            return self.cached_identifier_to_cover_url(book_doi(doi)) or self._cover_url(book_doi(doi))

    @staticmethod
    def _cover_url(doi):
        suffix = doi.split('/', 1)[1]
        # Book DOIs are the hyphenated eISBN, which is also the cover key.
        if re.match(r'^97[89]-[\d-]+[\dX]$', suffix):
            return COVER_URL.format(suffix)

    # }}}

    # Metadata construction {{{

    def _date(self, item):
        for key in ('published-online', 'published-print', 'issued', 'published'):
            parts = (item.get(key) or {}).get('date-parts') or []
            if parts and parts[0] and parts[0][0]:
                p = list(parts[0]) + [1, 1]
                from calibre.utils.date import utc_tz
                try:
                    return datetime(int(p[0]), int(p[1] or 1), int(p[2] or 1), 12, tzinfo=utc_tz)
                except (TypeError, ValueError):
                    continue

    @staticmethod
    def _names(people):
        names = []
        for p in people or ():
            name = ' '.join(x for x in (p.get('given', '').strip(), p.get('family', '').strip()) if x)
            name = name or (p.get('name') or '').strip()
            if name:
                names.append(name)
        return names

    def _choose_isbn(self, item, query_isbn):
        typed = item.get('isbn-type') or []
        all_isbns = [clean_isbn(x['value']) for x in typed] or [clean_isbn(x) for x in item.get('ISBN') or []]
        if query_isbn and query_isbn in all_isbns:
            return query_isbn
        wanted = self.prefs['isbn_type']
        for x in typed:
            if x.get('type') == wanted:
                return clean_isbn(x['value'])
        return all_isbns[0] if all_isbns else None

    def _item_to_metadata(self, item, query_isbn, log):
        from calibre.utils.localization import canonicalize_lang
        title = (item.get('title') or [''])[0].strip()
        subtitle = (item.get('subtitle') or [''])[0].strip()
        if subtitle and self.prefs['include_subtitle']:
            title = f'{title}: {subtitle}'
        authors = self._names(item.get('author')) or self._names(item.get('editor'))
        mi = Metadata(title, authors or [_('Unknown')])

        doi = book_doi(item['DOI'])
        mi.set_identifier('doi', doi)
        isbn = self._choose_isbn(item, query_isbn)
        if isbn:
            mi.isbn = isbn
            for x in item.get('ISBN') or ():
                self.cache_isbn_to_identifier(clean_isbn(x), doi)

        mi.publisher = item.get('publisher')
        mi.pubdate = self._date(item)
        if self.prefs['include_series']:
            series = (item.get('container-title') or [''])[0].strip()
            if series:
                mi.series = series
                vol = item.get('volume')
                try:
                    mi.series_index = float(vol) if vol else None
                except ValueError:
                    pass
        lang = canonicalize_lang(item.get('language'))
        if lang:
            mi.languages = [lang]
        abstract = strip_html(item.get('abstract'))
        if abstract:
            mi.comments = f'<p>{abstract}</p>'

        cover = self._cover_url(doi)
        if cover:
            self.cache_identifier_to_cover_url(doi, cover)
        mi.has_cover = bool(cover)
        return mi

    # }}}

    def clean_downloaded_metadata(self, mi):
        # Springer's titles are already properly capitalised; calibre's
        # generic English title-casing would turn "with" into "With", etc.
        # Only normalise author name case and the ISBN.
        from calibre.ebooks.metadata import check_isbn
        from calibre.utils.icu import capitalize
        mi.authors = [capitalize(a) if a.isupper() or a.islower() else a for a in mi.authors]
        if mi.isbn:
            mi.isbn = check_isbn(mi.isbn)

    # Springer Nature Meta API enrichment {{{

    def _enrich(self, mi, log, timeout):
        # The Meta API indexes book *chapters*, not books, so there is no book
        # description to be had. Every chapter record does carry the book-level
        # Springer subject classification and the exact online publication
        # date, so a single record is enough. Notes on the free plan (checked
        # 2026-11): isbn: queries only match the hyphenated form, title:
        # queries and page sizes above 25 are premium features.
        key = (self.prefs['api_key'] or '').strip()
        doi = mi.get_identifiers().get('doi')
        if not key or not doi:
            return
        hyphenated_eisbn = doi.split('/', 1)[1]
        if not re.match(r'^97[89]-[\d-]+[\dX]$', hyphenated_eisbn):
            return
        try:
            data = self._get_json(SPRINGER_META_API + '?' + urlencode(
                {'q': f'isbn:{hyphenated_eisbn}', 'p': '1', 'api_key': key}), timeout, log)
        except Exception as e:
            code = getattr(e, 'code', None)
            if code == 404:
                log.info('Springer: no Meta API records for', doi)
            elif code in (401, 403):
                log.warning(f'Springer: Meta API refused the request (HTTP {code}), check the API key')
            else:
                log.warning('Springer: Meta API request failed:', code or e)
            return
        records = data.get('records') or []
        if not records:
            log.info('Springer: no Meta API records for', doi)
            return
        rec = records[0]

        tags = []
        for t in rec.get('subjects') or ():
            if not isinstance(t, str):
                continue
            # "Linguistics, general" -> "Linguistics" (calibre tags can't
            # contain commas, and it would duplicate a plain "Linguistics").
            t = re.sub(r',\s*general$', '', t.strip(), flags=re.I)
            if t and t not in tags:
                tags.append(t)
        if tags:
            mi.tags = tags

        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', rec.get('onlineDate') or '')
        if m is not None:
            from calibre.utils.date import utc_tz
            try:
                mi.pubdate = datetime(*map(int, m.groups()), 12, tzinfo=utc_tz)
            except ValueError:
                pass

    # }}}

    def identify(self, log, result_queue, abort, title=None, authors=None,
                 identifiers={}, timeout=30):
        doi = identifiers.get('doi')
        isbn = clean_isbn(identifiers.get('isbn'))
        items = []
        try:
            if is_springer_doi(doi):
                item = self._crossref_doi(book_doi(doi), log, timeout)
                if item:
                    items = [item]
            if not items and isbn:
                items = self._crossref_search(log, timeout, 5, isbn=isbn)
            if not items and title:
                tokens = list(self.get_title_tokens(title, strip_joiners=False))
                atokens = list(self.get_author_tokens(authors or [], only_first_author=False))
                if tokens:
                    items = self._crossref_search(
                        log, timeout, int(self.prefs['max_results']),
                        title=' '.join(tokens), authors=atokens)
        except Exception as e:
            log.exception('Springer: Crossref query failed')
            return f'Failed to query Crossref: {e}'

        if abort.is_set():
            return
        items = [i for i in items if i.get('DOI') and i.get('type') in BOOK_TYPES]
        if not items:
            log.error('Springer: no matching Springer book found')
            return

        for relevance, item in enumerate(items):
            if abort.is_set():
                break
            mi = self._item_to_metadata(item, isbn, log)
            self._enrich(mi, log, timeout)
            mi.source_relevance = relevance
            self.clean_downloaded_metadata(mi)
            result_queue.put(mi)

    def download_cover(self, log, result_queue, abort, title=None, authors=None,
                       identifiers={}, timeout=30, get_best_cover=False):
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info('Springer: no cached cover URL, running identify first')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                          identifiers=identifiers, timeout=timeout)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('Springer: no cover found')
            return
        if abort.is_set():
            return
        log.info('Springer: downloading cover from', cached_url)
        try:
            br = self.browser
            br.addheaders = [('User-Agent', self._user_agent())]
            data = br.open_novisit(cached_url, timeout=timeout).read()
        except Exception:
            log.exception('Springer: failed to download cover from', cached_url)
            return
        if not data or len(data) < MIN_COVER_BYTES or \
                hashlib.sha1(data).hexdigest() in PLACEHOLDER_COVER_SHA1:
            log.info('Springer: server returned its "no cover" placeholder, ignoring')
            return
        result_queue.put((self, data))


if __name__ == '__main__':  # tests {{{
    # Run with: calibre-debug -e springer_metadata/__init__.py
    from calibre.ebooks.metadata.sources.test import (
        authors_test, test_identify_plugin, title_test)

    def series_test(series):
        def test(mi):
            if mi.series != series:
                print(f'Series test failed. Expected: {series!r} found {mi.series!r}')
                return False
            return True
        return test

    def isbn_test(isbn):
        def test(mi):
            if mi.isbn != isbn:
                print(f'ISBN test failed. Expected: {isbn!r} found {mi.isbn!r}')
                return False
            return True
        return test

    tests = [
        (  # search by eISBN
            {'identifiers': {'isbn': '9783031767708'}},
            [title_test('Introduction to Foundation Models', exact=True),
             authors_test(['Pin-Yu Chen', 'Sijia Liu']), isbn_test('9783031767708')]
        ),
        (  # search by print ISBN keeps the print ISBN
            {'identifiers': {'isbn': '978-3-319-09356-7'}},
            [title_test('The Elements of Knowledge Organization', exact=True),
             authors_test(['Richard P. Smiraglia']), isbn_test('9783319093567')]
        ),
        (  # search by DOI
            {'identifiers': {'doi': '10.1007/978-981-10-7805-7'}},
            [title_test('The Structure and Interpretation of the SMB Protocol', exact=True)]
        ),
        (  # title only, series
            {'title': 'Robust Explainable AI'},
            [title_test('Robust Explainable AI', exact=True),
             authors_test(['Francesco Leofante', 'Matthew Wicker']),
             series_test('SpringerBriefs in Intelligent Systems')]
        ),
        (  # title + author, subtitle
            {'title': 'Handling of Geospatial Data with QGIS', 'authors': ['Ko Ko Lwin']},
            [title_test('Handling of Geospatial Data with QGIS: A Basic and Practical '
                        'Visual Learning Material for Geospatial Data Processing, '
                        'Analysis and Visualisation', exact=True),
             authors_test(['Ko Ko Lwin', 'Yuji Murayama'])]
        ),
    ]
    test_identify_plugin(Springer.name, tests, fail_missing_meta=False)
# }}}
