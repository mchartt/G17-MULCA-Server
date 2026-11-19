"""build_openai_client: OpenAI by default (unchanged), Azure only with both variables."""

from __future__ import annotations

import pytest

from muca_server.agents.openai.client_config import (
    azure_urls,
    build_openai_client,
    uses_azure,
)


def test_without_azure_variables_it_is_plain_openai_as_before():
    client = build_openai_client({"OPENAI_API_KEY": "sk-test"})
    assert str(client.base_url) == "https://api.openai.com/v1/"
    assert client.api_key == "sk-test"


def test_without_azure_variables_the_openai_key_is_still_required():
    with pytest.raises(KeyError):
        build_openai_client({})


def test_one_azure_variable_alone_does_not_switch_to_azure():
    assert not uses_azure({"AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com"})
    assert not uses_azure({"AZURE_OPENAI_API_KEY": "k", "AZURE_OPENAI_ENDPOINT": "  "})


def test_both_azure_variables_point_rest_and_realtime_at_the_resource():
    client = build_openai_client(
        {
            "AZURE_OPENAI_ENDPOINT": "https://g17.openai.azure.com/",
            "AZURE_OPENAI_API_KEY": " key ",
        }
    )
    assert str(client.base_url) == "https://g17.openai.azure.com/openai/v1/"
    assert str(client.websocket_base_url) == "wss://g17.openai.azure.com/openai/v1"
    assert client.api_key == "key"


def test_an_endpoint_without_https_is_refused():
    with pytest.raises(ValueError):
        azure_urls("g17.openai.azure.com")
