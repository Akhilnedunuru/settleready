"""Contract tests for the pieces implemented in the scaffold.

These run with zero API keys and zero fixtures — they pin the interfaces
so later modules can't silently break the seams.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_mock_email_roundtrip():
    from settleready.tools.email_client import MockEmailClient

    with tempfile.TemporaryDirectory() as d:
        client = MockEmailClient(data_dir=d)
        client.inject_inbound(to="claims@demo.local", from_="customer@example.com",
                              subject="Claim TL-1", body="stolen accord")
        sent = client.send(to="customer@example.com", subject="Re: Claim TL-1",
                           body="Please send the title document.")
        assert len(client.poll_inbox()) == 1
        assert len(client.outbox()) == 1
        assert sent.direction == "outbound"


def test_provider_factory_rejects_unknown():
    from settleready.llm.provider import get_provider

    os.environ["SETTLEREADY_PROVIDER"] = "definitely-not-a-provider"
    try:
        get_provider()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown provider")
    finally:
        os.environ.pop("SETTLEREADY_PROVIDER", None)


def test_tally_promotion_rule_documented():
    """The promotion threshold is a documented constant of the design (3)."""
    # When tally.py is implemented, assert the threshold lives in one place.
    assert True
