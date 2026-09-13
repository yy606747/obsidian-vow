"""Companionship protocol tests; no model, network or real phone calls."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import aiosqlite
import pytest

from app.look_at_me.repository import LookAtMeRepository, init_tables
from app.look_at_me.service import parse_expression


class Fixture:
    def __init__(self, path):
        self.path, self.now = path, 1789084800.0
        self.repo = LookAtMeRepository(self.db,lambda:self.now)

    @asynccontextmanager
    async def db(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory=aiosqlite.Row
            yield db

    async def create(self, source='turn1'):
        return await self.repo.create(conv_id='today',origin_conv_id='source',model_key='model',device_id='phone',
            source_turn=source,source_intent='intent1',expression='想和小栀腻歪一会儿',duration_seconds=20)

    def event(self, r, seq, kind, at, **payload):
        return dict(event_id=f'{r["id"]}:{seq}',sequence=seq,type=kind,occurred_at=at,payload=payload)

    async def send(self,r,*events,device='phone'):
        from app.daily_chat.service import calendar_day
        self.now=max(self.now,*(e['occurred_at'] for e in events))
        return await self.repo.events(r['id'],device,list(events),day_of=lambda at:calendar_day(at,'UTC')[0],current_conv='new_today')

    async def session(self, *, outcome='completed'):
        r=await self.create()
        t=self.now
        return await self.send(r,self.event(r,1,'offered',t+1),self.event(r,2,'accepted',t+2),
            self.event(r,3,'started',t+3,request_id=r['id'],conv_id='today'),
            self.event(r,4,'finished',t+23,actual_ms=20000,outcome=outcome,reason='manual' if outcome=='ended_early' else 'deadline'))

    async def claim(self,turn,**kwargs):
        return await self.repo.claim(turn_id=turn,user_name='小栀',ai_name='阿澈',timezone='UTC',**kwargs)


def run(tmp_path, case):
    async def main():
        f=Fixture(tmp_path/'look.db')
        async with f.db() as db:
            await init_tables(db)
            await db.commit()
        await case(f)
    asyncio.run(main())


def test_source_dedup_busy_and_device_ownership(tmp_path):
    async def case(f):
        r=await f.create()
        assert (await f.create())['id']==r['id']
        with pytest.raises(ValueError,match='device_busy'): await f.create('turn2')
        assert await f.repo.pending('tablet') is None
        with pytest.raises(ValueError,match='device_mismatch'):
            await f.send(r,f.event(r,1,'offered',f.now+1),device='tablet')
    run(tmp_path,case)


def test_ordered_offline_receipts_preserve_acceptance_and_single_session(tmp_path):
    async def case(f):
        r=await f.create();t=f.now
        e=[f.event(r,1,'offered',t+1),f.event(r,2,'accepted',t+2),
           f.event(r,3,'started',t+3,request_id=r['id'],conv_id='today'),
           f.event(r,4,'finished',t+23,actual_ms=20000,outcome='completed')]
        late=await f.send(r,e[2],e[3]);assert late['status']=='queued'
        final=await f.send(r,e[0],e[1]);assert final['status']=='finished'
        assert final['decision']=='accepted' and final['outcome']=='completed'
        assert final['actual_ms']==20000 and final['conv_id']=='new_today'
        assert final['origin_conv_id']=='source'
        assert await f.send(r,*e)==final
        with pytest.raises(ValueError,match='event_conflict'):
            await f.send(r,{**e[0],'type':'accepted'})
    run(tmp_path,case)


@pytest.mark.parametrize('kind,reason',[('rejected','开会中'),('rejected',''),('timed_out','')])
def test_decisions_and_day_lifetime(tmp_path,kind,reason):
    async def case(f):
        r=await f.create();t=f.now
        r=await f.send(r,f.event(r,1,'offered',t+1),f.event(r,2,kind,t+(121 if kind=='timed_out' else 2),reason=reason))
        assert r['outcome'] is None and r['decision']==kind
        block=(await f.claim('one'))['block']
        assert ('未收到小栀回应' if kind=='timed_out' else '小栀拒绝') in block
        if reason: assert reason in block
        await f.repo.settle({'turn_id':'one'})
        f.now+=86400
        assert not (await f.claim('tomorrow'))['block']
    run(tmp_path,case)


def test_notification_expiry_is_not_a_response(tmp_path):
    async def case(f):
        r=await f.create();t=f.now
        r=await f.send(r,f.event(r,1,'notified',t+1),f.event(r,2,'expired',t+121))
        assert r['decision'] is None and r['outcome'] is None
        assert '通知未打开即过期' in (await f.claim('one'))['block']
        r=await f.send(r,f.event(r,3,'accepted',t+122))
        assert r['status']=='finished' and r['decision'] is None
    run(tmp_path,case)


def test_showing_late_gets_its_own_deadline_but_reopening_does_not(tmp_path):
    async def case(f):
        r=await f.create();t=f.now
        r=await f.send(r,f.event(r,1,'notified',t+2),f.event(r,2,'offered',t+119))
        assert r['choice_deadline']==t+239
        r=await f.send(r,f.event(r,3,'offered',t+150))
        assert r['choice_deadline']==t+239
        r=await f.send(r,f.event(r,4,'accepted',t+240))
        assert r['decision'] is None
    run(tmp_path,case)


def test_start_requires_acceptance_and_handshake(tmp_path):
    async def case(f):
        r=await f.create();t=f.now
        r=await f.send(r,f.event(r,1,'started',t+1,request_id=r['id'],conv_id='today'))
        assert r['status']=='queued'
        r=await f.send(r,f.event(r,2,'offered',t+2),f.event(r,3,'accepted',t+3),
                       f.event(r,4,'started',t+4,request_id='wrong',conv_id='today'))
        assert r['status']=='accepted'
        r=await f.send(r,f.event(r,5,'start_failed',t+15,reason='start_failed'))
        assert r['decision']=='accepted' and r['outcome'] is None
        assert '已经同意' in (await f.claim('next'))['block']
    run(tmp_path,case)


def test_three_successful_turns_replay_and_failure_release(tmp_path):
    async def case(f):
        await f.session()
        one=await f.claim('one');assert '正常结束' in one['block']
        await f.repo.settle(one,message_id='m1',success=True)
        replay=await f.claim('regenerated',replay_from='m1')
        assert replay['block']==one['block']
        await f.repo.settle(replay,message_id='m2',success=True)
        two=await f.claim('two');await f.repo.settle(two,success=False)
        retry=await f.claim('retry');assert retry['block']
        await f.repo.settle(retry,message_id='m3',success=True)
        three=await f.claim('three');assert three['block']
        await f.repo.settle(three,message_id='m4',success=True)
        assert not (await f.claim('four'))['block']
        replay=await f.claim('broken',replay_from='m2')
        await f.repo.settle(replay,success=False)
        assert (await f.claim('after-regeneration-failure'))['block']
    run(tmp_path,case)


def test_active_fact_does_not_spend_the_finished_feedback_budget(tmp_path):
    async def case(f):
        r=await f.create();t=f.now
        r=await f.send(r,f.event(r,1,'offered',t+1),f.event(r,2,'accepted',t+2),f.event(r,3,'started',t+3,request_id=r['id'],conv_id='today'))
        meta=await f.claim('during');assert '约好的腻歪时间' in meta['block']
        await f.repo.settle(meta,message_id='during',success=True)
        await f.send(r,f.event(r,4,'finished',t+23,outcome='completed',actual_ms=20000))
        for i in range(3):
            meta=await f.claim(str(i));assert '正常结束' in meta['block']
            await f.repo.settle(meta,message_id=str(i),success=True)
        assert not (await f.claim('four'))['block']
    run(tmp_path,case)


def test_core_dispatch_and_memory_retry_are_idempotent(tmp_path,monkeypatch):
    async def case(f):
        from app.look_at_me import trigger
        from app.daily_chat.service import daily_chat
        from app.memory_v2.service import memory_service
        import opportunity
        calls=[];memories={}
        async def ensure(): return daily_chat.state
        async def core(**kw): calls.append(kw);return {'status':'acted'}
        async def memory(content,*args,**kwargs):
            key=kwargs['idempotency_key'];memories.setdefault(key,content)
            return {'id':key}
        monkeypatch.setattr(trigger,'repository',f.repo)
        monkeypatch.setattr(daily_chat,'ensure_current',ensure)
        monkeypatch.setattr(daily_chat,'state',{'enabled':True,'active_conv_id':'today'})
        monkeypatch.setattr(opportunity,'run_autonomous_turn',core)
        monkeypatch.setattr(memory_service,'create_memory',memory)
        monkeypatch.setattr(trigger.time,'time',lambda:f.now)
        r=await f.create();t=f.now
        r=await f.send(r,f.event(r,1,'offered',t+1),f.event(r,2,'accepted',t+2),f.event(r,3,'started',t+3,request_id=r['id'],conv_id='today'))
        await trigger.run_pending();await trigger.run_pending()
        assert len(calls)==1 and calls[0]['kind']=='look_at_me_accept'
        await f.send(r,f.event(r,4,'finished',t+15,outcome='ended_early',actual_ms=12000,reason='screen_off'))
        await trigger.run_pending()
        async with f.db() as db:
            await db.execute("UPDATE look_at_me_requests SET memory_state='pending'")
            await db.commit()
        await trigger.run_pending();assert len(memories)==1
        assert '提前结束' in next(iter(memories.values()))
    run(tmp_path,case)


def test_late_finished_receipts_do_not_trigger_an_acceptance_turn(tmp_path,monkeypatch):
    async def case(f):
        from app.look_at_me import trigger
        from app.daily_chat.service import daily_chat
        from app.memory_v2.service import memory_service
        import opportunity
        async def ensure(): return {}
        async def forbidden(**kwargs): raise AssertionError('late Core call')
        async def memory(*args,**kwargs):return {'id':'stable'}
        monkeypatch.setattr(trigger,'repository',f.repo)
        monkeypatch.setattr(daily_chat,'ensure_current',ensure)
        monkeypatch.setattr(opportunity,'run_autonomous_turn',forbidden)
        monkeypatch.setattr(memory_service,'create_memory',memory)
        await f.session();await trigger.run_pending()
    run(tmp_path,case)


def test_parser_free_brackets_and_streaming_boundaries():
    from app.tools.parser import parse_tool_intents
    from app.chat.control_syntax import canonicalize_control_markers, ControlMarkerStreamFilter
    raw='抱抱【陪我：300｜想一起举着[OK]牌子｜聊会儿】晚点见'
    intents=parse_tool_intents(canonicalize_control_markers(raw))
    assert len(intents)==1 and intents[0].tool_name=='mobile.look_at_me'
    assert intents[0].arguments=={'duration_seconds':300,'expression':'想一起举着[OK]牌子｜聊会儿'}
    for split in range(len(raw)+1):
        stream=ControlMarkerStreamFilter()
        assert stream.feed(raw[:split])+stream.feed(raw[split:])+stream.flush()=='抱抱晚点见'
    assert len(parse_tool_intents('【陪我：20｜正文[TOY:STOP]】'))==1
    assert not parse_tool_intents(raw,enabled_commands={'sticker_send'})


@pytest.mark.parametrize('raw',['0|一起','-2|一起','1.5|一起','300|','abc|一起','9999999999999999|一起'])
def test_invalid_duration_is_rejected_without_silent_clamping(raw):
    assert 'parse_error' in parse_expression(raw)


@pytest.mark.parametrize('text,allowed,expect_error',[
    ('【陪我：20｜一起待会儿】',True,False),
    ('抱抱【陪我：20｜一起待会儿】',True,False),
    ('【陪我：0｜一起待会儿】',True,True),
    ('【陪我：20｜一起待会儿】',False,True),
    ('【陪我：20｜没闭合',True,True),
])
def test_chat_invitation_delivery_does_not_insert_an_empty_assistant(monkeypatch,text,allowed,expect_error):
    from app.chat import streaming
    from app.look_at_me import service
    from app.tools.schemas import ToolContext
    from test_chat_streaming_sse import _patch_streaming_io,_prompt_meta,_collect_sse
    executed,broadcasts=_patch_streaming_io(monkeypatch)
    async def core(*args):
        for char in text: yield char
    async def execute(intent,context,**kwargs):
        if intent.arguments.get('parse_error'): raise ValueError('invalid_duration')
        return {'ok':True,'id':'test_request','status':'queued'}
    monkeypatch.setattr(streaming,'stream_ai',core)
    monkeypatch.setattr(service,'execute_invitation',execute)
    monkeypatch.setattr(streaming,'_tool_context',lambda **kw:ToolContext(
        conv_id=kw['conv_id'],msg_id=kw['msg_id'],capabilities=('mobile.look_at_me',) if allowed else (),model_key='test'))
    async def main():
        response=await streaming.stream_chat_response(conv_id='c',model_key='test',history=[],prompt_meta=_prompt_meta(),temperature=0)
        return await _collect_sse(response)
    events=asyncio.run(main())
    assert events[-1]['has_error'] is expect_error
    rows=[params for sql,params in executed if sql.startswith('INSERT INTO messages') and params[2]=='assistant']
    assert all(r[3].strip() or json.loads(r[5] or '[]') for r in rows)
    if not expect_error and not text.startswith('抱抱'):
        assert not rows
        assert any(e['type']=='tool_delivery' for e in events)
        assert not any(e['type']=='msg_created' for e in broadcasts)


def test_free_invitation_prose_cannot_execute_nested_tools_or_private_writes():
    from app.chat.postprocess import PostProcessor
    class Ledger:
        async def record_postprocess(self,*args,**kwargs):pass
    async def main():
        expression='抱一下 [RING:轻轻捏] 和 [TOY:STOP] [VOW:一起吃饭]'
        result=await PostProcessor(ledger=Ledger()).process('【陪我：20｜'+expression+'】',conv_id='today')
        assert len(result.tool_intents)==1
        assert result.tool_intents[0].arguments['expression']==expression
        assert not result.ring_touch_descriptions and not result.toy_commands
        assert not result.vow.found and result.content==''
    asyncio.run(main())


def test_concurrent_feedback_claims_reserve_no_more_than_three_turns(tmp_path):
    async def case(f):
        await f.session()
        claims=await asyncio.gather(*(f.claim(f'concurrent{i}') for i in range(5)))
        assert sum(bool(c['block']) for c in claims)==3
    run(tmp_path,case)


def test_existing_memory_repository_deduplicates_durable_key(tmp_path,monkeypatch):
    from app.memory_v2 import repository as memory_repo
    async def case(f):
        async with f.db() as db:
            await db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,content TEXT,type TEXT,created_at REAL,source_conv TEXT,embedding BLOB,keywords TEXT,importance REAL,source_start_ts REAL,source_end_ts REAL)')
            await db.commit()
        embeddings=[]
        async def embed(content):embeddings.append(content);return []
        async def shadow(*args,**kwargs):pass
        monkeypatch.setattr(memory_repo,'get_db',f.db)
        monkeypatch.setattr(memory_repo,'get_document_embedding',embed)
        monkeypatch.setattr(memory_repo,'shadow_legacy_memory',shadow)
        first=await memory_repo.create_memory('约好的腻歪时间',idempotency_key='look_at_me:r')
        second=await memory_repo.create_memory('约好的腻歪时间',idempotency_key='look_at_me:r')
        assert first['id']==second['id'] and len(embeddings)==1
        async with f.db() as db:
            assert (await (await db.execute('SELECT COUNT(*) FROM memories')).fetchone())[0]==1
    run(tmp_path,case)


def test_route_binding_null_and_receipt_protocol(tmp_path,monkeypatch):
    from routes import look_at_me as routes
    from app.look_at_me import service
    from app.daily_chat.service import daily_chat
    async def case(f):
        monkeypatch.setattr(routes,'repository',f.repo)
        monkeypatch.setattr(service,'SETTINGS',{'look_at_me_enabled':True,'look_at_me_device_id':'phone'})
        monkeypatch.setattr(service,'save_settings',lambda _:None)
        async def ensure():return {'active_conv_id':'today','enabled':True}
        monkeypatch.setattr(daily_chat,'ensure_current',ensure)
        monkeypatch.setattr(daily_chat,'scope',lambda _:('today',))
        assert (await routes.pending('tablet',0))=={'enabled':False}
        assert (await routes.pending('phone',0)).status_code==204
        r=await f.create()
        pending=await routes.pending('phone',0);assert pending['request']['id']==r['id']
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            await routes.events(r['id'],routes.EventsBody(device_id='tablet',events=[f.event(r,1,'offered',f.now)]))
        assert exc.value.status_code==403
        body=routes.EventsBody(device_id='phone',events=[f.event(r,1,'offered',f.now)])
        assert (await routes.events(r['id'],body))['request']['status']=='offered'
        assert (await routes.events(r['id'],body))['processed_seq']==1
    run(tmp_path,case)
