# Demo question bank — verified against the running instance

Every question in the **Verified** sections below was sent to the live assistant on
**17 October 2026** through `POST /api/chat/message`, each in a fresh session, and
returned a correct, grounded answer. The same bank is embedded in the Client Demo
Runbook (`demo-runbook.html`, Act 02).

Use these questions in a client demo. Anything not on this list is an experiment:
try it before the demo, not during it.

---

## Demo caveat — keep prose questions out of the voice acts

Commercial and catalogue questions are answered deterministically from the
catalogue; they never reach the language model and answer in well under a second.

Open-ended prose questions do reach the model. On this box that loads
`qwen2.5:14b` (~10 GB of VRAM), which drops free memory below the 6,000 MiB
sibling floor. Within about 30 seconds the capacity ladder sheds voice, and every
call is answered with "All our lines are busy" while chat keeps working. If prose
has been used before a voice act, run:

```
ollama stop qwen2.5:14b
```

and wait about 30 seconds for the ladder to climb back to `full`.

---

## Commercial facts — deterministic and instant

These never reach the language model; they read the catalogue row and answer in
well under a second.

| Ask this | What the client sees |
|---|---|
| What is the price of the ultrasound gel? | ₹60/Piece list price, with the quotation caveat, cited |
| What is the price of the X-Ray Cassette? | ₹2,500/Piece, same caveat and citation |
| What is the price of the Medical Dry Laser Imager? | ₹4,00,000/Piece, same caveat and citation |
| What is the price of the Doctor Protective Apparel? | ₹9,000/Piece, same caveat and citation |
| What is the minimum order quantity for the ultrasound gel? | 10 Pieces, read from the catalogue |
| What is the minimum order quantity for the Microbar Paste? | 10 Pieces |
| What is the MOQ for the High Pressure Syringe & Tubing? | 100 Pieces |
| What is the delivery time for the X-Ray Film Drying Cabinet? | 7 Days |
| What is the delivery time for the Fuji Computed Radiography System? | 7 Days |
| What is the supply ability for the Ultrasound Gel? | 10 Per Day |
| Can I get a sample of the Microbar Powder? | Samples are available; the buyer pays shipping and taxes |

## Catalogue and company facts

Still grounded, still cited; these exercise the catalogue and company records
rather than a single commercial field.

| Ask this | What the client sees |
|---|---|
| What do you sell? | 24 products across 7 categories, counted from the catalogue |
| What are the specifications of the X-Ray Cassette? | Condition New; not portable; manual power source; 2–3.5 kg — each line from the spec table |
| What is the condition of the X-Ray Cassette? | "Condition: New", inside the full grounded specification list |
| What are your certifications? | ISO 9001:2015 and the NSIC-CRISIL ratings |

## Scope and honesty — the refusals are the feature

Ask at least one of these; a client trusts the answers more after seeing what the
assistant will not answer.

| Ask this | What the client sees |
|---|---|
| I have a headache, which medicine should I take? | A clean clinical refusal — no medical advice, ever |
| Do you sell MRI machines? | An honest decline, with an offer to get the team to confirm |
| What is the price of the Human Body? | The same honest decline for a product that does not exist — no invented price |
| Do you have branches in Delhi? | An honest "not in the catalogue" decline; branch data is not published |

## Optional — show what happens when it is unsure

**"What are the specifications of the Medical Dry Imager?"** — two products share
that name (₹5,50,000 and ₹2,00,000). The assistant does not guess: it lists both
and asks which one. Use this if the client asks how ambiguity is handled.

---

## Templates that work for any catalogue product

- `What is the price of <product>?`
- `What is the minimum order quantity for <product>?`
- `What is the delivery time for <product>?`
- `What is the supply ability for <product>?`
- `Can I get a sample of <product>?`
- `What are the specifications of <product>?`
- `What is the condition of <product>?`

## Catalogue reference

| Product | Price | MOQ |
|---|---|---|
| FCR Capsula X Imaging Systems | ₹14,00,000 | 1 Unit |
| FUJIFILM FCR Profect CS Dry Imager | ₹25,00,000 | 1 Unit |
| Medical Dry Imager | ₹5,50,000 | 1 Unit |
| Microbar Paste | ₹300 | 10 Pieces |
| Microbar Enema | ₹500 | 1 Piece |
| Microbar Powder | ₹900 | 10 Pieces |
| X-Ray Film Viewing Boxes | ₹6,000 | 10 Pieces |
| X-Ray Film Developing Hanger | ₹525 | 10 Pieces |
| X-Ray Film Drying Cabinet | ₹25,000 | 1 Unit |
| X-Ray Cassette | ₹2,500 | 1 Piece |
| Xray Intensifying Screen | ₹9,500/Pair | 1 |
| Doctor Protective Apparel | ₹9,000 | 1 Piece |
| Ultrasound Gel | ₹60 | 10 Pieces |
| Fuji Computed Radiography System | ₹14,00,000 | 1 |
| FCR Prima T Image Reader | ₹5,50,000 | 1 Piece |
| FCR Prima T2 Image Reader | ₹5,50,000 | 1 Piece |
| FCR Prima Tm Imaging Equipment | ₹5,50,000 | 1 Piece |
| Medical Dry Laser Imager | ₹4,00,000 | 1 |
| Medical Dry Imager | ₹2,00,000 | 1 Unit |
| Dry Imager | ₹4,00,000 | 1 Unit |
| Apollo RT CT Single Head Pressure Injector APO 100 | ₹5,50,000 | 1 Piece |
| Apollo RT CT Dual Head Pressure Injector APO 200 | ₹13,85,000 | 1 Piece |
| Apollo RT Cath Lab Pressure Injector RT-150 | ₹14,50,000 | 1 |
| High Pressure Syringe & Tubing | ₹1,450 | 100 Pieces |

All products are New condition and have a 7-day delivery time.

**Ambiguous name:** "Medical Dry Imager" matches two products (₹5,50,000 and
₹2,00,000). Use "Medical Dry Laser Imager" or include the price to disambiguate.

---

## Do not ask these in the demo

| Question | Why not |
|---|---|
| Can you give me a discount on the X-ray machine? | ~23 s, takes the LLM path, ends in a non-answer, and triggers the VRAM/voice-shed problem |
| What is the main domestic market for the Ultrasound Gel? | Returns the specification list, not the market |
| Which category does the Microbar Powder belong to? | Returns a "no published specification list" reply |
| What is the warranty on the X-Ray Cassette? | Returns the spec list, ignoring the warranty |

---

## What the assistant's knowledge covers

- The **24-product published catalogue** — prices, MOQ, delivery time, supply
  ability, sample availability, specifications, condition.
- The catalogue's **category summary** (via "What do you sell?").
- **Company records** — certifications, company identity.
- **Published FAQs** attached to products.

It does **not** cover: discounts or negotiation, warranty terms, branch or
service-centre data, stock counts, medical advice, or products outside the
catalogue. For those it declines and offers to have the team confirm.
