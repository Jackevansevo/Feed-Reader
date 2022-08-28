import io
import logging
import os
import posixpath
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from django.core.files.images import ImageFile
from django.db import IntegrityError, transaction

import feeds.parser as parser
import feeds.tasks as tasks
from feeds.models import Entry, Feed

logger = logging.getLogger(__name__)

timeout = httpx.Timeout(10.0)
limits = httpx.Limits(
    max_keepalive_connections=None, max_connections=None, keepalive_expiry=10
)


def translate_common_feed_extensions(url):
    parsed = urlparse(url)

    if parsed.netloc.endswith("wordpress.com") or parsed.netloc.endswith(
        "bearblog.dev"
    ):
        if not parsed.path.rstrip("/").endswith("/feed"):
            return parsed._replace(path=f"{parsed.path.strip('/')}/feed/").geturl()
    elif parsed.netloc.endswith("substack.com"):
        if not parsed.path.endswith("/feed"):
            return parsed._replace(path=f"{parsed.path}/feed").geturl()
    elif parsed.netloc.endswith("tumblr.com"):
        if parsed.path != "/rss":
            return urljoin(url, "rss")
    elif parsed.netloc.endswith("medium.com"):
        if not parsed.path.startswith("/feed"):
            return parsed._replace(path=f"feed{parsed.path}").geturl()
    elif parsed.netloc.endswith("blogspot.com"):
        if parsed.path != "/feeds/posts/default":
            return urljoin(url, "feeds/posts/default")

    return url


def find_favicons(base_url, soup):
    favicons = []

    for favicon_link in soup.findAll(
        "link", {"rel": re.compile(r".*icon.*"), "href": re.compile(r"^(?!data).*$")}
    ):
        logger.info(
            "Found favicon: {} in page body for {}".format(
                favicon_link["href"], base_url
            )
        )
        favicons.append(urljoin(base_url, favicon_link["href"]))

    # Fall back to checking common extensions
    for extension in ("/favicon.ico", "/favicon.png"):
        favicon_loc = urljoin(base_url, extension)
        if favicon_loc not in favicons:
            favicons.append(favicon_loc)

    return favicons


def find_rss_link(soup):
    rss_link = soup.find("link", {"type": re.compile(r"application\/(atom|rss)\+xml$")})

    if rss_link is None:
        rss_link = soup.find("a", string=re.compile("rss", re.I))

    if rss_link is None:
        rss_link = soup.find("a", {"href": re.compile(r"(index|feed|rss|atom).*.xml$")})

    if rss_link is None:
        rss_link = soup.find("a", {"href": re.compile(r".*(rss|atom)$")})

    return rss_link


def find_common_extensions(parsed_url):

    orig = parsed_url

    common_extensions = (
        "feed.xml",
        "index.xml",
        "rss.xml",
        "feed",
        "rss",
        "atom.xml",
        "atom",
        "feed.atom",
    )

    # Horrific

    last_part = parsed_url.path.rsplit("/", 1)[-1]
    if last_part in common_extensions:
        parsed_url = parsed_url._replace(
            path=parsed_url.path.replace(last_part, "").rstrip("/")
        )

    url = parsed_url.geturl()

    possible_locations = []

    # If we have a path i.e. site.com/blog check:
    # - site.com/blog/feed
    # - site.com/blog/index.xml
    if parsed_url.path.rstrip("/"):
        path = parsed_url._replace(path="").geturl()
        for extention in common_extensions:
            new_loc = posixpath.join(path, extention)
            if new_loc != orig.geturl():
                possible_locations.append(new_loc)

    for extention in common_extensions:
        new_loc = posixpath.join(url, extention)
        if new_loc != orig.geturl():
            possible_locations.append(new_loc)

    return possible_locations


async def scrape_common_endpoints(client, parsed_url):
    logger.info("Crawling common extensions for {}".format(parsed_url.geturl()))

    for loc in find_common_extensions(parsed_url):
        logger.info("Trying {}".format(loc))
        try:
            resp = await client.get(loc, headers={"User-Agent": tasks.USER_AGENT})
        except httpx.ConnectError:
            continue
        else:
            if resp.status_code != 404:
                return resp


async def check_favicon(client, path):
    # Verify the favicon exists
    try:
        resp = await client.get(
            path, follow_redirects=True, headers={"User-Agent": tasks.USER_AGENT}
        )
    except httpx.HTTPError:
        return

    if resp.status_code != 200:
        return

    if "html" in resp.headers["content-type"]:
        return

    parsed = urlparse(str(resp.url))
    _, ext = os.path.splitext(parsed.path)
    return ImageFile(io.BytesIO(resp.read()), name=f"{parsed.netloc}-favicon{ext}")


async def crawl_url(url):
    return await Crawler(url).crawl()


class Crawler:
    def __init__(self, url):
        self.targets = [url]
        self.crawled = set()

        self.feed = None
        self.feed_resp = None

        self.html_resp = None
        self.soup = None

    def __iter__(self):
        pass

    def __next__(self):
        pass

    def add_target(self, target_url):
        parsed_target = urlparse(target_url)
        if parsed_target.path.endswith("/"):
            parsed_target = parsed_target._replace(path=parsed_target.path.rstrip("/"))

        stripped = parser.strip_scheme(parsed_target.geturl())
        if stripped not in self.crawled:
            self.targets.append(target_url)

    async def crawl(self):
        async with httpx.AsyncClient(
            timeout=timeout, limits=limits, follow_redirects=True
        ) as client:

            url = self.targets.pop()
            parsed_url = urlparse(url)

            try:
                self.crawled.add(url)
                resp = await client.get(url, headers={"User-Agent": tasks.USER_AGENT})
            except httpx.RequestError:
                logger.info("Failed to fetch {}".format(url))
            else:
                if resp.status_code == 503:
                    logger.info("Service unavailable: {}".format(resp.url))
                    return

                content_type = resp.headers.get("content-type")

                if (
                    content_type is not None
                    and "html" in content_type
                    and self.html_resp is None
                ):
                    logger.info("{} returned HTML response".format(url))

                    self.html_resp = resp

                    # If we haven't scraped any HTML yet
                    if self.soup is None:
                        self.soup = BeautifulSoup(resp, features="html.parser")

                    # Swap the html and feed resp

                    if self.feed is None:

                        rss_link = find_rss_link(self.soup)

                        if rss_link is not None:
                            feed_url = urljoin(url, rss_link["href"])
                            logger.info(
                                "Found feed link: {} in page body of {}".format(
                                    feed_url, resp.url
                                )
                            )
                            if urlparse(feed_url).netloc != parsed_url.netloc:
                                logger.info("RSS links to different site: skipping")
                            else:
                                self.add_target(feed_url)
                        else:
                            logger.info("No feed link in page body for {}".format(url))
                            for ext in find_common_extensions(parsed_url):
                                self.add_target(ext)

                else:
                    if self.feed is None and resp.status_code != 404:
                        self.feed = parser.parse(io.BytesIO(resp.content))
                        self.feed_resp = resp

                    if self.soup is None:
                        if self.feed is not None:
                            # Try to infer the site url from the parsed feed
                            parsed_link = self.feed.get("link")
                            link = urljoin(str(resp.url), parsed_link)
                            logger.info(
                                "Found site link: {} in parsed feed {}".format(
                                    link, resp.url
                                )
                            )
                            self.add_target(link)
                        else:
                            if parsed_url.path:
                                self.add_target(
                                    urlparse(url)._replace(path="").geturl()
                                )

            if self.targets == [] and self.soup is None:
                if parsed_url.path:
                    if parsed_url.path.endswith("/"):
                        url = parsed_url._replace(
                            path=parsed_url.path.rstrip("/")
                        ).geturl()
                    link = posixpath.dirname(url)
                    if parser.strip_scheme(link) not in self.crawled:
                        self.add_target(link)

            # If we still have targets left to scrape
            if self.targets:
                return await self.crawl()

            favicon = None

            if self.html_resp is not None:
                for favicon_loc in find_favicons(str(self.html_resp.url), self.soup):
                    favicon = await check_favicon(client, favicon_loc)
                    if favicon is not None:
                        break

            return self.feed_resp, self.feed, favicon


@transaction.atomic
def ingest_feed(resp, parsed, favicon):
    parsed, entries = parser.parse_feed(resp, parsed, favicon)

    if not parsed:
        return None

    try:
        with transaction.atomic():
            feed = Feed.objects.create(**parsed)
    except IntegrityError:
        raise

    Entry.objects.bulk_create(
        entry
        for entry in (parser.parse_feed_entry(entry, feed) for entry in entries)
        if entry is not None
    )
    return feed
