"""
Web Scraper Agent — Core ReAct Loop
Uses Playwright for browsing + Groq (llama-3.3-70b-versatile) as the reasoning brain.
"""

import asyncio
import json
import re
import os
from typing import Optional
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Page, Browser
from groq import Groq
import trafilatura
from bs4 import BeautifulSoup


# ─── Config ────────────────────────────────────────────────────────────────────

MAX_PAGES      = 8          # safety cap — how many pages agent may visit
MAX_CONTENT_LEN = 6000      # chars sent to LLM per page (avoids token blowout)
MODEL          = "meta-llama/llama-4-scout-17b-16e-instruct"
TIMEOUT_MS     = 20_000     # Playwright wait timeout

SYSTEM_PROMPT = """You are a precise web scraping agent. Given a user query and the text content of a webpage, decide what to do next.

You MUST reply with ONLY valid JSON — no markdown, no prose, no explanation outside the JSON.

Your JSON must have exactly this shape:
{
  "action": "answer" | "navigate" | "not_found",
  "reasoning": "short explanation of your decision",
  "data": [
    { "field1": "value", "field2": "value", "source_url": "https://..." }
  ],
  "next_url": "https://..."
}

Rules:
- "answer"    → You found enough data. Fill "data" with all extracted items. Each item MUST include "source_url".
- "navigate"  → You need to visit another page. Set "next_url" to the best URL from the page links provided. Leave "data" as [].
- "not_found" → The page has no relevant data and no useful links to follow. Leave "data" as [] and "next_url" as "".

IMPORTANT:
- Extract ONLY what the user asked for. Do not invent fields.
- If the query asks for N items and you found M < N, return what you have and use "answer".
- source_url must be the actual page URL where that item was found.
- next_url must be a full absolute URL taken from the "Available links" list, not invented.
- Never return duplicate items.
- Be concise in "reasoning" (one sentence).

E-COMMERCE NAVIGATION STRATEGY:
When the query involves products with filters (price range, ratings, category) on sites like Amazon, Flipkart, etc.:
- PREFER links that contain search/filter parameters: s?k=, search?, /s?, rh=p_72, price, sort
- Look for links with keywords matching the product category (mobile, phone, laptop, etc.)
- A search results URL like /s?k=mobile+phones+under+10000 is FAR better than /bestsellers or /deals
- If no good filtered link exists in the provided links, choose the closest category page
- Never pick account, login, cart, wishlist, or app-download links
"""

# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class AgentState:
    query: str
    start_url: str
    visited: list[str] = field(default_factory=list)
    all_data: list[dict] = field(default_factory=list)
    pages_visited: int = 0
    final_reasoning: str = ""


@dataclass
class LLMResponse:
    action: str           # "answer" | "navigate" | "not_found"
    reasoning: str
    data: list[dict]
    next_url: str


# ─── Browser helpers ──────────────────────────────────────────────────────────

async def fetch_page(page: Page, url: str) -> tuple[str, list[str], str]:
    """
    Navigate to URL, return (clean_text, links, final_url).
    Uses trafilatura for best text extraction; falls back to BeautifulSoup.
    """
    try:
        await page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(1500)   # let JS settle
    except Exception as e:
        # Some pages never reach networkidle — try domcontentloaded
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(2000)
        except Exception as e2:
            return f"[Page load failed: {e2}]", [], url

    final_url = page.url
    html = await page.content()

    trafilatura_text = trafilatura.extract(html, include_links=False, include_tables=True) or ""

    # Always also run structured bs4 extraction targeting product/listing cards.
    # trafilatura ignores <article>, <li class="product_pod">, card divs, etc.
    # We take whichever extraction gives more content.
    bs4_text = _bs4_extract(html)
    # ── Text extraction ──
    text = trafilatura_text if len(trafilatura_text) >= len(bs4_text) else bs4_text
    if len(text) < 200:
        # fallback: BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)

    text = _clean_text(text)[:MAX_CONTENT_LEN]

    # ── Link extraction ──
    soup = BeautifulSoup(html, "html.parser")
    base = _base_url(final_url)
    links: list[str] = []
    seen_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("javascript:") or href == "#":
            continue
        abs_href = _absolutize(href, base)
        if abs_href and abs_href not in seen_links:
            seen_links.add(abs_href)
            label = a.get_text(strip=True)[:60]
            links.append(f"{label} → {abs_href}")
            if len(links) >= 40:
                break

    return text, links, final_url


def _clean_text(t: str) -> str:
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()

def _bs4_extract(html: str) -> str:
    """
    Structured extraction targeting product listing pages and data-rich containers
    that trafilatura ignores (article cards, li.product_pod, div.product, table rows).
    Returns a text block the LLM can read — one item per line.
    """
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []

    # ── Product / listing cards (e-commerce, books, directories) ──
    card_selectors = [
        "article.product_pod",          # books.toscrape
        "div.product-container",
        "div.s-result-item",            # Amazon
        "div[data-component-type='s-search-result']",  # Amazon
        "li.product",
        "div.product_pod",
        "div.item",
        "div.card",
        "div.product-item",
    ]
    found_cards = False
    for sel in card_selectors:
        cards = soup.select(sel)
        if cards:
            found_cards = True
            for card in cards:
                # Extract all text tokens from this card
                parts = []
                # Title / name: h1-h4, .title, [title] attr on <a>
                for heading in card.find_all(["h1","h2","h3","h4"]):
                    t = heading.get_text(strip=True)
                    if t:
                        parts.append(t)
                # Named <a title="...">
                for a in card.find_all("a", title=True):
                    t = a["title"].strip()
                    if t and t not in parts:
                        parts.append(t)
                # Price / rating / misc text spans
                for span in card.find_all(["p", "span", "div"], recursive=True):
                    cls = " ".join(span.get("class", []))
                    if any(k in cls.lower() for k in ["price","rating","star","stock","review","category","author"]):
                        t = span.get_text(strip=True)
                        if t and t not in parts:
                            parts.append(t)
                if parts:
                    lines.append(" | ".join(parts))
            break  # used the first selector that matched

    # ── Table rows (rankings, data tables) ──
    if not found_cards:
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if cells:
                    if headers:
                        row_text = " | ".join(f"{h}: {c}" for h, c in zip(headers, cells) if c)
                    else:
                        row_text = " | ".join(c for c in cells if c)
                    if row_text:
                        lines.append(row_text)

    # ── Fallback: strip scripts/styles and return cleaned body text ──
    if not lines:
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        raw = soup.get_text(separator="\n", strip=True)
        lines = [l for l in raw.splitlines() if len(l.strip()) > 10]

    return "\n".join(lines)

def _base_url(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _absolutize(href: str, base: str) -> Optional[str]:
    from urllib.parse import urljoin, urlparse
    try:
        abs_url = urljoin(base, href)
        p = urlparse(abs_url)
        if p.scheme in ("http", "https"):
            return abs_url
    except Exception:
        pass
    return None


# ─── LLM call ─────────────────────────────────────────────────────────────────

def call_llm(client: Groq, state: AgentState,
             page_text: str, links: list[str], current_url: str) -> LLMResponse:
    visited_summary = "\n".join(f"  • {u}" for u in state.visited) or "  (none yet)"
    data_so_far = json.dumps(state.all_data, ensure_ascii=False, indent=2) if state.all_data else "[]"

    links_block = "\n".join(f"  {l}" for l in links[:30]) or "  (no links found)"

    user_msg = f"""USER QUERY: {state.query}

CURRENT PAGE URL: {current_url}

PAGE CONTENT:
{page_text}

AVAILABLE LINKS ON THIS PAGE (label → absolute URL):
{links_block}

PAGES ALREADY VISITED:
{visited_summary}

DATA EXTRACTED SO FAR:
{data_so_far}

Based on the above, what is your next action?"""

    raw = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=2000,
    ).choices[0].message.content

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # Try to salvage JSON embedded in text
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        obj = json.loads(m.group()) if m else {}

    return LLMResponse(
        action    = obj.get("action", "not_found"),
        reasoning = obj.get("reasoning", ""),
        data      = obj.get("data", []),
        next_url  = obj.get("next_url", ""),
    )


# ─── Main agent loop ──────────────────────────────────────────────────────────

async def run_agent(url: str, query: str, groq_api_key: str,
                    progress_cb=None) -> dict:
    """
    Entry point. Returns:
    {
      "status":  "success" | "not_found" | "error",
      "data":    [...],
      "visited": [...],
      "reasoning": "...",
    }
    """
    def _log(msg: str):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg)

    client = Groq(api_key=groq_api_key)
    state  = AgentState(query=query, start_url=url)

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        current_url = url

        for step in range(MAX_PAGES):
            if current_url in state.visited:
                _log(f"Already visited {current_url} — stopping to avoid loop.")
                break

            _log(f"[{step+1}/{MAX_PAGES}] Fetching: {current_url}")
            page_text, links, resolved_url = await fetch_page(page, current_url)

            # Guard against redirect loops: resolved_url may differ from current_url
            if resolved_url in state.visited:
                _log(f"Redirect led to already-visited {resolved_url} — stopping.")
                break

            state.visited.append(resolved_url)
            state.pages_visited += 1

            if page_text.startswith("[Page load failed"):
                _log(f"Load error: {page_text}")
                break

            _log(f"Asking LLM ({len(page_text)} chars, {len(links)} links)…")
            resp = call_llm(client, state, page_text, links, resolved_url)
            _log(f"Action={resp.action} | {resp.reasoning}")

            if resp.action == "answer":
                # Merge new data, deduplicate by converting to JSON str
                seen = {json.dumps(d, sort_keys=True) for d in state.all_data}
                for item in resp.data:
                    key = json.dumps(item, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        state.all_data.append(item)
                state.final_reasoning = resp.reasoning
                _log(f"Done — {len(state.all_data)} items extracted.")
                # Do NOT call browser.close() here — let the async-with context
                # manager handle cleanup to avoid double-close TargetClosedError
                break

            elif resp.action == "navigate":
                if not resp.next_url:
                    _log("⚠  LLM chose navigate but gave no URL — stopping.")
                    break
                current_url = resp.next_url

            else:  # not_found
                state.final_reasoning = resp.reasoning
                _log("🔍 Not found on this path.")
                break

        # Explicitly close context before browser for clean resource release
        await context.close()
        await browser.close()

    if state.all_data:
        return {
            "status": "success",
            "data": state.all_data,
            "visited": state.visited,
            # Use the LLM's own reasoning if available; fall back only if agent
            # hit the page limit without an explicit "answer" action
            "reasoning": state.final_reasoning or "Data collected across visited pages.",
        }

    return {
        "status": "not_found",
        "data": [],
        "visited": state.visited,
        "reasoning": state.final_reasoning or "No relevant data found on the visited pages.",
    }
