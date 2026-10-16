"""MOD-10 — the outbound demo dialer, on the page and in the terminal.

The dialer is the one surface where the platform reaches out rather than
answers, so its tests are about what it *refuses*: a number nobody approved, an
hour nobody agreed to, an instance with no carrier credentials. A dialer that
places a call is easy; the work is in the paths that do not.
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from anita.dialer import DialRefused, OutboundDialer, read_numbers, write_numbers  # noqa: E402
from anita.leads import OutboundQueue  # noqa: E402
from anita.voice_webhook import ALLOWLIST_KEY  # noqa: E402

CALLEE = "+919999900000"
OTHER = "+919999911111"


# ---------------------------------------------------------------------------
# A Twilio stand-in. The dialer takes a factory, so nothing here needs a
# carrier account and no test can ring anybody by accident.
# ---------------------------------------------------------------------------
class _Call:
    sid = "CA-fake-0001"
    status = "queued"


class _Client:
    def __init__(self, sink):
        self._sink = sink
        outer = self

        class Calls:
            def create(self, **kwargs):
                outer._sink.append(kwargs)
                return _Call()

        self.calls = Calls()


def _window(*, open_now=True):
    """A queue whose window is either always open or always shut."""
    queue = OutboundQueue(timezone_offset_hours=0.0)
    queue.in_window = lambda now=None: open_now  # type: ignore[assignment]
    return queue


def _dialer(tmp_path, *, numbers=(CALLEE,), configured=True, window=None):
    env = tmp_path / ".env"
    if not env.exists():
        env.write_text(
            f"TWILIO_PHONE_NUMBER=+12792365960\n{ALLOWLIST_KEY}={','.join(numbers)}\n",
            encoding="utf-8",
        )
    sent = []
    dialer = OutboundDialer(
        account_sid="AC-test" if configured else "",
        auth_token="token" if configured else "",
        from_number="+12792365960" if configured else "",
        public_base_url="https://anita.example.com" if configured else "",
        env_path=env,
        window=window if window is not None else _window(),
        client_factory=lambda: _Client(sent),
    )
    return dialer, sent, env


# ---------------------------------------------------------------------------
class TestWhatItRefuses:
    def test_a_number_off_the_list_never_rings(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path)

        with pytest.raises(DialRefused) as caught:
            dialer.place(number=OTHER)

        assert "not on the dialable list" in str(caught.value)
        assert sent == [], "a refused call reached the carrier"

    def test_an_empty_list_refuses_everything(self, tmp_path):
        """Unset must not mean unrestricted."""
        dialer, sent, _ = _dialer(tmp_path, numbers=())

        with pytest.raises(DialRefused):
            dialer.place(number=CALLEE)

        assert sent == []

    def test_an_unconfigured_instance_says_what_is_missing(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path, configured=False)

        with pytest.raises(DialRefused) as caught:
            dialer.place(number=CALLEE)

        message = str(caught.value)
        assert "TWILIO_ACCOUNT_SID" in message
        assert "TUNNEL_HOST" in message, "the public URL is as necessary as the keys"
        assert sent == []

    def test_outside_the_window_it_refuses_and_names_the_hours(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path, window=_window(open_now=False))

        with pytest.raises(DialRefused) as caught:
            dialer.place(number=CALLEE)

        assert "calling window" in str(caught.value)
        assert sent == []

    def test_the_window_can_be_overridden_knowingly(self, tmp_path):
        """The override is a decision, and it is spelled out at the call site."""
        dialer, sent, _ = _dialer(tmp_path, window=_window(open_now=False))

        dialer.place(number=CALLEE, any_time=True)

        assert len(sent) == 1

    def test_a_number_that_is_not_a_number_is_refused(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path)

        with pytest.raises(DialRefused):
            dialer.place(number="not a phone number at all")

        assert sent == []


class TestWhatItPlaces:
    def test_an_allowed_number_is_dialled_with_the_public_twiml_url(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path)

        result = dialer.place(number=f"+91 99999 00000")   # formatting must not matter

        assert len(sent) == 1
        assert sent[0]["to"] == CALLEE
        assert sent[0]["from_"] == "+12792365960"
        assert sent[0]["url"] == "https://anita.example.com/twilio/outbound"
        assert sent[0]["method"] == "POST"
        assert sent[0]["status_callback"] == "https://anita.example.com/twilio/status"
        assert result["sid"] == "CA-fake-0001"

    def test_the_carrier_error_is_reported_rather_than_raised_as_a_crash(self, tmp_path):
        dialer, _, _ = _dialer(tmp_path)

        class Broken:
            class calls:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("21215: not a valid number")

        dialer._client_factory = lambda: Broken()

        with pytest.raises(DialRefused) as caught:
            dialer.place(number=CALLEE)

        assert "21215" in str(caught.value)


class TestTheList:
    def test_adding_normalises_and_persists(self, tmp_path):
        dialer, _, env = _dialer(tmp_path, numbers=())

        numbers = dialer.add("+91 99999 00000")

        assert numbers == [CALLEE]
        assert CALLEE in env.read_text(encoding="utf-8")

    def test_adding_twice_is_idempotent(self, tmp_path):
        dialer, _, _ = _dialer(tmp_path, numbers=())

        dialer.add(CALLEE)
        numbers = dialer.add(CALLEE)

        assert numbers == [CALLEE]

    def test_a_number_added_is_immediately_dialable(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path, numbers=())

        with pytest.raises(DialRefused):
            dialer.place(number=CALLEE)

        dialer.add(CALLEE)
        dialer.place(number=CALLEE)

        assert len(sent) == 1

    def test_removing_makes_it_undialable_again(self, tmp_path):
        dialer, sent, _ = _dialer(tmp_path)

        dialer.remove(CALLEE)

        with pytest.raises(DialRefused):
            dialer.place(number=CALLEE)
        assert sent == []

    def test_removing_a_number_that_is_absent_changes_nothing(self, tmp_path):
        dialer, _, _ = _dialer(tmp_path)

        assert dialer.remove(OTHER) == [CALLEE]


class TestTheConfigFileIsTreatedAsPrecious:
    """`.env` holds live credentials; editing it must not disturb them."""

    def test_the_rest_of_the_file_survives_an_edit(self, tmp_path):
        env = tmp_path / ".env"
        original = (
            "# a comment\n"
            "TWILIO_AUTH_TOKEN=super-secret\n"
            f"{ALLOWLIST_KEY}={CALLEE}\n"
            "SMTP_PASS=also-secret\n"
        )
        env.write_text(original, encoding="utf-8")

        write_numbers(env, [CALLEE, OTHER])

        after = env.read_text(encoding="utf-8")
        assert "TWILIO_AUTH_TOKEN=super-secret" in after
        assert "SMTP_PASS=also-secret" in after
        assert "# a comment" in after
        assert OTHER in after

    def test_a_file_without_the_key_gets_one_appended(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("TWILIO_AUTH_TOKEN=secret\n", encoding="utf-8")

        write_numbers(env, [CALLEE])

        assert read_numbers(env) == [CALLEE]
        assert "TWILIO_AUTH_TOKEN=secret" in env.read_text(encoding="utf-8")

    def test_a_commented_key_is_not_overwritten(self, tmp_path):
        """The template's commented example must stay a comment."""
        env = tmp_path / ".env"
        env.write_text(f"# {ALLOWLIST_KEY}=\n", encoding="utf-8")

        write_numbers(env, [CALLEE])

        text = env.read_text(encoding="utf-8")
        assert f"# {ALLOWLIST_KEY}=" in text, "the commented example was consumed"
        assert read_numbers(env) == [CALLEE]


# ---------------------------------------------------------------------------
class TestTheSurface:
    """`GET /dialer`'s backend, driven without a server."""

    @staticmethod
    def _surface(tmp_path, **kwargs):
        from anita.surfaces import DialerSurface

        dialer, sent, _ = _dialer(tmp_path, **kwargs)
        return DialerSurface(dialer=dialer), sent

    def test_it_reports_the_numbers_and_whether_calling_is_possible(self, tmp_path):
        surface, _ = self._surface(tmp_path)

        response = surface.dialable()

        assert response.status == 200
        assert response.body["numbers"] == [CALLEE]
        assert response.body["configured"] is True
        assert response.body["missing"] == []

    def test_an_unconfigured_instance_names_what_is_missing(self, tmp_path):
        surface, _ = self._surface(tmp_path, configured=False)

        response = surface.dialable()

        assert response.body["configured"] is False
        assert "TWILIO_ACCOUNT_SID" in response.body["missing"]

    def test_adding_and_removing_through_the_surface(self, tmp_path):
        surface, _ = self._surface(tmp_path, numbers=())

        added = surface.edit_dialable(number=OTHER, action="add")
        assert added.body["numbers"] == [OTHER]

        removed = surface.edit_dialable(number=OTHER, action="remove")
        assert removed.body["numbers"] == []

    def test_an_unknown_action_is_a_bad_request(self, tmp_path):
        surface, _ = self._surface(tmp_path)

        response = surface.edit_dialable(number=CALLEE, action="delete-everything")

        assert response.status == 400
        assert "add" in response.body["error"]

    def test_a_refused_call_comes_back_as_a_sentence_not_a_code(self, tmp_path):
        """The operator is authenticated; what stopped the call is a rule they
        can change, so the message is the useful part."""
        surface, sent = self._surface(tmp_path, numbers=())

        response = surface.call(number=CALLEE)

        assert response.status == 400
        assert "no numbers are dialable" in response.body["error"]
        assert sent == []

    def test_a_placed_call_returns_what_twilio_said(self, tmp_path):
        surface, sent = self._surface(tmp_path)

        response = surface.call(number=CALLEE)

        assert response.status == 200
        assert response.body["sid"] == "CA-fake-0001"
        assert len(sent) == 1


class TestTheRoutes:
    """Wiring, not behaviour: the tests above cover the behaviour.

    The other surfaces are stubs because `build_router` only needs to *refer*
    to their handlers here — standing up a real orchestrator to check that a
    route is authenticated would test the orchestrator instead.
    """

    class _Stub:
        """Any handler the router asks for, as a no-op."""

        def __getattr__(self, name):
            return lambda **kwargs: None

    @classmethod
    def _client(cls, *, dialer=None):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import build_router

        stub = cls._Stub()
        app = create_app(build_router(
            chat=stub, mic=stub, admin=stub, admin_token="secret", dialer=dialer,
        ))
        return TestClient(app)

    def test_the_dialer_routes_require_the_admin_token(self):
        client = self._client()

        assert client.get("/api/admin/dialable").status_code == 401
        assert client.post("/api/admin/call", json={"number": CALLEE}).status_code == 401
        assert client.post("/api/admin/dialable",
                           json={"number": CALLEE, "action": "add"}).status_code == 401

    def test_an_authenticated_read_reaches_the_handler(self, tmp_path):
        client = self._client(dialer=_dialer(tmp_path)[0])

        response = client.get("/api/admin/dialable",
                              headers={"Authorization": "Bearer secret"})

        assert response.status_code == 200
        assert response.json()["numbers"] == [CALLEE]

    def test_without_a_dialer_the_routes_say_so_rather_than_404(self):
        """An operator looking for the dialer should find the page that
        explains why it does nothing."""
        client = self._client()

        response = client.get("/api/admin/dialable",
                              headers={"Authorization": "Bearer secret"})

        assert response.status_code == 503
        assert "no outbound dialer" in response.json()["reason"]

    def test_the_dialer_page_is_served(self):
        from fastapi.testclient import TestClient

        from anita.server import create_app
        from anita.surfaces import Router

        client = TestClient(create_app(Router()))

        response = client.get("/dialer")

        assert response.status_code == 200
        assert "Outbound Dialer" in response.text
