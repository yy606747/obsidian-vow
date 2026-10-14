"""Exercise incremental rendering, bounded logs and scroll stability with mock data."""
import json
import mimetypes
import statistics
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
now = time.time()
errors = []
messages = [dict(id=f'm{i}', conv_id='c1', role='user' if i % 2 else 'assistant',
                 content=('这是一段用于测量页面渲染的模拟聊天文字。' * 9),
                 created_at=now + i, attachments=[]) for i in range(50)]


def route_request(route):
    path = urlparse(route.request.url).path
    if path == '/chat':
        route.fulfill(path=ROOT / 'static/chat.html', content_type='text/html; charset=utf-8')
    elif path.startswith('/static/'):
        asset = ROOT / path.lstrip('/')
        if asset.is_file():
            route.fulfill(path=asset, content_type=mimetypes.guess_type(str(asset))[0] or 'application/octet-stream')
        else:
            route.fulfill(status=404, body='missing')
    elif path in ('/api/avatar/ai', '/api/avatar/user'):
        icon = 'AIIcon.png' if path.endswith('/ai') else 'UserIcon.png'
        route.fulfill(path=ROOT.parent / 'public/optimized' / icon, content_type='image/png')
    elif path.startswith('/api/'):
        data = {}
        if path == '/api/worldbook': data = dict(ai_name='予安', user_name='南枝')
        elif path == '/api/models': data = [dict(key='fixture', audio_input=False)]
        elif path == '/api/chat/daily':
            data = dict(enabled=False, count_initialized=True, inherit_count=30, revision=0,
                        timezone='Asia/Shanghai', next_rollover_at=now+86400)
        elif path == '/api/conversations': data = [dict(id='c1', title='模拟测量', model='fixture')]
        elif path == '/api/conversations/c1/messages': data = messages
        elif path == '/api/avatar/info': data = dict(ai=0, user=0)
        elif path == '/api/tts/voices': data = dict(voices=[])
        elif path == '/api/heart-whispers': data = dict(items=[], total=0, page=1, page_size=100)
        elif path == '/api/schedule/missed-recent': data = dict(count=0, items=[])
        elif path.endswith('/stream-status'): data = dict(streaming=False)
        route.fulfill(json=data)
    else:
        route.fulfill(status=204, body='')


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(viewport=dict(width=390, height=844), is_mobile=True,
                                  has_touch=True, service_workers='block')
    context.add_init_script("localStorage.setItem('obsidian_tts_enabled', 'false')")
    context.route_web_socket('**/ws', lambda socket: socket.on_message(
        lambda data: socket.send('{"type":"pong"}') if json.loads(data).get('type') == 'ping' else None))
    context.route('**/*', route_request)
    page = context.new_page()
    page.on('pageerror', lambda error: (errors.append(str(error)), print('PAGE ERROR:', error, flush=True)))
    page.goto('http://diagnostic.test/chat', wait_until='load')
    page.wait_for_function('chatInitialized && currentMessages.length > 0')
    page.evaluate('hasMoreMessages = false')
    page.wait_for_timeout(300)
    cdp = context.new_cdp_session(page)
    cdp.send('Emulation.setCPUThrottlingRate', dict(rate=4))
    page.evaluate('''() => {
      window.__fixture = currentMessages.map(m => ({...m}));
      window.__measuredLongTasks = [];
      new PerformanceObserver(list => {
        __measuredLongTasks.push(...list.getEntries().map(x => ({start: x.startTime, duration: x.duration})));
      }).observe({type:'longtask', buffered:false});
    }''')
    output = dict(cpu_throttle=4, checks=[], render=[])
    page.evaluate("document.getElementById('messages').scrollTop = 1800")
    page.wait_for_timeout(100)
    page.evaluate("""() => {
      window.stableRows = new Map([...document.querySelectorAll('#messages > .msg-row')].map(row=>[row.id,row]));
      const top = document.getElementById('messages').getBoundingClientRect().top;
      window.readingRow = [...stableRows.values()].find(row => row.getBoundingClientRect().bottom > top);
      window.readingTop = readingRow.getBoundingClientRect().top;
      currentMessages.push({...__fixture[0],id:'incoming',content:'新到的消息'});
      renderMessages(); scrollBottom();
    }""")
    page.wait_for_timeout(100)
    assert page.evaluate('Math.abs(readingRow.getBoundingClientRect().top-readingTop) < 2')
    assert page.evaluate('[...stableRows].every(([id,node]) => document.getElementById(id)===node)')
    page.evaluate("""() => {
      const older = __fixture.map((row,i)=>({...row,id:'older_'+i,created_at:row.created_at-100}));
      currentMessages = [...older,...currentMessages];
      renderMessages({scroll:'preserve'});
    }""")
    page.wait_for_timeout(100)
    assert page.evaluate('Math.abs(readingRow.getBoundingClientRect().top-readingTop) < 2')
    assert page.evaluate('[...stableRows].every(([id,node]) => document.getElementById(id)===node)')
    page.evaluate("""() => {
      currentMessages.find(m=>m.id==='incoming').content='编辑后的内容';
      renderMessages();
      currentMessages=currentMessages.filter(m=>m.id!=='incoming');
      renderMessages();
    }""")
    assert page.locator('#m_incoming').count() == 0
    assert page.evaluate('[...stableRows].every(([id,node]) => document.getElementById(id)===node)')
    output['checks'].append('append_prepend_edit_delete_preserve_other_rows_and_reading_position')
    page.evaluate("""async () => {
      window.testStream = createSendStreamState();
      await handleSendStreamEvent({type:'start',id:'stream_test'},testStream);
      window.typingNode=document.querySelector('#m_stream_test .typing-bubble');
      currentMessages.push({...__fixture[0],id:'during_stream'});
      renderMessages();
    }""")
    assert page.evaluate("typingNode === document.querySelector('#m_stream_test .typing-bubble')")
    page.evaluate("handleSendStreamEvent({type:'retry',reason:'timeout'},testStream)")
    assert '正在重试' in page.locator('#m_stream_test').inner_text()
    page.evaluate("handleSendStreamEvent({type:'chunk',content:'**完整回复**'},testStream)")
    page.wait_for_timeout(150)
    assert '完整回复' in page.locator('#m_stream_test').inner_text()
    page.evaluate('_stopTypingAnim(); streamingAiId=null')
    output['checks'].append('streaming_survives_unrelated_render_and_timeout_retry')
    page.evaluate('ensureDebugLogLoaded()')
    page.evaluate("""() => {
      closeSystemLog(); clearSystemLog();
      window.logPrompt=Array.from({length:30},()=>({role:'user',content:'模拟提示内容。'.repeat(77)}));
      window.addFixtureLog = i => {
        const data={msg_id:'log_'+i,model:'fixture',prompt_messages:logPrompt,
          timeline:{status:'injected',timeline_version:5,entries:[{}],block:'[最近三天的事] <仅文本>',
                    source_coverage:{'2026-10-08':{selected:30,available:200}}}};
        msgDebugData[data.msg_id]=data; addSystemLog(data);
      };
      for(let i=0;i<65;i++) addFixtureLog(i);
    }""")
    assert page.locator('#sysLogList .syslog-entry').count() == 0
    page.evaluate('openSystemLog()')
    assert page.locator('#sysLogList .syslog-entry').count() == 65
    assert page.locator('#sysLogList .debug-prompt-item').count() == 0
    page.locator('#sysLogList .syslog-detail-toggle').first.click()
    assert page.locator('#sysLogList .debug-prompt-item').count() == 30
    assert '选取 30 / 200' in page.locator('#sysLogList').inner_text()
    assert '<仅文本>' in page.locator('#sysLogList').inner_text()
    page.evaluate("window.openDetail=document.querySelector('.syslog-detail.show'); addFixtureLog(65)")
    assert page.evaluate('openDetail.isConnected && openDetail.classList.contains("show")')
    page.evaluate('closeSystemLog()')
    assert page.locator('#sysLogList *').count() == 0
    page.evaluate('for(let i=66;i<300;i++) addFixtureLog(i)')
    assert page.evaluate('systemLogs.length <= 100 && Object.keys(msgDebugData).length <= 100')
    assert page.locator('#sysLogList *').count() == 0
    page.evaluate('openSystemLog()')
    assert page.locator('#sysLogList .debug-prompt-item').count() == 0
    page.evaluate('closeSystemLog()')
    output['checks'].append('closed_logs_do_no_dom_work_details_lazy_bounded_and_timeline_visible')
    for count in [50,250,500]:
        page.evaluate("""count => {
          currentMessages=Array.from({length:count},(_,i)=>({...__fixture[i%50],id:'measure_'+i}));
          renderMessages({scroll:'bottom'});
        }""",count)
        page.wait_for_timeout(100)
        samples=[]
        for repeat in range(3):
            samples.append(page.evaluate("""repeat => {
              currentMessages.at(-1).content='更新后的消息'+repeat;
              const start=performance.now(); renderMessages();
              const height=document.getElementById('messages').scrollHeight;
              return performance.now()-start;
            }""",repeat))
            page.wait_for_timeout(100)
        output['render'].append(dict(messages=count,samples_ms=[round(x,1) for x in samples],median_ms=round(statistics.median(samples),1)))
    page.evaluate('delete window.storeSystemLog; addFixtureLog(999)')
    assert page.evaluate("systemLogs[0].msg_id === 'log_999' && systemLogs.length <= 100")
    assert page.locator('#sysLogList *').count() == 0
    output['checks'].append('already_open_tabs_accept_new_lazy_log_module')
    assert not errors, errors
    output['errors']=errors
    Path('/tmp/obsidian-chat-smooth-after.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps(output,ensure_ascii=False,indent=2))
    browser.close()
