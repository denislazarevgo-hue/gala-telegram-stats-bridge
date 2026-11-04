import asyncio
from types import SimpleNamespace

import app as bridge


def test_batch_returns_first_message_line_as_title(monkeypatch):
    class FakeClient:
        async def get_entity(self, channel):
            return SimpleNamespace(id=123)

        async def __call__(self, request):
            assert request.increment is False
            return SimpleNamespace(views=[SimpleNamespace(views=2704)])

        async def get_messages(self, entity, ids):
            return [SimpleNamespace(id=ids[0], message="\nВышибалы\nОзвучка: AniLibria", views=2704, reactions=None)]

    item = bridge.StatsItem(id=1, url="https://t.me/anilibria/23319", channel="anilibria", messageId=23319)
    rows = asyncio.run(bridge._collect_channel(FakeClient(), "anilibria", [item]))
    assert rows[0]["title"] == "Вышибалы"
    assert rows[0]["views"] == 2704
    assert rows[0]["status"] == "ok"
