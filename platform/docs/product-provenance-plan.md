# Product thumbnails and source links in the answer

> **Status: built** for the Streamlit chat, hotlinking the CDN (§5, option A). The web chat
> page and the console are still open — §6.
>
> **What differed from this plan, now that it exists:** the image is attached in
> `answer_engine._product_citation` rather than on `ProductView` (less churn, same effect);
> the "which product is this about" rule lives in `anita.surfaces.product_card` rather than in
> the page, so every surface showing provenance agrees on it; and `st.image` needs a **fixed
> pixel width** — a narrow column resolves `width="stretch"` to a zero-width box, and an image
> with no box is never fetched, so it renders as blank space with no error anywhere.

## 1. What is being asked

In the Streamlit chat, alongside an answer, show:

* a **thumbnail of the product** the answer is about, and
* a **link to the page the answer was lifted from**, on the client's own website.

The point is provenance a buyer can check. An answer a buyer cannot verify is an
answer they have to take on trust, which is the thing this product exists to fix.

## 2. Most of the data already exists

This is smaller than it looks. Four of the five pieces are already in place and
verified:

| Piece | Where | State |
|---|---|---|
| The product behind an answer | `ChatReply.grounded_on[].product_id` / `product_name` | **Live.** See the real payload below |
| The page the data came from | `ChatReply.source_url`, and per-citation `source_url` | **Live** |
| Those citations reaching the page | `ChatReply.as_dict()` includes `grounded_on` — `answer_engine.py:89` | **Live** |
| A primary image per product | `media` table in `ams.sqlite` | **Live, 24/24 products** |
| Reading that table | `corpus.py:_connect()` already opens `ams.sqlite` read-only | **Already open** |

A real citation, taken from the running instance just now:

```json
{"chunk_id": "product:270694", "chunk_type": "product_record",
 "product_id": "270694", "product_name": "Ultrasound Gel",
 "source_url": "https://www.anitamedicalsystems.com/ultrasound-gel-270694.html",
 "content_hash": ""}
```

So the answer path already knows the product and the source page. **Nothing new
has to be inferred, and nothing new has to be crawled.**

What is missing is one join (product → image), one field on the wire, and the
rendering.

## 3. The image data, and the trap in it

`ams.sqlite` carries a `media` table:

| Column | Meaning |
|---|---|
| `product_id` | the product the image belongs to — **zero-padded** |
| `url` | the image |
| `role` | `primary` or `related` |
| `variant` | the rendition the crawler saw (`b/3`, `b/4`, `b/5`) |
| `alt` | alt text — **null on every row today** |
| `source_url` | the page the image was found on |

Counts: **24 `primary`** and 62 `related`.

### The trap: `images_by_product` is not "images of this product"

`data/out/media_manifest.json` also has an `images_by_product` map, keyed by
product id, and it is the obvious thing to reach for. **Do not use it.** It is
built from images *seen on* a product's page, so it mixes the product's own
photo with other products shown alongside it. Product `270680`'s list contains
images whose own `product_id` is `00270683`, `03935249`, `03935248`, `03935252` —
four other products.

Showing any of those under "FCR Capsula X Imaging Systems" would be exactly the
confidently-wrong answer this product is built to avoid, and it would be the
worst kind: a picture, which reads as evidence.

**Use `role = 'primary'`.** Measured against the catalogue:

* 24 primary rows, 24 distinct products
* no duplicates, no null product ids
* every primary maps to a catalogue product, and **every catalogue product has
  exactly one primary**
* the other 62 rows are `related` and belong to other products

That is a clean 1:1 join, and it is the only one in this data set.

## 4. The design — three changes

### 4.1 `corpus.py` — read the media table

Add a `Media` record and an accessor, following the shape already used for
specs and FAQs:

```
class Media:
    product_id: str
    url: str
    role: str          # "primary" | "related"
    variant: str
    alt: str
    source_url: str

def media_for(self, product_id: str) -> list[Media]: ...
def primary_image(self, product_id: str) -> Media | None: ...
```

and carry `primary_image` on `ProductView`, beside `specs`, `trade` and `faqs` —
it is the same kind of thing: data about one product, loaded for the same
reason.

**Normalise the id.** The table stores `00270694`; citations carry `270694`.
Both sides must agree or the join silently returns nothing. Normalise in the
accessor and assert it in a test, because a silent miss here looks like "this
product has no photo" rather than like a bug.

### 4.2 `answer_engine.py` — put the image on the citation

`_product_citation(product)` already builds the citation the surface receives.
It gains an `image_url` (and `image_source_url`, so the image's own provenance
travels with it) when the product has a primary image.

Attaching it **at composition time** rather than looking it up in the page is
the point: the surface must render what the reply says, never infer it. That
rule is already written into `streamlit_app.py`'s docstring, and the thumbnail
is the first thing in this product that could quietly break it.

### 4.3 `streamlit_app.py` — render it

Beside the answer: `st.image()` for the thumbnail, and the source URL promoted
from a small `source` word in the caption to a named link — *"View on
anitamedicalsystems.com"*. The link is the more valuable half of the two and it
is currently the easiest thing on the page to miss.

## 5. Where the images come from — the decision

The images are **not on disk**. `config/site.yaml:109` sets `media.download: false`,
and the manifest says so out loud: *"Manifest only — no image binaries were
downloaded."* Every URL points at the client's storefront CDN,
`cpimg.tistatic.com`. Probed just now: **HTTP 200, `image/jpeg`, ~31 KB** for a
product thumbnail, so the URLs are live today.

| | **A — hotlink the CDN** | **B — cache locally** |
|---|---|---|
| Work | none; the URLs already work | set `media.download: true`, re-crawl, serve from `/static` |
| Offline demo | breaks | works |
| Third-party dependency | the storefront's CDN, on every page load | none |
| Link rot | an image silently 404s | immune — the bytes are ours |
| Privacy | every viewer's browser calls that CDN | none |
| Freshness | always current | stale until the next crawl |

**Recommendation: A now, B when the demo is over.** For showing a client their
own product photos, hotlinking is honest and immediate. Caching is the right end
state — a client-facing page should not depend on a shop platform's CDN — but it
needs a re-crawl, and the re-crawl is a bigger decision than this feature.

**Do not try to be clever with the variant segment.** `b/3`, `b/4` and `b/5`
returned byte-identical images for the product tested, and the recorded variant
varies by product anyway (13 are `b/3`, 9 `b/4`, 2 `b/5`). The `url` column is
the one to use, as recorded.

## 6. What not to do

* **No thumbnail when the answer is not about a product.** A greeting, a
  clinical refusal, a company-address answer, a decline — none of these are
  about a product, and a picture under them would imply the assistant had
  identified one.
* **No thumbnail when the product has no primary image.** Today all 24 have one;
  the code must still render nothing rather than a neighbouring product's photo
  when that changes.
* **Never use a `related` row** for a product's own thumbnail.
* **Never pick the product from the top-ranked chunk.** Use the citation the
  answer engine already produced — the engine resolved the product from the
  question, and retrieval rank is a different (and weaker) signal.

## 7. Effort

| Work | Rough size |
|---|---|
| `corpus.py` — record, accessors, `ProductView` field | ~40 lines |
| `answer_engine.py` — citation gains the image | ~10 lines |
| `streamlit_app.py` — thumbnail and named source link | ~25 lines |
| Tests | ~8 |
| **Total** | **about half a day** |

## 8. How to verify it

Run the five demo questions from the runbook and check each one shows the right
photo, not merely a photo:

| Question | Expect |
|---|---|
| price of the ultrasound gel | Ultrasound Gel photo |
| what do you sell? | **no image** — a listing, not a product |
| MOQ of the ultrasound gel | Ultrasound Gel photo |
| "I have a headache…" | **no image** — nothing was retrieved |
| "Do you sell MRI machines?" | **no image** — the decline is the point |

The three "no image" rows are the test. A feature that always finds a picture is
worse than one that never does.

## 9. Open decisions

1. **Hotlink or cache** (§5) — my recommendation is hotlink now.
2. **Streamlit only, or the web chat page too?** `chat.html` gets the same
   citation payload, so it is the same rendering work in a different page. The
   console already has the data and does not need a picture.
3. **Not voice.** There is no visual channel on a phone call, and adding an
   image reference to a spoken answer would be inventing a capability.
