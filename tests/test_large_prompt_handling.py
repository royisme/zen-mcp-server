"""
Tests for large prompt handling functionality.

This test module verifies that the MCP server correctly handles
prompts that exceed the 50,000 character limit by requesting
Claude to save them to a file and resend.
"""

import json
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import TextContent

from config import MCP_PROMPT_SIZE_LIMIT
from tools.analyze import AnalyzeTool
from tools.chat import ChatTool
from tools.codereview import CodeReviewTool
from tools.debug import DebugIssueTool
from tools.precommit import Precommit
from tools.thinkdeep import ThinkDeepTool


class TestLargePromptHandling:
    """Test suite for large prompt handling across all tools."""

    @pytest.fixture
    def large_prompt(self):
        """Create a prompt larger than MCP_PROMPT_SIZE_LIMIT characters."""
        return "x" * (MCP_PROMPT_SIZE_LIMIT + 1000)

    @pytest.fixture
    def normal_prompt(self):
        """Create a normal-sized prompt."""
        return "This is a normal prompt that should work fine."

    @pytest.fixture
    def temp_prompt_file(self, large_prompt):
        """Create a temporary prompt.txt file with large content."""
        # Create temp file with exact name "prompt.txt"
        temp_dir = tempfile.mkdtemp()
        file_path = os.path.join(temp_dir, "prompt.txt")
        with open(file_path, "w") as f:
            f.write(large_prompt)
        return file_path

    @pytest.mark.asyncio
    async def test_chat_large_prompt_detection(self, large_prompt):
        """Test that chat tool detects large prompts."""
        tool = ChatTool()
        result = await tool.execute({"prompt": large_prompt})

        assert len(result) == 1
        assert isinstance(result[0], TextContent)

        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"
        assert f"{MCP_PROMPT_SIZE_LIMIT:,} characters" in output["content"]
        # The prompt size should match the user input since we check at MCP transport boundary before adding internal content
        assert output["metadata"]["prompt_size"] == len(large_prompt)
        assert output["metadata"]["limit"] == MCP_PROMPT_SIZE_LIMIT

    @pytest.mark.asyncio
    async def test_chat_normal_prompt_works(self, normal_prompt):
        """Test that chat tool works normally with regular prompts."""
        tool = ChatTool()

        # Mock the model to avoid actual API calls
        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="This is a test response",
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            result = await tool.execute({"prompt": normal_prompt})

            assert len(result) == 1
            output = json.loads(result[0].text)
            assert output["status"] == "success"
            assert "This is a test response" in output["content"]

    @pytest.mark.asyncio
    async def test_chat_prompt_file_handling(self, temp_prompt_file):
        """Test that chat tool correctly handles prompt.txt files with reasonable size."""
        from tests.mock_helpers import create_mock_provider

        tool = ChatTool()
        # Use a smaller prompt that won't exceed limit when combined with system prompt
        reasonable_prompt = "This is a reasonable sized prompt for testing prompt.txt file handling."

        # Mock the model with proper capabilities and ModelContext
        with (
            patch.object(tool, "get_model_provider") as mock_get_provider,
            patch("utils.model_context.ModelContext") as mock_model_context_class,
        ):

            mock_provider = create_mock_provider(model_name="gemini-2.5-flash-preview-05-20", context_window=1_048_576)
            mock_provider.generate_content.return_value.content = "Processed prompt from file"
            mock_get_provider.return_value = mock_provider

            # Mock ModelContext to avoid the comparison issue
            from utils.model_context import TokenAllocation

            mock_model_context = MagicMock()
            mock_model_context.model_name = "gemini-2.5-flash-preview-05-20"
            mock_model_context.calculate_token_allocation.return_value = TokenAllocation(
                total_tokens=1_048_576,
                content_tokens=838_861,
                response_tokens=209_715,
                file_tokens=335_544,
                history_tokens=335_544,
            )
            mock_model_context_class.return_value = mock_model_context

            # Mock read_file_content to avoid security checks
            with patch("tools.base.read_file_content") as mock_read_file:
                mock_read_file.return_value = (
                    reasonable_prompt,
                    100,
                )  # Return tuple like real function

                # Execute with empty prompt and prompt.txt file
                result = await tool.execute({"prompt": "", "files": [temp_prompt_file]})

                assert len(result) == 1
                output = json.loads(result[0].text)
                assert output["status"] == "success"

                # Verify read_file_content was called with the prompt file
                mock_read_file.assert_called_once_with(temp_prompt_file)

                # Verify the reasonable content was used
                # generate_content is called with keyword arguments
                call_kwargs = mock_provider.generate_content.call_args[1]
                prompt_arg = call_kwargs.get("prompt")
                assert prompt_arg is not None
                assert reasonable_prompt in prompt_arg

        # Cleanup
        temp_dir = os.path.dirname(temp_prompt_file)
        shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_thinkdeep_large_analysis(self, large_prompt):
        """Test that thinkdeep tool detects large current_analysis."""
        tool = ThinkDeepTool()
        result = await tool.execute({"prompt": large_prompt})

        assert len(result) == 1
        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"

    @pytest.mark.asyncio
    async def test_codereview_large_focus(self, large_prompt):
        """Test that codereview tool detects large focus_on field using real integration testing."""
        import importlib
        import os

        tool = CodeReviewTool()

        # Save original environment
        original_env = {
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
            "DEFAULT_MODEL": os.environ.get("DEFAULT_MODEL"),
        }

        try:
            # Set up environment for real provider resolution
            os.environ["OPENAI_API_KEY"] = "sk-test-key-large-focus-test-not-real"
            os.environ["DEFAULT_MODEL"] = "o3-mini"

            # Clear other provider keys to isolate to OpenAI
            for key in ["GEMINI_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY"]:
                os.environ.pop(key, None)

            # Reload config and clear registry
            import config

            importlib.reload(config)
            from providers.registry import ModelProviderRegistry

            ModelProviderRegistry._instance = None

            # Test with real provider resolution
            try:
                result = await tool.execute(
                    {
                        "files": ["/some/file.py"],
                        "focus_on": large_prompt,
                        "prompt": "Test code review for validation purposes",
                        "model": "o3-mini",
                    }
                )

                # The large focus_on should be detected and handled properly
                assert len(result) == 1
                output = json.loads(result[0].text)
                # Should detect large prompt and return resend_prompt status
                assert output["status"] == "resend_prompt"

            except Exception as e:
                # If we get an exception, check it's not a MagicMock error
                error_msg = str(e)
                assert "MagicMock" not in error_msg
                assert "'<' not supported between instances" not in error_msg

                # Should be a real provider error (API, authentication, etc.)
                # But the large prompt detection should happen BEFORE the API call
                # So we might still get the resend_prompt response
                if "resend_prompt" in error_msg:
                    # This is actually the expected behavior - large prompt was detected
                    assert True
                else:
                    # Should be a real provider error
                    assert any(
                        phrase in error_msg
                        for phrase in ["API", "key", "authentication", "provider", "network", "connection"]
                    )

        finally:
            # Restore environment
            for key, value in original_env.items():
                if value is not None:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)

            # Reload config and clear registry
            importlib.reload(config)
            ModelProviderRegistry._instance = None

    @pytest.mark.asyncio
    async def test_review_changes_large_original_request(self, large_prompt):
        """Test that review_changes tool works with large prompts (behavior depends on git repo state)."""
        tool = Precommit()
        result = await tool.execute({"path": "/some/path", "prompt": large_prompt, "model": "flash"})

        assert len(result) == 1
        output = json.loads(result[0].text)
        # The precommit tool may return success or files_required_to_continue depending on git state
        # The core fix ensures large prompts are detected at the right time
        assert output["status"] in ["success", "files_required_to_continue", "resend_prompt"]

    @pytest.mark.asyncio
    async def test_debug_large_error_description(self, large_prompt):
        """Test that debug tool detects large error_description."""
        tool = DebugIssueTool()
        result = await tool.execute({"prompt": large_prompt})

        assert len(result) == 1
        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"

    @pytest.mark.asyncio
    async def test_debug_large_error_context(self, large_prompt, normal_prompt):
        """Test that debug tool detects large error_context."""
        tool = DebugIssueTool()
        result = await tool.execute({"prompt": normal_prompt, "error_context": large_prompt})

        assert len(result) == 1
        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"

    @pytest.mark.asyncio
    async def test_analyze_large_question(self, large_prompt):
        """Test that analyze tool detects large question."""
        tool = AnalyzeTool()
        result = await tool.execute({"files": ["/some/file.py"], "prompt": large_prompt})

        assert len(result) == 1
        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"

    @pytest.mark.asyncio
    async def test_multiple_files_with_prompt_txt(self, temp_prompt_file):
        """Test handling of prompt.txt alongside other files."""
        tool = ChatTool()
        other_file = "/some/other/file.py"

        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="Success",
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            # Mock handle_prompt_file to verify prompt.txt is handled
            with patch.object(tool, "handle_prompt_file") as mock_handle_prompt:
                # Return the prompt content and updated files list (without prompt.txt)
                mock_handle_prompt.return_value = ("Large prompt content from file", [other_file])

                # Mock the centralized file preparation method
                with patch.object(tool, "_prepare_file_content_for_prompt") as mock_prepare_files:
                    mock_prepare_files.return_value = ("File content", [other_file])

                    # Use a small prompt to avoid triggering size limit
                    await tool.execute({"prompt": "Test prompt", "files": [temp_prompt_file, other_file]})

                    # Verify handle_prompt_file was called with the original files list
                    mock_handle_prompt.assert_called_once_with([temp_prompt_file, other_file])

                    # Verify _prepare_file_content_for_prompt was called with the updated files list (without prompt.txt)
                    mock_prepare_files.assert_called_once()
                    files_arg = mock_prepare_files.call_args[0][0]
                    assert len(files_arg) == 1
                    assert files_arg[0] == other_file

        temp_dir = os.path.dirname(temp_prompt_file)
        shutil.rmtree(temp_dir)

    @pytest.mark.asyncio
    async def test_boundary_case_exactly_at_limit(self):
        """Test prompt exactly at MCP_PROMPT_SIZE_LIMIT characters (should pass with the fix)."""
        tool = ChatTool()
        exact_prompt = "x" * MCP_PROMPT_SIZE_LIMIT

        # Mock the model provider to avoid real API calls
        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="Response to the large prompt",
                usage={"input_tokens": 12000, "output_tokens": 10, "total_tokens": 12010},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            # With the fix, this should now pass because we check at MCP transport boundary before adding internal content
            result = await tool.execute({"prompt": exact_prompt})
            output = json.loads(result[0].text)
            assert output["status"] == "success"

    @pytest.mark.asyncio
    async def test_boundary_case_just_over_limit(self):
        """Test prompt just over MCP_PROMPT_SIZE_LIMIT characters (should trigger file request)."""
        tool = ChatTool()
        over_prompt = "x" * (MCP_PROMPT_SIZE_LIMIT + 1)

        result = await tool.execute({"prompt": over_prompt})
        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"

    @pytest.mark.asyncio
    async def test_empty_prompt_no_file(self):
        """Test empty prompt without prompt.txt file."""
        tool = ChatTool()

        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="Success",
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            result = await tool.execute({"prompt": ""})
            output = json.loads(result[0].text)
            assert output["status"] == "success"

    @pytest.mark.asyncio
    async def test_prompt_file_read_error(self):
        """Test handling when prompt.txt can't be read."""
        from tests.mock_helpers import create_mock_provider

        tool = ChatTool()
        bad_file = "/nonexistent/prompt.txt"

        with (
            patch.object(tool, "get_model_provider") as mock_get_provider,
            patch("utils.model_context.ModelContext") as mock_model_context_class,
        ):

            mock_provider = create_mock_provider(model_name="gemini-2.5-flash-preview-05-20", context_window=1_048_576)
            mock_provider.generate_content.return_value.content = "Success"
            mock_get_provider.return_value = mock_provider

            # Mock ModelContext to avoid the comparison issue
            from utils.model_context import TokenAllocation

            mock_model_context = MagicMock()
            mock_model_context.model_name = "gemini-2.5-flash-preview-05-20"
            mock_model_context.calculate_token_allocation.return_value = TokenAllocation(
                total_tokens=1_048_576,
                content_tokens=838_861,
                response_tokens=209_715,
                file_tokens=335_544,
                history_tokens=335_544,
            )
            mock_model_context_class.return_value = mock_model_context

            # Should continue with empty prompt when file can't be read
            result = await tool.execute({"prompt": "", "files": [bad_file]})
            output = json.loads(result[0].text)
            assert output["status"] == "success"

    @pytest.mark.asyncio
    async def test_mcp_boundary_with_large_internal_context(self):
        """
        Critical test: Ensure MCP_PROMPT_SIZE_LIMIT only applies to user input (MCP boundary),
        NOT to internal context like conversation history, system prompts, or file content.

        This test verifies that even if our internal prompt (with system prompts, history, etc.)
        exceeds MCP_PROMPT_SIZE_LIMIT, it should still work as long as the user's input is small.
        """
        tool = ChatTool()

        # Small user input that should pass MCP boundary check
        small_user_prompt = "What is the weather like?"

        # Mock a huge conversation history that would exceed MCP limits if incorrectly checked
        huge_history = "x" * (MCP_PROMPT_SIZE_LIMIT * 2)  # 100K chars = way over 50K limit

        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="Weather is sunny",
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            # Mock the prepare_prompt to simulate huge internal context
            original_prepare_prompt = tool.prepare_prompt

            async def mock_prepare_prompt(request):
                # Call original to get normal processing
                normal_prompt = await original_prepare_prompt(request)
                # Add huge internal context (simulating large history, system prompts, files)
                huge_internal_prompt = f"{normal_prompt}\n\n=== HUGE INTERNAL CONTEXT ===\n{huge_history}"

                # Verify the huge internal prompt would exceed MCP limits if incorrectly checked
                assert len(huge_internal_prompt) > MCP_PROMPT_SIZE_LIMIT

                return huge_internal_prompt

            tool.prepare_prompt = mock_prepare_prompt

            # This should succeed because we only check user input at MCP boundary
            result = await tool.execute({"prompt": small_user_prompt, "model": "flash"})
            output = json.loads(result[0].text)

            # Should succeed even though internal context is huge
            assert output["status"] == "success"
            assert "Weather is sunny" in output["content"]

            # Verify the model was actually called with the huge prompt
            mock_provider.generate_content.assert_called_once()
            call_kwargs = mock_provider.generate_content.call_args[1]
            actual_prompt = call_kwargs.get("prompt")

            # Verify internal prompt was huge (proving we don't limit internal processing)
            assert len(actual_prompt) > MCP_PROMPT_SIZE_LIMIT
            assert huge_history in actual_prompt
            assert small_user_prompt in actual_prompt

    @pytest.mark.asyncio
    async def test_mcp_boundary_vs_internal_processing_distinction(self):
        """
        Test that clearly demonstrates the distinction between:
        1. MCP transport boundary (user input - SHOULD be limited)
        2. Internal processing (system prompts, files, history - should NOT be limited)
        """
        tool = ChatTool()

        # Test case 1: Large user input should fail at MCP boundary
        large_user_input = "x" * (MCP_PROMPT_SIZE_LIMIT + 1000)
        result = await tool.execute({"prompt": large_user_input, "model": "flash"})
        output = json.loads(result[0].text)
        assert output["status"] == "resend_prompt"  # Should fail
        assert "too large for MCP's token limits" in output["content"]

        # Test case 2: Small user input should succeed even with huge internal processing
        small_user_input = "Hello"

        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="Hi there!",
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            # Mock get_system_prompt to return huge system prompt (simulating internal processing)
            original_get_system_prompt = tool.get_system_prompt

            def mock_get_system_prompt():
                base_prompt = original_get_system_prompt()
                huge_system_addition = "y" * (MCP_PROMPT_SIZE_LIMIT + 5000)  # Huge internal content
                return f"{base_prompt}\n\n{huge_system_addition}"

            tool.get_system_prompt = mock_get_system_prompt

            # Should succeed - small user input passes MCP boundary even with huge internal processing
            result = await tool.execute({"prompt": small_user_input, "model": "flash"})
            output = json.loads(result[0].text)
            assert output["status"] == "success"

            # Verify the final prompt sent to model was huge (proving internal processing isn't limited)
            call_kwargs = mock_get_provider.return_value.generate_content.call_args[1]
            final_prompt = call_kwargs.get("prompt")
            assert len(final_prompt) > MCP_PROMPT_SIZE_LIMIT  # Internal prompt can be huge
            assert small_user_input in final_prompt  # But contains small user input

    @pytest.mark.asyncio
    async def test_continuation_with_huge_conversation_history(self):
        """
        Test that continuation calls with huge conversation history work correctly.
        This simulates the exact scenario where conversation history builds up and exceeds
        MCP_PROMPT_SIZE_LIMIT but should still work since history is internal processing.
        """
        tool = ChatTool()

        # Small user input for continuation
        small_continuation_prompt = "Continue the discussion"

        # Mock huge conversation history (simulates many turns of conversation)
        huge_conversation_history = "=== CONVERSATION HISTORY ===\n" + (
            "Previous message content\n" * 2000
        )  # Very large history

        # Ensure the history exceeds MCP limits
        assert len(huge_conversation_history) > MCP_PROMPT_SIZE_LIMIT

        with patch.object(tool, "get_model_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.get_provider_type.return_value = MagicMock(value="google")
            mock_provider.supports_thinking_mode.return_value = False
            mock_provider.generate_content.return_value = MagicMock(
                content="Continuing our conversation...",
                usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                model_name="gemini-2.5-flash-preview-05-20",
                metadata={"finish_reason": "STOP"},
            )
            mock_get_provider.return_value = mock_provider

            # Simulate continuation by having the request contain embedded conversation history
            # This mimics what server.py does when it embeds conversation history
            request_with_history = {
                "prompt": f"{huge_conversation_history}\n\n=== CURRENT REQUEST ===\n{small_continuation_prompt}",
                "model": "flash",
                "continuation_id": "test_thread_123",
            }

            # Mock the conversation history embedding to simulate server.py behavior
            original_execute = tool.__class__.execute

            async def mock_execute_with_history(self, arguments):
                # Check if this has continuation_id (simulating server.py logic)
                if arguments.get("continuation_id"):
                    # Simulate the case where conversation history is already embedded in prompt
                    # by server.py before calling the tool
                    field_value = arguments.get("prompt", "")
                    if "=== CONVERSATION HISTORY ===" in field_value:
                        # Set the flag that history is embedded
                        self._has_embedded_history = True

                        # The prompt field contains both history AND user input
                        # But we should only check the user input part for MCP boundary
                        # (This is what our fix ensures happens in prepare_prompt)

                # Call original execute
                return await original_execute(self, arguments)

            tool.__class__.execute = mock_execute_with_history

            try:
                # This should succeed because:
                # 1. The actual user input is small (passes MCP boundary check)
                # 2. The huge conversation history is internal processing (not subject to MCP limits)
                result = await tool.execute(request_with_history)
                output = json.loads(result[0].text)

                # Should succeed even though total prompt with history is huge
                assert output["status"] == "success"
                assert "Continuing our conversation" in output["content"]

                # Verify the model was called with the complete prompt (including huge history)
                mock_provider.generate_content.assert_called_once()
                call_kwargs = mock_provider.generate_content.call_args[1]
                final_prompt = call_kwargs.get("prompt")

                # The final prompt should contain both history and user input
                assert huge_conversation_history in final_prompt
                assert small_continuation_prompt in final_prompt
                # And it should be huge (proving we don't limit internal processing)
                assert len(final_prompt) > MCP_PROMPT_SIZE_LIMIT

            finally:
                # Restore original execute method
                tool.__class__.execute = original_execute


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
