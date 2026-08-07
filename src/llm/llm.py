import os
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
    DEFAULT_TEMPERATURE = 0.0

    QUESTION_MARKER = "###Question###"
    INSTRUCTIONS_MARKER = "###Instructions###"

    def __init__(
        self,
        prompt_file: str,
        context: Dict[str, str],
        temperature: float = DEFAULT_TEMPERATURE,
        formatter: Optional[PromptFormatter] = None,
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
        self.history: List[ChatCompletionMessageParam] = list()
        self.history.append(self.prompt_formatter.get_main_prompt(self.context))
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
        self.history.append(self.prompt_formatter.get_main_prompt(self.context))

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
                    max_tokens=LLM.MAX_TOKENS,
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
