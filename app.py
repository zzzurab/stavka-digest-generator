import json
import re
from datetime import datetime, timedelta
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

HOST = '0.0.0.0'
PORT = int(__import__('os').environ.get('PORT', '8765'))
BASE = Path(__file__).parent
TEMPLATE = BASE / 'digest_2026-09-08T12-24-09+00-00.html'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139 Safari/537.36',
    'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

CATEGORIES = [
    ('top', 'Топ-матчи'),
    ('high', 'Хай кэфы'),
    ('main', 'Основная линия'),
    ('sure', 'Верняки'),
]

ICON_TOP = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'><g transform='translate(0,40) scale(0.1,-0.1)' fill='%230161DD'><path d='M71 329c-29-31-33-67-7-71 46-7 46-106 0-118-20-5-21-8-10-40 9-28 19-38 52-49 59-20 74-11 74 44 0 32-6 51-20 65-11 11-20 29-20 40 0 11 9 29 20 40 14 14 20 33 20 65v45h-45c-32 0-50-6-64-21zm149-27c0-36 5-54 20-67 11-10 20-26 20-37 0-10-9-27-20-38-15-15-20-33-20-70 0-47 2-50 25-50 50 0 93 26 103 62 7 27 5 34-7 37-30 6-41 23-41 60 0 40 14 61 41 61 22 0 15 36-14 68-15 16-32 22-64 22h-43v-48z'/></g></svg>"
ICON_HIGH = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'><g transform='translate(0,40) scale(0.1,-0.1)' fill='%230161DB'><path d='M125 130c-16-33-32-60-35-60-4 0-17 11-30 25-28 29-50 32-50 7 0-21 70-92 91-92 15 0 89 137 89 165 0 8-8 15-18 15-13 0-28-19-47-60z'/></g></svg>"
ICON_MAIN = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'><g transform='translate(0,40) scale(0.1,-0.1)' fill='%230161DB'><path d='M200 360c-88 0-160-72-160-160S112 40 200 40s160 72 160 160-72 160-160 160zm0-55c14 0 25-11 25-25s-11-25-25-25-25 11-25 25 11 25 25 25zm0-160c-14 0-25 11-25 25s11 25 25 25 25-11 25-25-11-25-25-25z'/></g></svg>"
ICON_SURE = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 40 40'><g transform='translate(0,40) scale(0.1,-0.1)' fill='%230161DB'><path d='M175 356c-30-30-55-74-55-98 0-35-5-39-29-19-43 35-63-61-30-141 62-149 299-105 299 56 0 77-43 148-82 135-28-9-37 2-42 55l-6 66-55-54z'/></g></svg>"

SPORT_LABELS = {
    'soccer': '⚽', 'football': '⚽', 'hockey': '🏒',
    'tennis': '🎾', 'basketball': '🏀', 'volleyball': '🏐',
    'handball': '🤾', 'baseball': '⚾', 'rugby': '🏉', 'boxing': '🥊',
    'mma': '🥊', 'formula-1': '🏎️', 'motorsport': '🏎️', 'cycling': '🚴',
    'ski': '⛷️', 'biathlon': '🎿', 'esports': '🎮', 'darts': '🎯',
}


def clean(s):
    value = unescape(s or '')
    # Some rendered page fragments arrive as literal JSON unicode escapes.
    value = re.sub(r'\\u003[cC]', '<', value)
    value = re.sub(r'\\u003[eE]', '>', value)
    value = re.sub(r'\\u002[fF]', '/', value)
    value = re.sub(r'\\u0026', '&', value)
    # Remove escaped/embedded HTML fragments that are not match metadata.
    value = re.sub(r'<[^>]*>', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def text_of(el):
    return clean(el.get_text(' ', strip=True)) if el else ''


def absolute(src, page_url):
    return urljoin(page_url, src or '')


def is_image_url(src):
    if not src:
        return False
    s = src.lower()
    return s.startswith(('http://', 'https://', '//', '/')) and not s.startswith('data:')


def image_candidates(img):
    vals = []
    for attr in ('src', 'data-src', 'data-lazy-src', 'data-original', 'data-image', 'data-url'):
        v = img.get(attr)
        if v and is_image_url(v):
            vals.append(v)
    srcset = img.get('srcset') or img.get('data-srcset')
    if srcset:
        vals.extend(part.strip().split(' ')[0] for part in srcset.split(',') if part.strip())
    return vals


def _logo_src_from_img(img, page_url):
    """Return the real image URL from an img element, preferring the site's
    actual image attributes over placeholders/lazy-loader SVGs."""
    vals = image_candidates(img)
    for src in vals:
        low = src.lower()
        if any(x in low for x in ('logo', 'team', 'crest', 'emblem', 'flag', 'club')):
            return absolute(src, page_url)
    return absolute(vals[0], page_url) if vals else ''


def find_logo_near(anchor, page_url, team_name=''):
    """Find the logo belonging to THIS team link, not arbitrary page images.

    Priority is intentionally strict:
      1) img inside the team link itself;
      2) img inside the immediate visual wrapper around that team;
      3) img in a very small ancestor only if its alt/title/data attributes
         identify the same team.
    We never fall back to random images elsewhere on the page.
    """
    team_norm = clean(team_name).lower()

    # 1. The ideal case: the logo is part of the team anchor.
    direct = anchor.find_all('img')
    for img in direct:
        alt = clean(img.get('alt', '')).lower()
        title = clean(img.get('title', '')).lower()
        if team_norm and (team_norm in alt or team_norm in title):
            src = _logo_src_from_img(img, page_url)
            if src:
                return src
    for img in direct:
        src = _logo_src_from_img(img, page_url)
        if src:
            return src

    # 2. Walk only a few small ancestors. Prefer images explicitly associated
    # with this team; never choose a generic page/author/advert image.
    p = anchor
    for depth in range(1, 5):
        p = getattr(p, 'parent', None)
        if not p:
            break
        imgs = p.find_all('img', limit=12)
        if not imgs:
            continue
        ranked = []
        for img in imgs:
            alt = clean(img.get('alt', '')).lower()
            title = clean(img.get('title', '')).lower()
            attrs = ' '.join([
                alt, title,
                ' '.join(img.get('class', [])),
                str(img.get('data-testid', '')),
                str(img.get('data-name', '')),
                str(img.get('data-team', '')),
            ]).lower()
            srcs = image_candidates(img)
            if not srcs:
                continue
            src = _logo_src_from_img(img, page_url)
            if not src:
                continue
            low = src.lower()
            score = depth * 100
            # Strong signals that this exact image belongs to the team.
            if team_norm and team_norm in attrs:
                score -= 500
            if any(x in attrs or x in low for x in ('logo', 'crest', 'emblem', 'team', 'flag')):
                score -= 100
            if 'avatar' in attrs or 'author' in attrs or 'banner' in attrs or 'advert' in attrs:
                score += 500
            ranked.append((score, src))
        if ranked:
            ranked.sort(key=lambda x: x[0])
            # At ancestor level >1 require an explicit team/logo signal.
            if depth == 1 or ranked[0][0] < depth * 100:
                return ranked[0][1]
    return ''


def _team_link_candidates(soup):
    """Return likely team-name links, supporting the site's current /team(s)/
    routing and avoiding article/forecast links with the same text."""
    out = []
    for a in soup.find_all('a', href=True):
        txt = text_of(a)
        href = a.get('href', '')
        if not txt or len(txt) > 80:
            continue
        if re.search(r'/teams?/|/team-', href, re.I):
            out.append((txt, a))
    return out


def _best_team_pair(candidates, soup):
    """Choose the pair from the actual match header.

    The important distinction from the previous version is that we score a pair
    by its common DOM container containing BOTH team names and the kickoff time.
    This prevents links from 'other matches', related articles, forecasts, etc.
    from supplying random logos.
    """
    if len(candidates) < 2:
        return None
    time_re = re.compile(r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b')
    date_re = re.compile(r'\b\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b', re.I)
    best = None
    for i, (t1, a1) in enumerate(candidates):
        for t2, a2 in candidates[i+1:]:
            if clean(t1).lower() == clean(t2).lower():
                continue
            # Find the nearest common ancestor containing both anchors.
            ancestors = []
            p = a1
            while p:
                ancestors.append(p)
                p = getattr(p, 'parent', None)
            aset = {id(x): x for x in ancestors}
            p = a2
            common = None
            while p:
                if id(p) in aset:
                    common = p
                    break
                p = getattr(p, 'parent', None)
            if not common:
                continue
            txt = text_of(common)
            score = len(txt)
            if time_re.search(txt): score -= 3000
            if date_re.search(txt): score -= 2000
            if t1.lower() in txt.lower() and t2.lower() in txt.lower(): score -= 500
            imgs = common.find_all('img', limit=10)
            if imgs: score -= 200
            # Avoid huge article/body containers.
            if len(txt) > 1200: score += 5000
            item = (score, t1, a1, t2, a2)
            if best is None or item[0] < best[0]:
                best = item
    return best


def extract_teams(soup, page_url):
    candidates = _team_link_candidates(soup)
    pair = _best_team_pair(candidates, soup)
    if pair:
        _, team1, a1, team2, a2 = pair
        logo1 = find_logo_near(a1, page_url, team1)
        logo2 = find_logo_near(a2, page_url, team2)
        return team1, team2, logo1, logo2

    # Fallback: use the page title/h1 only for names. We deliberately DO NOT
    # scrape arbitrary images in this fallback, because that was the source of
    # the random-image problem.
    title = ''
    for sel in ['h1', 'meta[property="og:title"]', 'title']:
        el = soup.select_one(sel)
        if el:
            title = el.get('content', '') if el.name == 'meta' else text_of(el)
            if title:
                break
    title = clean(title)
    m = re.search(r'(.+?)\s+[—-]\s+(.+?)(?:\s+(?:прогноз|\d{1,2}\s+[а-яА-Я]+)|$)', title, re.I)
    return (m.group(1), m.group(2), '', '') if m else ('', '', '', '')

def extract_league(soup):
    # Current site header format: Европа • Лига чемпионов.
    # The rendered page may place round/table/hashtag controls in the same
    # text node, so stop before those unrelated elements.
    def normalize_league(value):
        value = clean(value)
        value = re.split(r'ОТКРЫТЬ|#|Турнирная таблица|Календарь|Составы|\\u003c|style=', value, maxsplit=1, flags=re.I)[0]
        value = re.sub(r'^\d+[-‑–]й\s+тур\s*', '', value, flags=re.I)
        value = re.sub(r'^Турнирная\s+таблица\s+', '', value, flags=re.I)
        value = clean(value).strip(' •,–—')
        # Reject leaked Vue/HTML fragments that occasionally appear on one page.
        if re.search(r'\\u00|\\u003|<|>|\\|\b(?:span|style|class|display)\b', value, re.I):
            return ''
        return value

    for el in soup.find_all(string=re.compile(r'\s•\s')):
        t = clean(str(el))
        m = re.search(r'[^•]{1,60}\s•\s([^\n]{2,100})', t)
        if m:
            result = normalize_league(m.group(1))
            if result:
                return result
    raw = soup.get_text('\n', strip=True)
    m = re.search(r'[^\n]{1,60}\s•\s([^\n]{2,100})', raw)
    candidate = normalize_league(m.group(1)) if m else ''
    if candidate:
        return candidate
    known = re.search(r'\b(Серия А|Серия B|Премьер-лига|Ла Лига|Лига 1|Бундеслига|КХЛ|НХЛ|ЕвроХоккейТур|РПЛ|ФНЛ|ATP|WTA|Евролига|НБА|Единая лига ВТБ)\b', raw, re.I)
    return known.group(1) if known else ''


MONTHS_RU = {
    'января': '01', 'февраля': '02', 'марта': '03', 'апреля': '04',
    'мая': '05', 'июня': '06', 'июля': '07', 'августа': '08',
    'сентября': '09', 'октября': '10', 'ноября': '11', 'декабря': '12',
}

def extract_match_datetime(soup, team1='', team2='', page_url=''):
    """Extract the match kickoff from the match header.

    The extracted kickoff is already in the local time shown on the
    match page. Do not apply an additional timezone offset.

    We only accept a time that belongs to the current match header area,
    never arbitrary user-forecast timestamps further down the page.
    """
    raw = clean(soup.get_text(' ', strip=True))
    time_re = r'([01]?\d|2[0-3]):([0-5]\d)'
    date_re = r'(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)'

    def make_result(day, month_name, source_time):
        return (
            f'{day} {month_name}, {source_time}',
            source_time,
            f'{day} {month_name}'
        )

    # Preferred: current page header has team1 -> time -> date -> team2.
    # Keep the search window deliberately tight so forecast timestamps cannot match.
    if team1 and team2:
        pattern = re.compile(time_re + r'\s+' + date_re, re.I)
        for m in pattern.finditer(raw):
            left = raw[max(0, m.start()-100):m.start()]
            right = raw[m.end():m.end()+100]
            if team1.lower() in left.lower() and team2.lower() in right.lower():
                day = int(m.group(3))
                month_name = m.group(4).lower()
                return make_result(day, month_name, f'{int(m.group(1)):02d}:{m.group(2)}')

        # Some pages expose date before time.
        pattern2 = re.compile(date_re + r'\s+' + time_re, re.I)
        for m in pattern2.finditer(raw):
            left = raw[max(0, m.start()-100):m.start()]
            right = raw[m.end():m.end()+100]
            if team1.lower() in left.lower() and team2.lower() in right.lower():
                day = int(m.group(1))
                month_name = m.group(2).lower()
                return make_result(day, month_name, f'{int(m.group(3)):02d}:{m.group(4)}')

    # Robust fallback for the current site's match pages:
    # take the first time+date pair before the long article/prediction body.
    # This is still restricted to the beginning of the page, avoiding user forecasts.
    header_text = raw[:5000]
    m = re.search(time_re + r'\s+' + date_re, header_text, re.I)
    if m:
        day = int(m.group(3))
        month_name = m.group(4).lower()
        return make_result(day, month_name, f'{int(m.group(1)):02d}:{m.group(2)}')

    # Last resort: date comes from the URL; time comes from the first header time.
    m_date = re.search(r'/matches/[^/]+/(\d{2})-(\d{2})-(\d{4})-', page_url)
    m_time = re.search(time_re, header_text)
    if m_date and m_time:
        day, month_num = m_date.group(1), m_date.group(2)
        month_name = next((k for k, v in MONTHS_RU.items() if v == month_num), month_num)
        source_time = f'{int(m_time.group(1)):02d}:{m_time.group(2)}'
        return make_result(int(day), month_name, source_time)

    return '', '', ''


def datetime_sort_key(match):
    """Sort by actual match kickoff, independent of input URL order."""
    date_text = match.get('date', '')
    time_text = match.get('time', '')
    m = re.search(r'(\d{1,2})\s+([а-яё]+)', date_text, re.I)
    t = re.match(r'^(\d{1,2}):(\d{2})$', time_text or '')
    if not m or not t:
        return (9999, 99, 99, 99)
    month = int(MONTHS_RU.get(m.group(2).lower(), '12'))
    return (2026, month, int(m.group(1)), int(t.group(1))*60 + int(t.group(2)))

def extract_main_prediction(soup):
    # Most reliable source: heading "Прогноз на матч ...: ... за 2.45".
    for h in soup.find_all(re.compile('^h[1-6]$')):
        t = text_of(h)
        if re.search(r'прогноз\s+на\s+матч', t, re.I):
            m = re.search(r'\b(?:за|кэф(?:\.|фициент)?[: ]+)\s*([1-9]\d?[.,]\d{2})', t, re.I)
            if not m:
                m = re.search(r'\b([1-9]\d?[.,]\d{2})\b', t)
            if m:
                return m.group(1).replace(',', '.'), t
    # Fallback to explicit "Основной прогноз" text in page.
    text = soup.get_text(' ', strip=True)
    idx = text.lower().find('основной прогноз')
    if idx >= 0:
        w = text[idx:idx+1800]
        m = re.search(r'\b([1-9]\d?[.,]\d{2})\b', w)
        if m:
            return m.group(1).replace(',', '.'), ''
    return '', ''


def infer_sport(url):
    path = urlparse(url).path.lower().split('/')
    parts = [p for p in path if p]
    for part in parts:
        if part in SPORT_LABELS:
            return SPORT_LABELS[part]
    full = (url or '').lower()
    for key, icon in SPORT_LABELS.items():
        if key in full:
            return icon
    return ''


def default_category(odds):
    try:
        x = float(odds.replace(',', '.'))
    except Exception:
        return 'main'
    if x >= 2.20: return 'high'
    if x <= 1.80: return 'sure'
    return 'main'


def _norm_team_name(name):
    """Normalize team names for matching."""
    s = clean(name).lower().replace('ё', 'е')
    s = re.sub(r'\bfc\b|\bхк\b|\bфк\b|\bбаскетбольный клуб\b', '', s)
    s = re.sub(r'\s*\(.*?\)\s*', ' ', s)
    s = re.sub(r'[^a-zа-я0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def norm_name(x):
    return re.sub(r'\s+', ' ', clean(x).lower().replace('ё', 'е')).strip()


def stavka_team_logos(soup, team1, team2, page_url):
    """Get the exact team images used by stavka.tv's main match block.

    We only accept images hosted on static.stavka.tv/upload/team/ and require
    the image alt/title to match the corresponding team. This prevents article,
    author, banner and unrelated match images from being selected.
    """
    def norm(x):
        x = clean(x).lower().replace('ё', 'е')
        x = re.sub(r'[«»"“”]', '', x)
        x = re.sub(r'\s+', ' ', x).strip()
        return x

    def variants(name):
        n = norm(name)
        vals = {n}
        vals.add(re.sub(r'\b(?:фк|хк|fc|fk|hc)\s+', '', n))
        return {v for v in vals if v}

    def get_src(img):
        # Prefer the 32px image used by the match block.
        for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
            v = img.get(attr)
            if v and 'static.stavka.tv/upload/team/' in v:
                return absolute(v, page_url)
        srcset = img.get('srcset') or img.get('data-srcset') or ''
        candidates = []
        for part in srcset.split(','):
            bits = part.strip().split()
            if bits and 'static.stavka.tv/upload/team/' in bits[0]:
                candidates.append(bits[0])
        if candidates:
            # Prefer w32-h32 over w64-h64 for compact email assets.
            candidates.sort(key=lambda x: (0 if 'w32-h32' in x else 1, len(x)))
            return absolute(candidates[0], page_url)
        return ''

    def find(name):
        vars_ = variants(name)
        exact = []
        loose = []
        for img in soup.find_all('img'):
            src = get_src(img)
            if not src:
                continue
            alt = norm(img.get('alt', ''))
            title = norm(img.get('title', ''))
            if alt in vars_ or title in vars_:
                exact.append(src)
            elif any(v == alt or v == title or (len(v) > 4 and v in alt) for v in vars_):
                loose.append(src)
        return exact[0] if exact else (loose[0] if loose else '')

    return find(team1), find(team2)




def team_page_logo(soup, team_name, page_url):
    """Open the exact Stavka TV team page linked from the match header and
    take its own team logo. This is more reliable than searching all match-page
    images because the team page has only that club's identity assets."""
    target = _norm_team_name(team_name)
    if not target:
        return ''
    hrefs = []
    for a in soup.find_all('a', href=True):
        txt = _norm_team_name(text_of(a))
        href = absolute(a.get('href',''), page_url)
        if not href or '/teams/' not in urlparse(href).path.lower():
            continue
        if txt == target or (len(target) > 4 and (target in txt or txt in target)):
            hrefs.append(href)
    # Prefer an exact team-link match.
    if not hrefs:
        return ''
    team_url = hrefs[0]
    rr = render_stavka_page(team_url)
    html = rr.get('html','')
    if not html:
        return ''
    rsoup = BeautifulSoup(html, 'html.parser')
    candidates = []
    for img in rsoup.find_all('img'):
        src = ''
        for attr in ('src','data-src','data-lazy-src','data-original'):
            v = img.get(attr)
            if v and 'static.stavka.tv/upload/team/' in v:
                src = absolute(v, team_url)
                break
        if not src:
            continue
        alt = _norm_team_name(img.get('alt',''))
        title = _norm_team_name(img.get('title',''))
        score = 0
        if alt == target or title == target:
            score -= 1000
        if alt and target and (target in alt or alt in target):
            score -= 500
        if 'w32-h32' in src: score -= 50
        candidates.append((score, src))
    if not candidates:
        return ''
    candidates.sort(key=lambda x:x[0])
    return candidates[0][1]

def render_stavka_page(url):
    """Render the visible match header and return HTML plus exact visible fields."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {'html': '', 'visible': {}}
    try:
        with sync_playwright() as pw:
            browser = None
            for kwargs in ({'headless': True}, {'channel': 'chrome', 'headless': True}):
                try:
                    browser = pw.chromium.launch(**kwargs)
                    break
                except Exception:
                    pass
            if browser is None:
                return {'html': '', 'visible': {}}
            context = browser.new_context(user_agent=HEADERS['User-Agent'], locale='ru-RU', timezone_id='Europe/Moscow', viewport={'width': 1440, 'height': 1200})
            page = context.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            try:
                page.wait_for_selector("img[src*='static.stavka.tv/upload/team/']", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
            visible = page.evaluate("""() => {
                const clean = s => (s || '').replace(/\s+/g, ' ').trim();
                const timeRe = /^(?:[01]?\d|2[0-3]):[0-5]\d$/;
                const dateRe = /^\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)$/i;
                const imgs = [...document.images].filter(img => { const src = img.currentSrc || img.src || ''; const r = img.getBoundingClientRect(); return /static\.stavka\.tv\/upload\/team\//i.test(src) && r.width > 0 && r.height > 0; }).map(img => { let p=img, ctx=[]; for(let i=0;i<5 && p;i++,p=p.parentElement){ const t=clean(p.innerText||p.textContent||''); if(t && t.length<300) ctx.push(t); } return {src: img.currentSrc || img.src, alt: clean(img.alt), title: clean(img.title), context: ctx.join(' | ')}; });
                const nodes = [...document.querySelectorAll('body *')].filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && clean(el.textContent).length < 180; });
                const times = nodes.map(el => clean(el.textContent)).filter(x => timeRe.test(x));
                const dates = nodes.map(el => clean(el.textContent)).filter(x => dateRe.test(x));
                const texts = nodes.map(el => clean(el.textContent));
                const header = texts.find(x => /\d{1,2}:\d{2}/.test(x) && /\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)/i.test(x)) || '';
                return {times, dates, header, imgs};
            }""")
            html = page.content()
            context.close(); browser.close()
            return {'html': html, 'visible': visible or {}}
    except Exception:
        return {'html': '', 'visible': {}}


def visible_datetime(visible, page_url):
    times = visible.get('times') or []
    dates = visible.get('dates') or []
    if times and dates:
        return f'{dates[0]}, {times[0]}', times[0], dates[0]
    header = visible.get('header') or ''
    m = re.search(r'((?:[01]?\d|2[0-3]):[0-5]\d).*?(\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))', header, re.I)
    if m:
        return f'{m.group(2)}, {m.group(1)}', m.group(1), m.group(2)
    return '', '', ''

def scrape(url):
    url = url.strip()

    if not re.match(r'^https?://', url):
        raise ValueError('Ссылка должна начинаться с http:// или https://')

    parsed = urlparse(url)
    if 'stavka.tv' not in parsed.netloc:
        raise ValueError('Сейчас генератор рассчитан на страницы матчей stavka.tv')

    # stavka.tv is a Vue app and may block ordinary requests.
    # Try requests briefly, but never let its timeout prevent browser rendering.
    soup = None
    rendered = ''
    visible = {}
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')
    except Exception:
        soup = None

    if soup is None:
        render_result = render_stavka_page(url)
        rendered = render_result.get('html', '')
        visible = render_result.get('visible', {})
        if not rendered:
            raise ValueError(
                'Не удалось открыть страницу stavka.tv через браузер. '
                'Проверь, что сайт открывается в обычном браузере.'
            )
        soup = BeautifulSoup(rendered, 'html.parser')

    team1, team2, logo1, logo2 = extract_teams(soup, url)

    # First try the exact team pages linked from the match header. Each team
    # page owns the club logo, so this cannot accidentally select a logo from
    # another match/article on the match page.
    if team1 and team2:
        tp1 = team_page_logo(soup, team1, url)
        tp2 = team_page_logo(soup, team2, url)
        logo1, logo2 = tp1, tp2

    # Fallback to the match page only when the exact team pages did not yield
    # both logos.
    if team1 and team2 and not (logo1 and logo2):
        m1, m2 = stavka_team_logos(soup, team1, team2, url)
        logo1 = logo1 or m1
        logo2 = logo2 or m2

    # The team <img> tags are normally created after Vue renders the page.
    if not (logo1 and logo2):
        if not rendered:
            render_result = render_stavka_page(url)
            rendered = render_result.get('html', '')
            visible = render_result.get('visible', {})
        if rendered:
            rsoup = BeautifulSoup(rendered, 'html.parser')
            rteam1, rteam2, rlogo1, rlogo2 = extract_teams(rsoup, url)
            if rteam1 and rteam2:
                team1, team2 = rteam1, rteam2
            if team1 and team2:
                tp1 = team_page_logo(rsoup, team1, url)
                tp2 = team_page_logo(rsoup, team2, url)
                logo1 = logo1 or tp1
                logo2 = logo2 or tp2
            if not (logo1 and logo2):
                m1, m2 = stavka_team_logos(rsoup, team1, team2, url)
                logo1 = logo1 or m1
                logo2 = logo2 or m2

    if (not logo1 or not logo2) and visible.get('imgs'):
        imgs = visible.get('imgs') or []
        def by_name(name):
            n = clean(name).lower()
            return next((x.get('src','') for x in imgs if n and n in (x.get('alt','') + ' ' + x.get('title','') + ' ' + x.get('context','')).lower()), '')
        logo1 = logo1 or by_name(team1)
        logo2 = logo2 or by_name(team2)
        # Never use arbitrary first images from the page: they may belong to another match.
        # If the team name cannot be matched, keep the logo empty rather than showing a wrong logo.

    if not team1 or not team2:
        raise ValueError('Не удалось определить команды')

    # Use the rendered DOM for visible match data; browser timezone is explicitly Europe/Moscow.
    # Raw HTML remains available as fallback when rendering is unavailable.
    data_soup = BeautifulSoup(rendered, 'html.parser') if rendered else soup

    league = extract_league(data_soup)
    datetime_text, time, date = visible_datetime(visible, url)
    if not time or not date:
        datetime_text, time, date = extract_match_datetime(soup, team1, team2, url)
    sport = infer_sport(url)
    odds, prediction = extract_main_prediction(data_soup)

    canonical = data_soup.find('link', rel='canonical')
    canonical_url = canonical.get('href') if canonical else url

    if not odds:
        raise ValueError('Не удалось найти коэффициент основного прогноза')

    return {
        'url': canonical_url or url,
        'team1': team1,
        'team2': team2,
        'logo1': logo1,
        'logo2': logo2,
        'league': league,
        'time': time,
        'date': date,
        'datetime': datetime_text,
        'sport': sport,
        'odds': odds,
        'logo_source': 'СТАВКА ТВ' if logo1 and logo2 else 'не найдено',
        'prediction': prediction,
        'category': default_category(odds),
    }

def icon_for(cat):
    return {'top': '🔥', 'high': '💣', 'main': '📈', 'sure': '✅'}[cat]


def section_meta(cat):
    return {
        'top': ('Топ-матчи', ''),
        'high': ('Хай кэфы', '2.20+'),
        'main': ('Основная линия', '1.81–2.19'),
        'sure': ('Верняки', 'до 1.80'),
    }[cat]


def with_utm(url, content):
    # Final UTM content names used by the digest.
    content = {'sure': 'vern', 'main': 'mainline'}.get(content, content)
    sep = '&' if '?' in url else '?'
    return url + sep + f'utm_source=email&utm_medium=digest&utm_campaign=matches&utm_content={content}'


def card(match, cat):
    league = escape(match.get('league',''))
    sport = escape(match.get('sport',''))
    team1 = escape(match.get('team1',''))
    team2 = escape(match.get('team2',''))
    date = escape(match.get('date',''))
    time = escape(match.get('time',''))
    odds = escape(match.get('odds',''))
    url = escape(match.get('url',''), quote=True)
    click_url = escape(with_utm(match.get('url',''), cat), quote=True)
    logo1 = escape(match.get('logo1',''), quote=True)
    logo2 = escape(match.get('logo2',''), quote=True)
    sport_icon = sport if sport else ''
    league_display = f'{sport_icon} {league}' if sport_icon else league
    meta = f'{league_display} • {date} {time}'.strip(' •')
    return f"""<tr>
<td class=\"compact-row-pad\" align=\"center\" valign=\"middle\" style=\"padding:10px 12px;border-top:1px solid #EDEEF1;\">
  <table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\">
    <tr>
      <td width=\"70\" align=\"left\" valign=\"middle\" style=\"padding-right:8px;\">
        <table cellpadding=\"0\" cellspacing=\"0\" border=\"0\"><tr>
          <td align=\"center\" valign=\"middle\" style=\"width:28px;height:28px;background:#f0f2f6;border-radius:50%;\"><img src=\"{logo1}\" width=\"28\" height=\"28\" alt=\"{team1}\" border=\"0\" style=\"display:block;width:28px;height:28px;object-fit:contain;border-radius:50%;\"></td>
          <td width=\"8\"></td>
          <td align=\"center\" valign=\"middle\" style=\"width:28px;height:28px;background:#f0f2f6;border-radius:50%;\"><img src=\"{logo2}\" width=\"28\" height=\"28\" alt=\"{team2}\" border=\"0\" style=\"display:block;width:28px;height:28px;object-fit:contain;border-radius:50%;\"></td>
        </tr></table>
      </td>
      <td align=\"left\" valign=\"middle\" style=\"padding-right:8px;\">
        <div class=\"match-name\" style=\"font-size:15px;line-height:19px;font-family:'Inter',Arial,sans-serif;color:#171921;font-weight:700;\">{team1} — {team2}</div>
        <div class=\"match-meta\" style=\"margin-top:2px;font-size:12px;line-height:16px;font-family:'Inter',Arial,sans-serif;color:#83889B;font-weight:500;\">{meta}</div>
      </td>
      <td width=\"126\" align=\"right\" valign=\"middle\">
        <table cellpadding=\"0\" cellspacing=\"0\" border=\"0\" align=\"right\"><tr>
          <td align=\"right\" valign=\"middle\" style=\"padding-right:5px;\"><div class=\"odds\" style=\"font-size:18px;line-height:22px;font-family:'Inter',Arial,sans-serif;color:#0161DA;font-weight:700;white-space:nowrap;\">{odds}</div></td>
          <td width=\"76\" align=\"right\" valign=\"middle\"><a href=\"{click_url}\" target=\"_blank\" style=\"display:inline-block;background:#0161DA;border-radius:10px;padding:8px 11px;color:#fff;text-decoration:none;font-size:11px;line-height:13px;font-family:'Inter',Arial,sans-serif;font-weight:700;white-space:nowrap;\">ЧИТАТЬ</a></td>
        </tr></table>
      </td>
    </tr>
  </table>
</td>
</tr>"""


def section(cat, matches):
    if not matches:
        return ''
    title, odds_range = section_meta(cat)
    icon = icon_for(cat)
    rows = ''.join(card(m, cat) for m in matches)
    return f"""<tr><td align=\"center\" valign=\"top\" style=\"padding:0 8px 16px;\"><table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\"><tr><td align=\"center\" valign=\"top\" style=\"background:#FFFFFF;border:1px solid #EDEEF1;border-radius:16px;overflow:hidden;\"><table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\"><tr><td align=\"left\" valign=\"middle\" style=\"padding:14px 14px 12px;\"><table width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\"><tr><td align=\"left\" valign=\"middle\"><div style=\"font-size:16px;line-height:22px;font-family:'Inter',Arial,sans-serif;color:#171921;font-weight:700;\">{icon} {title}</div></td><td align=\"right\" valign=\"middle\" style=\"padding-left:8px;\"><div style=\"font-size:12px;line-height:18px;font-family:'Inter',Arial,sans-serif;color:#83889B;font-weight:500;white-space:nowrap;\">{odds_range}</div></td></tr></table></td></tr>{rows}</table></td></tr></table></td></tr>"""

PROMO_BANNER = '''<tr em="block"><td class="px" style="padding: 0px 8px 12px;"><table border="0" cellpadding="0" cellspacing="0" role="presentation" style="background:#FFFFFF; border:1px solid #EDEEF1; border-radius:16px; overflow:hidden;" width="100%"><tr><td align="center" style="padding: 10px;"><a href="https://stavka.tv/cup/daily-september-14?utm_source=email&utm_medium=matches&utm_campaign=newdigest&utm_content=contest_banner" style="display:block; text-decoration:none;" target="_blank"><img alt="Турнир прогнозов" class="promo-img" src="https://img.us2-usndr.com/en/v5/user-files?userId=7016298&resource=himg&disposition=inline&name=68bc6cybic5igbq8tqbdc14xrdgpize7y9mf945jbkxsn1h8rn17kwjjfjf13fhj3xywqy45zfkcknn4atibwdmhcj9bze14j7ktgo4qrfrsfubt7hq5o" style="display: block; width: 100%; max-width: 599px; height: auto; border: 0px; border-radius: 12px;" width="599"></a></td></tr></table></td></tr>'''
FINAL_CTA = '''<tr em="block"><td class="px" style="padding:0 8px 12px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#FFFFFF;border:1px solid #EDEEF1;border-radius:16px;"><tr><td align="center" style="padding:18px 16px;"><div style="font-size:18px;line-height:22px;color:#171921;font-family:Arial,Helvetica,sans-serif;font-weight:900;">Больше матчей — на СТАВКА ТВ</div><div style="padding-top:6px;font-size:13px;line-height:18px;color:#83889B;font-family:Arial,Helvetica,sans-serif;">Все прогнозы, коэффициенты и разборы — в одном месте.</div><div style="padding-top:14px;"><a href="https://stavka.tv/matches?utm_source=email&utm_medium=matches&utm_campaign=newdigest&utm_content=openmatches" target="_blank" style="display:inline-block;background:#0161DA;color:#FFFFFF;border-radius:12px;padding:13px 22px;font-size:14px;line-height:16px;font-family:Arial,Helvetica,sans-serif;font-weight:900;">Смотреть все матчи</a></div></td></tr></table></td></tr>'''

def build_html(matches, subject='', preheader='Посмотри, какие прогнозы собрала наша редакция👉'):
    template = TEMPLATE.read_text(encoding='utf-8')
    main_marker = '<!-- Main -->'
    matches_marker = '<!-- Matches-line1 -->'
    footer_marker = '<!-- Footer -->'
    a = template.index(matches_marker)
    b = template.index(footer_marker)
    # Keep header + Main wrapper + existing title, replace only match content.
    before = template[:a]
    after = template[b:]
    # Input links can be in any order. Always sort matches chronologically
    # before placing them into their category blocks.
    matches = sorted(matches, key=datetime_sort_key)
    grouped = {k: [] for k,_ in CATEGORIES}
    for m in matches:
        grouped.setdefault(m.get('category','main'), []).append(m)
    for cat in grouped:
        grouped[cat].sort(key=datetime_sort_key)
    blocks = [section(k, grouped[k]) for k,_ in CATEGORIES if grouped[k]]
    generated = PROMO_BANNER.join(blocks) + (FINAL_CTA if blocks else '')
    # Close the main table/wrapper opened in the original template.
    closing = '\n</table>\n</div>\n</center>\n</td>\n</tr>\n'
    html = before + generated + closing + after
    if subject:
        html = re.sub(r'<title>.*?</title>', '<title>' + escape(subject) + '</title>', html, count=1, flags=re.S)
    if preheader:
        html = re.sub(r'(<div class="preheader"[^>]*>).*?(</div>)', lambda m: m.group(1)+escape(preheader)+m.group(2), html, count=1, flags=re.S)
    return html


INDEX = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Генератор дайджеста СТАВКА ТВ</title><style>
body{font-family:Inter,Arial,sans-serif;background:#f4f6fa;color:#171921;margin:0}.wrap{max-width:1180px;margin:28px auto;padding:0 18px}.card{background:#fff;border:1px solid #e3e7ef;border-radius:14px;padding:22px;box-shadow:0 6px 24px rgba(20,30,60,.05)}h1{margin:0 0 5px;font-size:26px}.sub{color:#697083;margin:0 0 18px}.urls{width:100%;min-height:170px;box-sizing:border-box;border:1px solid #d8deea;border-radius:9px;padding:12px;font:14px/20px monospace}.btn{margin-top:10px;background:#0161DA;color:#fff;border:0;border-radius:8px;padding:11px 17px;font-weight:700;cursor:pointer}.btn:disabled{opacity:.6}.secondary{background:#eef3fb;color:#171921;margin-left:8px}.status{margin:15px 0;font-weight:600}.warning{background:#fff7e6;border:1px solid #ffd591;padding:10px;border-radius:8px;color:#8a5700}.row{border:1px solid #e1e6ef;border-radius:10px;padding:11px;margin:8px 0;background:#fff}.grid{display:grid;grid-template-columns:40px 1fr 40px 1fr 190px 90px 150px;gap:10px;align-items:center}.logo{width:32px;height:32px;object-fit:contain}.meta{font-size:11px;color:#83889B}.odd{font-size:17px;font-weight:700;color:#0161DA}.cat{width:100%;border:1px solid #d7ddea;border-radius:7px;padding:7px;background:#fff}.error{color:#b42318}.download{background:#171921}.small{font-size:12px;color:#737b8c}.json{width:100%;min-height:130px;box-sizing:border-box;border:1px solid #d8deea;border-radius:8px;padding:10px;font:12px/17px monospace;margin-top:12px}@media(max-width:850px){.grid{grid-template-columns:34px 1fr 34px 1fr}.meta,.odd,.cat{grid-column:1/-1}}
</style></head><body><div class="wrap"><div class="card"><h1>Генератор дайджеста</h1><p class="sub">Вставь ссылки на матчи в любом порядке — генератор соберёт данные, найдёт логотипы команд из основного блока матча, переведёт время в МСК и автоматически расставит матчи по времени. Для рабочего дайджеста рекомендуется 15+ матчей.</p><textarea id="urls" class="urls" placeholder="По одной ссылке на строку:\nhttps://stavka.tv/matches/soccer/...\nhttps://stavka.tv/matches/tennis/...\n..."></textarea><br><button class="btn" id="go">1. Собрать матчи</button><button class="btn secondary" id="html" disabled>2. Скачать HTML</button><button class="btn secondary" id="copy" disabled>Скопировать данные</button><div id="status" class="status"></div><div id="results"></div><textarea id="json" class="json" readonly></textarea></div></div>
<script>let data=[];const $=x=>document.querySelector(x);function esc(s){return String(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))}function render(){let n=data.length;$('#results').innerHTML=data.map((x,i)=>`<div class="row"><div class="grid"><img class="logo" src="${esc(x.logo1)}" onerror="this.style.visibility='hidden'"><b>${esc(x.team1)}</b><img class="logo" src="${esc(x.logo2)}" onerror="this.style.visibility='hidden'"><b>${esc(x.team2)}</b><div><div class="meta">${esc(x.datetime || x.time)} · ${esc(x.sport)}</div><div class="meta">${esc(x.league)}</div><div class="meta">Лого: ${esc(x.logo_source || 'СТАВКА ТВ')}</div></div><div class="odd">${esc(x.odds)}</div><select class="cat" data-i="${i}"><option value="top">Топовые матчи</option><option value="high">Хай кэфы</option><option value="main">Основная линия</option><option value="sure">Верняки</option></select></div></div>`).join('');data.forEach((x,i)=>document.querySelector(`.cat[data-i="${i}"]`).value=x.category);document.querySelectorAll('.cat').forEach(s=>s.onchange=e=>{data[+e.target.dataset.i].category=e.target.value;save()})}function save(){$('#json').value=JSON.stringify(data,null,2);let ok=data.length>=15;$('#status').innerHTML=ok?`Готово: <b>${data.length}</b> матчей. Матчи автоматически отсортированы по времени. Можно распределить их по блокам и скачать HTML.`:`<div class="warning">Сейчас ${data.length} матчей. В твоём стандартном дайджесте должно быть минимум 15 — добавь ещё ${15-data.length}.</div>`;$('#html').disabled=!data.length;$('#copy').disabled=!data.length}
$('#go').onclick=async()=>{const urls=$('#urls').value.split(/\n+/).map(x=>x.trim()).filter(Boolean);if(!urls.length)return;$('#go').disabled=true;$('#status').textContent='Собираю актуальные данные со страниц…';data=[];$('#results').innerHTML='';try{let r=await fetch('/api/scrape_many',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})});let j=await r.json();if(!Array.isArray(j))throw Error(j.error||'Некорректный ответ сервера');j.forEach(item=>{if(item.error){$('#results').insertAdjacentHTML('beforeend',`<div class="row error"><b>Не удалось обработать:</b> ${esc(item.url||'')}<br>${esc(item.error)}</div>`)}else data.push(item)});render();save()}catch(e){$('#status').innerHTML='<div class="warning">Ошибка соединения с сервером: '+esc(e.message)+'</div>'}finally{$('#go').disabled=false}};$('#html').onclick=async()=>{if(data.length<15&&!confirm('В письме меньше 15 матчей. Всё равно сформировать HTML?'))return;let r=await fetch('/api/html',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({matches:data})});let blob=await r.blob();let a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='digest_generated.html';a.click();URL.revokeObjectURL(a.href)};$('#copy').onclick=()=>navigator.clipboard.writeText($('#json').value);</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def send_body(self, body, status=200, ctype='text/html; charset=utf-8', headers=None):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(status); self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(b)))
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith('/api/scrape'):
            q=parse_qs(urlparse(self.path).query); url=q.get('url',[''])[0]
            try:self.send_body(json.dumps(scrape(url),ensure_ascii=False),ctype='application/json; charset=utf-8')
            except Exception as e:self.send_body(json.dumps({'error':str(e)},ensure_ascii=False),400,'application/json; charset=utf-8')
        else:self.send_body(INDEX)
    def do_POST(self):
        try:
            n=int(self.headers.get('Content-Length','0'))
            payload=json.loads(self.rfile.read(n))
            if self.path=='/api/scrape_many':
                urls=[str(x).strip() for x in payload.get('urls',[]) if str(x).strip()]
                results=[None]*len(urls)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs={pool.submit(scrape,u):i for i,u in enumerate(urls)}
                    for job,i in [(f,i) for f,i in jobs.items()]:
                        try: results[i]=job.result()
                        except Exception as e: results[i]={'url':urls[i],'error':str(e)}
                self.send_body(json.dumps(results,ensure_ascii=False),ctype='application/json; charset=utf-8')
                return
            if self.path=='/api/html':
                html=build_html(payload.get('matches',[]))
                self.send_body(html.encode('utf-8'),ctype='text/html; charset=utf-8',headers={'Content-Disposition':'attachment; filename="digest_generated.html"'})
                return
            self.send_body('Not found',404)
        except Exception as e:self.send_body(json.dumps({'error':str(e)},ensure_ascii=False),400,'application/json; charset=utf-8')
    def log_message(self,*args): pass

if __name__=='__main__':
    print(f'Открой http://127.0.0.1:{PORT}')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
