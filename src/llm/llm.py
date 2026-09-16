import os
import time
from typing import Dict, Iterable, List, Optional

from openai import OpenAI, OpenAIError
from openai.types import CompletionUsage
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionMessageToolCall,
    ChatCompletionMessageToolCallParam,
)
from openai.types.chat.chat_completion_message_tool_call_param import (
    Function as FunctionParam,
)

from src.graph import Graph
from src.modules_repository import Module
from src.position import PositionInfo

from .prompt_formatter import PromptFormatter


class LLM(Module):
    MODEL = "gpt-4o-2024-08-06"
    MAX_TOKENS = 2000
    LOCAL_MAX_TOKENS = 768
    # Headroom for what the message list does not count: llama.cpp renders the
    # tool definitions into the prompt itself, and they are not free.
    CTX_MARGIN_TOKENS = 1024
    DEFAULT_TEMPERATURE = 0.0

    QUESTION_MARKER = "###Question###"
    INSTRUCTIONS_MARKER = "###Instructions###"

    def __init__(
        self,
        prompt_file: str,
        context: Dict[str, str],
        temperature: float = DEFAULT_TEMPERATURE,
        formatter: Optional[PromptFormatter] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        super().__init__()

        self.temperature = temperature

        # LLM_BASE_URL switches to a local OpenAI-compatible server (llama.cpp
        # router). LLM_MODEL selects the tier ("l3"). Unset -> OpenAI, as before.
        base_url = os.environ.get("LLM_BASE_URL")
        if base_url:
            self.client = OpenAI(
                base_url=base_url,
                api_key=os.environ.get("OPENAI_API_KEY") or "local",
            )
        else:
            self.client = OpenAI()
        self.model = os.environ.get("LLM_MODEL") or LLM.MODEL

        # 2000 was sized for a 128K window. Locally the whole request has to fit
        # in 8192 alongside a ~6.5K prompt, so reserving 2000 for output is
        # arithmetic the window cannot satisfy -- the server's own cap is what
        # holds today, silently. 768 is 1.4x the longest completion observed
        # across every curated run (551 tokens on NY-S2; p99 518, mean 59).
        default_max = LLM.LOCAL_MAX_TOKENS if base_url else LLM.MAX_TOKENS
        self.max_tokens = max_tokens or int(
            os.environ.get("LLM_MAX_TOKENS", default_max)
        )

        # Gemma emits chain-of-thought by default, and with `tools` present the
        # CoT eats the whole token budget before any call is emitted;
        # reasoning_budget is silently ignored once tools are present. The chat
        # template kwarg is the only reliable off-switch (design doc §8.0).
        self.extra_body: Dict[str, object] = (
            {"chat_template_kwargs": {"enable_thinking": False}} if base_url else {}
        )

        # Local serving fits an 8192-token window (design doc §8), which two
        # MapIO habits overflow: re-injecting the full instruction block after
        # every tool round, and keeping every past turn's per-question
        # scaffolding verbatim. Both default off/compacted in local mode and
        # unchanged against OpenAI; override with LLM_REINJECT_INSTRUCTIONS=1 /
        # LLM_COMPACT_HISTORY=0 to measure the original behavior.
        self.reinject_instructions = (
            os.environ.get("LLM_REINJECT_INSTRUCTIONS", "0" if base_url else "1") == "1"
        )
        # "1" compacts every turn, "0" never, "auto" only when the next request
        # would not otherwise fit.
        #
        # Compacting every turn rewrites the middle of the request, so llama.cpp
        # can only reuse the frozen system prompt: measured cache_n pinned at
        # 3688 from turn 3 on, while prompt_n grew 1461 -> 3295. Never
        # compacting restores reuse (cache_n climbed to 13958) but the prompt
        # grows too, so prefill only fell 102s -> 88s across 7 turns -- and
        # decode doubled, 62s -> 127s, because the model answers at much greater
        # length when it can see every past turn's scaffolding. Net 1.31x
        # slower, and turn 9 exceeded a 16384 window outright.
        #
        # "auto" keeps the prefix while it is free and pays for it only at the
        # window edge. Not the default: the measured win is small and the
        # verbosity effect above is a quality change that wants grading first.
        mode = os.environ.get("LLM_COMPACT_HISTORY", "1" if base_url else "0").lower()
        self.compact_history_mode = mode if mode in ("0", "1", "auto") else "1"
        self.compact_history_enabled = self.compact_history_mode != "0"

        # The window "auto" is fitting into. The client cannot ask the server
        # for it, so it is stated here and must match the tier's ctx-size.
        default_ctx = 32768 if (base_url and formatter is None) else 8192
        self.ctx_size = int(os.environ.get("LLM_CTX_SIZE", default_ctx))

        # Characters per token, re-derived from every response: the server
        # reports exactly how many tokens the messages we just sent became, so
        # the ratio calibrates itself to this prompt and this tokeniser instead
        # of relying on a chars/4 rule of thumb.
        self._chars_per_token: float = 4.0

        self.prompt_formatter = formatter or PromptFormatter(prompt_file, self.__graph)

        self.context = context

        # get_main_prompt() stamps datetime.now() into the MIDDLE of the system
        # message, so rebuilding it makes each new prompt differ from the last
        # mid-prefix -- and Gemma 4's shared KV cannot prefix-reuse around a
        # mid-prompt difference (ggml-org/llama.cpp#21468). Measured on the
        # benchmark: 38.8s -> 14.3s on the first turn, 15.5s -> 6.2s on the
        # second, purely from building this message once and keeping it.
        # Frozen only when serving locally; against OpenAI reset() rebuilds it
        # exactly as before.
        self.freeze_system_prompt = bool(base_url)
        self.system_message = self.prompt_formatter.get_main_prompt(self.context)

        self.history: List[ChatCompletionMessageParam] = [self.system_message]
        self.usage: List[Optional[CompletionUsage]] = list()

        # llama.cpp returns a `timings` object alongside `usage`, splitting each
        # round into prompt_ms (prefill) and predicted_ms (decode), with cache_n
        # for how much of the prompt the prefix cache served. Token counts alone
        # cannot tell those apart, and which one dominates decides whether the
        # lever is prompt shape or decode speed. Appended in step with usage, so
        # index i of one matches index i of the other. Always None against
        # OpenAI, which sends no such field.
        self.timings: List[Optional[Dict[str, object]]] = list()

        self.running = False

    @property
    def __graph(self) -> Graph:
        return self._repository[Graph]

    def is_waiting_for_response(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False

    def reset(self) -> None:
        self.running = False
        self.history.clear()
        if not self.freeze_system_prompt:
            self.system_message = self.prompt_formatter.get_main_prompt(self.context)
        self.history.append(self.system_message)

    def warm_up(self) -> Optional[float]:
        """Prime the server's cache with the system prefix. Returns seconds.

        Without this the first question pays the whole prefill -- 38.8s against
        14.3s warm, on the same turn. `tools` is load-bearing: llama.cpp renders
        the tool definitions into the prompt, so a warm-up that omits them
        shares no prefix with a real turn and caches nothing (measured: 4205
        tokens primed, cached=0 on the turn that followed).

        Safe from a background thread -- it appends nothing to history -- and
        safe to skip, since failing only costs latency on the first question.
        """

        start = time.time()
        try:
            self.client.chat.completions.create(
                model=self.model,
                messages=[
                    self.system_message,
                    {"role": "user", "content": "###Question###\n\nready\n"},
                ],
                max_tokens=1,
                temperature=self.temperature,
                tools=self.prompt_formatter.get_tool_calls(),
                extra_body=self.extra_body,
            )
        except OpenAIError as e:
            print(f"Prefix warm-up failed; the first question will be slower: {e}")
            return None

        return round(time.time() - start, 2)

    def __compact_history(self) -> None:
        """Cut PAST turns down to their question core.

        Each user turn carries per-question scaffolding — retrieved candidates,
        the position update, a restated instruction block — that only matters
        for the round it was sent in. Kept verbatim, a multi-turn session blows
        the 8192-token window by turn 3 (measured: 18.5K tokens at turn 9 on
        the Detroit study session). Past user turns shrink to their
        ###Question### section and past instruction re-injections are dropped;
        assistant and tool messages stay, because they carry the facts that
        follow-ups refer back to (bookmarks, chosen restaurants, distances).
        This forfeits llama.cpp prefix-cache reuse across turns (§6.1), but
        fitting the window at all comes first.
        """
        compacted: List[ChatCompletionMessageParam] = []
        for msg in self.history:
            content = msg.get("content")
            if msg.get("role") == "user" and isinstance(content, str):
                if content.lstrip().startswith(LLM.INSTRUCTIONS_MARKER):
                    continue
                if LLM.QUESTION_MARKER in content:
                    core = content.split(LLM.QUESTION_MARKER, 1)[1]
                    core = core.split(LLM.INSTRUCTIONS_MARKER, 1)[0]
                    msg = dict(msg, content=LLM.QUESTION_MARKER + core.rstrip() + "\n")
            compacted.append(msg)
        self.history[:] = compacted

    @staticmethod
    def __message_chars(messages: List[ChatCompletionMessageParam]) -> int:
        """Rough size of a message list, tool-call arguments included."""
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
            for call in (msg.get("tool_calls") or []):
                function = call.get("function") or {}
                total += len(str(function.get("name", "")))
                total += len(str(function.get("arguments", "")))
        return total

    def __would_overflow(self, new_message: ChatCompletionMessageParam) -> bool:
        """Would sending history + new_message leave no room to answer?

        The reservation is max_tokens for the completion plus a margin for the
        tool definitions, which are rendered into the prompt by llama.cpp and
        so never appear in the message list this counts.
        """
        chars = LLM.__message_chars(self.history) + LLM.__message_chars([new_message])
        estimated = chars / max(self._chars_per_token, 1.0)
        return estimated + self.max_tokens + LLM.CTX_MARGIN_TOKENS > self.ctx_size

    def ask(self, question: str, position: Optional[PositionInfo]) -> Optional[str]:
        new_message = self.prompt_formatter.get_user_message(question, position)

        if self.compact_history_mode == "1":
            self.__compact_history()
        elif self.compact_history_mode == "auto" and self.__would_overflow(new_message):
            # Late rather than never: everything up to this turn stays byte
            # identical for as long as it fits, so llama.cpp keeps reusing it.
            print("Compacting history to fit the context window.")
            self.__compact_history()

        self.history.append(new_message)
        self.running = True

        output = ""

        try:
            while self.running:
                print("Sending API request...")

                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=self.history,
                    tools=self.prompt_formatter.get_tool_calls(),
                    extra_body=self.extra_body,
                )
                self.usage.append(response.usage)
                extra = getattr(response, "model_extra", None) or {}
                self.timings.append(extra.get("timings"))

                # Recalibrate the estimator against what the server actually
                # tokenised. Only when the count is plausible: a failed or empty
                # round would otherwise poison the ratio and make "auto" compact
                # far too early or far too late.
                if response.usage is not None and response.usage.prompt_tokens > 0:
                    sent = LLM.__message_chars(self.history)
                    if sent > 0:
                        self._chars_per_token = sent / response.usage.prompt_tokens

                if not self.running:
                    break
                print("Got API response")

                response_message = response.choices[0].message
                if (
                    response_message.content is not None
                    and len(response_message.content) > 0
                ):
                    # Every round's text is concatenated, and Gemma frequently
                    # emits its whole answer alongside the tool call and then
                    # again after seeing the tool result -- so the user hears
                    # the same sentences twice. Skip a block already present
                    # rather than keeping only the last round, which would drop
                    # the answer entirely when a model says its piece on the
                    # tool round and nothing after.
                    if response_message.content.strip() not in output:
                        output += response_message.content + "\n"

                self.history.append(convert_assistant_message(response_message))

                if (
                    response_message.tool_calls is not None
                    and len(response_message.tool_calls) > 0
                ):
                    for tool_call in response_message.tool_calls:
                        self.history.append(
                            self.prompt_formatter.handle_tool_call(tool_call)
                        )
                    if self.reinject_instructions:
                        self.history.append(
                            self.prompt_formatter.get_instructions_prompt()
                        )

                else:
                    break

        except OpenAIError as e:
            print(f"An error occurred: {e}")
            return None

        finally:
            self.running = False

        return output[:-1]

    def save_chat(self, filename: str) -> None:
        msgs: List[str] = list()
        assistants_cnt = 0

        for msg in self.history:
            if "content" in msg and msg["content"] is not None and msg["content"] != "":
                msgs.append(f"{msg['role']}:\n{msg['content']}")

            if "tool_calls" in msg and msg.get("tool_calls", None) is not None:
                msgs.append(f"{msg['role']}:")
                for tool_call in msg.get("tool_calls", list()):
                    msgs.append(
                        f"Tool call: {tool_call['function']['name']}\n"
                        f"Parameters: {tool_call['function']['arguments']}"
                    )

            if msg["role"] == "assistant":
                usage = self.usage[assistants_cnt]
                if usage is not None:
                    msgs.append(f"Usage: {str(usage.total_tokens)} tokens")
                assistants_cnt += 1

        with open(filename, "w") as f:
            f.write("\n\n".join(msgs))


def convert_assistant_message(msg: ChatCompletionMessage) -> ChatCompletionMessageParam:
    return ChatCompletionAssistantMessageParam(
        role="assistant",
        content=msg.content or "",
        tool_calls=convert_tool_calls(msg.tool_calls),
    )


def convert_tool_calls(
    tool_calls: Optional[List[ChatCompletionMessageToolCall]],
) -> Optional[Iterable[ChatCompletionMessageToolCallParam]]:
    if tool_calls is None:
        return None

    return [
        ChatCompletionMessageToolCallParam(
            id=tool_call.id,
            function=FunctionParam(
                arguments=tool_call.function.arguments, name=tool_call.function.name
            ),
            type=tool_call.type,
        )
        for tool_call in tool_calls
    ]
