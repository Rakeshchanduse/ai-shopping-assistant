"""
server.py
=========
MCP server exposing a small, clean set of shopping tools backed by MagentoClient.

Every tool returns either:
    {"status": "ok", "data": <clean user-facing data>}
    {"status": "ok", "message": "..."}          (for actions)
    {"status": "error", "message": "..."}        (so the model can explain it)

The data is already cleaned in magento_client.py - no ids, no tax_class_id, no
attribute codes - so nothing internal can reach the user.

Transport: stdio by default (works with the Streamlit app and Claude Desktop).
For an HTTP deployment:  MCP_TRANSPORT=streamable-http python server.py
"""

import difflib
import logging
import os

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Logging -> error-trace.log. Set MAGENTO_DEBUG=1 for verbose request/response tracing.
logging.basicConfig(
    filename="error-trace.log",
    level=logging.DEBUG if os.getenv("MAGENTO_DEBUG") == "1" else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("server")

try:
    from magento_client import MagentoClient, MagentoError
except ImportError:                       # when imported as a package (ADK / pytest)
    from .magento_client import MagentoClient, MagentoError

mcp = FastMCP("ecommerce-shopping-tools")
client = MagentoClient(base_url=os.getenv("MAGENTO_BASE_URL"))


# -- helpers ------------------------------------------------------------------
def _safe(fn, *args, **kwargs):
    """Run a client call; return its data, or a clean error the model can explain."""
    try:
        return {"status": "ok", "data": fn(*args, **kwargs)}
    except MagentoError as e:
        log.warning("MagentoError: %s", e)
        return {"status": "error", "message": str(e.body)}
    except Exception as e:                 # noqa: BLE001
        log.exception("Tool failed")
        return {"status": "error", "message": str(e)}


def _cart_after(fn, *args):
    """Run a cart mutation, then return the fresh cart (client mutations already do this,
    but this keeps the error handling in one place)."""
    return _safe(fn, *args)


def _check_region(country, country_id, state_or_region):
    """If the country has predefined regions (e.g. US, India), make sure the state/region the
    customer typed matches one - and if not, SUGGEST the closest match ('Did you mean ...?') so
    the spelling can be fixed immediately. Returns an error dict, or None when it's fine.
    Never raises: if the region list can't be fetched, it just skips (returns None)."""
    try:
        regions = client.region_options(country_id)
    except Exception:  # noqa: BLE001 - a directory lookup must never crash checkout
        return None
    if not regions:
        return None
    names = [r.get("name") for r in regions if r.get("name")]
    needle = state_or_region.strip().lower()
    if any(needle == (r.get("name") or "").lower() or needle == (r.get("code") or "").lower()
           for r in regions):
        return None
    close = difflib.get_close_matches(state_or_region.strip(), names, n=1, cutoff=0.6)
    if close:
        return {"status": "error",
                "message": f"'{state_or_region}' isn't a valid state/region for {country}. "
                           f"Did you mean '{close[0]}'? Please use the exact name."}
    sample = ", ".join(names[:8])
    return {"status": "error",
            "message": f"'{state_or_region}' isn't a valid state/region for {country}. "
                       f"Please use one of these, for example: {sample}."}


def _validate_address(firstname, lastname, street, city, state_or_region,
                      postcode, telephone, country, email="", need_email=False):
    """Validate address fields the SAME way for both the address book and checkout.
    Returns (error_dict, country_id); error_dict is None when valid. Each error says WHAT is
    wrong and gives a suggestion, so the assistant can ask the customer to fix exactly that."""
    fields = {
        "name": (firstname.strip() + " " + lastname.strip()).strip(),
        "street": street, "city": city, "state/region": state_or_region,
        "postcode": postcode, "country": country, "phone": telephone,
    }
    if need_email:
        fields["email"] = email
    missing = [k for k, v in fields.items() if not str(v).strip()]
    if missing:
        return ({"status": "error",
                 "message": "These fields are still empty: " + ", ".join(missing)
                            + ". Please fill them in and resend."}, None)

    country_id = client.resolve_country(country)
    if not country_id:
        return ({"status": "error",
                 "message": f"I couldn't recognise the country '{country}'. Please write the full "
                            f"country name, e.g. 'United States' or 'India'."}, None)

    region_err = _check_region(country, country_id, state_or_region)
    if region_err:
        return (region_err, None)

    digits = "".join(ch for ch in telephone if ch.isdigit())
    if len(digits) < 7:
        return ({"status": "error",
                 "message": f"'{telephone}' doesn't look like a valid phone number - please enter at "
                            f"least 7 digits, e.g. 5551234567."}, None)

    if need_email:
        e = email.strip()
        if "@" not in e or "." not in e.split("@")[-1]:
            return ({"status": "error",
                     "message": f"'{email}' doesn't look like a valid email - please enter a real "
                                f"email address, e.g. name@example.com."}, None)

    return (None, country_id)


# -- account ------------------------------------------------------------------
@mcp.tool()
def register_customer(email: str, firstname: str, lastname: str, password: str) -> dict:
    """Create a customer account, then log in."""
    res = _safe(client.register_customer, email, firstname, lastname, password)
    if res["status"] != "ok":
        return res
    login_res = _safe(client.login, email, password)
    if login_res["status"] == "ok":
        return {"status": "ok", "message": f"Account created. Logged in as {email}."}
    return login_res


@mcp.tool()
def login(email: str, password: str) -> dict:
    """Log the customer in. Any items added as a guest are moved into their account cart."""
    res = _safe(client.login, email, password)
    if res["status"] == "ok":
        moved = res["data"].get("merged_items", 0)
        msg = f"Logged in as {email}."
        if moved:
            msg += f" Your guest cart ({moved} item(s)) was moved to your account."
        return {"status": "ok", "message": msg}
    return res


@mcp.tool()
def use_guest_checkout() -> dict:
    """Shop and check out without an account."""
    return _safe(client.start_guest)


@mcp.tool()
def get_my_profile() -> dict:
    """The logged-in customer's name, email and saved addresses."""
    return _safe(client.get_my_profile)


@mcp.tool()
def update_my_profile(firstname: str = "", lastname: str = "") -> dict:
    """Update the customer's first and/or last name (login required)."""
    return _safe(client.update_my_profile, firstname or None, lastname or None)


@mcp.tool()
def add_customer_address(
    firstname: str,
    lastname: str,
    street: str,
    city: str,
    state_or_region: str,
    postcode: str,
    telephone: str,
    country: str,
) -> dict:
    """Save a NEW address to the LOGGED-IN customer's address book. First SHOW the customer a
    FILLED key:value example so they see the format, then ask them to send THEIR OWN details the
    same way. Every value (including the name and country) must come from the customer - set NO
    defaults; if anything is missing, this returns which fields are empty so you can ask for them.
    On any invalid field it returns WHAT is wrong + a suggestion - relay that and show the example
    again with the values they already got right. Login required; never invent an address."""
    if client.is_guest:
        return {"status": "error", "message": "Please log in to save an address to your account."}
    err, country_id = _validate_address(firstname, lastname, street, city,
                                        state_or_region, postcode, telephone, country)
    if err:
        return err
    addr = client.build_address(
        firstname=firstname, lastname=lastname, street=street, city=city,
        postcode=postcode, telephone=telephone, country_id=country_id,
        region=state_or_region,
    )
    return _safe(client.add_address, addr)


# -- catalog ------------------------------------------------------------------
@mcp.tool()
def list_categories() -> dict:
    """List the store's categories so the customer can pick one to browse."""
    return _safe(client.list_categories)


@mcp.tool()
def find_products(query: str) -> dict:
    """PRIMARY way to handle 'show me X' / 'I want to buy X'. Finds the best products for a
    shopper phrase by trying, in order: an exact product-type/style attribute match (e.g.
    'sweatshirt' -> style_general=Sweatshirt, 'backpack' -> style_bags=Backpack), then a category
    match (e.g. 'women bags' -> Gear > Bags), then a keyword search. Returns {method, matched,
    products}: show the products and use `matched` as the heading (e.g. 'Sweatshirts', the category
    path, or the search term). Prefer this over search_products / find_categories for browse and
    'show me' requests."""
    return _safe(client.find_products, query)


@mcp.tool()
def find_categories(query: str) -> dict:
    """Find the store categories that best match a shopper phrase like 'women sweatshirt',
    'men shoes' or 'yoga pants'. Returns categories with their full path (e.g.
    'Women > Tops > Hoodies & Sweatshirts'), best match first, honouring BOTH the gender and the
    product type. Use this for browse-by-type requests, then call products_in_category on the
    chosen category id - this is more reliable than search_products, whose results depend on the
    words in each product's name."""
    return _safe(client.find_categories, query)


@mcp.tool()
def list_products() -> dict:
    """Show browsable products (simple or configurable) with name, price, image."""
    return _safe(client.list_products)


@mcp.tool()
def products_in_category(category_id: int) -> dict:
    """Show the products inside a category (category_id comes from list_categories)."""
    return _safe(client.products_in_category, category_id)


@mcp.tool()
def search_products(query: str) -> dict:
    """Search products by name (name, price, image, short description)."""
    return _safe(client.search_products, query)


@mcp.tool()
def get_product(sku: str) -> dict:
    """Full detail for one product by SKU. Depending on `type`:
    - configurable -> `variants` (each a label like 'Color: Blue, Size: M' + its own sku +
      price + in_stock); let the customer pick one and add THAT variant's sku with add_to_cart.
    - bundle -> `bundle_options` (each option has a title, required flag, and selections that
      carry option_id + selection_id); let the customer choose, then use add_bundle_to_cart.
    - grouped -> `grouped_items` (simple products with sku, name, price); the customer adds
      each one with add_to_cart.
    - simple -> just add its sku with add_to_cart."""
    return _safe(client.get_product, sku)


# -- cart ---------------------------------------------------------------------
@mcp.tool()
def add_to_cart(sku: str, quantity: int = 1) -> dict:
    """Add a product to the cart by sku, then return the updated cart. Use this for a simple
    product, a chosen configurable VARIANT's sku, or an individual grouped item's sku. For a
    bundle product use add_bundle_to_cart instead."""
    return _cart_after(client.add_to_cart, sku, quantity)


@mcp.tool()
def add_bundle_to_cart(sku: str, selections: list[dict], quantity: int = 1) -> dict:
    """Add a BUNDLE product to the cart. First call get_product to read its bundle_options,
    let the customer choose one selection per required option, then pass `selections` as a list
    of {"option_id": <id>, "selection_id": <id>, "qty": <n>} using the ids from bundle_options.
    Returns the updated cart."""
    return _cart_after(client.add_bundle_to_cart, sku, selections, quantity)


@mcp.tool()
def update_cart_item(item_id: int, quantity: int) -> dict:
    """Change a cart line's quantity (item_id comes from the cart), then return the cart."""
    return _cart_after(client.update_cart_item, item_id, quantity)


@mcp.tool()
def remove_cart_item(item_id: int) -> dict:
    """Remove a cart line by item_id (from the cart), then return the cart."""
    return _cart_after(client.remove_cart_item, item_id)


@mcp.tool()
def view_cart() -> dict:
    """The cart: line items (name, price, qty, total, image) plus totals."""
    return _safe(client.get_cart)


# -- coupon -------------------------------------------------------------------
@mcp.tool()
def apply_coupon(coupon_code: str) -> dict:
    """Apply a coupon code, then return the updated cart."""
    return _cart_after(client.apply_coupon, coupon_code)


@mcp.tool()
def remove_coupon() -> dict:
    """Remove the applied coupon, then return the updated cart."""
    return _cart_after(client.remove_coupon)


# -- offers -------------------------------------------------------------------
@mcp.tool()
def list_offers() -> dict:
    """List active store offers/coupons and how to unlock them (minimum spend, percent or
    fixed discount, and the coupon code if one is needed). Use this to proactively tell the
    customer what discounts exist and what they could do to save more BEFORE placing an order
    (e.g. 'spend $X more to get 10% off')."""
    return _safe(client.get_active_offers)


# -- checkout -----------------------------------------------------------------
@mcp.tool()
def estimate_shipping(country: str, state_or_region: str, postcode: str) -> dict:
    """List available shipping methods for a country (by NAME), region and postcode."""
    country_id = client.resolve_country(country)
    if not country_id:
        return {"status": "error", "message": f"Unrecognised country '{country}'. Ask for a valid country name."}
    region_err = _check_region(country, country_id, state_or_region)
    if region_err:                     # catch a bad state/region NOW, not at order time
        return region_err
    addr = {"country_id": country_id, "postcode": postcode}
    addr.update(client.resolve_region(country_id, state_or_region))
    return _safe(client.estimate_shipping, addr)


@mcp.tool()
def set_address(
    firstname: str,
    lastname: str,
    street: str,
    city: str,
    state_or_region: str,
    postcode: str,
    country: str,
    telephone: str,
    email: str = "",
    shipping_carrier_code: str = "",
    shipping_method_code: str = "",
) -> dict:
    """Save the shipping + billing address (country by NAME) and the shipping method the
    customer CHOSE. First call estimate_shipping to get the available methods and show them to
    the customer (if more than one, ask them to pick); then pass that method's
    shipping_carrier_code and shipping_method_code here. Do NOT guess or default the method.
    Collect every address field from the customer first - never invent an address; guests must
    also give an email."""
    err, country_id = _validate_address(firstname, lastname, street, city,
                                        state_or_region, postcode, telephone, country,
                                        email=email, need_email=client.is_guest)
    if err:
        return err
    if not shipping_carrier_code.strip() or not shipping_method_code.strip():
        return {"status": "error",
                "message": "Please call estimate_shipping first, show the customer the available "
                           "shipping methods, and pass the chosen shipping_carrier_code and "
                           "shipping_method_code - do not default them."}
    if client.is_guest and email:
        client.customer_email = email

    addr = client.build_address(
        firstname=firstname, lastname=lastname, street=street, city=city,
        postcode=postcode, telephone=telephone, country_id=country_id,
        region=state_or_region, email=email or None,
    )
    return _safe(client.set_address, addr, shipping_carrier_code, shipping_method_code)


@mcp.tool()
def get_payment_methods() -> dict:
    """Available payment methods; let the customer choose one."""
    return _safe(client.get_payment_methods)


@mcp.tool()
def place_order(payment_method: str = "checkmo") -> dict:
    """Place the order after the address is set. Tells the customer their order number."""
    res = _safe(client.place_order, payment_method)
    if res["status"] == "ok":
        number = res["data"]["order_number"]
        return {"status": "ok", "message": f"Order placed successfully! Your order number is {number}."}
    return res


# -- orders -------------------------------------------------------------------
@mcp.tool()
def get_order(order_number: str) -> dict:
    """One order by its number (e.g. #000000006): status, items and total."""
    return _safe(client.get_order, order_number)


@mcp.tool()
def my_orders() -> dict:
    """The logged-in customer's past orders (number, status, items, total)."""
    return _safe(client.my_orders)


@mcp.tool()
def session_status() -> dict:
    """Debug: current mode (guest/logged-in), cart id, last order id and store config."""
    return {"status": "ok", "data": client.session()}


@mcp.tool()
def refresh_settings() -> dict:
    """Reload the store configuration cache (currency, media URL, locale, ...).
    Use this after changing settings in the Magento admin so the app picks them up."""
    res = _safe(client.refresh_config)
    if res["status"] == "ok":
        cfg = res["data"] or {}
        res["message"] = (f"Settings refreshed. Currency: {cfg.get('currency')}, "
                          f"locale: {cfg.get('locale')}.")
    return res


if __name__ == "__main__":
    mcp.run(transport=os.getenv("MCP_TRANSPORT", "stdio"))