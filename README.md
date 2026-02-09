# City Scrapers Columbus

[![CI build status](https://github.com/City-Bureau/city-scrapers-columbus/workflows/CI/badge.svg)](https://github.com/City-Bureau/city-scrapers-columbus/actions?query=workflow%3ACI)
[![Cron build status](https://github.com/City-Bureau/city-scrapers-columbus/workflows/Cron/badge.svg)](https://github.com/City-Bureau/city-scrapers-columbus/actions?query=workflow%3ACron)

Web scrapers for public meetings in the Columbus, OH area.

Part of the [City Scrapers](https://cityscrapers.org) project.

## Spider prefix

All spiders in this repo use the `cbus` prefix (e.g. `cbus_city_council`).

## Development

See the [development documentation](https://cityscrapers.org/docs/development/) for more info on how to get started.

### Setup

```bash
pipenv install --dev
```

### Running spiders

```bash
pipenv run scrapy crawl cbus_city_council
```

### Running all spiders

```bash
pipenv run scrapy list | xargs -I {} pipenv run scrapy crawl {}
```

### Running tests

```bash
pipenv run pytest
```
