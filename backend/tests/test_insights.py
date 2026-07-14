"""Tests for the LLM narrative insight generator using Gemini and Claude."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.insights import InsightEngine


@pytest.mark.asyncio
async def test_llm_narrative_gemini_success():
    """Verify that Gemini is called when gemini_api_key is set."""
    engine = InsightEngine(gemini_api_key="test-gemini-key", narrative_test_mode=True)
    
    # Mock response data for Gemini generateContent endpoint
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": "The market is showing upward momentum. Buy signals on AAPL and NVDA have been accepted."
                        }
                    ]
                }
            }
        ]
    }
    
    # Patch httpx.AsyncClient.post
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        result = await engine.llm_narrative("Test context")
        
        assert result == "The market is showing upward momentum. Buy signals on AAPL and NVDA have been accepted."
        assert len(engine.items) == 1
        item = engine.items[0]
        assert item.kind == "narrative"
        assert item.title == "Analyst note"
        assert item.detail == result
        
        # Verify post arguments
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "gemini-2.5-flash:generateContent" in args[0]
        assert "key=test-gemini-key" in args[0]
        assert kwargs["json"]["contents"][0]["parts"][0]["text"].endswith("Test context")


@pytest.mark.asyncio
async def test_llm_narrative_anthropic_fallback_on_gemini_fail():
    """Verify fallback to Claude when Gemini fails but Claude is configured."""
    engine = InsightEngine(gemini_api_key="test-gemini-key", anthropic_api_key="test-anthropic-key", narrative_test_mode=True)
    
    # Mock post call
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        # First call (Gemini) raises exception, second call (Anthropic) succeeds
        mock_gemini_response = MagicMock()
        mock_gemini_response.raise_for_status.side_effect = Exception("Gemini API error")
        
        mock_anthropic_response = MagicMock()
        mock_anthropic_response.status_code = 200
        mock_anthropic_response.json.return_value = {
            "content": [
                {
                    "text": "Market is flat. Strategy weights rebalanced."
                }
            ]
        }
        
        mock_post.side_effect = [Exception("Gemini network error"), mock_anthropic_response]
        
        result = await engine.llm_narrative("Test context")
        
        assert result == "Market is flat. Strategy weights rebalanced."
        assert len(engine.items) == 1
        assert engine.items[0].detail == result
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_llm_narrative_anthropic_direct():
    """Verify that Claude is called directly if only Claude is configured."""
    engine = InsightEngine(anthropic_api_key="test-anthropic-key", narrative_test_mode=True)
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "content": [
            {
                "text": "Claude narrative result."
            }
        ]
    }
    
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        result = await engine.llm_narrative("Test context")
        
        assert result == "Claude narrative result."
        assert len(engine.items) == 1
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "api.anthropic.com" in args[0]


@pytest.mark.asyncio
async def test_llm_narrative_none_when_keys_missing():
    """Verify that it returns None immediately when no keys are configured."""
    engine = InsightEngine()
    result = await engine.llm_narrative("Test context")
    assert result is None
    assert len(engine.items) == 0
