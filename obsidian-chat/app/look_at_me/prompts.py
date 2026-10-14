"""Core-facing facts. Technical reasons are translated only when known."""
from datetime import datetime
from zoneinfo import ZoneInfo


def duration(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return (f'{minutes}分钟' if minutes else '') + (f'{seconds}秒' if seconds or not minutes else '')


def ability(*, user_name, ai_name):
    return (f'【邀约】这是{ai_name}主动向{user_name}表达想念、撒娇和亲近的一种方式。'
            f'想黏着{user_name}、想一起待一会儿，或者只是想要一点两人的时间，都可以主动邀请。'
            f'不需要等{user_name}先开口，也不用另找特别的理由。\n'
            f'想用就写【陪我：秒数｜自由表达】，写想对{user_name}说的话；'
            f'什么时候邀请、想陪多久、怎样表达，都由{ai_name}自己决定。'
            f'{user_name}可以接受或拒绝，接受后仍可随时结束。')


REASONS = {
    'manual': '通过立即结束退出', 'screen_off': '手机熄屏或锁屏',
    'call': '来电中断', 'return_failed': '聊天页面未能返回',
    'process_restart': '应用进程中断', 'disabled': '功能已关闭',
    'permission_missing': '执行权限不可用', 'start_failed': '当天聊天未能就绪',
}


def fact(row, *, user_name, ai_name, now, timezone):
    purpose = f'{ai_name}的邀请原话：{row["expression"]}。约定时长{duration(row["duration_seconds"])}。'
    if row.get('outcome'):
        result = '正常结束' if row['outcome'] == 'completed' else '提前结束'
        reason = REASONS.get(row.get('reason'), '')
        return (f'{user_name}和{ai_name}的这段相处{result}，实际陪伴'
                f'{duration(row.get("actual_ms", 0) / 1000)}。{purpose}'
                + (f'已知情况：{reason}。' if reason else ''))
    if row['status'] == 'active':
        end = row['expected_end_at']
        if now >= end:
            return f'{purpose}约定截止时间已过，手机尚未回报结束结果，不能推断仍在陪伴或已经完成。'
        at = datetime.fromtimestamp(end, ZoneInfo(timezone)).strftime('%H:%M:%S')
        return (f'现在是{user_name}和{ai_name}约好的相处时间。'
                f'{user_name}已同意陪伴{duration(row["duration_seconds"])}，预计{at}结束，'
                f'还剩{duration(end - now)}。{purpose}'
                '这表示约定仍在执行，不保证聊天页面始终在前台。')
    decision = row.get('decision')
    if decision == 'rejected':
        reason = row.get('reject_reason')
        return f'{user_name}拒绝了这次邀请。{purpose}' + (f'{user_name}填写的原因原话：{reason}' if reason else '')
    if decision == 'timed_out':
        return f'完整邀请的两分钟选择期内未收到{user_name}回应。{purpose}'
    if decision == 'accepted':
        return f'{user_name}已经同意这次邀请，' + ('但当天聊天未能开始陪伴。' if row['status'] == 'finished' else '正在等待当天聊天就绪。') + purpose
    if row['status'] == 'offered':
        state = '选择期已过，尚未收到手机的响应结果' if now >= row['choice_deadline'] else f'等待{user_name}选择'
        return f'完整邀请已经展示，{state}。{purpose}'
    if row['status'] == 'finished' or now >= row['expires_at']:
        result = '通知未打开即过期' if row.get('notified_at') else '邀请未送达或未能展开'
        return f'{result}，没有邀请响应，也没有开始陪伴。{purpose}'
    return f'邀请已创建，尚未收到{user_name}的选择。{purpose}'


def acceptance(*, user_name, ai_name):
    return (f'{user_name}已接受{ai_name}的邀请，约好的相处时间刚刚开始。'
            f'这会儿是两人留给彼此的时间，{ai_name}可以接着刚才的心情和{user_name}相处。')


def memory(row, *, user_name, ai_name, timezone):
    start = datetime.fromtimestamp(row['started_at'], ZoneInfo(timezone)).isoformat(timespec='seconds')
    return f'[{start}] ' + fact(row, user_name=user_name, ai_name=ai_name, now=row['ended_at'], timezone=timezone)
