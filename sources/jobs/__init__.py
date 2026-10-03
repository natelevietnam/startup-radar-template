"""ATS board connectors.

The radar already knows which companies it tracks, and almost every startup in
this segment hosts its board on Ashby, Greenhouse or Lever — each with a public
JSON endpoint and no key. So postings are pulled from the companies themselves
rather than scraped off aggregators.

Each connector exposes the same call:

    fetch_board(token, company_name="") -> list[dict]

returning rows in the shape job_matches already stores, so main.py can run a
board through exactly the same filter chain as any feed.
"""

from . import ashby, greenhouse, lever

PROVIDERS = {
    ashby.PROVIDER: ashby,
    greenhouse.PROVIDER: greenhouse,
    lever.PROVIDER: lever,
}

__all__ = ["ashby", "greenhouse", "lever", "PROVIDERS"]
