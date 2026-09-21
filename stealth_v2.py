"""A stricter block classifier for the late read of the paired reader.

is_blocked() in run_stealth_eval waives every block pattern on a page over 3 KB.
That waiver credits interstitials that are larger than 3 KB: a Cloudflare
"Just a moment..." page, Chrome's net-error page, a DataDome captcha document and
a Kasada bootstrap. is_blocked_v2() keeps everything is_blocked() catches and adds
checks for those four, each keyed on markup that a real page does not carry.

is_blocked() itself is untouched and still scores the old read.
"""

import re

from run_stealth_eval import is_blocked

_STRIPPED = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")


def visible_text(html: str) -> str:
    body = _STRIPPED.sub(" ", html)
    body = _TAG.sub(" ", body)
    body = re.sub(r"&[a-zA-Z#0-9]+;", " ", body)
    return " ".join(body.split())


# Titles that only a block page carries.
HARD_TITLE_RE = re.compile(
    r"^\s*just a moment"
    r"|access to this page has been denied"
    r"|you have been blocked"
    r"|sei stato bloccato"
    r"|attention required! \| cloudflare"
    r"|pardon our interruption"
    r"|prove your humanity",
    re.IGNORECASE,
)

# Cloudflare managed challenge. `_cf_chl_opt` is only emitted on the challenge
# document. `/cdn-cgi/challenge-platform/h/` also appears in the bot-management
# snippet on real pages, so it counts only with a challenge status code.
CF_CHL_OPT = "_cf_chl_opt"
CF_RT_TK = "__cf_chl_rt_tk="
CF_H_PATH = "/cdn-cgi/challenge-platform/h/"

# DataDome. The captcha and interstitial documents carry `var dd={...}` with
# 'rt':'c' (captcha) or 'rt':'i' (interstitial), and t:'bv' on a hard block.
DD_OBJ_RE = re.compile(r"\bdd\s*=\s*\{[^}]*'(?:rt'\s*:\s*'(?:c|i)'|t'\s*:\s*'bv')", re.DOTALL)
DD_CAPTCHA_IFRAME_RE = re.compile(
    r"<iframe[^>]+src=[\"'][^\"']*captcha-delivery\.com/captcha", re.IGNORECASE
)
DD_INTERSTITIAL = "captcha-delivery.com/interstitial"

# Kasada bootstrap: the 429 page is just `window.KPSDK={}` plus the loader.
KPSDK_RE = re.compile(r"window\.KPSDK\s*=")

# Akamai Bot Manager interstitials: the behavioral captcha container and the
# crypto proof-of-work page.
AKAMAI_CPT_RE = re.compile(r'id="sec-if-cpt-container"|id="sec-cpt-if"|\bbm-verify\b')
# Imperva/Distil block body. The title sits inside <noscript>, so document.title is empty.
IMPERVA_RE = re.compile(r"pardon our interruption", re.IGNORECASE)
INCAPSULA_INCIDENT_RE = re.compile(r"Incapsula incident ID|_Incapsula_Resource\?SWUDNSAI=", re.IGNORECASE)
SHAPE_RE = re.compile(r"window\['istlWasHere'\]")
PX_RE = re.compile(r'id="px-captcha"|press (?:&amp;|&) hold to confirm you are', re.IGNORECASE)
REDDIT_JSC_RE = re.compile(r'namedItem\("solution"\)|js_challenge=1')

# A real page renders more text than this. Only used to separate a challenge
# stub from a real page that also loads the same vendor script.
STUB_VISIBLE_TEXT = 200
SHELL_BYTES = 15_000

# A block answered with a block status and a near-empty body, whatever the vendor
# markup. Reddit's "You've been blocked by network security" page came back as
# HTTP 403, 190 KB of markup around 221 characters of text, and matched none of
# the vendor patterns above. Keyed on the final top document's status, so a real
# page served 200 is never touched by it.
BLOCK_STATUSES = (401, 403, 429, 503)
BLOCK_STATUS_MAX_TEXT = 1500
NETSEC_BLOCK_RE = re.compile(r"you'?ve been blocked by network security", re.IGNORECASE)


def is_blocked_v2(html: str, title: str, final_url: str = "", status=None) -> tuple[bool, str]:
    blocked, reason = is_blocked(html, title)
    if blocked:
        return True, reason

    url = final_url or ""
    head = html[:300_000]

    if url.startswith("chrome-error://"):
        return True, "v2 net-error: chrome-error:// final URL"
    if 'id="main-frame-error"' in head or re.search(r"<body[^>]*class=\"neterror", head):
        return True, "v2 net-error: Chrome net-error page markup"

    if title and HARD_TITLE_RE.search(title):
        return True, f"v2 challenge title: {title.strip()[:60]}"
    if CF_RT_TK in url:
        return True, "v2 cloudflare: __cf_chl_rt_tk= in final URL"
    if CF_CHL_OPT in head:
        return True, "v2 cloudflare: _cf_chl_opt challenge markup"
    if CF_H_PATH in head and status in (403, 429, 503):
        return True, f"v2 cloudflare: challenge-platform/h/ with HTTP {status}"

    if DD_CAPTCHA_IFRAME_RE.search(head):
        return True, "v2 datadome: captcha-delivery.com/captcha iframe"
    if DD_OBJ_RE.search(head):
        return True, "v2 datadome: dd={...} captcha/interstitial marker"
    if DD_INTERSTITIAL in head:
        return True, "v2 datadome: interstitial"

    vis = len(visible_text(html))
    if KPSDK_RE.search(head) and vis < STUB_VISIBLE_TEXT:
        return True, "v2 kasada: KPSDK bootstrap with no page content"

    if AKAMAI_CPT_RE.search(head):
        return True, "v2 akamai: sec-if-cpt / bm-verify challenge"
    if vis < 1500 and IMPERVA_RE.search(visible_text(head)):
        return True, "v2 imperva: Pardon Our Interruption body"
    if vis < 1500 and INCAPSULA_INCIDENT_RE.search(head):
        return True, "v2 imperva: Incapsula incident block"
    if vis < 1500 and PX_RE.search(head):
        return True, "v2 perimeterx: px-captcha press and hold"
    if vis < STUB_VISIBLE_TEXT and SHAPE_RE.search(head):
        return True, "v2 shape/f5: istlWasHere interstitial"
    if not title.strip() and vis < 50:
        return True, "v2 no title and no rendered text"
    if vis < STUB_VISIBLE_TEXT and REDDIT_JSC_RE.search(head):
        return True, "v2 reddit: js_challenge solution form"
    if vis < BLOCK_STATUS_MAX_TEXT and NETSEC_BLOCK_RE.search(visible_text(head)):
        return True, "v2 network-security block page"
    if status in BLOCK_STATUSES and vis < BLOCK_STATUS_MAX_TEXT:
        return True, f"v2 HTTP {status} with only {vis} chars of rendered text"

    return False, ""


def looks_like_challenge(html: str, title: str, final_url: str = "", status=None) -> bool:
    """Should the reader keep dwelling past the old read?"""
    if is_blocked_v2(html, title, final_url, status)[0]:
        return True
    if status is not None and status >= 400:
        return True
    # A small document with almost no rendered text is a shell that may still be
    # a challenge in progress. Large SPAs whose text lives in shadow DOM are not
    # dwelled on; they are over this size.
    if not title.strip() and len(visible_text(html)) < STUB_VISIBLE_TEXT:
        return True
    if len(html) < SHELL_BYTES and len(visible_text(html)) < STUB_VISIBLE_TEXT:
        return True
    return False
