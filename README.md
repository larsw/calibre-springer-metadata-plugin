# Springer metadata source for calibre

A calibre metadata source plugin for books published by Springer Nature
(Springer, Apress, Palgrave Macmillan, Birkhäuser, Springer Singapore and other imprints).
It downloads metadata and high-resolution covers. The only API it calls is Crossref's, which
needs no API key.

## Where the data comes from

| Data | Source | Key needed? |
|---|---|---|
| Title, subtitle, authors (editors for edited volumes), publisher, publication date, ISBN, DOI, book series, language | Records Springer deposits with [Crossref](https://api.crossref.org), limited to Springer Nature (Crossref member 297) | No |
| Cover (about 827×1250 px) | Springer's cover CDN, `media.springernature.com` | No |
| Description (blurb) | Only when Crossref has a book abstract, which is rare | |
| Subject tags | Not available: Crossref book records carry no subject classification | |

The plugin doesn't use the Springer Nature Meta API (`api.springernature.com`), because its
keys were blocked quickly in practice. It also doesn't scrape `link.springer.com`, because
those pages sit behind a JavaScript bot challenge.

## Install

```sh
./build.sh install        # or: calibre-customize -a Springer-Metadata.zip
```

You can also install it in the GUI: *Preferences → Plugins → Load plugin from file* and pick
`Springer-Metadata.zip`. Then enable **Springer** under
*Preferences → Metadata download*.

To uninstall: `calibre-customize -r Springer`.

## Configuration

In *Preferences → Metadata download*, select **Springer** and click *Configure selected source*:

- **Maximum number of results**: number of matches for title/author searches (default 5).
- **Preferred ISBN**: eBook ISBN (default) or print ISBN. If you search with an ISBN, the plugin
  keeps that ISBN.
- **Add subtitle to title**: on by default.
- **Set series**: use the Springer book series, e.g. *SpringerBriefs in Computer Science*.
  Springer doesn't publish volume numbers through these APIs, so the series index stays at 1.
- **Contact email for Crossref**: optional. Crossref serves requests that include a contact
  address from its faster "polite" pool, which is worth setting before large bulk downloads.

## How lookups work

1. If the book has a Springer `doi:` identifier, the plugin fetches that record directly.
2. Otherwise, if it has an ISBN (print or eBook), the plugin looks the book up by ISBN.
3. Otherwise it searches by title and author.

The plugin stores a `doi:` identifier on each match, so later downloads go straight to the
right record. It also understands `link.springer.com/book/...` URLs.

## Testing

```sh
./build.sh test
```

This runs calibre's identify test harness against the live Crossref API.

The plugin throttles requests and retries on HTTP 429/503. If you hit the
Crossref rate limit anyway, set a contact email.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
