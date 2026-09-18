import asyncio

from tendies.models import User


async def test_concurrent_wallet_updates_are_serialized(db):
    async with db.session() as session:
        session.add(User(guild_id=1, user_id=1, wallet=100))

    async def spend():
        async with db.session() as session:
            user = await session.get(User, (1, 1))
            before = user.wallet
            await asyncio.sleep(0.01)
            if before >= 80:
                user.wallet -= 80
                return True
            return False

    assert sorted(await asyncio.gather(spend(), spend())) == [False, True]
    async with db.session() as session:
        assert (await session.get(User, (1, 1))).wallet == 20


async def test_failed_transaction_rolls_back_and_releases_lock(db):
    try:
        async with db.session() as session:
            session.add(User(guild_id=1, user_id=1, wallet=100))
            await session.flush()
            raise ValueError('abort')
    except ValueError:
        pass
    async with db.session() as session:
        assert await session.get(User, (1, 1)) is None
