from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from character import Character, Memory, Persona
from character.agent import REFUSAL_REPLY

PERSONA_PATH = Path(__file__).resolve().parent.parent / "personas" / "asya.json"


class Block(SimpleNamespace):
    def to_dict(self):
        return {k: v for k, v in vars(self).items()}


def text(t):
    return Block(type="text", text=t)


def tool_use(fact, id_="tu_1"):
    return Block(type="tool_use", id=id_, name="remember_fact", input={"fact": fact})


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=block.text)
                )

    def get_final_message(self):
        return self.message


class FakeClient:
    """Отдаёт заранее заданные ответы и запоминает параметры запросов."""

    def __init__(self, *messages):
        self.messages_queue = list(messages)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.messages_queue.pop(0))


def msg(stop_reason, *content):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content))


def make(tmp_path, *messages):
    client = FakeClient(*messages)
    memory = Memory(tmp_path / "mem.json")
    return Character(Persona.load(PERSONA_PATH), memory, client=client), client, memory


def test_persona_prompt_contains_character():
    persona = Persona.load(PERSONA_PATH)
    prompt = persona.system_prompt()
    assert persona.name in prompt
    assert "remember_fact" in prompt


def test_simple_reply_streams_and_persists(tmp_path):
    character, client, memory = make(tmp_path, msg("end_turn", text("Привет!")))
    chunks = []
    assert character.reply("Хай", on_text=chunks.append) == "Привет!"
    assert chunks == ["Привет!"]
    assert memory.history[-1] == {"role": "assistant", "content": [{"type": "text", "text": "Привет!"}]}
    assert Memory(tmp_path / "mem.json").history == memory.history
    req = client.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["fallbacks"] == "default"


def test_remember_fact_tool_loop(tmp_path):
    character, client, memory = make(
        tmp_path,
        msg("tool_use", tool_use("Зовут Дима")),
        msg("end_turn", text("Приятно познакомиться, Дима!")),
    )
    reply = character.reply("Я Дима")
    assert reply == "Приятно познакомиться, Дима!"
    assert memory.facts == ["Зовут Дима"]
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1" and not tool_result["is_error"]
    # Новый факт виден модели в следующем запросе.
    character.client = FakeClient(msg("end_turn", text("ок")))
    character.reply("Как дела?")
    assert "Зовут Дима" in character.client.requests[0]["system"][1]["text"]


def test_invalid_tool_input_is_reported(tmp_path):
    bad = Block(type="tool_use", id="tu_x", name="remember_fact", input={"fact": ""})
    character, client, memory = make(tmp_path, msg("tool_use", bad), msg("end_turn", text("ок")))
    character.reply("…")
    assert memory.facts == []
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_rolls_back_turn(tmp_path):
    character, _, memory = make(tmp_path, msg("refusal"))
    chunks = []
    assert character.reply("плохой запрос", on_text=chunks.append) == REFUSAL_REPLY
    assert memory.history == []
    assert chunks == [REFUSAL_REPLY]


def test_api_error_rolls_back_turn(tmp_path):
    character, client, memory = make(tmp_path)  # пустая очередь → IndexError
    try:
        character.reply("привет")
    except IndexError:
        pass
    assert memory.history == []


def test_fallback_echo_drops_pre_switch_thinking(tmp_path):
    content = [
        Block(type="thinking", thinking="", signature="s"),
        Block(type="fallback", **{"from": {"model": "a"}, "to": {"model": "b"}}),
        text("Ответ"),
    ]
    character, _, memory = make(tmp_path, msg("end_turn", *content))
    character.reply("?")
    assert memory.history[-1]["content"] == [{"type": "text", "text": "Ответ"}]


def test_memory_dedupes_facts(tmp_path):
    memory = Memory()
    assert memory.add_fact("Любит  кофе")
    assert not memory.add_fact("любит кофе")
    assert memory.facts == ["Любит кофе"]
