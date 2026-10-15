import asyncio
import json
import uuid
from unittest.mock import Mock,patch
import pytest
import redis
import redis.asyncio as aioredis
from django.conf import settings
from core import cache,events
from core.models import Role,Rate

@pytest.mark.django_db
def test_rate_invalidation_excludes_unassigned_product(data):
    user=data['users'][Role.APP];rate=Rate.objects.first()
    event={'feature':'rates','franchise_id':None,'resource_id':str(rate.pk)}
    assert events.allowed(user,event)
    rate.product.franchises.remove(user.franchise)
    assert not events.allowed(user,event)

@pytest.mark.django_db
def test_live_events_are_filtered_for_each_connected_scope(data):
    app = data['users'][Role.APP]
    own = {'feature': 'customers', 'franchise_id': str(app.franchise_id), 'resource_id': 'own'}
    other_book = next(book for book in data['books'] if book.id != app.franchise_id)
    other = {**own, 'franchise_id': str(other_book.id), 'resource_id': 'other'}
    assert events.allowed(app, own)
    assert not events.allowed(app, other)

@pytest.mark.django_db
def test_cache_does_not_publish_stale_fill(data):
    client=Mock();client.get.return_value=None
    with patch('core.cache.redis.Redis.from_url',return_value=client),patch('core.cache.generation',side_effect=[1,2]):
        assert cache.cached_read(data['users'][Role.HQ],'example',{},lambda:{'value':1})=={'value':1}
    client.setex.assert_not_called()

@pytest.mark.django_db
def test_cache_loss_falls_back_and_scope_keys_differ(data):
    client=Mock();client.get.side_effect=redis.ConnectionError();client.setex.side_effect=redis.ConnectionError()
    with patch('core.cache.redis.Redis.from_url',return_value=client):
        for role in [Role.HQ,Role.APP]:assert cache.cached_read(data['users'][role],'example',{},lambda:{'safe':True})=={'safe':True}
    assert client.get.call_args_list[0]!=client.get.call_args_list[1]

@pytest.mark.django_db
def test_minimum_version_bypasses_older_disposable_cache(data):
    client=Mock();client.get.return_value=json.dumps({'stale':True})
    with patch('core.cache.redis.Redis.from_url',return_value=client),patch('core.cache.generation',return_value=3):
        value=cache.cached_read(data['users'][Role.HQ],'example',{},lambda:{'fresh':True},minimum_version=4)
    assert value=={'fresh':True}
    client.get.assert_not_called()
    client.setex.assert_not_called()

def test_independent_dispatchers_receive_same_stream_record():
    async def run():
        stream='scrappy:test:events:'+uuid.uuid4().hex
        client=aioredis.from_url(settings.REDIS_URL,decode_responses=True)
        first,second=events.Hub(),events.Hub()
        with patch('core.events.STREAM',stream):
            q1,q2=await asyncio.gather(first.add(),second.add())
            try:
                ident=await client.xadd(stream,{'data':json.dumps({'feature':'synthetic'})})
                results=await asyncio.wait_for(asyncio.gather(q1.get(),q2.get()),timeout=5)
                assert results[0][0]==results[1][0]==ident
                assert q1.maxsize==q2.maxsize==100
            finally:
                first.remove(q1);second.remove(q2)
                await asyncio.gather(first.task,second.task)
                await client.delete(stream);await client.aclose()
        assert not first.listeners and not second.listeners
    asyncio.run(run())

def test_hub_signals_resync_when_a_slow_client_queue_overflows():
    class FakeRedis:
        async def xrevrange(self, *args, **kwargs):
            return []
        async def xread(self, *args, **kwargs):
            await asyncio.sleep(0)
            return [('scrappy:test', [('2-0', {'data': json.dumps({'feature': 'rates'})})])]
        async def aclose(self):
            return None

    async def run():
        hub = events.Hub()
        with patch('core.events.aioredis.from_url', return_value=FakeRedis()):
            queue = await hub.add()
            for index in range(queue.maxsize):
                queue.put_nowait((f'{index}-0', {}))
            await asyncio.sleep(0.05)
            assert any(item[0] == 'reset' for item in list(queue._queue))
            hub.remove(queue)
            await asyncio.wait_for(hub.task, timeout=1)
            assert not hub.listeners

    asyncio.run(run())
