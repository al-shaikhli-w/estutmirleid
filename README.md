# Wien Apartment Watch

Scrapes cooperative-apartment ("Genossenschaftswohnung") listings for Vienna
from [mygewo.at](https://mygewo.at) — an aggregator that already indexes
offers from all the GBV/gemeinnützige providers — and shows them on a small
dashboard, refreshed 6 times a day via GitHub Actions.

## How it works

- `main.py` fetches https://mygewo.at/genossenschaftswohnungen/wien,
  parses each listing (address, rent, size, rooms, deposit, source site),
  and writes them to `data/listings.json`. It keeps a rolling history of
  each run so you can see how many new listings appeared.
- `.github/workflows/scrape.yml` runs that script every 4 hours
  (`0 4,8,12,16,20,0 * * *` UTC = 6x/day), commits the updated JSON, and
  publishes `dashboard/index.html` + the JSON to GitHub Pages.
- `dashboard/index.html` is a static page (no backend needed) that fetches
  `listings.json` and renders filterable/sortable cards.

## Setup (one-time, ~5 minutes)
