import json
import unittest
from unittest.mock import patch

import gateway


TOOL_READ = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ranges": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["path"],
        },
    },
}

TOOL_WRITE = {
    "type": "function",
    "function": {
        "name": "write",
        "description": "Write a file",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}


def decode_sse(payloads):
    events = []
    for payload in payloads:
        for line in payload.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[6:]))
    return events


class ToolCallParserTests(unittest.TestCase):
    def test_parses_nested_multiline_arguments_and_preserves_text(self):
        text = '''before
<function_call>
{
  "name": "read",
  "arguments": {
    "path": "C:/tmp/a.txt",
    "ranges": [1, 2],
    "meta": {"note": "line one
line two"}
  }
}
</function_call>
after'''

        calls, clean = gateway._parse_tool_calls(text)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read")
        arguments = json.loads(calls[0]["function"]["arguments"], strict=False)
        self.assertEqual(arguments["ranges"], [1, 2])
        self.assertEqual(arguments["meta"]["note"], "line one\nline two")
        self.assertIn("before", clean)
        self.assertIn("after", clean)
        self.assertNotIn("function_call", clean)

    def test_accepts_observed_malformed_closer(self):
        text = '''<function_call>
{"name":"read","arguments":{"path":"C:/Users/User/.omp/agent/config.yml"}}
</function_function_call>'''

        calls, clean = gateway._parse_tool_calls(text)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read")
        self.assertEqual(clean, "")

    def test_parses_unclosed_call_with_trailing_text(self):
        text = '<function_call>{"name":"write","arguments":{"path":"x","content":"y"}}\ntrailing text'

        calls, clean = gateway._parse_tool_calls(text)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "write")
        self.assertEqual(clean, "trailing text")

    def test_parses_multiple_calls_and_unknown_names(self):
        text = '''<function_call>{"name":"read","arguments":{"path":"a"}}</function_function_call>
between
<function_call>{"name":"custom_arbitrary_tool","arguments":{"nested":{"ok":true}}}</function_call>'''

        calls, clean = gateway._parse_tool_calls(text)

        self.assertEqual([c["function"]["name"] for c in calls], ["read", "custom_arbitrary_tool"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"nested": {"ok": True}})
        self.assertEqual(clean, "between")


class ToolMessageTests(unittest.TestCase):
    def test_follow_up_tool_result_does_not_inject_none_content(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "file body"},
        ]

        converted = gateway._build_conol_messages(messages, [TOOL_READ])
        joined = "\n".join(message["content"] for message in converted)

        self.assertNotIn("\nNone", joined)
        self.assertIn("file body", joined)
        self.assertIn('"name": "read"', joined)
        self.assertIn("[Tool result #call_1 (read)]", joined)

    def test_tool_choice_none_hides_tools_and_specific_choice_filters_schema(self):
        messages = [{"role": "user", "content": "hello"}]

        disabled = gateway._build_conol_messages(messages, [TOOL_READ, TOOL_WRITE], "none")
        selected = gateway._build_conol_messages(
            messages,
            [TOOL_READ, TOOL_WRITE],
            {"type": "function", "function": {"name": "write"}},
        )

        self.assertNotIn("<tools>", "\n".join(m["content"] for m in disabled))
        selected_prompt = "\n".join(m["content"] for m in selected)
        self.assertIn('"name":"write"', selected_prompt)
        self.assertNotIn('"name":"read"', selected_prompt)

    def test_tool_prompt_uses_only_final_protocol_tags(self):
        prompt = gateway._build_tool_system_prompt([TOOL_READ])

        self.assertIn("<final>", prompt)
        self.assertIn("</final>", prompt)
        self.assertNotIn("<answer_text>", prompt)


class ToolValidationTests(unittest.TestCase):
    def test_validates_arbitrary_nested_json_schema(self):
        tool = {
            "type": "function",
            "function": {
                "name": "custom_nested",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"type": "integer"}},
                        "options": {
                            "type": "object",
                            "properties": {"mode": {"enum": ["strict"]}},
                            "required": ["mode"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["items", "options"],
                    "additionalProperties": False,
                },
            },
        }
        valid = gateway._make_tool_call({
            "name": "custom_nested",
            "arguments": {"items": [1, 2], "options": {"mode": "strict"}},
        })
        invalid = gateway._make_tool_call({
            "name": "custom_nested",
            "arguments": {"items": [1, "bad"], "options": {"mode": "loose"}},
        })

        self.assertEqual(gateway._validate_tool_calls([valid], [tool]), [])
        errors = gateway._validate_tool_calls([invalid], [tool])
        self.assertTrue(any("items" in error for error in errors))
        self.assertTrue(any("options.mode" in error for error in errors))

    def test_rejects_unknown_tool_and_missing_required_arguments(self):
        unknown = gateway._make_tool_call({"name": "not_exposed", "arguments": {}})
        missing = gateway._make_tool_call({"name": "read", "arguments": {}})

        self.assertIn("not exposed", gateway._validate_tool_calls([unknown], [TOOL_READ])[0])
        self.assertIn("path", gateway._validate_tool_calls([missing], [TOOL_READ])[0])


class ToolRepairStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_retries_invalid_forced_call_before_emitting(self):
        attempts = []

        async def fake_stream(_cookies, messages, *_args, **_kwargs):
            attempts.append(messages)
            arguments = {} if len(attempts) == 1 else {"path": "C:/tmp/probe.txt"}
            yield '<function_call>' + json.dumps({"name": "read", "arguments": arguments}) + '</function_call>'

        account = gateway.Account("test@example.invalid", "test", "unused")
        payloads = []
        with patch.object(gateway, "_conol_stream_text", fake_stream):
            async for payload in gateway._sse_stream_realtime(
                account,
                [{"role": "user", "content": "read the probe"}],
                "claude-opus-5",
                "chatcmpl-repair",
                [TOOL_READ],
                {"type": "function", "function": {"name": "read"}},
            ):
                payloads.append(payload)

        events = decode_sse(payloads)
        tool_deltas = [
            delta
            for event in events
            for delta in event["choices"][0]["delta"].get("tool_calls", [])
        ]
        arguments = "".join(delta["function"].get("arguments", "") for delta in tool_deltas)

        self.assertEqual(len(attempts), 2)
        self.assertIn("required property", attempts[1][-1]["content"])
        self.assertEqual(json.loads(arguments), {"path": "C:/tmp/probe.txt"})
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")


class ToolStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_streams_plain_text_and_withholds_split_tool_markup(self):
        stream_filter = gateway._ToolCallStreamFilter()

        self.assertEqual(stream_filter.feed("plain text<fun"), "")
        self.assertEqual(stream_filter.feed('ction_call>{"name":"read","arguments":{"path":"a"}}'), "")
        self.assertEqual(stream_filter.feed("</function_function_call>after"), "")
        tail, calls = stream_filter.finish()

        self.assertEqual(tail, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read")

    async def test_filter_handles_every_opening_tag_split(self):
        text = 'prefix<function_call>{"name":"read","arguments":{"path":"a"}}</function_function_call>suffix'
        for split in range(1, len(text)):
            stream_filter = gateway._ToolCallStreamFilter()
            emitted = stream_filter.feed(text[:split]) + stream_filter.feed(text[split:])
            tail, calls = stream_filter.finish()
            self.assertEqual(emitted + tail, "", split)
            self.assertEqual(len(calls), 1, split)
            self.assertEqual(calls[0]["function"]["name"], "read", split)

    async def test_filter_handles_multiple_consecutive_calls(self):
        stream_filter = gateway._ToolCallStreamFilter()
        emitted = stream_filter.feed(
            'start<function_call>{"name":"read","arguments":{"path":"a"}}</function_call>'
            'middle<function_call>{"name":"write","arguments":{"path":"a","content":"b"}}'
            '</function_function_call>end'
        )
        tail, calls = stream_filter.finish()

        self.assertEqual(emitted + tail, "")
        self.assertEqual([call["function"]["name"] for call in calls], ["read", "write"])

    async def test_filter_discards_fabricated_text_when_tool_call_follows(self):
        stream_filter = gateway._ToolCallStreamFilter()
        emitted = stream_filter.feed("[Tool result: fake]\nREAD-TRACE-PASS")
        emitted += stream_filter.feed(
            '<function_call>{"name":"read","arguments":{"path":"a"}}</function_call>'
        )
        tail, calls = stream_filter.finish()

        self.assertEqual(emitted + tail, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read")

    async def test_filter_discards_unwrapped_fabricated_tool_result(self):
        stream_filter = gateway._ToolCallStreamFilter()

        self.assertEqual(stream_filter.feed("[Tool result: fake]\nREAD-TRACE-PASS"), "")
        tail, calls = stream_filter.finish()

        self.assertEqual(tail, "")
        self.assertEqual(calls, [])

    async def test_filter_streams_explicit_final_text_without_markup(self):
        stream_filter = gateway._ToolCallStreamFilter()
        emitted = stream_filter.feed("<fi")
        emitted += stream_filter.feed("nal>hello ")
        emitted += stream_filter.feed("world</fi")
        emitted += stream_filter.feed("nal>")
        tail, calls = stream_filter.finish()

        self.assertEqual(emitted + tail, "hello world")
        self.assertEqual(calls, [])

    async def test_filter_preserves_unmarked_plain_text_as_end_fallback(self):
        stream_filter = gateway._ToolCallStreamFilter()

        self.assertEqual(stream_filter.feed("plain fallback"), "")
        tail, calls = stream_filter.finish()

        self.assertEqual(tail, "plain fallback")
        self.assertEqual(calls, [])

    async def test_sse_reconstructs_generic_tool_call_and_following_text(self):
        async def fake_stream(*args, **kwargs):
            for part in [
                "before ",
                "<fun",
                'ction_call>{"name":"custom_arbitrary_tool","arguments":{"items":[1,2]}',
                "}</function_call>",
                " after",
            ]:
                yield part

        account = gateway.Account("test@example.invalid", "test", "unused")
        payloads = []
        with patch.object(gateway, "_conol_stream_text", fake_stream):
            async for payload in gateway._sse_stream_realtime(
                account,
                [{"role": "user", "content": "use the tool"}],
                "claude-opus-5",
                "chatcmpl-test",
                [{
                    "type": "function",
                    "function": {
                        "name": "custom_arbitrary_tool",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "items": {"type": "array", "items": {"type": "integer"}},
                            },
                            "required": ["items"],
                        },
                    },
                }],
                "auto",
            ):
                payloads.append(payload)

        events = decode_sse(payloads)
        self.assertEqual(events[0]["choices"][0]["delta"]["role"], "assistant")
        content = "".join(
            event["choices"][0]["delta"].get("content", "")
            for event in events
        )
        self.assertEqual(content, "")

        tool_deltas = [
            delta
            for event in events
            for delta in event["choices"][0]["delta"].get("tool_calls", [])
        ]
        self.assertEqual(tool_deltas[0]["index"], 0)
        self.assertEqual(tool_deltas[0]["function"]["name"], "custom_arbitrary_tool")
        arguments = "".join(delta["function"].get("arguments", "") for delta in tool_deltas)
        self.assertEqual(json.loads(arguments), {"items": [1, 2]})
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")


if __name__ == "__main__":
    unittest.main()
