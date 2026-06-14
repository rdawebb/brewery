"""Invoked by launchd every 30 minutes to refresh the catalog."""

from __future__ import annotations

import asyncio

import httpx

from brewery.core import catalog_api, catalog_parser
from brewery.core.catalog import Catalog
from brewery.core.catalog_api import CatalogFetchError, _HttpClient
from brewery.core.logging import BreweryLogger, configure_logging, get_logger

log: BreweryLogger = get_logger(name=__name__)

_TIMEOUT = 60.0


async def refresh_catalog(catalog: Catalog, client: _HttpClient | None = None) -> None:
    """Conditionally fetch each catalog feed and load any that changed.

    Validators are persisted only after a changed feed's body is written, so a
    crash mid-load never leaves a stored ETag without the matching content.

    Args:
        catalog: The catalog store to refresh.
        client: An HTTP client to reuse; an ephemeral one is created if None.
    """
    if client is not None:
        await _refresh(catalog, client)
        return

    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT) as owned:
        await _refresh(catalog, owned)


async def _refresh(catalog: Catalog, client: _HttpClient) -> None:
    """Iterate over all catalog feeds, loading any that have changed since the last run.

    Args:
        catalog: The catalog store to refresh.
        client: The HTTP client to use for fetching feeds.
    """
    for feed in catalog_api.FEEDS:
        etag, last_modified = catalog_api.read_validators(catalog=catalog, feed=feed)

        result = await catalog_api.fetch_feed(feed, etag, last_modified, client=client)

        if result.modified and result.body is not None:
            if feed.name == "formula":
                count = catalog_parser.load_formulae(catalog, result.body)

            else:
                count = catalog_parser.load_casks(catalog, result.body)
            log.info(event="catalog_feed_loaded", feed=feed.name, count=count)

        else:
            log.info(event="catalog_feed_unchanged", feed=feed.name)

        # After the load (or on a 304) record the validators
        catalog_api.store_validators(catalog=catalog, result=result)


async def background_refresh() -> None:
    """Refresh the default catalog store."""
    await refresh_catalog(catalog=Catalog())


def main() -> None:
    """Entry point invoked by launchd."""
    configure_logging(level="INFO")

    try:
        asyncio.run(background_refresh())

    except CatalogFetchError as e:
        # Transient: launchd will retry at the next interval
        log.warning(event="catalog_refresh_failed", error=str(object=e))


if __name__ == "__main__":
    main()
