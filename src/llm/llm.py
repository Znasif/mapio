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
        self.compact_history_enabled = (
            os.environ.get("LLM_COMPACT_HISTORY", "1" if base_url else "0") == "1"
        )

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

    def ask(self, question: str, position: Optional[PositionInfo]) -> Optional[str]:
        if self.compact_history_enabled:
            self.__compact_history()
        new_message = self.prompt_formatter.get_user_message(question, position)
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

                if not self.running:
                    break
                print("Got API response")

                response_message = response.choices[0].message
                if (
                    response_message.content is not None
                    and len(response_message.content) > 0
                ):
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
        content=msg.content,
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
