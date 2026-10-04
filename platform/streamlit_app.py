"""The buyer chat, as a Streamlit app (MOD-10).

Run:
    .venv\\Scripts\\streamlit run streamlit_app.py

This is a **surface**, not a second implementation. It calls the same
``ChatSurface`` the HTTP adapter calls, through the same ``assemble()``, so a
question asked here takes the identical path: intent classification, the
deterministic engine for commercial facts, retrieval with the grounding gate,
and the published-FAQ shortcut. Anything that validates here validates the
product; anything that does not is a defect in the product rather than in this
page.

It exists because Streamlit is the fastest way for someone to *use* the system
and judge the answers, which is the validation that matters. The page is
deliberately thin — it renders what the surface returns and adds no intelligence
of its own. A UI that improves an answer is a UI that hides a defect.

**Why the dense index is built at start-up.** The vector store is in memory, so
it is empty in every new process. Without building it, retrieval silently
degrades to the sparse leg alone — which still works, and would therefore never
announce itself. The sidebar says which legs are live so a validation run is not
quietly testing half the system.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

PLATFORM = Path(__file__).resolve().parent
sys.path.insert(0, str(PLATFORM / "app"))

st.set_page_config(page_title="ANITA MEDICAL SYSTEMS — Product Assistant",
                   page_icon="🩻", layout="centered")


# ---------------------------------------------------------------------------
# The instance — assembled once per process
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Starting the assistant…")
def startup() -> dict:
    """Assemble the platform, build the index, and report what is missing.

    A missing admin token is tolerated here and only here: this process serves
    the buyer chat and no admin route, so the token is genuinely unused. Every
    other preflight failure still refuses — an instance sharing a database or a
    collection with a sibling product must not start, and that check is the
    reason the boot sequence exists.
    """
    from anita.assembly import assemble, deployed_environment
    from anita.boot import preflight, render_report

    report = preflight()
    allowed_only = {"SECRETS"}
    blockers = [phase for phase in report.phases if not phase.ok and phase.name not in allowed_only]
    if blockers:
        return {"failed": render_report(report)}

    # Returned rather than written to session_state: this function is cached
    # across sessions, and touching session state inside it would tie one
    # visitor's session to the process-wide instance.
    note = ""
    if not report.ok:
        note = ("Preflight passed except SECRETS (ANITA_ADMIN_TOKEN unset). "
                "This process serves the buyer chat only and uses no admin route.")

    built = assemble(require_preflight=False, env=deployed_environment())

    indexed = 0
    if built.chat.retriever is not None:
        try:
            indexed = built.chat.retriever.build_index()
        except Exception as exc:  # noqa: BLE001 - report, never crash the page
            built.notes.append(f"dense index not built — {type(exc).__name__}: {exc}")

    return {"built": built, "indexed": indexed, "report": render_report(report),
            "note": note}


state = startup()

if "failed" in state:
    st.error("The assistant could not start.")
    st.code(state["failed"])
    st.stop()

built = state["built"]
chat = built.chat
retriever = chat.retriever
indexed = state["indexed"]


# ---------------------------------------------------------------------------
# Sidebar — what is actually running
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Instance")
    st.caption(f"**{built.settings.identity.company_name}**")
    st.caption(f"agent · {built.settings.identity.agent_name}")
    st.caption(f"collection · `{built.settings.isolation.chroma_collection}`")

    st.subheader("Retrieval")
    if retriever is None:
        st.error("No retriever — every question will be declined.")
    else:
        st.caption(f"corpus · {len(retriever.chunks)} chunks")
        if indexed:
            st.success(f"dense · {indexed} chunks embedded")
        else:
            st.warning("dense · NOT built (sparse only)")
        st.caption(f"sparse · BM25 over {len(retriever.chunks)} chunks")
        st.caption(f"gate threshold · {built.settings.retrieval.similarity_threshold}")

    if built.notes:
        st.subheader("Degraded")
        for note in built.notes:
            st.warning(note, icon="⚠️")

    if state.get("note"):
        st.info(state["note"], icon="ℹ️")

    st.divider()
    st.caption("Answers are grounded in the published catalogue. "
               "Prices are list prices; a quotation is required for an order.")


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
# Streamlit's default chat composer renders below 16px. iOS Safari zooms the
# whole page when a focused input is smaller than that, which pushes the
# composer off-screen mid-sentence — on the mobile-web surface that decision D1
# says is supported. The plain-HTML chat page already sets 16px for this reason;
# this brings the Streamlit surface to the same standard.
st.markdown(
    """
    <style>
      [data-testid="stChatInput"] textarea,
      [data-testid="stChatInputTextArea"] {
        font-size: 16px !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Product assistant")
st.caption("Ask about specifications, prices, minimum order quantity or delivery.")

if "session_id" not in st.session_state:
    import uuid

    # A per-session id, so a reload does not merge two people's conversations.
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("meta"):
            st.caption(message["meta"])

if prompt := st.chat_input("Ask about a product…"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Checking the catalogue…"):
            response = chat.post_message(
                message=prompt, session_id=st.session_state.session_id
            )
        body = response.body if isinstance(response.body, dict) else {}
        text = body.get("text") or "Something went wrong."

        st.markdown(text)

        # The provenance line. An answer a buyer cannot check is an answer they
        # have to take on trust, which is the thing this product is for.
        # The surface says what the reply is; the page must not infer it.
        # Deriving "declined" from `grounded == False` labelled a greeting, a
        # capability statement and an offer to call back all as refusals — the
        # assistant answered and the page told the buyer it had refused.
        bits = []
        if body.get("declined"):
            bits.append("declined")
        elif body.get("grounded"):
            bits.append("grounded in the catalogue")
        elif body.get("unavailable"):
            bits.append("could not reach the catalogue")
        else:
            bits.append("answered")
        if body.get("intent"):
            bits.append(f"intent: {body['intent']}")
        if body.get("source_url"):
            bits.append(f"[source]({body['source_url']})")
        meta = " · ".join(bits)
        st.caption(meta)

    st.session_state.messages.append(
        {"role": "assistant", "content": text, "meta": meta}
    )
