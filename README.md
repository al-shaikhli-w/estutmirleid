# Wien Apartment Watch

Scrapes cooperative-apartment ("Genossenschaftswohnung") listings for Vienna
from [mygewo.at](https://mygewo.at) — an aggregator that already indexes
offers from all the GBV/gemeinnützige providers — and shows them on a small
dashboard, refreshed 6 times a day via GitHub Actions.

## How it works

- `main.py` fetches https://mygewo.at/genossenschaftswohnungen/wien,