import asyncio
from pathlib import Path

import asyncpg
import fakeredis
import pytest


class FakeClock:
    """Time that only moves when the code under test sleeps."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def redis():
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


MIGRATIONS = Path(__file__).parent.parent / "db" / "migrations"


@pytest.fixture(scope="session")
def pg_url():
    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("pgvector/pgvector:pg17", driver=None)
        container.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"docker unavailable: {e}")

    async def migrate():
        c = await asyncpg.connect(container.get_connection_url())
        await c.execute("CREATE ROLE gateway; CREATE ROLE orchestrator; "
                        "CREATE ROLE market_data; CREATE ROLE memory; CREATE ROLE broker;")
        for f in sorted(MIGRATIONS.glob("[0-9]*.sql")):
            await c.execute(f.read_text(encoding="utf-8"))
        await c.close()

    asyncio.run(migrate())
    yield container.get_connection_url()
    container.stop()
