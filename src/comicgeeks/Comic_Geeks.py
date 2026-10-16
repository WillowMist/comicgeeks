import http.client
import logging
import os
import random
import string
import time
from datetime import datetime
from typing import Union

import cloudscraper
import requests
from bs4 import BeautifulSoup

http.client.MAXHEADERS = 1000

from comicgeeks.classes import Character, Creator, Issue, Series, Trade_Paperback
from comicgeeks.utils import dump_bad_response, get_series, is_trade_paperback

logger = logging.getLogger(__name__)


def _generate_session_id():
    """Generate a random sticky session ID for ScrapingAnt."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))


def _is_cloudflare_challenge(resp):
    """Detect Cloudflare challenge responses (GIF challenge or captcha page)."""
    content_type = resp.headers.get('Content-Type', '')
    if content_type == 'image/gif' or len(resp.content) == 43:
        return True
    if resp.status_code == 403 and 'cf-chl-bypass' in resp.headers:
        return True
    if resp.status_code == 200 and b'challenge-platform' in resp.content[:2000]:
        return True
    return False


# Upstream requests can legitimately take a while (ScrapingBee render_js,
# ScrapingAnt headless browser). Give them a generous ceiling so a hung
# connection becomes a retryable error instead of a truncated 200 body.
REQUEST_TIMEOUT = int(os.getenv('COMIC_GEEKS_REQUEST_TIMEOUT', '180'))

_BLOCK_PAGE_MARKERS = (
    b'<title>restricted</title>',
    b'just a moment',
    b'attention required',
    b'access denied',
    b'cf-chl',
    b'challenge-platform',
)


def _is_block_page(resp):
    """Detect upstream block / challenge / empty responses, including HTTP 200.

    ``_is_cloudflare_challenge`` only catches a narrow set of markers. LoCG's
    Cloudflare block page comes back as ``HTTP 200`` with a
    ``<title>Restricted</title>`` body (verified by fetching from the VPS IP),
    which used to slip through and get parsed as a real page -- surfacing far
    downstream as ``no header-intro``. Treat these as retryable / fallback so a
    block page is never handed to the parser.
    """
    if resp.status_code in (401, 403, 429, 500, 502, 503, 504):
        return True

    content = resp.content or b''
    head = content[:4096].lower()
    if any(marker in head for marker in _BLOCK_PAGE_MARKERS):
        return True

    # Only apply the "suspiciously small HTML body" heuristic to HTML; a small
    # JSON payload (e.g. an empty series search) is a legitimate response.
    content_type = (resp.headers.get('Content-Type') or '').lower()
    if 'text/html' in content_type and len(content) < 512:
        return True

    return False


class ScrapingBeeSession(requests.Session):
    """Routes HTTP requests through ScrapingBee API to bypass Cloudflare.

    Supports a configurable list of fallback backends.
    """

    def __init__(self, api_key: str, fallbacks=None, log_init=True):
        super().__init__()
        self.scrapingbee_api_key = api_key
        self.fallbacks = fallbacks or []
        self.authenticated = False
        if log_init:
            logger.info("Scraping backend: ScrapingBee")

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
        api_params = {
            "api_key": self.scrapingbee_api_key,
            "url": url,
            "render_js": "false",
            "transparent_status_code": "true",
        }

        if self.cookies:
            cookie_str = ";".join(f"{c.name}={c.value}" for c in self.cookies)
            api_params["cookies"] = cookie_str

        req_cookies = kwargs.pop('cookies', None)
        if req_cookies:
            extra = []
            if isinstance(req_cookies, dict):
                extra.extend(f"{k}={v}" for k, v in req_cookies.items())
            elif isinstance(req_cookies, requests.cookies.CookieJar):
                extra.extend(f"{c.name}={c.value}" for c in req_cookies)
            if extra:
                extra_str = ";".join(extra)
                api_params["cookies"] = (
                    api_params["cookies"] + ";" + extra_str
                    if api_params.get("cookies")
                    else extra_str
                )

        data = kwargs.pop('data', None)
        headers = kwargs.get('headers', {})
        api_url = "https://app.scrapingbee.com/api/v1"

        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            # Use JS rendering + premium proxy on final attempt (25 credits)
            if attempt == max_retries - 1:
                api_params["premium_proxy"] = "true"
                api_params["render_js"] = "true"
                logger.debug(f"ScrapingBee using premium_proxy + render_js for {url}")
            try:
                if method.upper() == 'POST' and data:
                    headers['Content-Type'] = 'application/x-www-form-urlencoded'
                    resp = super().request('POST', api_url, params=api_params, data=data, headers=headers, **kwargs)
                else:
                    resp = super().request('GET', api_url, params=api_params, headers=headers, **kwargs)

                if _is_block_page(resp) or _is_cloudflare_challenge(resp):
                    reason = f"HTTP {resp.status_code} block/challenge"
                    if attempt < max_retries - 1:
                        delay = 10 * (attempt + 1)
                        logger.debug(f"ScrapingBee {reason} for {url}, retrying in {delay}s")
                        time.sleep(delay)
                        continue
                    dump_bad_response(resp, url, "bee-final")
                    last_error = resp
                    break

                resp.url = url
                return resp
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = 10 * (attempt + 1)
                    logger.debug(f"ScrapingBee error for {url}: {e}, retrying in {delay}s")
                    time.sleep(delay)

        if self.fallbacks:
            reason = str(last_error) if isinstance(last_error, Exception) else f"HTTP {last_error.status_code}"
            for fb_name, fb_session in self.fallbacks:
                logger.warning(f"ScrapingBee failed ({reason}), falling back to {fb_name}")
                try:
                    fb_session.cookies.clear()
                    for cookie in self.cookies:
                        fb_session.cookies.set(cookie.name, cookie.value)
                    resp = fb_session.request(method, url, **kwargs)
                    if _is_block_page(resp) or _is_cloudflare_challenge(resp):
                        dump_bad_response(resp, url, f"{fb_name}-block")
                        raise RuntimeError(f"{fb_name} blocked/challenge (HTTP {resp.status_code}) for {url}")
                    resp.url = url
                    return resp
                except Exception as fb_e:
                    logger.warning(f"[session:fallback] {fb_name} failed: {fb_e}")
                    last_error = fb_e
                    continue

        if isinstance(last_error, requests.Response):
            last_error.url = url
            return last_error
        raise last_error


class CloudscraperSession(requests.Session):
    """Direct cloudscraper session (no proxy, uses VPS IP)."""

    def __init__(self, log_init=True):
        super().__init__()
        self._scraper = cloudscraper.create_scraper()
        self.authenticated = False
        if log_init:
            logger.info("Scraping backend: cloudscraper (direct/VPS IP)")

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
        resp = self._scraper.request(method, url, **kwargs)
        resp.url = url
        return resp


class ScrapingAntSession(requests.Session):
    """Routes HTTP requests through ScrapingAnt API to bypass Cloudflare.

    Uses a sticky proxy session (single exit IP) to keep cf_clearance cookies valid.
    Supports a configurable list of fallback backends.
    """

    def __init__(self, api_key: str, fallbacks=None, proxy_session_id=None):
        super().__init__()
        self.scrapingant_api_key = api_key
        self.fallbacks = fallbacks or []
        self.proxy_session_id = proxy_session_id or _generate_session_id()
        self.authenticated = False
        logger.info(f"Scraping backend: ScrapingAnt (sticky session: {self.proxy_session_id})")

    def request(self, method, url, **kwargs):
        kwargs.setdefault('timeout', REQUEST_TIMEOUT)
        api_params = {
            "url": url,
            "x-api-key": self.scrapingant_api_key,
            "transparent_status_code": "true",
            "return_page_source": "true",
            "proxy_session_id": self.proxy_session_id,
        }

        if self.cookies:
            cookie_str = ";".join(f"{c.name}={c.value}" for c in self.cookies)
            api_params["cookies"] = cookie_str

        req_cookies = kwargs.pop('cookies', None)
        if req_cookies:
            extra = []
            if isinstance(req_cookies, dict):
                extra.extend(f"{k}={v}" for k, v in req_cookies.items())
            elif isinstance(req_cookies, requests.cookies.CookieJar):
                extra.extend(f"{c.name}={c.value}" for c in req_cookies)
            if extra:
                extra_str = ";".join(extra)
                api_params["cookies"] = (
                    api_params["cookies"] + ";" + extra_str
                    if api_params.get("cookies")
                    else extra_str
                )

        data = kwargs.pop('data', None)
        headers = kwargs.get('headers', {})
        api_url = "https://api.scrapingant.com/v2/general"

        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                if method.upper() == 'POST' and data:
                    headers['Ant-Content-Type'] = 'application/x-www-form-urlencoded'
                    resp = super().request('POST', api_url, params=api_params, data=data, headers=headers, **kwargs)
                else:
                    resp = super().request('GET', api_url, params=api_params, headers=headers, **kwargs)

                if _is_block_page(resp) or _is_cloudflare_challenge(resp):
                    if attempt < max_retries - 1:
                        delay = 10 * (attempt + 1)
                        logger.debug(f"ScrapingAnt HTTP {resp.status_code} block/challenge for {url}, retrying in {delay}s (attempt {attempt+1}/{max_retries})")
                        time.sleep(delay)
                        continue
                    dump_bad_response(resp, url, "ant-final")
                    last_error = resp
                    break

                resp.url = url
                return resp
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = 10 * (attempt + 1)
                    logger.debug(f"ScrapingAnt error for {url}: {e}, retrying in {delay}s")
                    time.sleep(delay)

        if self.fallbacks:
            reason = str(last_error) if isinstance(last_error, Exception) else f"HTTP {last_error.status_code}"
            for fb_name, fb_session in self.fallbacks:
                logger.warning(f"[session:{self.proxy_session_id}] ScrapingAnt failed ({reason}), falling back to {fb_name}")
                try:
                    # Copy parent cookies to fallback session (clear first to avoid accumulation)
                    fb_session.cookies.clear()
                    for cookie in self.cookies:
                        fb_session.cookies.set(cookie.name, cookie.value)
                    resp = fb_session.request(method, url, **kwargs)
                    if _is_block_page(resp) or _is_cloudflare_challenge(resp):
                        dump_bad_response(resp, url, f"{fb_name}-block")
                        raise RuntimeError(f"{fb_name} blocked/challenge (HTTP {resp.status_code}) for {url}")
                    resp.url = url
                    return resp
                except Exception as fb_e:
                    logger.warning(f"[session:fallback] {fb_name} failed: {fb_e}")
                    last_error = fb_e
                    continue

            # All fallbacks failed - rotate to a new ScrapingAnt exit IP
            old_session = self.proxy_session_id
            self.proxy_session_id = _generate_session_id()
            self.cookies.clear()
            logger.info(f"[session:{self.proxy_session_id}] New sticky session (was: {old_session})")
            # Retry with new session
            api_params["proxy_session_id"] = self.proxy_session_id
            if self.cookies:
                cookie_str = ";".join(f"{c.name}={c.value}" for c in self.cookies)
                api_params["cookies"] = cookie_str
            try:
                if method.upper() == 'POST' and data:
                    headers['Ant-Content-Type'] = 'application/x-www-form-urlencoded'
                    resp = super().request('POST', api_url, params=api_params, data=data, headers=headers, **kwargs)
                else:
                    resp = super().request('GET', api_url, params=api_params, headers=headers, **kwargs)
                if _is_block_page(resp) or _is_cloudflare_challenge(resp):
                    dump_bad_response(resp, url, "ant-newsession")
                    raise RuntimeError(f"New session also failed for {url}")
                resp.url = url
                return resp
            except Exception as new_session_e:
                logger.error(f"[session:{self.proxy_session_id}] New session also failed for {url}: {new_session_e}")
                last_error = new_session_e

        if isinstance(last_error, requests.Response):
            last_error.url = url
            return last_error
        raise last_error


class FallbackSession(requests.Session):
    """Chains multiple scraping backends in order.

    Tries each backend sequentially until one succeeds.
    Designed to support ScrapingAnt, cloudscraper, ScrapingBee, etc.
    """

    def __init__(self, backends):
        """
        Args:
            backends: list of (name, session) tuples, e.g.
                [("scrapingant", ScrapingAntSession(...)), ("direct", CloudscraperSession())]
        """
        super().__init__()
        self.backends = backends
        self.authenticated = False

    def request(self, method, url, **kwargs):
        last_error = None
        for name, session in self.backends:
            try:
                resp = session.request(method, url, **kwargs)

                if _is_block_page(resp) or _is_cloudflare_challenge(resp):
                    dump_bad_response(resp, url, f"{name}-block")
                    logger.debug(f"{name} returned HTTP {resp.status_code} block/challenge for {url}, trying next backend")
                    last_error = resp
                    continue

                resp.url = url
                return resp
            except Exception as e:
                logger.debug(f"{name} failed for {url}: {e}")
                last_error = e

        if isinstance(last_error, requests.Response):
            last_error.url = url
            return last_error
        raise last_error


class Comic_Geeks:
    """League of Comic Geeks client

    Attributes:
        ci_session (str): Cookie ci_session of League of Comic Geeks
    """

    def __init__(self, ci_session: str = None) -> None:
        scrapingant_key = os.getenv('SCRAPINGANT_API_KEY')
        scrapingbee_key = os.getenv('SCRAPINGBEE_API_KEY')

        fallbacks = []
        if scrapingant_key:
            fallbacks.append(("scrapingant", ScrapingAntSession(scrapingant_key)))
        fallbacks.append(("cloudscraper", CloudscraperSession(log_init=False)))

        if scrapingbee_key:
            self._session = ScrapingBeeSession(scrapingbee_key, fallbacks=fallbacks)
        elif scrapingant_key:
            self._session = ScrapingAntSession(scrapingant_key, fallbacks=fallbacks)
        else:
            self._session = CloudscraperSession()

        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
        )
        if ci_session:
            self._session.cookies.set("ci_session", ci_session)
            self._session.authenticated = True
        else:
            self._session.authenticated = False

    def login(self, username: str, password: str) -> bool:
        """Login to League of Comic Geeks

        Args:
            username (str): Username
            password (str): Password

        Returns:
            str: ci_session cookie
        """
        url = "https://leagueofcomicgeeks.com/login"
        r = self._session.post(url, data={"username": username, "password": password})
        r.raise_for_status()
        ci_session = self._validate_session(r.cookies.get("ci_session"))
        if ci_session:
            self._session.authenticated = True
            return True
        else:
            self._session.authenticated = False
            return False

    def _validate_session(self, ci_session: str = None) -> bool:
        """Check if session is valid.
        Make a request to the main page and if is redirected to the dashboard, the session is valid.
        """
        if ci_session is None or ci_session == "":
            return False
        try:
            r = self._session.get(
                "https://leagueofcomicgeeks.com/", cookies={"ci_session": ci_session}
            )
            if r.status_code == 200 and "dashboard" in r.url:
                return True
        except:
            pass
        self._session.cookies.clear()
        return False

    def search_series(self, query: str) -> list[Series]:
        """Search series by name

        Args:
            query (str): Series name

        Returns:
            list (Series): List of series
        """
        query = query.strip().lower().replace(" ", "+")
        url = f"https://leagueofcomicgeeks.com/comic/get_comics?&list=search&list_option=series&view=thumbs&title={query}&order=alpha-asc&format[]=1&format[]=6"
        r = self._session.get(url)
        r.raise_for_status()
        r = r.json()
        if r["count"] == 0:
            return []

        soup = BeautifulSoup(r["list"], features="lxml")
        content = soup.find(id="comic-list-block")
        comics = content.find_all("li")
        data = get_series(comics, Series, self._session)
        return data

    def search_creator(self, query: str) -> list[Creator]:
        """Search series by name

        Args:
            query (str): Series name

        Returns:
            list (Series): List of series
        """
        query = query.strip().lower().replace(" ", "+")
        url = f"https://leagueofcomicgeeks.com/search/creators?keyword={query}"
        r = self._session.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, features="lxml")
        content = soup.find(class_="comic-series-thumb-list")
        if content is None:
            return []
        data = []
        for item in content.findAll("li"):
            creator_id = int(item.find("a")["href"].split("/")[2])
            creator = Creator(creator_id, self._session)
            creator.name = item.find(class_="title").text.strip()
            creator.url = item.find("a")["href"]
            if item.find("img"):
                creator.image = item.find("img")["src"]
            data.append(creator)
        return data

    def search_character(self, query: str) -> list[Character]:
        query = query.strip().lower().replace(" ", "+")
        url = f"https://leagueofcomicgeeks.com/search/characters?keyword={query}"
        r = self._session.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, features="lxml")
        content = soup.find(class_="character-thumb-list")
        if content is None:
            return []
        data = []
        for item in content.findAll("li"):
            character_id = int(item.find("a")["href"].split("/")[2])
            character = Character(character_id, self._session)
            character.name = item.find(class_="title").text.strip()
            character.url = item.find("a")["href"]
            character.real_name = (
                item.find(class_="publisher").text.strip()
                if item.find(class_="publisher")
                else ""
            )
            if item.find(class_="series"):
                character.publisher, character.universe = item.find(
                    class_="series"
                ).text.split("·")
            if item.find("img"):
                character.image = item.find("img")["data-src"]
            data.append(character)
        return data

    def new_releases(self, date: datetime = "now") -> list[Issue]:
        """Get this week new releases

        Args:
            date (datetime): Date to get new releases

        Returns:
            list (Issue): List of issues
        """
        if date == "now":
            date = datetime.now()
        date = f"{date.month}/{date.day}/{date.year}"

        url = f"https://leagueofcomicgeeks.com/comic/get_comics?list=releases&view=thumbs&format[]=1%2C6&date_type=week&date={date}&order=pulls"
        r = self._session.get(url).json()
        if r["count"] == 0:
            return []

        soup = BeautifulSoup(r["list"], features="lxml")
        content = soup.find(id="comic-list-block")
        comics = content.find_all("li")
        data = []
        for comic in comics:
            a = comic.find("a")
            price = (
                float(comic.find(class_="price").text.split("·")[1].strip()[1::])
                if comic.find(class_="price")
                else "Unknown"
            )
            pulls = comic["data-pulls"]
            rating = comic["data-community"]
            name = comic.find(class_="title").text.strip()
            issue_id = int(a["href"].split("/")[2])
            url = a["href"]
            store_date = comic.find(class_="date")["data-date"]
            publisher = comic.find(class_="publisher").text.strip()
            cover = comic.find("img")["data-src"]
            community = {"rating": rating, "pull": pulls}

            issue = Issue(
                issue_id=issue_id,
                session=self._session,
            )

            issue.name = name
            issue.url = url
            issue.store_date = store_date
            issue.price = price
            issue.publisher = publisher
            issue.cover = cover
            issue.community = community
            data.append(issue)
        return data

    def series_info(self, series_id: int) -> Series:
        """Get series info by id

        Args:
            series_id (int): series id

        Returns:
            Series: Series class
        """
        return Series(series_id, self._session)

    def issue_info(self, issue_id: int, url: str = None) -> Union[Issue, Trade_Paperback]:
        """Get issue info by id

        Args:
            issue_id (int): issue id
            url (str): optional URL slug for the issue

        Returns:
            Issue | Trade_Paperback: Issue object
        """
        if is_trade_paperback(issue_id):
            issue = Trade_Paperback(issue_id, self._session)
        else:
            issue = Issue(issue_id, self._session)
        if url:
            issue.url = url
        return issue

    def creator_info(self, creator_id: int) -> Creator:
        """Get creator info by id

        Args:
            creator_id (int): creator id

        Returns:
            Creator: Creator object
        """
        return Creator(creator_id, self._session)

    def character_info(self, character_id: int) -> Character:
        """Get character info by id

        Args:
            character_id (int): character id

        Returns:
            Character: Character object
        """
        return Character(character_id, self._session)
