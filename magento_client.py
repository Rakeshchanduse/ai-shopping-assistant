"""
magento_client.py
==================
A thin Magento 2 REST client for a shopping assistant.

Design goals
------------
* SMALL surface: only what a shopper needs - browse, cart, checkout, orders.
* CLEAN output: every public method returns a *user-facing* dict (name, price,
  image, ...). Internal Magento fields (id, tax_class_id, attribute_set_id,
  attribute codes, tokens) are NEVER returned, so they can't leak to the user.

Four helpers do all the cleaning - `_clean_product`, `_clean_cart`,
`_clean_order`, `_clean_profile`. To change WHAT the user sees, edit those four.

Auth: catalog + orders use the ADMIN (integration) token; cart/profile/checkout
use the logged-in CUSTOMER token. Guest carts use a masked cart id (no token).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("magento")

MEDIA_PATH = "catalog/product"                      # sub-path under <base>/media for product images
DEBUG_BODIES = os.getenv("MAGENTO_DEBUG") == "1"    # log request/response bodies (may be sensitive)


# ------------------------------------------------------------
# small text helpers
# ------------------------------------------------------------
def strip_html(text) -> str:
    """Remove HTML tags and collapse whitespace (Magento descriptions are HTML)."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text))).strip()


def format_description(text) -> str:
    """Turn a Magento HTML description into clean HTML the UI can render nicely: the lead
    paragraph(s) as text, followed by a real <ul> bullet list (so bullet points appear one per
    line instead of collapsing onto a single line). Falls back to plain stripped text when there
    is no list."""
    if not text:
        return ""
    html = str(text)
    items = re.findall(r"(?is)<li[^>]*>(.*?)</li>", html)
    intro_html = re.sub(r"(?is)<ul[^>]*>.*?</ul>|<ol[^>]*>.*?</ol>", "", html)  # drop list blocks
    intro = strip_html(intro_html)
    parts = []
    if intro:
        parts.append(f"<div>{intro}</div>")
    lis = [strip_html(i) for i in items if strip_html(i)]
    if lis:
        parts.append("<ul>" + "".join(f"<li>{li}</li>" for li in lis) + "</ul>")
    return "".join(parts) if parts else strip_html(html)


def money(value):
    """Coerce a Magento price to a rounded float, or None."""
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def compact(d: dict) -> dict:
    """Drop keys whose value is None or '' so the output stays tidy."""
    return {k: v for k, v in d.items() if v not in (None, "")}


def fmt_address(addr) -> str:
    """One readable line from a Magento order address dict (billing/shipping)."""
    if not isinstance(addr, dict):
        return ""
    name = " ".join(p for p in (addr.get("firstname"), addr.get("lastname")) if p)
    street = addr.get("street")
    street = ", ".join(street) if isinstance(street, list) else (street or "")
    region = addr.get("region")
    region = region.get("region") if isinstance(region, dict) else region
    city_line = ", ".join(p for p in (addr.get("city"), region, addr.get("postcode")) if p)
    parts = [name, street, city_line, addr.get("country_id"),
             addr.get("telephone"), addr.get("email")]
    return " | ".join(p for p in parts if p)


# Friendly labels for the common offline payment method codes.
_PAYMENT_LABELS = {
    "checkmo": "Check / Money Order",
    "banktransfer": "Bank Transfer",
    "cashondelivery": "Cash On Delivery",
    "purchaseorder": "Purchase Order",
    "free": "No Payment Required",
}


# Currency code -> display symbol. Written with \u escapes so this SOURCE FILE
# stays pure ASCII (non-ASCII bytes in source broke imports on some setups);
# at runtime these become the real symbols, which is fine inside output strings.
CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "INR": "\u20b9",
    "JPY": "\u00a5", "CNY": "\u00a5", "AUD": "A$", "CAD": "C$",
    "SGD": "S$", "AED": "AED ",
}


class MagentoError(Exception):
    """A failed Magento API call. `body` is the (safe) message to show the user."""

    def __init__(self, status, body, url, method=""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"[{method} {status}] {url} -> {body}")


# ------------------------------------------------------------
# client
# ------------------------------------------------------------
class MagentoClient:
    def __init__(self, base_url=None, verify_ssl=False, timeout=30):
        self.base_url = (base_url or os.getenv("MAGENTO_BASE_URL", "https://magento.test")).rstrip("/")
        self.rest = f"{self.base_url}/rest/V1"
        self.verify = verify_ssl
        self.timeout = timeout
        self.http = requests.Session()

        # session state
        self.admin_token = os.getenv("MAGENTO_ADMIN_TOKEN") or None
        self.customer_token = None
        self.customer_email = None
        self.is_guest = True
        self.cart_id = None          # masked string (guest) or int (logged-in)
        self.order_id = None

        # caches
        self._img_cache: dict[str, str] = {}                 # sku -> small image url
        self._country_cache: dict[str, str] | None = None
        self._attr_opt_cache: dict[str, dict] = {}           # attr code -> {value_index: label}
        self._attr_code_cache: dict = {}                     # attribute_id -> attribute_code
        self._filter_attr_cache = None                       # cached filterable select attributes
        self._attr_set_cache = None                          # cached attribute sets (id + name)
        self._cat_tree_cache = None                          # cached full category tree
        self._children_cache: dict = {}                      # cached configurable children per sku
        self._last_total = None                              # total_count of the last product filter

        # store configuration (fetched once, then cached for this process)
        self._config: dict | None = None                     # parsed storeConfigs
        self._config_at = 0.0                                # load time (for optional TTL)
        self.config_ttl = int(os.getenv("MAGENTO_CONFIG_TTL", "0"))   # seconds; 0 = never auto-expire
        self._media_override = os.getenv("MAGENTO_MEDIA_URL")         # explicit media base (e.g. CDN)
        self._media_base: str | None = self._media_override
        self._currency: str | None = None                    # display currency code

    # -- low level HTTP -------------------------------------------------------
    def _request(self, method, url, token=None, payload=None, params=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        log.debug("%s %s", method, url)
        if DEBUG_BODIES and payload is not None:
            log.debug("  payload=%s", json.dumps(payload)[:1000])
        resp = self.http.request(
            method, url, headers=headers,
            data=json.dumps(payload) if payload is not None else None,
            params=params, verify=self.verify, timeout=self.timeout,
        )
        try:
            data = resp.json()
        except ValueError:
            data = resp.text                       # token endpoints return a bare string
        log.debug("  -> %s", resp.status_code)
        if DEBUG_BODIES:
            log.debug("  body=%s", str(data)[:1000])
        if not resp.ok:
            msg = data.get("message") if isinstance(data, dict) else data   # Magento: {"message": ...}
            raise MagentoError(resp.status_code, msg or data, url, method)
        return data

    def _get(self, url, token=None, params=None):
        return self._request("GET", url, token=token, params=params)

    def _post(self, url, payload=None, token=None):
        return self._request("POST", url, token=token, payload=payload)

    def _put(self, url, payload=None, token=None):
        return self._request("PUT", url, token=token, payload=payload)

    def _delete(self, url, token=None):
        return self._request("DELETE", url, token=token)

    # -- auth -----------------------------------------------------------------
    def ensure_admin(self) -> str:
        """Return an admin token, fetching one from env credentials if needed.
        Prefer MAGENTO_ADMIN_TOKEN (integration token) - it bypasses 2FA."""
        if not self.admin_token:
            self.admin_token = self._post(
                f"{self.rest}/integration/admin/token",
                {"username": os.getenv("MAGENTO_ADMIN_USER", "admin"),
                 "password": os.getenv("MAGENTO_ADMIN_PASS", "")},
            )
        return self.admin_token

    def register_customer(self, email, firstname, lastname, password) -> dict:
        self._post(f"{self.rest}/customers", {
            "customer": {"email": email, "firstname": firstname, "lastname": lastname},
            "password": password,
        })
        self.customer_email = email
        return {"email": email, "message": "Account created."}

    def login(self, email, password) -> dict:
        """Log in, then MOVE any guest-cart items into the customer's cart."""
        pending = self._guest_cart_items() if (self.is_guest and self.cart_id) else []
        self.customer_token = self._post(
            f"{self.rest}/integration/customer/token",
            {"username": email, "password": password},
        )
        self.customer_email = email
        self.is_guest = False
        self.cart_id = None
        self._ensure_customer_cart()
        merged = self._merge_items(pending)
        return {"email": email, "merged_items": merged}

    def start_guest(self) -> dict:
        self.customer_token = None
        self.is_guest = True
        self.cart_id = None
        return {"mode": "guest"}

    # -- profile --------------------------------------------------------------
    def get_my_profile(self) -> dict:
        if not self.customer_token:
            raise MagentoError(0, "Please log in first.", "")
        return self._clean_profile(self._get(f"{self.rest}/customers/me", token=self.customer_token))

    def update_my_profile(self, firstname=None, lastname=None) -> dict:
        if not self.customer_token:
            raise MagentoError(0, "Please log in first.", "")
        profile = self._get(f"{self.rest}/customers/me", token=self.customer_token)  # full object needed for PUT
        if firstname:
            profile["firstname"] = firstname
        if lastname:
            profile["lastname"] = lastname
        return self._clean_profile(
            self._put(f"{self.rest}/customers/me", {"customer": profile}, token=self.customer_token)
        )

    def add_address(self, address) -> dict:
        """Save a NEW address to the LOGGED-IN customer's address book, so it is stored for
        future orders. The first saved address becomes the default billing + shipping. The
        region is re-nested because the customer address book expects a region OBJECT, not a
        flat region_id. Name/country come straight from what the customer entered (no defaults)."""
        if not self.customer_token:
            raise MagentoError(0, "Please log in first.", "")
        profile = self._get(f"{self.rest}/customers/me", token=self.customer_token)  # full object needed for PUT

        book = {k: address[k] for k in
                ("street", "city", "postcode", "country_id", "telephone")
                if address.get(k)}
        book["firstname"] = address.get("firstname")
        book["lastname"] = address.get("lastname")
        region = compact({
            "region_id": address.get("region_id"),
            "region": address.get("region"),
            "region_code": address.get("region_code"),
        })
        if region:
            book["region"] = region

        existing = profile.get("addresses") or []
        if not existing:                       # first address -> make it the default
            book["default_billing"] = True
            book["default_shipping"] = True
        profile["addresses"] = existing + [book]

        return self._clean_profile(
            self._put(f"{self.rest}/customers/me", {"customer": profile}, token=self.customer_token)
        )

    # -- store configuration (fetched once, cached, refreshable) --------------
    def load_config(self, force: bool = False) -> dict:
        """Fetch the whole store configuration in ONE call and cache it for this process.

        Populates media base, currency, locale, timezone, weight unit, store code, etc.
        Loads lazily on first use; pass force=True (or call refresh_config()) after you
        change settings in Magento admin. With MAGENTO_CONFIG_TTL set, it also auto-reloads
        after that many seconds."""
        cached = self._config is not None and not force
        if cached and self.config_ttl and (time.time() - self._config_at) > self.config_ttl:
            cached = False
        if cached:
            return self._config

        try:
            data = self._get(f"{self.rest}/store/storeConfigs", token=self.ensure_admin())
            raw = data[0] if isinstance(data, list) and data else {}
        except Exception:  # noqa: BLE001 - never let config loading crash a request
            raw = {}

        cfg = {
            "store_code": raw.get("code"),
            "base_url": raw.get("base_url"),
            "secure_base_url": raw.get("secure_base_url"),
            "base_media_url": raw.get("base_media_url"),
            "locale": raw.get("locale"),
            "timezone": raw.get("timezone"),
            "weight_unit": raw.get("weight_unit"),
            "currency": (raw.get("default_display_currency_code")
                         or raw.get("base_currency_code") or "USD"),
        }
        # Derive the values the rest of the client reads, from this single response.
        if not self._media_override:
            bm = cfg["base_media_url"]
            self._media_base = (self._rehost(f"{bm.rstrip('/')}/{MEDIA_PATH}") if bm
                                else f"{self.base_url}/media/{MEDIA_PATH}")
        self._currency = cfg["currency"]
        self._config = cfg
        self._config_at = time.time()
        log.info("Store config loaded: currency=%s locale=%s media_base=%s",
                 cfg["currency"], cfg["locale"], self._media_base)
        return self._config

    def refresh_config(self) -> dict:
        """Re-fetch store configuration (call after changing settings in admin)."""
        return self.load_config(force=True)

    def config(self) -> dict:
        """The cached store configuration (loads on first use)."""
        return self.load_config()

    # -- images ---------------------------------------------------------------
    def media_base(self) -> str:
        """Absolute base URL for product images, e.g. https://magento.test/media/catalog/product.
        Comes from the cached store config; forced onto the reachable MAGENTO_BASE_URL host
        (Magento's stored base_media_url is often a wrong/placeholder host). Override entirely
        with MAGENTO_MEDIA_URL."""
        if not self._media_base:
            self.load_config()
        return (self._media_base or f"{self.base_url}/media/{MEDIA_PATH}").rstrip("/")

    def _rehost(self, url: str) -> str:
        """Swap an absolute URL's scheme+host to MAGENTO_BASE_URL, keeping the path."""
        try:
            u, b = urlsplit(url), urlsplit(self.base_url)
            if not u.netloc or u.netloc == b.netloc:
                return url.rstrip("/")
            return urlunsplit((b.scheme or u.scheme, b.netloc, u.path, u.query, u.fragment)).rstrip("/")
        except Exception:  # noqa: BLE001
            return url.rstrip("/")

    def image_url(self, file_path) -> str:
        """Turn a Magento image reference ('/m/b/x.jpg' or absolute) into a full URL."""
        if not file_path:
            return ""
        f = str(file_path).strip()
        if f.startswith("http"):
            return self._rehost(f) if "/media/" in f else f
        return f"{self.media_base()}/{f.lstrip('/')}"

    @staticmethod
    def _attr(product, code):
        """Read one value from a product's custom_attributes list."""
        for a in product.get("custom_attributes", []) or []:
            if a.get("attribute_code") == code:
                return a.get("value")
        return None

    def _small_image(self, product) -> str:
        """The product's small image (falls back to thumbnail, then main, then gallery)."""
        ref = (self._attr(product, "small_image") or self._attr(product, "thumbnail")
               or self._attr(product, "image"))
        if not ref:
            gallery = product.get("media_gallery_entries") or []
            ref = gallery[0].get("file") if gallery else None
        return self.image_url(ref)

    def _gallery(self, product) -> list[str]:
        return [self.image_url(g.get("file"))
                for g in (product.get("media_gallery_entries") or []) if g.get("file")]

    def _image_by_sku(self, sku) -> str:
        """Cached small-image lookup for cart/order line items (which carry only a sku)."""
        if not sku:
            return ""
        if sku not in self._img_cache:
            try:
                p = self._get(f"{self.rest}/products/{sku}", token=self.ensure_admin())
                self._img_cache[sku] = self._small_image(p)
            except Exception:  # noqa: BLE001
                self._img_cache[sku] = ""
        return self._img_cache[sku]

    # -- currency / price formatting ------------------------------------------
    def currency_code(self) -> str:
        """The store's display currency code (e.g. USD, INR), from the cached config."""
        if self._currency is None:
            self.load_config()
        return self._currency or "USD"

    def _symbol(self, code=None) -> str:
        code = code or self.currency_code()
        return CURRENCY_SYMBOLS.get(code, code + " ")     # unknown code -> "BRL 10.00"

    def price_str(self, value, code=None):
        """Format a number as a price WITH the currency symbol, e.g. '$48.00' / '\u20b948.00'.
        Returns None for missing values so compact() drops the field."""
        m = money(value)
        if m is None:
            return None
        return f"{self._symbol(code)}{m:.2f}"

    # -- cleaners: raw Magento dict -> user-facing dict  (EDIT HERE to change output) --
    def _clean_product(self, p: dict) -> dict:
        tid = p.get("type_id")
        stock = (p.get("extension_attributes") or {}).get("stock_item") or {}
        out = {
            "name": p.get("name"),
            "sku": p.get("sku"),
            "type": tid if tid != "simple" else None,  # e.g. "configurable" / "bundle" / "grouped"
            "price": self.price_str(p.get("price")),
            "special_price": self.price_str(self._attr(p, "special_price")),
            "image": self._small_image(p),
            "description": strip_html(self._attr(p, "short_description") or self._attr(p, "description"))[:200],
        }
        if tid == "configurable":
            # a configurable has no price of its own -> show the lowest variant price.
            # Stock lives on the PARENT's stock_item (Magento indexes it); the /children
            # endpoint often omits stock_item, so we trust the parent for availability.
            low, count = self._configurable_summary(p.get("sku"))
            if low is not None:
                out["price"] = self.price_str(low)     # lowest variant price
                out["price_note"] = "from"             # UI shows 'From <price>'
            out["in_stock"] = bool(stock.get("is_in_stock")) if stock else None
            out["variant_count"] = count or None
        elif stock:
            out["in_stock"] = bool(stock.get("is_in_stock"))
        return compact(out)

    def _clean_product_detail(self, p: dict) -> dict:
        out = self._clean_product(p)
        tid = p.get("type_id")
        out["description"] = format_description(self._attr(p, "description") or self._attr(p, "short_description"))
        out["images"] = self._gallery(p) or ([out["image"]] if out.get("image") else [])
        if tid == "configurable":
            # A configurable has no stock of its own - it lives on its variants.
            # Show the variants so the customer can pick one (add it by its sku).
            variants = self._variants(p)
            if variants:
                out["variants"] = variants
                out["in_stock"] = any(v.get("in_stock") for v in variants)
        elif tid == "bundle":
            # Groups of selectable products; the customer picks one per option, then
            # add_bundle_to_cart is called with the chosen option_id/selection_id.
            out["bundle_options"] = self._bundle_options(p)
        elif tid == "grouped":
            # A set of simple products; the customer adds each one by its own sku.
            out["grouped_items"] = self._grouped_items(p)
        else:
            stock = (p.get("extension_attributes") or {}).get("stock_item") or {}
            if stock.get("qty") is not None:
                out["qty"] = stock.get("qty")
        return compact(out)

    def _clean_cart(self, cart: dict, totals: dict) -> dict:
        cur = totals.get("quote_currency_code")
        rows = {t.get("item_id"): t for t in (totals.get("items") or [])}
        items = []
        for it in cart.get("items", []) or []:
            t = rows.get(it.get("item_id"), {})
            items.append(compact({
                "item_id": it.get("item_id"),        # needed to update/remove; not shown to user
                "name": it.get("name"),
                "sku": it.get("sku"),
                "price": self.price_str(it.get("price"), cur),
                "qty": it.get("qty"),
                "row_total": self.price_str(t.get("row_total"), cur),
                "image": self._image_by_sku(it.get("sku")),
            }))
        return compact({
            "items": items,
            "currency": cur,
            "items_qty": totals.get("items_qty"),
            "subtotal": self.price_str(totals.get("subtotal"), cur),
            "discount": self.price_str(totals.get("discount_amount"), cur),
            "coupon": totals.get("coupon_code"),
            "shipping": self.price_str(totals.get("shipping_amount"), cur),
            "tax": self.price_str(totals.get("tax_amount"), cur),
            "grand_total": self.price_str(totals.get("grand_total"), cur),
        })

    def _clean_order(self, o: dict) -> dict:
        cur = o.get("order_currency_code")
        items = [compact({
            "name": it.get("name"),
            "sku": it.get("sku"),
            "qty": it.get("qty_ordered"),
            "price": self.price_str(it.get("price"), cur),
            "row_total": self.price_str(it.get("row_total"), cur),
            "image": self._image_by_sku(it.get("sku")),
        }) for it in (o.get("items") or []) if it.get("parent_item_id") is None]
        # shipping address + method live under extension_attributes.shipping_assignments
        assignments = (o.get("extension_attributes") or {}).get("shipping_assignments") or []
        ship = (assignments[0] or {}).get("shipping") if assignments else {}
        ship = ship or {}
        pay_code = (o.get("payment") or {}).get("method") or ""
        return compact({
            "order_number": "#" + str(o.get("increment_id")) if o.get("increment_id") else None,
            "status": o.get("status"),
            "date": (o.get("created_at") or "")[:10],
            "items": items,
            "subtotal": self.price_str(o.get("subtotal"), cur),
            "discount": self.price_str(o.get("discount_amount"), cur),
            "shipping": self.price_str(o.get("shipping_amount"), cur),
            "tax": self.price_str(o.get("tax_amount"), cur),
            "grand_total": self.price_str(o.get("grand_total"), cur),
            "total": self.price_str(o.get("grand_total"), cur),   # alias (back-compat)
            "currency": cur,
            "shipping_address": fmt_address(ship.get("address")),
            "billing_address": fmt_address(o.get("billing_address")),
            "shipping_method": o.get("shipping_description") or ship.get("method") or "",
            "payment_method": _PAYMENT_LABELS.get(pay_code, pay_code),
        })

    def _clean_profile(self, c: dict) -> dict:
        addresses = []
        for a in c.get("addresses", []) or []:
            region = a.get("region", {}).get("region") if isinstance(a.get("region"), dict) else a.get("region")
            parts = [", ".join(a.get("street") or []), a.get("city"), region, a.get("postcode"), a.get("country_id")]
            addresses.append(", ".join(p for p in parts if p))
        return compact({
            "name": " ".join(x for x in [c.get("firstname"), c.get("lastname")] if x),
            "email": c.get("email"),
            "addresses": addresses,
        })

    # -- catalog --------------------------------------------------------------
    # Visibility: 1=not visible (hidden variants), 2=catalog, 3=search, 4=catalog+search.
    # We show 2,3,4 so configurable parents AND standalone simples appear, but the
    # hidden simple variants of a configurable do NOT clutter the listing.
    VISIBLE = "2,3,4"

    def list_products(self, limit=12) -> list[dict]:
        """Products a shopper can browse and pick (simple or configurable), enabled + visible."""
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "status",
            "searchCriteria[filterGroups][0][filters][0][value]": "1",
            "searchCriteria[filterGroups][1][filters][0][field]": "visibility",
            "searchCriteria[filterGroups][1][filters][0][value]": self.VISIBLE,
            "searchCriteria[filterGroups][1][filters][0][conditionType]": "in",
            "searchCriteria[pageSize]": limit,
        }
        data = self._get(f"{self.rest}/products", token=self.ensure_admin(), params=params)
        return [self._clean_product(p) for p in data.get("items", [])]

    def search_products(self, query, limit=12) -> list[dict]:
        """Real catalog search via Magento's search engine (the storefront search box), so a
        term like 'sweatshirt' or 'woman hoodie' matches on name/description/relevance, not just
        the exact product name. Falls back to a plain name match if the engine returns nothing."""
        ids = self._search_ids(query, limit)
        if ids:
            products = self._products_by_ids(ids)
            if products:
                return [self._clean_product(p) for p in products]
        return self._search_by_name(query, limit)

    def _search_ids(self, query, limit):
        """Product ids from the search engine, best-match first (empty on any failure)."""
        params = {
            "searchCriteria[requestName]": "quick_search_container",
            "searchCriteria[filterGroups][0][filters][0][field]": "search_term",
            "searchCriteria[filterGroups][0][filters][0][value]": query,
            "searchCriteria[pageSize]": limit,
        }
        try:
            data = self._get(f"{self.rest}/search", token=self.ensure_admin(), params=params)
        except Exception:  # noqa: BLE001 - search engine may be down/misconfigured
            return []
        return [it.get("id") for it in (data.get("items") or []) if it.get("id")]

    def _products_by_ids(self, ids):
        """Full product rows for the given ids, kept in the search's relevance order."""
        if not ids:
            return []
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "entity_id",
            "searchCriteria[filterGroups][0][filters][0][value]": ",".join(str(i) for i in ids),
            "searchCriteria[filterGroups][0][filters][0][conditionType]": "in",
            "searchCriteria[pageSize]": len(ids),
        }
        data = self._get(f"{self.rest}/products", token=self.ensure_admin(), params=params)
        items = data.get("items", []) or []
        order = {int(i): n for n, i in enumerate(ids)}
        items.sort(key=lambda pr: order.get(pr.get("id"), 10**9))
        return items

    def _search_by_name(self, query, limit):
        """Fallback: simple visible name LIKE (used if the search engine returns nothing)."""
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "name",
            "searchCriteria[filterGroups][0][filters][0][value]": f"%{query}%",
            "searchCriteria[filterGroups][0][filters][0][conditionType]": "like",
            "searchCriteria[filterGroups][1][filters][0][field]": "visibility",
            "searchCriteria[filterGroups][1][filters][0][value]": self.VISIBLE,
            "searchCriteria[filterGroups][1][filters][0][conditionType]": "in",
            "searchCriteria[pageSize]": limit,
        }
        data = self._get(f"{self.rest}/products", token=self.ensure_admin(), params=params)
        return [self._clean_product(p) for p in data.get("items", [])]

    def get_product(self, sku) -> dict:
        p = self._get(f"{self.rest}/products/{sku}", token=self.ensure_admin())
        return self._clean_product_detail(p)

    def _category_tree(self):
        """The full active category tree, fetched once and cached."""
        if self._cat_tree_cache is None:
            try:
                self._cat_tree_cache = self._get(f"{self.rest}/categories", token=self.ensure_admin())
            except Exception:  # noqa: BLE001
                self._cat_tree_cache = {}
        return self._cat_tree_cache

    def _descendant_ids(self, category_id):
        """A category id plus all its descendant ids, read from the cached tree. Magento's
        category_id filter is direct-only, so to list a PARENT category (e.g. Women > Tops) we
        must include every subcategory id or the leaf products are missed."""
        def collect(n):
            ids = [n["id"]] if n.get("id") is not None else []
            for ch in n.get("children_data") or []:
                ids += collect(ch)
            return ids

        def find(n):
            if str(n.get("id")) == str(category_id):
                return collect(n)
            for ch in n.get("children_data") or []:
                r = find(ch)
                if r:
                    return r
            return []

        return find(self._category_tree()) or [category_id]

    def products_in_category(self, category_id, limit=50) -> list[dict]:
        """Visible products inside a category AND all of its subcategories."""
        ids = self._descendant_ids(category_id)
        return self._products_by_filters(
            [("category_id", ",".join(str(i) for i in ids), "in")], limit)

    # -- categories -----------------------------------------------------------
    def list_categories(self) -> list[dict]:
        """The active category tree (names + ids) so the customer can pick one to browse."""
        tree = self._get(f"{self.rest}/categories", token=self.ensure_admin())
        return [self._clean_category(c) for c in (tree.get("children_data") or []) if c.get("is_active")]

    _CAT_SYNONYMS = {
        "woman": "women", "womens": "women", "ladies": "women", "lady": "women", "girl": "women",
        "man": "men", "mens": "men", "guy": "men", "boy": "men", "kid": "kids", "child": "kids",
    }

    def find_categories(self, query, limit=8) -> list[dict]:
        """Find categories that match a shopper phrase like 'women sweatshirt' by scoring how many
        of its words appear in each category's full path (with a few gender synonyms). This is how
        we honour BOTH the gender and the product type: 'women sweatshirt' ->
        'Women > Tops > Hoodies & Sweatshirts', whose products don't need 'sweatshirt' in their
        name. Returns best-first: id, name, path, product_count."""
        try:
            tree = self._get(f"{self.rest}/categories", token=self.ensure_admin())
        except Exception:  # noqa: BLE001
            return []
        words = [self._CAT_SYNONYMS.get(w, w)
                 for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2]
        qwords = {self._norm_word(w) for w in words}
        if not qwords:
            return []
        skip = {"root catalog", "default category"}
        matches = []

        def walk(node, path):
            name = (node.get("name") or "").strip()
            keep = bool(name) and name.lower() not in skip
            full = path + [name] if keep else path
            if node.get("is_active") and keep:
                # WHOLE-WORD match (so 'men' does NOT match 'women', 'jacket' matches 'Jackets')
                path_words = set()
                for seg in full:
                    path_words |= {self._norm_word(x)
                                   for x in re.split(r"[^a-z0-9]+", seg.lower()) if x}
                name_words = {self._norm_word(x)
                              for x in re.split(r"[^a-z0-9]+", name.lower()) if x}
                score = len(qwords & path_words)          # gender + type words in the path
                name_hit = bool(qwords & name_words)       # a query word hits THIS category's name
                if score and name_hit:
                    matches.append((score, len(full), {
                        "id": node.get("id"),
                        "name": name,
                        "path": " > ".join(full),
                        "product_count": node.get("product_count"),
                    }))
            for child in node.get("children_data") or []:
                walk(child, full)

        walk(tree, [])
        matches.sort(key=lambda m: (-m[0], -m[1]))   # highest score, then most specific
        return [m[2] for m in matches[:limit]]

    # -- attribute-based product finding (precise product type / style / facet) ----
    def _filterable_attributes(self):
        """The merchant-defined facet attributes (select/multiselect) with their options, cached.
        We use is_user_defined rather than is_filterable, because useful facets like Style General
        ('Sweatshirt', 'Hoodie', ...), Activity, Material, Sleeve, Color, Size and the bag styles
        are often NOT flagged filterable in layered navigation yet can still be filtered via the
        API. 'style' attributes are listed first so a type word like 'sweatshirt' matches a style
        before, say, a size option."""
        if self._filter_attr_cache is not None:
            return self._filter_attr_cache
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "is_user_defined",
            "searchCriteria[filterGroups][0][filters][0][value]": 1,
            "searchCriteria[filterGroups][0][filters][0][conditionType]": "eq",
            "searchCriteria[pageSize]": 300,
        }
        try:
            data = self._get(f"{self.rest}/products/attributes", token=self.ensure_admin(), params=params)
        except Exception:  # noqa: BLE001
            self._filter_attr_cache = []
            return self._filter_attr_cache
        attrs = []
        for a in data.get("items", []) or []:
            if a.get("frontend_input") not in ("select", "multiselect"):
                continue
            options = [(o.get("value"), o.get("label"))
                       for o in (a.get("options") or []) if o.get("value")]
            if not options:
                continue
            attrs.append({
                "code": a.get("attribute_code"),
                "label": a.get("default_frontend_label"),
                "multi": a.get("frontend_input") == "multiselect",
                "options": options,
            })
        attrs.sort(key=lambda x: 0 if "style" in (x["code"] or "") else 1)
        self._filter_attr_cache = attrs
        return attrs

    @staticmethod
    def _norm_word(w):
        return w[:-1] if len(w) > 4 and w.endswith("s") else w   # light singular (bags->bag)

    def match_attribute_value(self, query):
        """Match a shopper word to a facet option WITHOUT knowing attribute names in advance:
        we scan EVERY merchant facet attribute and match the word against its option LABELS. So
        'sweatshirt' matches because some attribute has an option 'Sweatshirt' - we never hard-code
        that the attribute is called 'style_general'. Scoring keeps it precise when several
        attributes share a word: a fully-matched label wins, then more overlapping words, and only
        as a last tie-break a 'style'-named attribute. Returns
        {code, value, label, multi, attribute} or None."""
        qwords = {self._norm_word(w)
                  for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2}
        if not qwords:
            return None
        best, best_rank = None, ()
        for attr in self._filterable_attributes():
            style = 1 if "style" in (attr["code"] or "") else 0   # soft tie-break only
            for value, label in attr["options"]:
                lwords = {self._norm_word(x)
                          for x in re.split(r"[^a-z0-9]+", (label or "").lower()) if x}
                common = qwords & lwords
                if not common:
                    continue
                whole_label = 1 if lwords and lwords <= qwords else 0   # the whole option label matched
                rank = (whole_label, len(common), style)
                if rank > best_rank:
                    best_rank = rank
                    best = {"code": attr["code"], "value": value, "label": label,
                            "multi": attr["multi"], "attribute": attr["label"]}
        return best

    def products_by_attribute(self, code, value, multi=False, limit=20):
        """Visible products whose attribute `code` equals option `value`."""
        return self._products_by_filters(
            [(code, value, "finset" if multi else "eq")], limit)

    # -- attribute SETS (Top / Bottom / Bag / Gear) for broad product types ----
    _ATTR_SET_SKIP = {"default", "downloadable"}
    _ATTR_SET_ALIASES = {   # shopper word -> a word in the attribute-set name
        "shirt": "top", "tshirt": "top", "tee": "top",
        "pant": "bottom", "trouser": "bottom", "legging": "bottom", "jean": "bottom",
    }

    def _attribute_sets(self):
        """All attribute sets (id + name), cached. In Luma: Top, Bottom, Bag, Gear, ..."""
        if self._attr_set_cache is not None:
            return self._attr_set_cache
        try:
            data = self._get(f"{self.rest}/products/attribute-sets/sets/list",
                             token=self.ensure_admin(),
                             params={"searchCriteria[pageSize]": 100})
            self._attr_set_cache = data.get("items", []) or []
        except Exception:  # noqa: BLE001
            self._attr_set_cache = []
        return self._attr_set_cache

    def match_attribute_set(self, query):
        """Match a broad type word to an attribute SET, e.g. 'bags'->Bag, 'tops'->Top,
        'pants'->Bottom. Returns {'id', 'name'} or None (skips Default/Downloadable)."""
        words = {self._norm_word(w)
                 for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2}
        words |= {self._ATTR_SET_ALIASES[w] for w in list(words) if w in self._ATTR_SET_ALIASES}
        if not words:
            return None
        for s in self._attribute_sets():
            name = (s.get("attribute_set_name") or "").strip()
            if name.lower() in self._ATTR_SET_SKIP:
                continue
            set_words = {self._norm_word(x) for x in re.split(r"[^a-z0-9]+", name.lower()) if x}
            if words & set_words:
                return {"id": s.get("attribute_set_id"), "name": name}
        return None

    def products_by_attribute_set(self, set_id, limit=20):
        return self._products_by_filters([("attribute_set_id", set_id, "eq")], limit)

    def _products_by_filters(self, filters, limit=20):
        """Visible products matching a list of (field, value, conditionType) AND-filters."""
        params = {}
        gi = 0
        for field, value, cond in filters:
            params[f"searchCriteria[filterGroups][{gi}][filters][0][field]"] = field
            params[f"searchCriteria[filterGroups][{gi}][filters][0][value]"] = value
            params[f"searchCriteria[filterGroups][{gi}][filters][0][conditionType]"] = cond
            gi += 1
        params[f"searchCriteria[filterGroups][{gi}][filters][0][field]"] = "visibility"
        params[f"searchCriteria[filterGroups][{gi}][filters][0][value]"] = self.VISIBLE
        params[f"searchCriteria[filterGroups][{gi}][filters][0][conditionType]"] = "in"
        params["searchCriteria[pageSize]"] = limit
        data = self._get(f"{self.rest}/products", token=self.ensure_admin(), params=params)
        self._last_total = data.get("total_count")
        return [self._clean_product(pr) for pr in data.get("items", [])]

    _GENDER_WORDS = {"women", "woman", "womens", "ladies", "girl", "girls",
                     "men", "man", "mens", "boy", "boys", "kid", "kids"}

    def _raw_products(self, filters, limit=200):
        """Raw product rows (with custom_attributes) matching AND-filters + visible. Used when we
        need to inspect attributes/variants ourselves rather than let Magento filter."""
        params = {}
        gi = 0
        for field, value, cond in filters:
            params[f"searchCriteria[filterGroups][{gi}][filters][0][field]"] = field
            params[f"searchCriteria[filterGroups][{gi}][filters][0][value]"] = value
            params[f"searchCriteria[filterGroups][{gi}][filters][0][conditionType]"] = cond
            gi += 1
        params[f"searchCriteria[filterGroups][{gi}][filters][0][field]"] = "visibility"
        params[f"searchCriteria[filterGroups][{gi}][filters][0][value]"] = self.VISIBLE
        params[f"searchCriteria[filterGroups][{gi}][filters][0][conditionType]"] = "in"
        params["searchCriteria[pageSize]"] = limit
        data = self._get(f"{self.rest}/products", token=self.ensure_admin(), params=params)
        self._last_total = data.get("total_count")
        return data.get("items", []) or []

    def _attr_matches(self, p, code, value):
        """True if product p's own attribute `code` contains `value` (handles multiselect)."""
        v = self._attr(p, code)
        return v is not None and str(value) in [s.strip() for s in str(v).split(",")]

    def _match_variant(self, p, refinements):
        """Return (matches, variant). Refinements the PARENT already satisfies (e.g. a style like
        Jacket, which lives on the parent) are taken off; the REMAINING ones (colour/size, which
        live on the variants) must ALL be satisfied by a SINGLE child - that child is returned so
        we can show its image. This handles a mix of parent-level and variant-level facets, e.g.
        'black jacket' = style Jacket on the parent + colour Black on a variant."""
        if not refinements:
            return True, p
        unmet = [r for r in refinements if not self._attr_matches(p, r["code"], r["value"])]
        if not unmet:
            return True, p
        if p.get("type_id") == "configurable":
            for ch in self._children(p.get("sku")):
                if all(str(self._attr(ch, r["code"])) == str(r["value"]) for r in unmet):
                    return True, ch
        return False, None

    def _match_all_attributes(self, query):
        """Every facet attribute that has a matching option (best option per attribute), so we can
        apply SEVERAL facets at once - e.g. a colour AND a size. Each: {code, value, label, multi,
        words, style}."""
        qwords = {self._norm_word(w)
                  for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2}
        out = []
        for attr in self._filterable_attributes():
            best, best_rank = None, ()
            for value, label in attr["options"]:
                lwords = {self._norm_word(x)
                          for x in re.split(r"[^a-z0-9]+", (label or "").lower()) if x}
                common = qwords & lwords
                if not common:
                    continue
                rank = (1 if lwords and lwords <= qwords else 0, len(common))
                if rank > best_rank:
                    best_rank = rank
                    best = {"code": attr["code"], "value": value, "label": label,
                            "multi": attr["multi"], "words": common,
                            "style": "style" in (attr["code"] or "")}
            if best:
                out.append(best)
        return out

    def find_products(self, query, limit=50) -> dict:
        """Layered-navigation-style finder. Resolves ALL facets in the phrase and combines them
        like the storefront does (e.g. 'women white hoodie' = category Hoodies & Sweatshirts +
        colour White). Colour/size are matched VARIANT-AWARE (they live on configurable variants),
        and the matching variant's image is shown. Returns {method, matched, products, total}."""
        ql = (query or "").lower()
        has_gender = bool(self._GENDER_WORDS & set(re.split(r"[^a-z]+", ql)))
        attrs = self._match_all_attributes(query)      # e.g. [colour=White, style=Hoodie]

        def refine(raw, refinements):
            """Keep raw products matching every refinement (variant-aware); swap in the matching
            variant's image so a colour filter shows that colour's picture."""
            kept = []
            for p in raw:
                ok, variant = self._match_variant(p, refinements)
                if not ok:
                    continue
                clean = self._clean_product(p)
                if variant is not p:                    # matched a specific variant
                    img = self._small_image(variant)
                    if img:
                        clean["image"] = img
                kept.append(clean)
            return kept[:limit], len(kept)

        def label_of(base, refinements):
            return (" ".join(r["label"] for r in refinements) + " " + base).strip() if refinements else base

        # 1) gender present -> base = the gender/type category; refine by facets NOT already in its name
        if has_gender:
            cats = self.find_categories(query)
            cat = cats[0] if cats else None
            if cat:
                cat_words = {self._norm_word(x)
                             for x in re.split(r"[^a-z0-9]+", cat["path"].lower()) if x}
                refinements = [a for a in attrs if not (a["words"] & cat_words)]
                raw = self._raw_products(
                    [("category_id", ",".join(str(i) for i in self._descendant_ids(cat["id"])), "in")], 200)
                prods, total = refine(raw, refinements)
                if prods:
                    return {"method": "filter" if refinements else "category",
                            "matched": label_of(cat["path"], refinements),
                            "products": prods, "total": total}
                base = [self._clean_product(p) for p in raw[:limit]]
                if base:      # refinements too strict -> show the whole category
                    return {"method": "category", "matched": cat["path"],
                            "products": base, "total": len(raw)}

        # 2) no gender -> base = a style/type attribute, refine by colour/size
        type_attr = next((a for a in attrs if a["style"]), None)
        refinements = [a for a in attrs if a is not type_attr]
        if type_attr:
            raw = self._raw_products(
                [(type_attr["code"], type_attr["value"], "finset" if type_attr["multi"] else "eq")], 200)
            prods, total = refine(raw, refinements)
            if prods:
                return {"method": "attribute", "matched": label_of(type_attr["label"], refinements),
                        "products": prods, "total": total}

        # 3) broad type word -> attribute SET (bags / tops / bottoms), refine by colour/size
        aset = self.match_attribute_set(query)
        if aset:
            raw = self._raw_products([("attribute_set_id", aset["id"], "eq")], 200)
            prods, total = refine(raw, attrs)
            if prods:
                return {"method": "attribute_set", "matched": label_of(aset["name"], attrs),
                        "products": prods, "total": total}

        # 4) a single facet on its own (e.g. just a colour) -> direct filter, then keyword search
        if attrs:
            a = attrs[0]
            prods = self.products_by_attribute(a["code"], a["value"], a["multi"], limit)
            if prods:
                return {"method": "attribute", "matched": a["label"], "products": prods}
        cats = self.find_categories(query)
        if cats:
            prods = self.products_in_category(cats[0]["id"], limit)
            if prods:
                return {"method": "category", "matched": cats[0]["path"], "products": prods}
        return {"method": "search", "matched": query,
                "products": self.search_products(query, limit)}

    def _clean_category(self, c: dict) -> dict:
        subs = [self._clean_category(s) for s in (c.get("children_data") or []) if s.get("is_active")]
        return compact({
            "id": c.get("id"),                      # handle for products_in_category; not shown to user
            "name": c.get("name"),
            "product_count": c.get("product_count"),
            "subcategories": subs or None,
        })

    # -- configurable / bundle / grouped helpers --
    def _children(self, sku):
        """Configurable children for a sku, fetched once and cached. Filtering by colour/size
        touches the same product's children several times (match, price, image); without caching
        we would refetch each time and some rapid calls would time out. A failed/empty fetch is
        NOT cached (and is retried once), so one transient error can't permanently drop a product."""
        if self._children_cache.get(sku):          # only trust a cached NON-empty result
            return self._children_cache[sku]
        children = []
        for _ in range(2):                          # one retry on a transient failure
            try:
                children = self._get(f"{self.rest}/configurable-products/{sku}/children",
                                     token=self.ensure_admin())
            except Exception:  # noqa: BLE001
                children = []
            if children:
                break
        if children:
            self._children_cache[sku] = children
        return children or []

    def _configurable_summary(self, sku):
        """(lowest_price, variant_count) for a configurable, from its children. Light-weight
        (no label building) so it is cheap enough to use in listings."""
        children = self._children(sku)
        prices = [money(ch.get("price")) for ch in (children or [])
                  if money(ch.get("price")) is not None]
        return (min(prices) if prices else None, len(children or []))

    def _bundle_options(self, p: dict) -> list[dict]:
        """Bundle option groups and their selectable products. Each option carries its
        option_id and each selection its selection_id - both needed by add_bundle_to_cart."""
        opts = (p.get("extension_attributes") or {}).get("bundle_product_options") or []
        result = []
        for opt in opts:
            selections = []
            for sel in opt.get("product_links") or []:
                selections.append(compact({
                    "selection_id": sel.get("id"),        # -> add_bundle_to_cart
                    "sku": sel.get("sku"),
                    "qty": money(sel.get("qty")) or sel.get("qty"),
                    "price": self.price_str(sel.get("price")) if money(sel.get("price")) else None,
                }))
            result.append(compact({
                "option_id": opt.get("option_id"),        # -> add_bundle_to_cart
                "title": opt.get("title"),
                "required": opt.get("required"),
                "type": opt.get("type"),                  # select / radio / checkbox / multi
                "selections": selections,
            }))
        return result

    def _grouped_items(self, p: dict) -> list[dict]:
        """The simple products that make up a grouped product. The customer adds each one
        (by its sku) with add_to_cart; default_qty is the grouped default."""
        items = []
        for link in p.get("product_links") or []:
            if link.get("link_type") != "associated":
                continue
            sku = link.get("linked_product_sku")
            prod = None
            try:
                prod = self._get(f"{self.rest}/products/{sku}", token=self.ensure_admin())
            except Exception:  # noqa: BLE001
                pass
            ext = link.get("extension_attributes") or {}
            items.append(compact({
                "sku": sku,
                "name": prod.get("name") if isinstance(prod, dict) else None,
                "price": self.price_str(prod.get("price")) if isinstance(prod, dict) else None,
                "default_qty": money(ext.get("qty")) or ext.get("qty"),
            }))
        return items

    # -- configurable variants (so a customer can pick size/colour, then add by sku) --
    def _variants(self, parent: dict) -> list[dict]:
        """Child products of a configurable, each labelled like 'Color: Blue, Size: M'."""
        sku = parent.get("sku")
        children = self._children(sku)
        out = []
        for ch in children or []:
            stock = (ch.get("extension_attributes") or {}).get("stock_item") or {}
            in_stock = stock.get("is_in_stock")
            if in_stock is None:                            # /children omits stock_item
                in_stock = self._is_salable(ch.get("sku"))  # ask the inventory endpoint
            out.append(compact({
                "sku": ch.get("sku"),                       # add this exact sku to cart
                "label": self._variant_label(parent, ch),
                "price": self.price_str(ch.get("price")),
                "in_stock": bool(in_stock) if in_stock is not None else None,
            }))
        return out

    def _is_salable(self, sku):
        """is_in_stock for one sku via the inventory endpoint (used when the /children
        response omits stock_item). Returns True/False, or None on failure."""
        try:
            item = self._get(f"{self.rest}/stockItems/{sku}", token=self.ensure_admin())
        except Exception:  # noqa: BLE001
            return None
        return bool(item.get("is_in_stock")) if isinstance(item, dict) else None

    def _variant_label(self, parent: dict, child: dict) -> str:
        parts = []
        options = (parent.get("extension_attributes") or {}).get("configurable_product_options") or []
        for opt in options:
            code = self._attr_code(opt.get("attribute_id"))
            if not code:
                continue
            value = self._attr(child, code)
            if value is None:
                continue
            label = self._attr_options(code).get(str(value), value)
            parts.append(f"{opt.get('label') or code}: {label}")
        return ", ".join(parts)

    def _attr_code(self, attribute_id):
        """attribute_id -> attribute_code (cached)."""
        if attribute_id not in self._attr_code_cache:
            code = None
            try:
                params = {
                    "searchCriteria[filterGroups][0][filters][0][field]": "attribute_id",
                    "searchCriteria[filterGroups][0][filters][0][value]": attribute_id,
                    "searchCriteria[pageSize]": 1,
                }
                items = self._get(f"{self.rest}/products/attributes",
                                  token=self.ensure_admin(), params=params).get("items") or []
                code = items[0].get("attribute_code") if items else None
            except Exception:  # noqa: BLE001
                pass
            self._attr_code_cache[attribute_id] = code
        return self._attr_code_cache[attribute_id]

    def _attr_options(self, code) -> dict:
        """attribute option value_index -> text label, e.g. {'50': 'Blue'} (cached)."""
        if code not in self._attr_opt_cache:
            mapping = {}
            try:
                for o in self._get(f"{self.rest}/products/attributes/{code}/options",
                                   token=self.ensure_admin()):
                    if o.get("value") not in (None, ""):
                        mapping[str(o["value"])] = o.get("label")
            except Exception:  # noqa: BLE001
                pass
            self._attr_opt_cache[code] = mapping
        return self._attr_opt_cache[code]

    # -- cart -----------------------------------------------------------------
    def _cart_base(self) -> str:
        if self.is_guest:
            if not self.cart_id:
                self.create_cart()
            return f"{self.rest}/guest-carts/{self.cart_id}"
        return f"{self.rest}/carts/mine"

    def _cart_token(self):
        return None if self.is_guest else self.customer_token

    def create_cart(self) -> str:
        if self.is_guest:
            self.cart_id = self._post(f"{self.rest}/guest-carts")
        else:
            self._ensure_customer_cart()
        return self.cart_id

    def _ensure_customer_cart(self):
        self.cart_id = self._post(f"{self.rest}/carts/mine", token=self.customer_token)
        return self.cart_id

    def _guest_cart_items(self):
        """(sku, qty) pairs currently in the guest cart - used to migrate on login."""
        try:
            items = self._get(f"{self.rest}/guest-carts/{self.cart_id}/items")
            return [(i.get("sku"), i.get("qty")) for i in items if i.get("sku")]
        except Exception:  # noqa: BLE001
            return []

    def _merge_items(self, items) -> int:
        moved = 0
        for sku, qty in items:
            try:
                self.add_to_cart(sku, qty)
                moved += 1
            except Exception:  # noqa: BLE001
                pass
        return moved

    def add_to_cart(self, sku, qty=1):
        if not self.cart_id:
            self.create_cart()
        payload = {"cartItem": {"sku": sku, "qty": qty, "quote_id": self.cart_id}}
        self._post(f"{self._cart_base()}/items", payload, token=self._cart_token())
        return self.get_cart()

    def add_bundle_to_cart(self, sku, selections, qty=1):
        """Add a bundle product. `selections` is a list of dicts, each:
        {"option_id": <id>, "selection_id": <id>, "qty": <n>}  (from get_product's
        bundle_options). One entry per option the customer chose."""
        if not self.cart_id:
            self.create_cart()
        bundle_options = []
        for sel in selections or []:
            bundle_options.append({
                "option_id": int(sel["option_id"]),
                "option_qty": int(sel.get("qty") or 1),
                "option_selections": [int(sel["selection_id"])],
            })
        payload = {"cartItem": {
            "sku": sku, "qty": qty, "quote_id": self.cart_id,
            "product_option": {"extension_attributes": {"bundle_options": bundle_options}},
        }}
        self._post(f"{self._cart_base()}/items", payload, token=self._cart_token())
        return self.get_cart()

    def update_cart_item(self, item_id, qty):
        payload = {"cartItem": {"item_id": item_id, "qty": qty, "quote_id": self.cart_id}}
        self._put(f"{self._cart_base()}/items/{item_id}", payload, token=self._cart_token())
        return self.get_cart()

    def remove_cart_item(self, item_id):
        self._delete(f"{self._cart_base()}/items/{item_id}", token=self._cart_token())
        return self.get_cart()

    def get_cart(self) -> dict:
        """The cart as the shopper sees it: line items + totals, all cleaned."""
        token = self._cart_token()
        cart = self._get(self._cart_base(), token=token)
        totals = self._get(f"{self._cart_base()}/totals", token=token)
        return self._clean_cart(cart, totals)

    # -- coupon ---------------------------------------------------------------
    def apply_coupon(self, code):
        self._put(f"{self._cart_base()}/coupons/{code}", token=self._cart_token())
        return self.get_cart()

    def remove_coupon(self):
        self._delete(f"{self._cart_base()}/coupons", token=self._cart_token())
        return self.get_cart()

    # -- offers ---------------------------------------------------------------
    @staticmethod
    def _rule_min_subtotal(rule):
        """Best-effort: pull a 'base_subtotal >= X' threshold out of a rule's conditions
        so we can tell the customer how much more to spend. Returns a float or None."""
        def scan(node):
            if not isinstance(node, dict):
                return None
            if node.get("attribute") in ("base_subtotal", "base_subtotal_with_discount") \
                    and node.get("operator") in (">=", ">"):
                try:
                    return float(node.get("value"))
                except (TypeError, ValueError):
                    return None
            for child in node.get("conditions", []) or []:
                got = scan(child)
                if got is not None:
                    return got
            return None
        return scan(rule.get("condition") or {})

    def _coupon_codes_for_rule(self, rule_id):
        """The actual coupon code(s) attached to a SPECIFIC_COUPON rule."""
        if not rule_id:
            return []
        try:
            params = {
                "searchCriteria[filterGroups][0][filters][0][field]": "rule_id",
                "searchCriteria[filterGroups][0][filters][0][value]": rule_id,
                "searchCriteria[pageSize]": 20,
            }
            data = self._get(f"{self.rest}/coupons/search", token=self.admin_token, params=params)
            return [c.get("code") for c in data.get("items", []) if c.get("code")]
        except Exception:  # noqa: BLE001
            return []

    def get_active_offers(self):
        """List active store offers (cart price rules) so the assistant can tell the customer
        what discounts exist and how to unlock them. Each item: name, description, action,
        discount, coupon_codes, min_subtotal, auto_apply (no coupon needed)."""
        self.ensure_admin()
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "is_active",
            "searchCriteria[filterGroups][0][filters][0][value]": 1,
            "searchCriteria[pageSize]": 50,
        }
        data = self._get(f"{self.rest}/salesRules/search", token=self.admin_token, params=params)
        offers = []
        for rule in data.get("items", []) or []:
            ctype = rule.get("coupon_type")          # NO_COUPON / SPECIFIC_COUPON / AUTO
            codes = self._coupon_codes_for_rule(rule.get("rule_id")) \
                if ctype and ctype != "NO_COUPON" else []
            offers.append(compact({
                "name": rule.get("name"),
                "description": rule.get("description") or "",
                "action": rule.get("simple_action"),  # by_percent / by_fixed / cart_fixed / ...
                "discount": rule.get("discount_amount"),
                "coupon_codes": codes,
                "auto_apply": ctype == "NO_COUPON",
                "min_subtotal": self._rule_min_subtotal(rule),
            }))
        return offers

    # -- checkout -------------------------------------------------------------
    def _countries(self) -> dict:
        if self._country_cache is None:
            idx: dict[str, str] = {}
            try:
                for c in self._get(f"{self.rest}/directory/countries"):
                    cid = c.get("id")
                    if not cid:
                        continue
                    idx[cid.lower()] = cid
                    for key in ("two_letter_abbreviation", "three_letter_abbreviation",
                                "full_name_english", "full_name_locale"):
                        if c.get(key):
                            idx[str(c[key]).lower()] = cid
            except Exception:  # noqa: BLE001
                pass
            self._country_cache = idx
        return self._country_cache

    def resolve_country(self, country):
        if not country:
            return None
        raw = str(country).strip().lower()
        return self._countries().get(raw) or (raw.upper() if len(raw) == 2 and raw.isalpha() else None)

    def resolve_region(self, country_id, region) -> dict:
        """Resolve a state/region name or code to Magento's region_id."""
        if not region:
            return {}
        try:
            data = self._get(f"{self.rest}/directory/countries/{country_id}")
        except MagentoError:
            return {"region": region}
        needle = str(region).strip().lower()
        for r in data.get("available_regions") or []:
            if needle in (str(r.get("code", "")).lower(), str(r.get("name", "")).lower()):
                return {"region_id": int(r["id"]), "region": r["name"], "region_code": r["code"]}
        return {"region": region}

    def region_options(self, country_id) -> list:
        """The valid {name, code} regions for a country. Empty list = no predefined regions
        (free-text allowed). Used to validate a customer's state/region. Never raises."""
        try:
            data = self._get(f"{self.rest}/directory/countries/{country_id}")
        except Exception:  # noqa: BLE001 - never block checkout on a directory lookup
            return []
        if not isinstance(data, dict):
            return []
        return [{"name": r.get("name"), "code": r.get("code")}
                for r in (data.get("available_regions") or [])]

    def build_address(self, firstname, lastname, street, city, postcode, telephone,
                      country_id="US", region="", email=None) -> dict:
        addr = {
            "firstname": firstname, "lastname": lastname,
            "street": [street] if isinstance(street, str) else list(street),
            "city": city, "postcode": postcode, "telephone": telephone,
            "country_id": country_id, "save_in_address_book": 0,
        }
        addr.update(self.resolve_region(country_id, region))
        if self.is_guest and (email or self.customer_email):
            addr["email"] = email or self.customer_email
        return addr

    def estimate_shipping(self, address) -> list[dict]:
        methods = self._post(f"{self._cart_base()}/estimate-shipping-methods",
                             {"address": address}, token=self._cart_token())
        return [compact({
            "carrier": m.get("carrier_code"),
            "method": m.get("method_code"),
            "label": f"{m.get('carrier_title')} - {m.get('method_title')}",
            "price": money(m.get("amount")),
        }) for m in (methods or []) if m.get("available", True)]

    def set_address(self, address, carrier="flatrate", method="flatrate") -> dict:
        """Save shipping+billing (same address) and pick a shipping method."""
        payload = {"addressInformation": {
            "shipping_address": address,
            "billing_address": dict(address),
            "shipping_carrier_code": carrier,
            "shipping_method_code": method,
        }}
        self._post(f"{self._cart_base()}/shipping-information", payload, token=self._cart_token())
        return {"message": "Address saved."}

    def get_payment_methods(self) -> list[dict]:
        methods = self._get(f"{self._cart_base()}/payment-methods", token=self._cart_token())
        return [{"code": m.get("code"), "title": m.get("title")} for m in (methods or [])]

    def place_order(self, payment_method="checkmo", email=None) -> dict:
        payload = {"paymentMethod": {"method": payment_method}}
        if self.is_guest:
            payload["email"] = email or self.customer_email
        entity_id = self._put(f"{self._cart_base()}/order", payload, token=self._cart_token())
        self.cart_id = None
        self.order_id = entity_id
        return {"order_number": self._order_number(entity_id)}

    def _order_number(self, entity_id) -> str:
        """Magento returns the internal entity id; fetch the friendly #000000006."""
        try:
            o = self._get(f"{self.rest}/orders/{entity_id}", token=self.ensure_admin())
            return "#" + str(o.get("increment_id") or entity_id)
        except Exception:  # noqa: BLE001
            return "#" + str(entity_id)

    # -- orders ---------------------------------------------------------------
    def get_order(self, order_id) -> dict:
        """Look up an order by friendly number (#000000006) or internal id - but ONLY if it
        belongs to this session (the logged-in customer, or the guest's just-placed order).
        Stops a customer reading someone else's order by guessing the number."""
        self.ensure_admin()
        key = str(order_id).strip().lstrip("#")
        if key.isdigit() and len(key) < 9:
            o = self._get(f"{self.rest}/orders/{key}", token=self.admin_token)
        else:
            o = self._order_by_increment(key)
        if self.is_guest:
            if str(o.get("entity_id")) != str(self.order_id):
                raise MagentoError(403, "This order does not belong to you.", "")
        elif (o.get("customer_email") or "").lower() != (self.customer_email or "").lower():
            raise MagentoError(403, "This order does not belong to you.", "")
        return self._clean_order(o)

    def _order_by_increment(self, increment_id) -> dict:
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "increment_id",
            "searchCriteria[filterGroups][0][filters][0][value]": increment_id,
            "searchCriteria[pageSize]": 1,
        }
        items = self._get(f"{self.rest}/orders", token=self.admin_token, params=params).get("items") or []
        if not items:
            raise MagentoError(404, f"Order {increment_id} not found.", "")
        return items[0]

    def my_orders(self, email=None) -> list[dict]:
        self.ensure_admin()
        email = email or self.customer_email
        if not email:
            raise MagentoError(0, "Please log in first.", "")
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "customer_email",
            "searchCriteria[filterGroups][0][filters][0][value]": email,
            "searchCriteria[sortOrders][0][field]": "created_at",
            "searchCriteria[sortOrders][0][direction]": "DESC",
            "searchCriteria[pageSize]": 20,
        }
        data = self._get(f"{self.rest}/orders", token=self.admin_token, params=params)
        return [self._clean_order(o) for o in data.get("items", [])]

    # -- session (debug only) --------------------------------------------------
    def session(self) -> dict:
        return {
            "mode": "guest" if self.is_guest else "logged-in",
            "customer_email": self.customer_email,
            "cart_id": self.cart_id,
            "last_order_id": self.order_id,
            "store": self._config,            # None until first config load
        }