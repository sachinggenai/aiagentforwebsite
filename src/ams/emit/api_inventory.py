"""API and endpoint inventory.

Documents every form action, AJAX target and popup handler referenced by the
site, so a later chat/voice layer knows what exists and what it is permitted to
touch when submitting an enquiry on a user's behalf.

**Boundary:** this module performs *static analysis only*. No endpoint is ever
called — several sit under ``robots.txt`` Disallow paths, and probing the
inquiry machinery of a live business site is not something a data-collection
run should do. Each entry records its robots status so the chat layer can make
an explicit, reviewable decision rather than an accidental one.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..models import PageDocument
from . import write_json

# Inline-JS shapes that reference an endpoint.
AJAX_PATTERNS = (
    re.compile(r"\$\.ajax\s*\(\s*\{[^}]{0,400}?url\s*:\s*[\"']([^\"']+)", re.I | re.S),
    re.compile(r"\$\.(?:get|post)\s*\(\s*[\"']([^\"']+)", re.I),
    re.compile(r"\bfetch\s*\(\s*[\"']([^\"']+)", re.I),
    re.compile(r"\.load\s*\(\s*[\"']([^\"']+)", re.I),
    re.compile(r"url\s*:\s*[\"'](/[^\"']*?\.html[^\"']*)", re.I),
)
FORM_ACTION_RE = re.compile(r"<form[^>]*\baction\s*=\s*[\"']([^\"']+)[\"']", re.I)
POPUP_RE = re.compile(r"open_popup\s*\(\s*[\"']([^\"']+)", re.I)
HREF_JS_RE = re.compile(r"href\s*=\s*[\"']javascript:([^\"']+)", re.I)

PURPOSES = {
    "get_logindetails": "Session / login detail probe used to prefill the inquiry form.",
    "check_email_mobile_exist": "Lead validation — checks whether an email or mobile is already registered.",
    "city_branches_json": "Branch lookup by city.",
    "get_pincode_city_ajax": "Pincode to city resolution for the inquiry form.",
    "locator_suggest_json": "Location autocomplete suggestions.",
    "show_callme_offline_pupup": "'Call me' popup — offline callback request flow.",
    "user_browsing_country_code": "Geo / country detection for the visitor.",
    "send_otp": "Sends a one-time password to verify an inquiry submitter.",
    "request_a_callback_action": "Submits a 'request a callback' lead.",
    "unregistered_user_callback_action": "Callback submission for a visitor with no account.",
    "prod_search": "Product search form submission.",
    "sms_now": "SMS-to-seller flow opened in a popup.",
    "send_inquiry": "Product inquiry submission.",
    "inquiry": "General inquiry submission.",
    "contact_us": "Contact form submission.",
    "take_mobile": "Mobile capture prompt.",
    "visitor_form": "Chat visitor form.",
}


def load_robots(config: Config) -> tuple[list[str], str]:
    """Read robots.txt from the raw cache, if it was fetched."""
    raw_dir = config.path("raw")
    for candidate in raw_dir.glob("robots*"):
        if candidate.suffix == ".json":
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        disallow = [
            m.group(1).strip()
            for m in re.finditer(r"^\s*Disallow:\s*(\S+)", text, re.I | re.M)
        ]
        return disallow, text
    return [], ""


def _is_disallowed(path: str, disallow: list[str]) -> bool:
    """Match a path against the Disallow rules.

    Query-specific rules must be handled carefully: this site has
    ``Disallow: /?fd=1``, whose path component is just ``"/"``. Naively
    prefix-matching on that would flag *every* endpoint on the site as
    disallowed, which is both wrong and useless.
    """
    for rule in disallow:
        if not rule:
            continue
        if "?" in rule:
            # Query-scoped rule — only an exact/prefix match on the full rule.
            if path.startswith(rule):
                return True
            continue
        if rule == "/":
            return True  # genuine blanket disallow
        if path == rule or path.startswith(rule):
            return True
    return False


def _purpose(path: str) -> str | None:
    stem = path.rstrip("/").rsplit("/", 1)[-1]
    stem = stem.replace(".html", "").replace("-", "_")
    for key, description in PURPOSES.items():
        if key in stem:
            return description
    return None


def build_inventory(config: Config, raw_html: dict[str, str]) -> dict:
    """Scan cached HTML for every referenced endpoint."""
    disallow, robots_text = load_robots(config)

    endpoints: dict[str, dict] = {}

    def record(
        path: str,
        kind: str,
        page_url: str,
        *,
        method: str | None = None,
        params: list[str] | None = None,
        trigger: str | None = None,
        notes: str | None = None,
    ) -> None:
        if not path or path.startswith(("http://", "https://", "#", "mailto:", "tel:")):
            return
        if not path.startswith("/"):
            return
        entry = endpoints.setdefault(
            path,
            {
                "path": path,
                "kinds": [],
                "methods": [],
                "parameters": [],
                "triggers": [],
                "found_on": [],
                "robots_allowed": not _is_disallowed(path, disallow),
                "purpose": _purpose(path),
                "notes": None,
            },
        )
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        if method and method not in entry["methods"]:
            entry["methods"].append(method)
        for param in params or []:
            if param not in entry["parameters"]:
                entry["parameters"].append(param)
        if trigger and trigger not in entry["triggers"]:
            entry["triggers"].append(trigger)
        if page_url not in entry["found_on"]:
            entry["found_on"].append(page_url)
        if notes and not entry["notes"]:
            entry["notes"] = notes

    for page_url, html in raw_html.items():
        for match in FORM_ACTION_RE.finditer(html):
            action = match.group(1)
            form_block = html[match.start() : match.start() + 4000]
            method_match = re.search(r"method\s*=\s*[\"'](\w+)", form_block[:400], re.I)
            params = sorted(
                set(
                    re.findall(
                        r"<(?:input|select|textarea)[^>]*\bname\s*=\s*[\"']([^\"']+)",
                        form_block,
                        re.I,
                    )
                )
            )
            record(
                action,
                "form_action",
                page_url,
                method=(method_match.group(1).upper() if method_match else "GET"),
                params=params,
                trigger="<form action>",
            )

        for pattern in AJAX_PATTERNS:
            for match in pattern.finditer(html):
                record(match.group(1), "ajax", page_url, trigger="inline JS")

        for match in POPUP_RE.finditer(html):
            record(match.group(1), "popup", page_url, trigger="javascript:open_popup()")

        for match in HREF_JS_RE.finditer(html):
            for inner in re.findall(r"[\"'](/[^\"']+\.html[^\"']*)", match.group(1)):
                record(inner, "link", page_url, trigger="javascript: href")

    # Every Disallow path is recorded, flagged, and never called.
    for rule in disallow:
        if not rule.startswith("/"):
            continue
        entry = endpoints.setdefault(
            rule,
            {
                "path": rule,
                "kinds": [],
                "methods": [],
                "parameters": [],
                "triggers": [],
                "found_on": [],
                "robots_allowed": False,
                "purpose": _purpose(rule),
                "notes": None,
            },
        )
        entry["robots_allowed"] = False
        if "robots_disallow" not in entry["kinds"]:
            entry["kinds"].append("robots_disallow")
        entry["notes"] = "Listed in robots.txt Disallow. Documented only — never invoked."

    entries = sorted(endpoints.values(), key=lambda e: (e["robots_allowed"], e["path"]))
    return {
        "note": (
            "Static analysis only. No endpoint in this inventory was called. "
            "Entries with robots_allowed=false are documented but must not be "
            "invoked without explicit human approval."
        ),
        "robots_url": f"{config.site.base_url}{config.site.robots_path}",
        "robots_disallow_count": len(disallow),
        "totals": {
            "endpoints": len(entries),
            "allowed": sum(1 for e in entries if e["robots_allowed"]),
            "disallowed": sum(1 for e in entries if not e["robots_allowed"]),
        },
        "endpoints": entries,
    }


def render_markdown(inventory: dict) -> str:
    """Render the inventory as a human-readable document."""
    lines = [
        "# API & Endpoint Inventory",
        "",
        "**Static analysis only — no endpoint below was called.**",
        "",
        f"Source: `{inventory['robots_url']}` · "
        f"{inventory['totals']['endpoints']} endpoints · "
        f"{inventory['totals']['allowed']} permitted · "
        f"{inventory['totals']['disallowed']} robots-disallowed",
        "",
        "> Entries marked **⛔ disallowed** appear in `robots.txt` Disallow rules.",
        "> They are documented for awareness and must not be invoked without",
        "> explicit human approval.",
        "",
    ]

    allowed = [e for e in inventory["endpoints"] if e["robots_allowed"]]
    blocked = [e for e in inventory["endpoints"] if not e["robots_allowed"]]

    for title, group in (("Permitted endpoints", allowed), ("Robots-disallowed endpoints", blocked)):
        lines += [f"## {title}", ""]
        if not group:
            lines += ["_None._", ""]
            continue
        for entry in group:
            flag = "" if entry["robots_allowed"] else " ⛔"
            lines.append(f"### `{entry['path']}`{flag}")
            if entry["purpose"]:
                lines.append(f"{entry['purpose']}")
            lines.append("")
            lines.append(f"- **Kind:** {', '.join(entry['kinds'])}")
            if entry["methods"]:
                lines.append(f"- **Method:** {', '.join(entry['methods'])}")
            if entry["parameters"]:
                lines.append(f"- **Parameters:** {', '.join(f'`{p}`' for p in entry['parameters'])}")
            if entry["triggers"]:
                lines.append(f"- **Triggered by:** {', '.join(entry['triggers'])}")
            lines.append(f"- **Found on:** {len(entry['found_on'])} page(s)")
            if entry["notes"]:
                lines.append(f"- **Note:** {entry['notes']}")
            lines.append("")

    lines += [
        "## Why this is documentation-only",
        "",
        "This inventory exists so the eventual chatbot can submit an enquiry on a",
        "user's behalf *knowingly*. Several endpoints sit under `Disallow` paths,",
        "and the callback/OTP flows send real messages to real people — invoking",
        "them from a crawler would be both against the site's stated wishes and",
        "operationally harmful. Any integration must be a deliberate decision.",
        "",
    ]
    return "\n".join(lines)


def emit(config: Config, documents: list[PageDocument], raw_html: dict[str, str]) -> dict[str, int]:
    """Write ``api_inventory.json`` and ``API_INVENTORY.md``."""
    out = config.path("out")
    inventory = build_inventory(config, raw_html)
    write_json(out / "api_inventory.json", inventory)
    (out / "API_INVENTORY.md").write_text(render_markdown(inventory), encoding="utf-8")
    return {
        "endpoints": inventory["totals"]["endpoints"],
        "allowed": inventory["totals"]["allowed"],
        "disallowed": inventory["totals"]["disallowed"],
    }
